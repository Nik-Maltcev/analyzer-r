"""Incremental completed 5-minute candles for the Reversal Lab."""

from __future__ import annotations

import asyncio
import math
import sqlite3
import time
from datetime import UTC, datetime, timedelta

import httpx

from app.data.mexc import MEXC_REST_URL, fetch_mexc_spot_symbols, normalize_mexc_symbol

REVERSAL_TICKERS = (
    "BTC/USD", "ETH/USD", "BNB/USD", "SOL/USD", "XRP/USD",
    "DOGE/USD", "ADA/USD", "LINK/USD", "AVAX/USD", "LTC/USD",
    "SUI/USD", "BCH/USD",
)
INTERVAL_MS = 5 * 60 * 1000
HISTORY_DAYS = 90
# MEXC documents 1000 as the maximum, but the live endpoint currently caps
# kline responses at 500. Pagination must use the effective limit; otherwise a
# 500-row response looks like the final page and leaves the dataset stale.
PAGE_LIMIT = 500


async def refresh_reversal_candles(
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
) -> dict:
    """Upsert completed MEXC candles, resuming from each ticker's last bar."""
    now = now or datetime.now(UTC)
    completed_before = int(now.timestamp() * 1000) // INTERVAL_MS * INTERVAL_MS
    initial_start = int((now - timedelta(days=HISTORY_DAYS)).timestamp() * 1000)
    latest = {
        str(row[0]): int(row[1])
        for row in conn.execute(
            "SELECT ticker, MAX(open_time) FROM reversal_candles GROUP BY ticker"
        ).fetchall()
        if row[1] is not None
    }
    inserted = 0
    failures: list[str] = []
    request_at = 0.0

    async with httpx.AsyncClient(timeout=30) as client:
        available = await fetch_mexc_spot_symbols(client)
        for ticker in REVERSAL_TICKERS:
            symbol = normalize_mexc_symbol(ticker)
            if not symbol or symbol not in available:
                failures.append(ticker)
                continue
            cursor = max(initial_start, latest.get(ticker, initial_start) - INTERVAL_MS)
            try:
                while cursor < completed_before:
                    delay = 0.15 - (time.monotonic() - request_at)
                    if delay > 0:
                        await asyncio.sleep(delay)
                    response = await client.get(
                        f"{MEXC_REST_URL}/klines",
                        params={
                            "symbol": symbol,
                            "interval": "5m",
                            "startTime": cursor,
                            "endTime": completed_before - 1,
                            "limit": PAGE_LIMIT,
                        },
                    )
                    request_at = time.monotonic()
                    response.raise_for_status()
                    payload = response.json()
                    if not isinstance(payload, list) or not payload:
                        break
                    batch = []
                    last_open = cursor
                    for candle in payload:
                        if not isinstance(candle, list) or len(candle) < 6:
                            continue
                        open_time = int(candle[0])
                        values = [float(candle[index] or 0) for index in range(1, 6)]
                        if open_time >= completed_before or not all(
                            math.isfinite(value) for value in values
                        ):
                            continue
                        quote_volume = 0.0
                        if len(candle) > 7:
                            try:
                                quote_volume = float(candle[7] or 0)
                            except (TypeError, ValueError):
                                quote_volume = 0.0
                        batch.append((
                            ticker, open_time, values[0], values[1], values[2],
                            values[3], values[4], quote_volume, "mexc",
                        ))
                        last_open = max(last_open, open_time)
                    if batch:
                        before = conn.total_changes
                        conn.executemany(
                            """
                            INSERT INTO reversal_candles (
                                ticker, open_time, open, high, low, close,
                                volume, quote_volume, provider
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(ticker, open_time) DO UPDATE SET
                                open=excluded.open, high=excluded.high,
                                low=excluded.low, close=excluded.close,
                                volume=excluded.volume,
                                quote_volume=excluded.quote_volume,
                                provider=excluded.provider,
                                imported_at=datetime('now')
                            """,
                            batch,
                        )
                        inserted += conn.total_changes - before
                        conn.commit()
                    next_cursor = last_open + INTERVAL_MS
                    if next_cursor <= cursor or len(payload) < PAGE_LIMIT:
                        break
                    cursor = next_cursor
            except Exception as exc:
                failures.append(f"{ticker}: {str(exc)[:120]}")

    count, ticker_count, minimum, maximum = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT ticker), MIN(open_time), MAX(open_time) FROM reversal_candles"
    ).fetchone()
    return {
        "inserted": inserted,
        "candle_count": int(count or 0),
        "ticker_count": int(ticker_count or 0),
        "data_start": minimum,
        "data_end": maximum,
        "completed_before": completed_before,
        "failures": failures,
    }
