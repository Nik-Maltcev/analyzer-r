"""MEXC contract (USDT-margined perpetual) data collectors."""

from __future__ import annotations

import asyncio
import math
import sqlite3
import time
from datetime import UTC, datetime, timedelta

import httpx

from app.data.mexc import normalize_mexc_symbol
from app.data.mexc_intraday import INTRADAY_TICKERS

MEXC_CONTRACT_URL = "https://contract.mexc.com/api/v1"
# Funding history is only needed to match the hourly short-term backtest depth.
FUNDING_HISTORY_DAYS = 370
FUNDING_PAGE_SIZE = 100


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
