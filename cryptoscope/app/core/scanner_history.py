"""Persistent lifecycle tracking for daily scanner signals."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Iterable

import aiosqlite
import pandas as pd

from app.core.scanners import (
    corr_breakdown_scan,
    drawdown_scan,
    momentum_scan,
)
from app.db.schema import (
    CREATE_SCANNER_SIGNAL_INDICES,
    CREATE_SCANNER_SIGNAL_PERIODS,
)

SCANNER_HORIZONS = {
    "corrbreak": 3,
    "momentum": 5,
    "drawdown": 10,
}
CRYPTO_AUTO_CLOSE_DAILY_GAIN_PCT = 30.0


def is_scanner_signal_within_horizon(scanner: str, age_days: Any) -> bool:
    """Return whether a scanner condition is still an actionable recommendation."""
    try:
        age = max(1, int(age_days or 1))
    except (TypeError, ValueError):
        age = 1
    return age <= SCANNER_HORIZONS.get(scanner, 3)


def _iso_date(value: Any) -> str:
    if isinstance(value, (date, datetime)):
        return value.strftime("%Y-%m-%d")
    return str(value).strip()[:10]


def format_scanner_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").strftime("%d.%m.%Y")
    except (TypeError, ValueError):
        return str(value)


def build_scanner_snapshot(
    wide: pd.DataFrame,
    scanner: str,
) -> tuple[pd.DataFrame, list[dict[str, str]]]:
    """Calculate a scanner and extract the rows that represent active signals."""
    tickers = list(wide.columns)
    if scanner == "momentum":
        frame = momentum_scan(
            wide.values,
            tickers,
            list(wide.index.astype(str)),
        )
    elif scanner == "drawdown":
        frame = drawdown_scan(wide.values, tickers)
    else:
        scanner = "corrbreak"
        frame = (
            corr_breakdown_scan(wide, tickers)
            if len(tickers) >= 2
            else pd.DataFrame()
        )

    active: list[dict[str, str]] = []
    if frame.empty:
        return frame, active

    for row in frame.to_dict(orient="records"):
        if scanner == "corrbreak":
            ticker_a = str(row["ticker_a"])
            ticker_b = str(row["ticker_b"])
            direction = (
                "avoid"
                if row.get("recommendation") == "Не открывать пару"
                else "review"
            )
            signal_key = f"{ticker_a}|{ticker_b}"
        else:
            recommendation_class = str(
                row.get("recommendation_class") or "wait"
            )
            allowed = (
                {"long", "short"}
                if scanner == "momentum"
                else {"long"}
            )
            if recommendation_class not in allowed:
                continue
            ticker_a = str(row["ticker"])
            ticker_b = ""
            direction = recommendation_class
            signal_key = ticker_a
        active.append({
            "signal_key": signal_key,
            "ticker_a": ticker_a,
            "ticker_b": ticker_b,
            "direction": direction,
            "confidence": (
                str(row.get("confidence") or "").strip()
                if scanner != "corrbreak"
                else ""
            ),
        })
    return frame, active


async def ensure_scanner_history_schema(conn) -> None:
    await conn.execute(CREATE_SCANNER_SIGNAL_PERIODS)
    cursor = await conn.execute("PRAGMA table_info(scanner_signal_periods)")
    columns = {str(row["name"]) for row in await cursor.fetchall()}
    migrations = {
        "confidence": "TEXT",
        "strategy_admitted_date": "TEXT",
        "strategy_confidence": "TEXT",
        "close_reason": "TEXT",
        "closed_price": "REAL",
        "closed_at": "TEXT",
    }
    for column, column_type in migrations.items():
        if column not in columns:
            await conn.execute(
                f"ALTER TABLE scanner_signal_periods "
                f"ADD COLUMN {column} {column_type}"
            )
    # Existing medium/high crypto positions were already part of the strategy
    # before admission tracking existed. Backfill them once without admitting
    # low-confidence periods retroactively.
    await conn.execute(
        """
        UPDATE scanner_signal_periods
        SET strategy_admitted_date = first_seen_date,
            strategy_confidence = confidence
        WHERE market = 'crypto'
          AND scanner = 'momentum'
          AND direction = 'long'
          AND strategy_admitted_date IS NULL
          AND TRIM(COALESCE(confidence, '')) IN ('Средняя', 'Высокая')
        """
    )
    for statement in CREATE_SCANNER_SIGNAL_INDICES:
        await conn.execute(statement)
    await conn.commit()


async def close_crypto_ticker_periods(
    conn,
    ticker: str,
    close_date: str,
    close_price: float,
    reason: str,
) -> int:
    """Close every active LONG scanner period represented by one crypto position."""
    await ensure_scanner_history_schema(conn)
    # Auto-close takes profit on positions opened earlier; a signal born from
    # that very daily pump must survive its first day. Manual closes are
    # explicit user actions and stay unrestricted.
    age_clause = (
        "AND COALESCE(strategy_admitted_date, first_seen_date) < ?"
        if reason == "auto_30_daily"
        else ""
    )
    params: list[Any] = [
        close_date,
        reason,
        float(close_price),
        ticker,
        close_date,
        close_date,
    ]
    if age_clause:
        params.append(close_date)
    cursor = await conn.execute(
        f"""
        UPDATE scanner_signal_periods
        SET status = 'suppressed',
            ended_date = ?,
            close_reason = ?,
            closed_price = ?,
            closed_at = datetime('now'),
            updated_at = datetime('now')
        WHERE market = 'crypto'
          AND scanner IN ('momentum', 'drawdown')
          AND direction = 'long'
          AND ticker_a = ?
          AND status = 'active'
          AND (
              (
                  last_seen_date = ?
                  AND observation_count <= CASE scanner
                      WHEN 'momentum' THEN 5
                      ELSE 10
                  END
              )
              OR (
                  last_seen_date < ?
                  AND observation_count < CASE scanner
                      WHEN 'momentum' THEN 5
                      ELSE 10
                  END
              )
          )
          {age_clause}
        """,
        tuple(params),
    )
    await conn.commit()
    return max(0, int(cursor.rowcount or 0))


async def auto_close_crypto_positions(
    conn,
    wide: pd.DataFrame,
    data_date: str,
) -> list[dict[str, Any]]:
    """Close existing crypto positions after a 30% or larger daily rise."""
    closed: list[dict[str, Any]] = []
    normalized_data_date = _iso_date(data_date)
    for ticker in wide.columns:
        series = wide[ticker].dropna()
        if len(series) < 2:
            continue
        ticker_data_date = _iso_date(series.index[-1])
        if ticker_data_date != normalized_data_date:
            continue
        previous_price = float(series.iloc[-2])
        current_price = float(series.iloc[-1])
        if previous_price <= 0 or current_price <= 0:
            continue
        daily_change_pct = (current_price / previous_price - 1) * 100
        if daily_change_pct < CRYPTO_AUTO_CLOSE_DAILY_GAIN_PCT:
            continue
        affected = await close_crypto_ticker_periods(
            conn,
            str(ticker),
            ticker_data_date,
            current_price,
            "auto_30_daily",
        )
        if affected:
            closed.append({
                "ticker": str(ticker),
                "daily_change_pct": round(daily_change_pct, 2),
                "close_price": current_price,
            })
    return closed


async def sync_scanner_periods(
    conn,
    market: str,
    scanner: str,
    data_date: str,
    active_signals: Iterable[dict[str, str]],
) -> dict[str, dict[str, Any]]:
    """Advance scanner periods once per unique market data date."""
    await ensure_scanner_history_schema(conn)
    data_date = _iso_date(data_date)
    cursor = await conn.execute(
        """
        SELECT *
        FROM scanner_signal_periods
        WHERE market = ? AND scanner = ? AND status = 'active'
        """,
        (market, scanner),
    )
    active_rows = {
        str(row["signal_key"]): dict(row)
        for row in await cursor.fetchall()
    }
    cursor = await conn.execute(
        """
        SELECT *
        FROM scanner_signal_periods
        WHERE market = ? AND scanner = ? AND status = 'suppressed'
        """,
        (market, scanner),
    )
    suppressed_rows = {
        str(row["signal_key"]): dict(row)
        for row in await cursor.fetchall()
    }
    current = {
        str(signal["signal_key"]): signal
        for signal in active_signals
    }

    for signal_key, signal in current.items():
        previous = active_rows.get(signal_key)
        direction = str(signal["direction"])
        confidence = str(signal.get("confidence") or "").strip() or None
        suppressed = suppressed_rows.get(signal_key)
        if suppressed and str(suppressed["direction"]) == direction:
            if data_date > str(suppressed["last_seen_date"]):
                await conn.execute(
                    """
                    UPDATE scanner_signal_periods
                    SET last_seen_date = ?,
                        observation_count = observation_count + 1,
                        updated_at = datetime('now')
                    WHERE id = ?
                    """,
                    (data_date, int(suppressed["id"])),
                )
                suppressed["last_seen_date"] = data_date
                suppressed["observation_count"] = (
                    int(suppressed.get("observation_count") or 0) + 1
                )
            continue
        if suppressed:
            await conn.execute(
                """
                UPDATE scanner_signal_periods
                SET status = 'closed', updated_at = datetime('now')
                WHERE id = ?
                """,
                (int(suppressed["id"]),),
            )
            suppressed_rows.pop(signal_key, None)

        if previous and str(previous["direction"]) != direction:
            await conn.execute(
                """
                UPDATE scanner_signal_periods
                SET status = 'closed', ended_date = ?, updated_at = datetime('now')
                WHERE id = ?
                """,
                (data_date, int(previous["id"])),
            )
            previous = None

        if previous:
            next_observation_count = int(
                previous.get("observation_count") or 0
            )
            if data_date > str(previous["last_seen_date"]):
                next_observation_count += 1
            should_admit = (
                market == "crypto"
                and scanner == "momentum"
                and direction == "long"
                and confidence in {"Средняя", "Высокая"}
                and not str(
                    previous.get("strategy_admitted_date") or ""
                ).strip()
                and next_observation_count <= SCANNER_HORIZONS["momentum"]
            )
            if should_admit:
                await conn.execute(
                    """
                    UPDATE scanner_signal_periods
                    SET strategy_admitted_date = ?,
                        strategy_confidence = ?,
                        updated_at = datetime('now')
                    WHERE id = ?
                    """,
                    (data_date, confidence, int(previous["id"])),
                )
                previous["strategy_admitted_date"] = data_date
                previous["strategy_confidence"] = confidence
            if confidence and not str(previous.get("confidence") or "").strip():
                await conn.execute(
                    """
                    UPDATE scanner_signal_periods
                    SET confidence = ?, updated_at = datetime('now')
                    WHERE id = ?
                    """,
                    (confidence, int(previous["id"])),
                )
            if data_date > str(previous["last_seen_date"]):
                await conn.execute(
                    """
                    UPDATE scanner_signal_periods
                    SET last_seen_date = ?,
                        observation_count = observation_count + 1,
                        updated_at = datetime('now')
                    WHERE id = ?
                    """,
                    (data_date, int(previous["id"])),
                )
            continue

        strategy_admitted_date = None
        strategy_confidence = None
        if (
            market == "crypto"
            and scanner == "momentum"
            and direction == "long"
            and confidence in {"Средняя", "Высокая"}
        ):
            strategy_admitted_date = data_date
            strategy_confidence = confidence
        await conn.execute(
            """
            INSERT INTO scanner_signal_periods (
                market, scanner, signal_key, ticker_a, ticker_b, direction,
                confidence, strategy_admitted_date, strategy_confidence,
                first_seen_date, last_seen_date, observation_count, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'active')
            """,
            (
                market,
                scanner,
                signal_key,
                signal["ticker_a"],
                signal.get("ticker_b", ""),
                direction,
                confidence,
                strategy_admitted_date,
                strategy_confidence,
                data_date,
                data_date,
            ),
        )

    for signal_key, previous in active_rows.items():
        if signal_key in current:
            continue
        if data_date <= str(previous["last_seen_date"]):
            continue
        await conn.execute(
            """
            UPDATE scanner_signal_periods
            SET status = 'closed', ended_date = ?, updated_at = datetime('now')
            WHERE id = ?
            """,
            (data_date, int(previous["id"])),
        )

    for signal_key, suppressed in suppressed_rows.items():
        if signal_key in current:
            continue
        await conn.execute(
            """
            UPDATE scanner_signal_periods
            SET status = 'closed', updated_at = datetime('now')
            WHERE id = ?
            """,
            (int(suppressed["id"]),),
        )

    await conn.commit()
    cursor = await conn.execute(
        """
        SELECT *
        FROM scanner_signal_periods
        WHERE market = ? AND scanner = ?
          AND status IN ('active', 'suppressed')
        """,
        (market, scanner),
    )
    return {
        str(row["signal_key"]): dict(row)
        for row in await cursor.fetchall()
    }


async def fetch_active_scanner_periods(
    conn,
    market: str,
    scanner: str,
) -> dict[str, dict[str, Any]]:
    """Load active/suppressed scanner periods without mutating them."""
    cursor = await conn.execute(
        """
        SELECT *
        FROM scanner_signal_periods
        WHERE market = ? AND scanner = ?
          AND status IN ('active', 'suppressed')
        """,
        (market, scanner),
    )
    return {
        str(row["signal_key"]): dict(row)
        for row in await cursor.fetchall()
    }


def annotate_scanner_results(
    records: list[dict[str, Any]],
    scanner: str,
    periods: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    horizon = SCANNER_HORIZONS.get(scanner, 3)
    for record in records:
        key = (
            f"{record.get('ticker_a')}|{record.get('ticker_b')}"
            if scanner == "corrbreak"
            else str(record.get("ticker") or "")
        )
        period = periods.get(key)
        if not period:
            continue
        age = max(1, int(period.get("observation_count") or 1))
        remaining = max(0, horizon - age)
        record.update({
            "signal_age_days": age,
            "signal_first_seen_date": format_scanner_date(
                period.get("first_seen_date")
            ),
            "signal_last_seen_date": format_scanner_date(
                period.get("last_seen_date")
            ),
            "signal_horizon_days": horizon,
            "signal_remaining_days": remaining,
            "signal_within_horizon": is_scanner_signal_within_horizon(
                scanner,
                age,
            ),
            "signal_suppressed": period.get("status") == "suppressed",
        })
    return records


async def sync_all_scanner_states(
    db_path: str,
    markets: Iterable[str],
) -> None:
    """Persist all scanner snapshots after the daily price refresh."""
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        await ensure_scanner_history_schema(conn)
        for market in markets:
            cursor = await conn.execute(
                """
                SELECT ticker, date, close
                FROM prices
                WHERE market = ?
                ORDER BY ticker, date
                """,
                (market,),
            )
            rows = await cursor.fetchall()
            if not rows:
                continue
            prices = pd.DataFrame([dict(row) for row in rows])
            wide = prices.pivot(index="date", columns="ticker", values="close")
            data_date = _iso_date(max(wide.index))
            if market == "crypto":
                await auto_close_crypto_positions(
                    conn,
                    wide,
                    data_date,
                )
            for scanner in SCANNER_HORIZONS:
                _, active = build_scanner_snapshot(wide, scanner)
                await sync_scanner_periods(
                    conn,
                    market,
                    scanner,
                    data_date,
                    active,
                )
