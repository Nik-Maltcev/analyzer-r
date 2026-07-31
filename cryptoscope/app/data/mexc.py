"""Completed MEXC spot candles and safe crypto-provider cutover."""

from __future__ import annotations

import asyncio
import math
import sqlite3
import time
from datetime import UTC, date, datetime, timedelta
from typing import Iterable

import httpx
import pandas as pd

from app.db.schema import (
    CREATE_CRYPTO_PRICE_VERSIONS,
    CREATE_MARKET_DATA_STATE,
    CREATE_CRYPTO_PRICE_VERSION_INDICES,
)

MEXC_REST_URL = "https://api.mexc.com/api/v3"
MEXC_PROVIDER = "mexc"
LEGACY_PROVIDER = "twelvedata_legacy"
MEXC_HISTORY_START = date(2023, 1, 1)
MEXC_KLINE_LIMIT = 1000
DAY_MS = 86_400_000
MEXC_REQUEST_INTERVAL_SECONDS = 0.15


class _RequestPacer:
    """Space public REST calls so the initial history import avoids bursts."""

    def __init__(self, interval_seconds: float) -> None:
        self._interval = interval_seconds
        self._lock = asyncio.Lock()
        self._last_request_at = 0.0

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            delay = self._interval - (now - self._last_request_at)
            if delay > 0:
                await asyncio.sleep(delay)
            self._last_request_at = time.monotonic()


def normalize_mexc_symbol(ticker: str) -> str | None:
    """Convert the application's BTC/USD notation to MEXC BTCUSDT."""
    if "/" not in str(ticker):
        return None
    base, quote = str(ticker).upper().split("/", 1)
    if not base:
        return None
    if quote in {"USD", "USDT"}:
        quote = "USDT"
    return f"{base}{quote}"


async def fetch_mexc_spot_symbols(
    client: httpx.AsyncClient | None = None,
) -> set[str]:
    """Return enabled MEXC spot symbols."""
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=30)
    try:
        response = await client.get(f"{MEXC_REST_URL}/exchangeInfo")
        response.raise_for_status()
        payload = response.json()
        enabled = set()
        for row in payload.get("symbols", []):
            symbol = str(row.get("symbol") or "").upper()
            status = str(row.get("status") or "").upper()
            spot_allowed = row.get("isSpotTradingAllowed")
            if not symbol:
                continue
            if status and status not in {"1", "ENABLED", "TRADING"}:
                continue
            if spot_allowed is False:
                continue
            enabled.add(symbol)
        return enabled
    finally:
        if owns_client:
            await client.aclose()


def ensure_mexc_schema(conn: sqlite3.Connection) -> None:
    """Create staging/state tables and the provider column when needed."""
    conn.execute(CREATE_CRYPTO_PRICE_VERSIONS)
    conn.execute(CREATE_MARKET_DATA_STATE)
    columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(prices)").fetchall()
    }
    if "provider" not in columns:
        conn.execute(
            "ALTER TABLE prices "
            "ADD COLUMN provider TEXT NOT NULL DEFAULT 'legacy'"
        )
    for statement in CREATE_CRYPTO_PRICE_VERSION_INDICES:
        conn.execute(statement)
    conn.commit()


def archive_legacy_crypto_prices(conn: sqlite3.Connection) -> int:
    """Copy current non-MEXC crypto rows into an immutable provider archive."""
    ensure_mexc_schema(conn)
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO crypto_price_versions (
            provider, ticker, date, close, volume, market
        )
        SELECT
            CASE
                WHEN provider = 'mexc' THEN 'mexc'
                ELSE ?
            END,
            ticker,
            date,
            close,
            volume,
            'crypto'
        FROM prices
        WHERE market = 'crypto'
        """,
        (LEGACY_PROVIDER,),
    )
    conn.commit()
    return max(0, int(cursor.rowcount or 0))


def _to_milliseconds(value: date) -> int:
    return int(
        datetime(
            value.year,
            value.month,
            value.day,
            tzinfo=UTC,
        ).timestamp()
        * 1000
    )


def _is_completed_daily_candle(
    open_ms: int,
    completed_before_ms: int,
) -> bool:
    """Treat candles opened before today's UTC boundary as completed."""
    return open_ms < completed_before_ms


async def _fetch_symbol_klines(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    ticker: str,
    symbol: str,
    start_date: date,
    end_date: date,
    pacer: _RequestPacer,
) -> list[dict]:
    """Fetch completed UTC daily candles for one symbol with pagination."""
    cursor_ms = _to_milliseconds(start_date)
    end_ms = _to_milliseconds(end_date + timedelta(days=1)) - 1
    completed_before_ms = _to_milliseconds(datetime.now(UTC).date())
    rows: list[dict] = []

    while cursor_ms <= end_ms:
        payload = None
        for attempt in range(3):
            try:
                async with semaphore:
                    await pacer.wait()
                    response = await client.get(
                        f"{MEXC_REST_URL}/klines",
                        params={
                            "symbol": symbol,
                            "interval": "1d",
                            "startTime": cursor_ms,
                            "endTime": end_ms,
                            "limit": MEXC_KLINE_LIMIT,
                        },
                    )
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, list):
                    raise ValueError("MEXC returned a non-list kline payload")
                break
            except Exception:
                if attempt == 2:
                    raise
                await asyncio.sleep(1.5 * (attempt + 1))

        if not payload:
            break

        last_open_ms = cursor_ms
        for candle in payload:
            if not isinstance(candle, list) or len(candle) < 6:
                continue
            try:
                open_ms = int(candle[0])
                close_price = float(candle[4])
                volume = float(candle[5] or 0)
            except (TypeError, ValueError):
                continue
            last_open_ms = max(last_open_ms, open_ms)
            if (
                not _is_completed_daily_candle(
                    open_ms,
                    completed_before_ms,
                )
                or not math.isfinite(close_price)
                or close_price <= 0
            ):
                continue
            rows.append({
                "ticker": ticker,
                "date": datetime.fromtimestamp(
                    open_ms / 1000,
                    UTC,
                ).date().isoformat(),
                "close": close_price,
                "volume": volume,
                "market": "crypto",
                "provider": MEXC_PROVIDER,
            })

        next_cursor = last_open_ms + DAY_MS
        if next_cursor <= cursor_ms or len(payload) < MEXC_KLINE_LIMIT:
            break
        cursor_ms = next_cursor

    return rows


def _mexc_start_dates(
    conn: sqlite3.Connection,
    tickers: Iterable[str],
) -> dict[str, date]:
    existing = {
        str(row[0]): str(row[1])
        for row in conn.execute(
            """
            SELECT ticker, MAX(date)
            FROM crypto_price_versions
            WHERE provider = ?
            GROUP BY ticker
            """,
            (MEXC_PROVIDER,),
        ).fetchall()
        if row[1]
    }
    result = {}
    for ticker in tickers:
        latest = existing.get(str(ticker))
        if latest:
            parsed = datetime.strptime(latest[:10], "%Y-%m-%d").date()
            result[str(ticker)] = max(
                MEXC_HISTORY_START,
                parsed - timedelta(days=7),
            )
        else:
            result[str(ticker)] = MEXC_HISTORY_START
    return result


async def fetch_mexc_daily_prices(
    tickers: Iterable[str],
    start_dates: dict[str, date],
    end_date: date | None = None,
) -> tuple[pd.DataFrame, dict[str, str], list[str]]:
    """Fetch MEXC history and report unsupported/failed application tickers."""
    selected = [str(ticker).upper() for ticker in tickers]
    end_date = end_date or (datetime.now(UTC).date() - timedelta(days=1))
    timeout = httpx.Timeout(45, connect=15)
    async with httpx.AsyncClient(timeout=timeout) as client:
        available = await fetch_mexc_spot_symbols(client)
        mapping = {
            ticker: symbol
            for ticker in selected
            if (symbol := normalize_mexc_symbol(ticker)) in available
        }
        unsupported = sorted(set(selected) - set(mapping))
        semaphore = asyncio.Semaphore(5)
        pacer = _RequestPacer(MEXC_REQUEST_INTERVAL_SECONDS)
        tasks = {
            ticker: asyncio.create_task(
                _fetch_symbol_klines(
                    client,
                    semaphore,
                    ticker,
                    symbol,
                    start_dates.get(ticker, MEXC_HISTORY_START),
                    end_date,
                    pacer,
                )
            )
            for ticker, symbol in mapping.items()
        }
        rows: list[dict] = []
        failed: list[str] = []
        for ticker, task in tasks.items():
            try:
                rows.extend(await task)
            except Exception as exc:
                failed.append(ticker)
                print(f"[MEXC] History failed for {ticker}: {exc}")

    frame = pd.DataFrame(
        rows,
        columns=[
            "ticker",
            "date",
            "close",
            "volume",
            "market",
            "provider",
        ],
    )
    if not frame.empty:
        frame = (
            frame.drop_duplicates(["ticker", "date"], keep="last")
            .sort_values(["ticker", "date"])
            .reset_index(drop=True)
        )
    return frame, mapping, sorted(set(unsupported + failed))


def upsert_mexc_price_versions(
    conn: sqlite3.Connection,
    prices: pd.DataFrame,
) -> int:
    """Upsert MEXC candles into staging without touching active prices."""
    ensure_mexc_schema(conn)
    if prices.empty:
        return 0
    rows = [
        (
            MEXC_PROVIDER,
            str(row.ticker),
            str(row.date)[:10],
            float(row.close),
            float(row.volume or 0),
            "crypto",
        )
        for row in prices.itertuples(index=False)
        if float(row.close) > 0
    ]
    before = conn.total_changes
    conn.executemany(
        """
        INSERT INTO crypto_price_versions (
            provider, ticker, date, close, volume, market
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(provider, ticker, date) DO UPDATE SET
            close = excluded.close,
            volume = excluded.volume,
            imported_at = datetime('now')
        """,
        rows,
    )
    conn.commit()
    return conn.total_changes - before


def activate_mexc_prices(
    conn: sqlite3.Connection,
    expected_tickers: Iterable[str],
    minimum_history_rows: int = 180,
) -> dict:
    """Atomically make validated MEXC candles the active crypto dataset."""
    ensure_mexc_schema(conn)
    expected = {
        str(ticker).upper()
        for ticker in expected_tickers
        if str(ticker).strip()
    }
    yesterday = (datetime.now(UTC).date() - timedelta(days=1)).isoformat()
    rows = conn.execute(
        """
        SELECT ticker, COUNT(*) AS rows, MAX(date) AS latest
        FROM crypto_price_versions
        WHERE provider = ?
        GROUP BY ticker
        """,
        (MEXC_PROVIDER,),
    ).fetchall()
    valid = {
        str(row[0]): {
            "rows": int(row[1] or 0),
            "latest": str(row[2] or ""),
        }
        for row in rows
        if str(row[0]) in expected
        if int(row[1] or 0) >= minimum_history_rows
        and str(row[2] or "") >= yesterday
    }
    minimum_tickers = max(1, math.ceil(len(expected) * 0.85))
    problems = []
    if "BTC/USD" not in valid:
        problems.append("BTC/USD is missing or stale")
    if len(valid) < minimum_tickers:
        problems.append(
            f"coverage {len(valid)}/{len(expected)}; "
            f"minimum {minimum_tickers}"
        )
    if problems:
        raise RuntimeError("MEXC cutover rejected: " + "; ".join(problems))

    previous = conn.execute(
        """
        SELECT active_provider
        FROM market_data_state
        WHERE market = 'crypto'
        """
    ).fetchone()
    previous_provider = (
        str(previous[0])
        if previous
        else LEGACY_PROVIDER
    )

    selected_tickers = sorted(valid)
    placeholders = ",".join("?" for _ in selected_tickers)
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM prices WHERE market = 'crypto'")
        conn.execute(
            f"""
            INSERT INTO prices (
                ticker, date, close, volume, market, provider
            )
            SELECT ticker, date, close, volume, 'crypto', ?
            FROM crypto_price_versions
            WHERE provider = ?
              AND ticker IN ({placeholders})
            """,
            (MEXC_PROVIDER, MEXC_PROVIDER, *selected_tickers),
        )
        active_stats = conn.execute(
            """
            SELECT MAX(date), COUNT(DISTINCT ticker), COUNT(*)
            FROM prices
            WHERE market = 'crypto' AND provider = ?
            """,
            (MEXC_PROVIDER,),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO market_data_state (
                market, active_provider, previous_provider,
                latest_data_date, ticker_count, row_count,
                switched_at, updated_at
            ) VALUES ('crypto', ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
            ON CONFLICT(market) DO UPDATE SET
                previous_provider = CASE
                    WHEN market_data_state.active_provider != excluded.active_provider
                    THEN market_data_state.active_provider
                    ELSE market_data_state.previous_provider
                END,
                active_provider = excluded.active_provider,
                latest_data_date = excluded.latest_data_date,
                ticker_count = excluded.ticker_count,
                row_count = excluded.row_count,
                switched_at = CASE
                    WHEN market_data_state.active_provider != excluded.active_provider
                    THEN datetime('now')
                    ELSE market_data_state.switched_at
                END,
                updated_at = datetime('now')
            """,
            (
                MEXC_PROVIDER,
                previous_provider,
                str(active_stats[0]),
                int(active_stats[1]),
                int(active_stats[2]),
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return {
        "provider": MEXC_PROVIDER,
        "latest_date": str(active_stats[0]),
        "tickers": int(active_stats[1]),
        "rows": int(active_stats[2]),
        "excluded_tickers": sorted(expected - set(selected_tickers)),
    }


def rollback_crypto_provider(
    conn: sqlite3.Connection,
    provider: str = LEGACY_PROVIDER,
) -> dict:
    """Restore an archived provider without deleting any versioned candles."""
    ensure_mexc_schema(conn)
    stats = conn.execute(
        """
        SELECT MAX(date), COUNT(DISTINCT ticker), COUNT(*)
        FROM crypto_price_versions
        WHERE provider = ? AND market = 'crypto'
        """,
        (provider,),
    ).fetchone()
    if not stats or int(stats[2] or 0) < 1:
        raise RuntimeError(f"Crypto rollback rejected: {provider} is empty")

    previous = conn.execute(
        """
        SELECT active_provider
        FROM market_data_state
        WHERE market = 'crypto'
        """
    ).fetchone()
    previous_provider = str(previous[0]) if previous else MEXC_PROVIDER

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM prices WHERE market = 'crypto'")
        conn.execute(
            """
            INSERT INTO prices (
                ticker, date, close, volume, market, provider
            )
            SELECT ticker, date, close, volume, 'crypto', provider
            FROM crypto_price_versions
            WHERE provider = ? AND market = 'crypto'
            """,
            (provider,),
        )
        conn.execute(
            """
            INSERT INTO market_data_state (
                market, active_provider, previous_provider,
                latest_data_date, ticker_count, row_count,
                switched_at, updated_at
            ) VALUES ('crypto', ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
            ON CONFLICT(market) DO UPDATE SET
                previous_provider = excluded.previous_provider,
                active_provider = excluded.active_provider,
                latest_data_date = excluded.latest_data_date,
                ticker_count = excluded.ticker_count,
                row_count = excluded.row_count,
                switched_at = datetime('now'),
                updated_at = datetime('now')
            """,
            (
                provider,
                previous_provider,
                str(stats[0]),
                int(stats[1]),
                int(stats[2]),
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return {
        "provider": provider,
        "latest_date": str(stats[0]),
        "tickers": int(stats[1]),
        "rows": int(stats[2]),
    }


async def refresh_mexc_crypto_market(
    conn: sqlite3.Connection,
    tickers: Iterable[str],
) -> dict:
    """Archive legacy prices, refresh staging and activate validated MEXC data."""
    selected = [str(ticker).upper() for ticker in tickers]
    archived = archive_legacy_crypto_prices(conn)
    start_dates = _mexc_start_dates(conn, selected)
    prices, mapping, unavailable = await fetch_mexc_daily_prices(
        selected,
        start_dates,
    )
    fetched_tickers = (
        int(prices["ticker"].nunique())
        if not prices.empty
        else 0
    )
    latest_date = (
        str(prices["date"].max())
        if not prices.empty
        else "none"
    )
    print(
        "[MEXC] Daily refresh: "
        f"{len(prices)} rows, "
        f"{fetched_tickers}/{len(selected)} tickers, "
        f"latest={latest_date}, "
        f"unsupported_or_failed={len(unavailable)}"
    )
    written = upsert_mexc_price_versions(conn, prices)
    activation = activate_mexc_prices(conn, selected)
    return {
        **activation,
        "archived_legacy_rows": archived,
        "staged_rows_written": written,
        "supported_tickers": len(mapping),
        "unavailable_tickers": unavailable,
    }
