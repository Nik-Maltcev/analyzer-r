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
        })
    return frame, active


async def ensure_scanner_history_schema(conn) -> None:
    await conn.execute(CREATE_SCANNER_SIGNAL_PERIODS)
    for statement in CREATE_SCANNER_SIGNAL_INDICES:
        await conn.execute(statement)


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
    current = {
        str(signal["signal_key"]): signal
        for signal in active_signals
    }

    for signal_key, signal in current.items():
        previous = active_rows.get(signal_key)
        direction = str(signal["direction"])
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

        await conn.execute(
            """
            INSERT INTO scanner_signal_periods (
                market, scanner, signal_key, ticker_a, ticker_b, direction,
                first_seen_date, last_seen_date, observation_count, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 'active')
            """,
            (
                market,
                scanner,
                signal_key,
                signal["ticker_a"],
                signal.get("ticker_b", ""),
                direction,
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

    await conn.commit()
    cursor = await conn.execute(
        """
        SELECT *
        FROM scanner_signal_periods
        WHERE market = ? AND scanner = ? AND status = 'active'
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
            for scanner in SCANNER_HORIZONS:
                _, active = build_scanner_snapshot(wide, scanner)
                await sync_scanner_periods(
                    conn,
                    market,
                    scanner,
                    data_date,
                    active,
                )
