"""MEXC contract (USDT-margined perpetual) data collectors."""

from __future__ import annotations

import asyncio
import math
import sqlite3
import time
from datetime import UTC, datetime, timedelta

import httpx

from app.data.mexc import normalize_mexc_symbol
from app.data.mexc_intraday import INTRADAY_TICKERS, SHORT_TERM_HISTORY_DAYS

MEXC_CONTRACT_URL = "https://contract.mexc.com/api/v1"
FUNDING_HISTORY_DAYS = SHORT_TERM_HISTORY_DAYS
FUNDING_PAGE_SIZE = 100
PERP_PAGE_LIMIT = 1000


def normalize_mexc_contract_symbol(ticker: str) -> str | None:
    """Convert the application's BTC/USD notation to the BTC_USDT contract."""
    spot = normalize_mexc_symbol(ticker)
    if not spot or not spot.endswith("USDT"):
        return None
    return f"{spot[:-4]}_USDT"


async def fetch_mexc_contract_symbols(
    client: httpx.AsyncClient | None = None,
) -> set[str]:
    """Return listed MEXC perpetual contract symbols."""
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=30)
    try:
        response = await client.get(f"{MEXC_CONTRACT_URL}/contract/detail")
        response.raise_for_status()
        payload = response.json()
        return {
            str(row.get("symbol") or "").upper()
            for row in payload.get("data", [])
            if row.get("symbol")
        }
    finally:
        if owns_client:
            await client.aclose()


async def refresh_short_term_funding_rates(
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
) -> dict:
    """Incrementally maintain settled funding-rate history for the lab universe.

    The endpoint returns pages newest-first, so each ticker resumes from its
    latest stored settlement and stops paging once a page reaches already
    stored (or too old) history.
    """
    now = now or datetime.now(UTC)
    initial_start = int(
        (now - timedelta(days=FUNDING_HISTORY_DAYS)).timestamp() * 1000
    )
    coverage = {
        str(row[0]): int(row[1])
        for row in conn.execute(
            "SELECT ticker, MAX(settle_time) "
            "FROM short_term_funding_rates GROUP BY ticker"
        ).fetchall()
        if row[1] is not None
    }
    inserted = 0
    failures: list[str] = []
    request_at = 0.0

    async with httpx.AsyncClient(timeout=30) as client:
        available = await fetch_mexc_contract_symbols(client)
        eligible_tickers = [
            ticker for ticker in INTRADAY_TICKERS
            if (symbol := normalize_mexc_contract_symbol(ticker))
            and symbol in available
        ]
        unavailable_tickers = sorted(set(INTRADAY_TICKERS) - set(eligible_tickers))
        failures.extend(
            f"{ticker}: no MEXC perpetual contract" for ticker in unavailable_tickers
        )
        for ticker in eligible_tickers:
            symbol = normalize_mexc_contract_symbol(ticker)
            floor = max(initial_start, coverage.get(ticker, 0))
            page = 1
            try:
                while True:
                    delay = 0.12 - (time.monotonic() - request_at)
                    if delay > 0:
                        await asyncio.sleep(delay)
                    response = await client.get(
                        f"{MEXC_CONTRACT_URL}/contract/funding_rate/history",
                        params={
                            "symbol": symbol,
                            "page_num": page,
                            "page_size": FUNDING_PAGE_SIZE,
                        },
                    )
                    request_at = time.monotonic()
                    response.raise_for_status()
                    payload = response.json()
                    if not payload.get("success"):
                        raise RuntimeError(f"API error code {payload.get('code')}")
                    data = payload.get("data") or {}
                    rows = data.get("resultList") or []
                    if not rows:
                        break
                    batch = []
                    for row in rows:
                        try:
                            settle_time = int(row.get("settleTime") or 0)
                            rate = float(row.get("fundingRate"))
                        except (TypeError, ValueError):
                            continue
                        if settle_time <= floor or not math.isfinite(rate):
                            continue
                        batch.append((ticker, settle_time, rate, "mexc-contract"))
                    if batch:
                        before = conn.total_changes
                        conn.executemany(
                            """
                            INSERT INTO short_term_funding_rates (
                                ticker, settle_time, funding_rate, provider
                            ) VALUES (?, ?, ?, ?)
                            ON CONFLICT(ticker, settle_time) DO UPDATE SET
                                funding_rate=excluded.funding_rate,
                                provider=excluded.provider,
                                imported_at=datetime('now')
                            """,
                            batch,
                        )
                        inserted += conn.total_changes - before
                        conn.commit()
                    page_oldest = min(int(row.get("settleTime") or 0) for row in rows)
                    total_pages = int(data.get("totalPage") or page)
                    if page_oldest <= floor or page >= total_pages:
                        break
                    page += 1
            except Exception as exc:
                failures.append(f"{ticker}: {str(exc)[:120]}")

    count, ticker_count, minimum, maximum = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT ticker), MIN(settle_time), MAX(settle_time) "
        "FROM short_term_funding_rates"
    ).fetchone()
    return {
        "inserted": inserted,
        "rate_count": int(count or 0),
        "ticker_count": int(ticker_count or 0),
        "requested_ticker_count": len(INTRADAY_TICKERS),
        "eligible_ticker_count": len(eligible_tickers),
        "data_start": minimum,
        "data_end": maximum,
        "failures": failures,
    }


async def refresh_short_term_perp_candles(
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
) -> dict:
    """Incrementally maintain hourly perpetual-futures candles for basis computations.

    The contract kline API uses Unix-seconds timestamps; we convert to ms
    so every table in the lab stays on the same time representation.
    """
    now = now or datetime.now(UTC)
    interval_ms = 60 * 60 * 1000
    completed_before = int(now.timestamp() * 1000) // interval_ms * interval_ms
    initial_start = int(
        (now - timedelta(days=FUNDING_HISTORY_DAYS)).timestamp() * 1000
    )
    coverage = {
        str(row[0]): (int(row[1]), int(row[2]))
        for row in conn.execute(
            "SELECT ticker, MIN(open_time), MAX(open_time) "
            "FROM short_term_perp_candles GROUP BY ticker"
        ).fetchall()
        if row[1] is not None and row[2] is not None
    }
    inserted = 0
    failures: list[str] = []
    request_at = 0.0

    def upsert(payload: list, ticker: str, range_end: int) -> int:
        batch = []
        for candle in payload:
            if not isinstance(candle, list) or len(candle) < 6:
                continue
            open_time = int(candle[0])
            values = [float(candle[index] or 0) for index in range(1, 6)]
            if open_time >= range_end or not all(math.isfinite(v) for v in values):
                continue
            amount = 0.0
            if len(candle) > 6:
                try:
                    amount = float(candle[6] or 0)
                except (TypeError, ValueError):
                    amount = 0.0
            batch.append((
                ticker, open_time, values[0], values[1], values[2], values[3],
                values[4], amount, "mexc-contract-perp",
            ))
        if not batch:
            return 0
        before = conn.total_changes
        conn.executemany(
            """
            INSERT INTO short_term_perp_candles (
                ticker, open_time, open, high, low, close,
                volume, quote_volume, provider
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker, open_time) DO UPDATE SET
                open=excluded.open, high=excluded.high, low=excluded.low,
                close=excluded.close, volume=excluded.volume,
                quote_volume=excluded.quote_volume, provider=excluded.provider,
                imported_at=datetime('now')
            """,
            batch,
        )
        changed = conn.total_changes - before
        conn.commit()
        return changed

    async with httpx.AsyncClient(timeout=30) as client:
        available = await fetch_mexc_contract_symbols(client)
        eligible_tickers = [
            ticker for ticker in INTRADAY_TICKERS
            if (symbol := normalize_mexc_contract_symbol(ticker))
            and symbol in available
        ]
        for ticker in eligible_tickers:
            symbol = normalize_mexc_contract_symbol(ticker)
            try:
                async def fetch_page(page_start_ms: int, page_end_ms: int) -> list:
                    nonlocal request_at
                    delay = 0.12 - (time.monotonic() - request_at)
                    if delay > 0:
                        await asyncio.sleep(delay)
                    start_s = page_start_ms // 1000
                    end_s = page_end_ms // 1000
                    response = await client.get(
                        f"{MEXC_CONTRACT_URL}/contract/kline/{symbol}",
                        params={
                            "interval": "Min60",
                            "start": start_s,
                            "end": end_s,
                            "limit": PERP_PAGE_LIMIT,
                        },
                    )
                    request_at = time.monotonic()
                    response.raise_for_status()
                    payload = response.json()
                    raw = payload.get("data", {}) if payload.get("success") else {}
                    rows = []
                    times = raw.get("time") or []
                    opens = raw.get("open") or []
                    closes = raw.get("close") or []
                    highs = raw.get("high") or []
                    lows = raw.get("low") or []
                    vols = raw.get("vol") or []
                    amounts = raw.get("amount") or []
                    for i in range(len(times)):
                        try:
                            t_ms = int(times[i]) * 1000
                            rows.append([
                                t_ms, float(opens[i]), float(highs[i]),
                                float(lows[i]), float(closes[i]), float(vols[i]),
                                float(amounts[i]),
                            ])
                        except (IndexError, TypeError, ValueError):
                            continue
                    return rows

                if ticker in coverage:
                    earliest, latest = coverage[ticker]
                    cursor = max(initial_start, latest - interval_ms)
                    while cursor < completed_before:
                        page_end = min(completed_before, cursor + PERP_PAGE_LIMIT * interval_ms)
                        payload = await fetch_page(cursor, page_end)
                        added = upsert(payload, ticker, page_end)
                        inserted += added
                        if len(payload) < PERP_PAGE_LIMIT:
                            break
                        cursor = (payload[-1][0] or cursor) + interval_ms
                    backfill_end = earliest
                else:
                    backfill_end = completed_before

                while backfill_end > initial_start:
                    page_start = max(initial_start, backfill_end - PERP_PAGE_LIMIT * interval_ms)
                    payload = await fetch_page(page_start, backfill_end)
                    if not payload:
                        break
                    added = upsert(payload, ticker, backfill_end)
                    inserted += added
                    backfill_end = page_start
                    if len(payload) < PERP_PAGE_LIMIT:
                        break
            except Exception as exc:
                failures.append(f"{ticker}: {str(exc)[:120]}")

    count, ticker_count, minimum, maximum = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT ticker), MIN(open_time), MAX(open_time) "
        "FROM short_term_perp_candles"
    ).fetchone()
    btc = conn.execute(
        "SELECT MIN(open_time), MAX(open_time) FROM short_term_perp_candles "
        "WHERE ticker='BTC/USD'"
    ).fetchone()
    btc_start = int(btc[0]) if btc and btc[0] is not None else None
    btc_end = int(btc[1]) if btc and btc[1] is not None else None
    return {
        "inserted": inserted,
        "candle_count": int(count or 0),
        "ticker_count": int(ticker_count or 0),
        "eligible_ticker_count": len(eligible_tickers),
        "data_start": minimum,
        "data_end": maximum,
        "btc_start": btc_start,
        "btc_end": btc_end,
        "coverage_days": (
            int((btc_end - btc_start) // (24 * interval_ms)) + 1
            if btc_start is not None and btc_end is not None else 0
        ),
        "failures": failures,
    }
