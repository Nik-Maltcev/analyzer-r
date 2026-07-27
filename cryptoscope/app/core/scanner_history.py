"""Persistent lifecycle tracking for daily scanner signals."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any, Iterable

import aiosqlite
import pandas as pd

from app.core.scanners import (
    corr_breakdown_scan,
    drawdown_scan,
    momentum_scan,
)
from app.db.schema import (
    CREATE_CRYPTO_STRATEGY_TRADES,
    CREATE_SCANNER_SIGNAL_INDICES,
    CREATE_SCANNER_SIGNAL_PERIODS,
)

SCANNER_HORIZONS = {
    "corrbreak": 3,
    "momentum": 5,
    "drawdown": 10,
}
CRYPTO_AUTO_CLOSE_DAILY_GAIN_PCT = 30.0
CRYPTO_STRATEGY_STAKE = 100.0
CRYPTO_STRATEGY_VERSION = "momentum-long-v1"


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


def _positive_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


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
    await conn.execute(CREATE_CRYPTO_STRATEGY_TRADES)
    cursor = await conn.execute("PRAGMA table_info(scanner_signal_periods)")
    columns = {str(row["name"]) for row in await cursor.fetchall()}
    migrations = {
        "confidence": "TEXT",
        "strategy_admitted_date": "TEXT",
        "strategy_confidence": "TEXT",
        "strategy_entry_price": "REAL",
        "strategy_entry_recorded_at": "TEXT",
        "strategy_entry_source": "TEXT",
        "strategy_exit_date": "TEXT",
        "strategy_exit_price": "REAL",
        "strategy_exit_recorded_at": "TEXT",
        "strategy_exit_reason": "TEXT",
        "strategy_return_pct": "REAL",
        "strategy_cash_result": "REAL",
        "strategy_stake": "REAL",
        "strategy_version": "TEXT",
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
    # Freeze the entry quote for legacy admitted periods exactly once. From
    # this point onward price imports and provider corrections cannot rewrite
    # the strategy entry.
    await conn.execute(
        """
        UPDATE scanner_signal_periods
        SET strategy_entry_price = (
                SELECT p.close
                FROM prices AS p
                WHERE p.market = 'crypto'
                  AND p.ticker = scanner_signal_periods.ticker_a
                  AND p.date >= scanner_signal_periods.strategy_admitted_date
                  AND p.close > 0
                ORDER BY p.date
                LIMIT 1
            ),
            strategy_entry_recorded_at = datetime('now'),
            strategy_entry_source = 'daily_close',
            strategy_stake = ?,
            strategy_version = ?
        WHERE market = 'crypto'
          AND scanner = 'momentum'
          AND direction = 'long'
          AND strategy_admitted_date IS NOT NULL
          AND strategy_entry_price IS NULL
          AND EXISTS (
              SELECT 1
              FROM prices AS p
              WHERE p.market = 'crypto'
                AND p.ticker = scanner_signal_periods.ticker_a
                AND p.date >= scanner_signal_periods.strategy_admitted_date
                AND p.close > 0
          )
        """,
        (CRYPTO_STRATEGY_STAKE, CRYPTO_STRATEGY_VERSION),
    )
    # Repair false +30% exits created before same-day admission protection was
    # introduced. These rows have no holding period and must remain active
    # while the underlying scanner condition is still suppressed.
    await conn.execute(
        """
        UPDATE scanner_signal_periods
        SET status = 'active',
            ended_date = NULL,
            close_reason = NULL,
            closed_price = NULL,
            closed_at = NULL,
            strategy_exit_date = NULL,
            strategy_exit_price = NULL,
            strategy_exit_recorded_at = NULL,
            strategy_exit_reason = NULL,
            strategy_return_pct = NULL,
            strategy_cash_result = NULL,
            updated_at = datetime('now')
        WHERE market = 'crypto'
          AND scanner = 'momentum'
          AND direction = 'long'
          AND status = 'suppressed'
          AND close_reason = 'auto_30_daily'
          AND ended_date = COALESCE(
              strategy_admitted_date,
              first_seen_date
          )
        """
    )
    await _append_crypto_trade_journal(conn)
    for statement in CREATE_SCANNER_SIGNAL_INDICES:
        await conn.execute(statement)
    await conn.commit()


async def _append_crypto_trade_journal(
    conn,
    period_ids: Iterable[int] | None = None,
) -> int:
    """Append newly completed strategy trades without rewriting old results."""
    params: tuple[Any, ...] = ()
    period_clause = ""
    normalized_ids = tuple(int(period_id) for period_id in (period_ids or ()))
    if normalized_ids:
        placeholders = ", ".join("?" for _ in normalized_ids)
        period_clause = f"AND id IN ({placeholders})"
        params = normalized_ids
    cursor = await conn.execute(
        f"""
        INSERT OR IGNORE INTO crypto_strategy_trades (
            period_id, market, scanner, signal_key, ticker, direction,
            confidence, opened_on, entry_price, entry_recorded_at,
            entry_source, closed_on, exit_price, exit_recorded_at,
            exit_reason, return_pct, cash_result, stake, strategy_version
        )
        SELECT
            id, market, scanner, signal_key, ticker_a, direction,
            COALESCE(strategy_confidence, confidence),
            strategy_admitted_date, strategy_entry_price,
            strategy_entry_recorded_at,
            COALESCE(strategy_entry_source, 'daily_close'),
            strategy_exit_date, strategy_exit_price,
            strategy_exit_recorded_at, strategy_exit_reason,
            strategy_return_pct, strategy_cash_result,
            COALESCE(strategy_stake, ?),
            COALESCE(strategy_version, ?)
        FROM scanner_signal_periods
        WHERE market = 'crypto'
          AND scanner = 'momentum'
          AND direction = 'long'
          AND strategy_admitted_date IS NOT NULL
          AND strategy_entry_price > 0
          AND strategy_exit_date IS NOT NULL
          AND strategy_exit_price > 0
          AND strategy_return_pct IS NOT NULL
          AND strategy_cash_result IS NOT NULL
          {period_clause}
        """,
        (
            CRYPTO_STRATEGY_STAKE,
            CRYPTO_STRATEGY_VERSION,
            *params,
        ),
    )
    return max(0, int(cursor.rowcount or 0))


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
            strategy_exit_date = COALESCE(strategy_exit_date, ?),
            strategy_exit_price = COALESCE(strategy_exit_price, ?),
            strategy_exit_recorded_at = COALESCE(
                strategy_exit_recorded_at,
                datetime('now')
            ),
            strategy_exit_reason = COALESCE(strategy_exit_reason, ?),
            strategy_return_pct = COALESCE(
                strategy_return_pct,
                CASE
                    WHEN strategy_entry_price > 0
                    THEN (? / strategy_entry_price - 1) * 100
                END
            ),
            strategy_cash_result = COALESCE(
                strategy_cash_result,
                CASE
                    WHEN strategy_entry_price > 0
                    THEN COALESCE(strategy_stake, ?) *
                         (? / strategy_entry_price - 1)
                END
            ),
            strategy_stake = COALESCE(strategy_stake, ?),
            strategy_version = COALESCE(strategy_version, ?),
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
        tuple(
            params[:3]
            + [
                close_date,
                float(close_price),
                reason,
                float(close_price),
                CRYPTO_STRATEGY_STAKE,
                float(close_price),
                CRYPTO_STRATEGY_STAKE,
                CRYPTO_STRATEGY_VERSION,
            ]
            + params[3:]
        ),
    )
    affected = max(0, int(cursor.rowcount or 0))
    if affected:
        await _append_crypto_trade_journal(conn)
    await conn.commit()
    return affected


async def auto_close_crypto_positions(
    conn,
    wide: pd.DataFrame,
    data_date: str,
    evaluated_at: datetime | None = None,
) -> list[dict[str, Any]]:
    """Close positions after a 30% rise between two completed UTC days."""
    closed: list[dict[str, Any]] = []
    evaluation_time = evaluated_at or datetime.now(UTC)
    current_utc_date = evaluation_time.astimezone(UTC).date().isoformat()
    normalized_data_date = _iso_date(data_date)
    for ticker in wide.columns:
        series = wide[ticker].dropna()
        completed = [
            (str(raw_date)[:10], float(raw_price))
            for raw_date, raw_price in series.items()
            if str(raw_date)[:10] < current_utc_date
        ]
        # Historical/test datasets can legitimately end before today. In that
        # case every available row is already a completed daily candle.
        if (
            not completed
            and normalized_data_date < current_utc_date
        ):
            completed = [
                (str(raw_date)[:10], float(raw_price))
                for raw_date, raw_price in series.items()
            ]
        if len(completed) < 2:
            continue
        ticker_data_date, current_price = completed[-1]
        _, previous_price = completed[-2]
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


async def snapshot_crypto_strategy_positions(
    conn,
    wide: pd.DataFrame,
) -> int:
    """Freeze crypto strategy entry and exit values exactly once."""
    await ensure_scanner_history_schema(conn)
    cursor = await conn.execute(
        """
        SELECT *
        FROM scanner_signal_periods
        WHERE market = 'crypto'
          AND scanner = 'momentum'
          AND direction = 'long'
          AND strategy_admitted_date IS NOT NULL
        ORDER BY id
        """
    )
    periods = [dict(row) for row in await cursor.fetchall()]
    price_cache: dict[str, list[tuple[str, float]]] = {}
    for ticker in wide.columns:
        rows = []
        for raw_date, raw_price in wide[ticker].dropna().items():
            price = _positive_float(raw_price)
            if price is not None:
                rows.append((_iso_date(raw_date), price))
        price_cache[str(ticker)] = sorted(rows)

    updated = 0
    for period in periods:
        ticker = str(period["ticker_a"])
        prices = price_cache.get(ticker, [])
        admitted_date = _iso_date(period["strategy_admitted_date"])
        entry_price = _positive_float(period.get("strategy_entry_price"))
        if entry_price is None:
            entry = next(
                (
                    (price_date, price)
                    for price_date, price in prices
                    if price_date >= admitted_date
                ),
                None,
            )
            if entry is None:
                continue
            _, entry_price = entry

        exit_date = str(period.get("strategy_exit_date") or "").strip()
        exit_price = _positive_float(period.get("strategy_exit_price"))
        exit_reason = str(
            period.get("strategy_exit_reason")
            or period.get("close_reason")
            or ""
        ).strip()

        if exit_price is None:
            close_reason = str(period.get("close_reason") or "").strip()
            if close_reason in {"manual", "auto_30_daily"}:
                exit_date = _iso_date(
                    period.get("ended_date")
                    or period.get("last_seen_date")
                )
                exit_price = _positive_float(period.get("closed_price"))
                exit_reason = close_reason
            else:
                horizon = SCANNER_HORIZONS["momentum"]
                raw_start = _iso_date(period["first_seen_date"])
                raw_prices = [
                    item for item in prices if item[0] >= raw_start
                ]
                observations = int(period.get("observation_count") or 0)
                if observations >= horizon and len(raw_prices) >= horizon:
                    exit_date, exit_price = raw_prices[horizon - 1]
                    exit_reason = "horizon"
                elif str(period.get("status") or "") == "closed":
                    exit_date = _iso_date(
                        period.get("ended_date")
                        or period.get("last_seen_date")
                    )
                    available = [
                        item for item in prices if item[0] <= exit_date
                    ]
                    if available:
                        exit_date, exit_price = available[-1]
                        exit_reason = "signal_ended"

        return_pct = None
        cash_result = None
        if exit_price is not None:
            return_pct = (exit_price / entry_price - 1) * 100
            cash_result = CRYPTO_STRATEGY_STAKE * return_pct / 100

        update_cursor = await conn.execute(
            """
            UPDATE scanner_signal_periods
            SET strategy_entry_price = COALESCE(strategy_entry_price, ?),
                strategy_entry_recorded_at = COALESCE(
                    strategy_entry_recorded_at,
                    datetime('now')
                ),
                strategy_entry_source = COALESCE(
                    strategy_entry_source,
                    'daily_close'
                ),
                strategy_exit_date = COALESCE(strategy_exit_date, ?),
                strategy_exit_price = COALESCE(strategy_exit_price, ?),
                strategy_exit_recorded_at = CASE
                    WHEN strategy_exit_price IS NULL AND ? IS NOT NULL
                    THEN datetime('now')
                    ELSE strategy_exit_recorded_at
                END,
                strategy_exit_reason = COALESCE(strategy_exit_reason, ?),
                strategy_return_pct = COALESCE(strategy_return_pct, ?),
                strategy_cash_result = COALESCE(strategy_cash_result, ?),
                strategy_stake = COALESCE(strategy_stake, ?),
                strategy_version = COALESCE(strategy_version, ?),
                updated_at = CASE
                    WHEN strategy_entry_price IS NULL
                      OR (strategy_exit_price IS NULL AND ? IS NOT NULL)
                    THEN datetime('now')
                    ELSE updated_at
                END
            WHERE id = ?
            """,
            (
                entry_price,
                exit_date or None,
                exit_price,
                exit_price,
                exit_reason or None,
                return_pct,
                cash_result,
                CRYPTO_STRATEGY_STAKE,
                CRYPTO_STRATEGY_VERSION,
                exit_price,
                int(period["id"]),
            ),
        )
        updated += max(0, int(update_cursor.rowcount or 0))

    await _append_crypto_trade_journal(conn)
    await conn.commit()
    return updated


async def sync_scanner_periods(
    conn,
    market: str,
    scanner: str,
    data_date: str,
    active_signals: Iterable[dict[str, str]],
    current_prices: dict[str, float] | None = None,
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
    current_prices = current_prices or {}

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
                entry_price = _positive_float(
                    current_prices.get(str(previous["ticker_a"]))
                )
                await conn.execute(
                    """
                    UPDATE scanner_signal_periods
                    SET strategy_admitted_date = ?,
                        strategy_confidence = ?,
                        strategy_entry_price = COALESCE(
                            strategy_entry_price,
                            ?
                        ),
                        strategy_entry_recorded_at = CASE
                            WHEN strategy_entry_price IS NULL AND ? IS NOT NULL
                            THEN datetime('now')
                            ELSE strategy_entry_recorded_at
                        END,
                        strategy_entry_source = COALESCE(
                            strategy_entry_source,
                            CASE WHEN ? IS NOT NULL THEN 'daily_close' END
                        ),
                        strategy_stake = COALESCE(strategy_stake, ?),
                        strategy_version = COALESCE(strategy_version, ?),
                        updated_at = datetime('now')
                    WHERE id = ?
                    """,
                    (
                        data_date,
                        confidence,
                        entry_price,
                        entry_price,
                        entry_price,
                        CRYPTO_STRATEGY_STAKE,
                        CRYPTO_STRATEGY_VERSION,
                        int(previous["id"]),
                    ),
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
        strategy_entry_price = None
        if (
            market == "crypto"
            and scanner == "momentum"
            and direction == "long"
            and confidence in {"Средняя", "Высокая"}
        ):
            strategy_admitted_date = data_date
            strategy_confidence = confidence
            strategy_entry_price = _positive_float(
                current_prices.get(str(signal["ticker_a"]))
            )
        await conn.execute(
            """
            INSERT INTO scanner_signal_periods (
                market, scanner, signal_key, ticker_a, ticker_b, direction,
                confidence, strategy_admitted_date, strategy_confidence,
                strategy_entry_price, strategy_entry_recorded_at,
                strategy_entry_source, strategy_stake, strategy_version,
                first_seen_date, last_seen_date, observation_count, status
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                CASE WHEN ? IS NOT NULL THEN datetime('now') END,
                CASE WHEN ? IS NOT NULL THEN 'daily_close' END,
                ?, ?, ?, ?, 1, 'active'
            )
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
                strategy_entry_price,
                strategy_entry_price,
                strategy_entry_price,
                (
                    CRYPTO_STRATEGY_STAKE
                    if strategy_admitted_date
                    else None
                ),
                (
                    CRYPTO_STRATEGY_VERSION
                    if strategy_admitted_date
                    else None
                ),
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

    await _append_crypto_trade_journal(conn)
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
            current_prices = {
                str(ticker): float(series.dropna().iloc[-1])
                for ticker, series in wide.items()
                if not series.dropna().empty
            }
            for scanner in SCANNER_HORIZONS:
                _, active = build_scanner_snapshot(wide, scanner)
                await sync_scanner_periods(
                    conn,
                    market,
                    scanner,
                    data_date,
                    active,
                    current_prices=current_prices,
                )
            if market == "crypto":
                await snapshot_crypto_strategy_positions(conn, wide)
