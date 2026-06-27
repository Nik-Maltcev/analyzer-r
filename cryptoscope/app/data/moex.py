"""Daily MOEX price ingestion through the public ISS API."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import date, timedelta

import aiohttp
import pandas as pd

from app.data.tickers import RU_TICKERS

MOEX_CANDLES_URL = (
    "https://iss.moex.com/iss/engines/stock/markets/shares/"
    "boards/TQBR/securities/{ticker}/candles.json"
)
DEFAULT_HISTORY_YEARS = 3
DEFAULT_CONCURRENCY = 6
PAGE_SIZE = 500

RequestFn = Callable[[str, dict], Awaitable[dict]]


def normalize_moex_candles(payload: dict, ticker: str) -> pd.DataFrame:
    """Convert an ISS candles response into the application's price schema."""
    candles = payload.get("candles") or {}
    columns = candles.get("columns") or []
    data = candles.get("data") or []
    if not columns or not data:
        return pd.DataFrame(columns=["ticker", "date", "close", "volume"])

    frame = pd.DataFrame(data, columns=columns)
    if "begin" not in frame or "close" not in frame:
        return pd.DataFrame(columns=["ticker", "date", "close", "volume"])

    volumes = (
        pd.to_numeric(frame["volume"], errors="coerce").fillna(0)
        if "volume" in frame
        else pd.Series(0.0, index=frame.index)
    )
    prices = pd.DataFrame({
        "ticker": ticker,
        "date": frame["begin"].astype(str).str[:10],
        "close": pd.to_numeric(frame["close"], errors="coerce"),
        "volume": volumes,
    })
    prices = prices[
        prices["date"].str.match(r"^\d{4}-\d{2}-\d{2}$", na=False)
        & prices["close"].notna()
        & (prices["close"] > 0)
    ]
    return prices.drop_duplicates(subset=["ticker", "date"], keep="last")


async def fetch_ru_prices(
    tickers: Sequence[str] | None = None,
    start_dates: Mapping[str, str] | None = None,
    history_years: int = DEFAULT_HISTORY_YEARS,
    retries: int = 1,
    concurrency: int = DEFAULT_CONCURRENCY,
    request_fn: RequestFn | None = None,
) -> pd.DataFrame:
    """Download completed daily candles for liquid MOEX equities."""
    selected = list(tickers or RU_TICKERS)
    end_date = date.today()
    end_date_str = end_date.isoformat()
    default_start = (end_date - timedelta(days=366 * history_years)).isoformat()
    starts = dict(start_dates or {})
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def fetch_all(request: RequestFn) -> list[pd.DataFrame]:
        async def fetch_ticker(ticker: str) -> pd.DataFrame:
            ticker_start = starts.get(ticker, default_start)
            frames = []
            offset = 0

            while True:
                params = {
                    "from": ticker_start,
                    "till": end_date_str,
                    "interval": 24,
                    "start": offset,
                    "iss.meta": "off",
                    "iss.only": "candles",
                }
                payload = None
                for attempt in range(retries + 1):
                    try:
                        async with semaphore:
                            payload = await request(ticker, params)
                        break
                    except Exception as exc:
                        if attempt == retries:
                            print(f"[RU] Failed {ticker} at offset {offset}: {exc}")
                        else:
                            await asyncio.sleep(2 ** attempt)

                if payload is None:
                    break

                page_data = (payload.get("candles") or {}).get("data") or []
                page = normalize_moex_candles(payload, ticker)
                if not page.empty:
                    page = page[
                        (page["date"] >= ticker_start)
                        & (page["date"] < end_date_str)
                    ]
                    frames.append(page)

                if len(page_data) < PAGE_SIZE:
                    break
                offset += PAGE_SIZE

            if not frames:
                return pd.DataFrame(columns=["ticker", "date", "close", "volume"])
            return pd.concat(frames, ignore_index=True)

        return await asyncio.gather(*(fetch_ticker(ticker) for ticker in selected))

    if request_fn is not None:
        frames = await fetch_all(request_fn)
    else:
        timeout = aiohttp.ClientTimeout(total=60)
        headers = {"User-Agent": "CryptoScope/1.0"}
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async def request(ticker: str, params: dict) -> dict:
                url = MOEX_CANDLES_URL.format(ticker=ticker)
                async with session.get(url, params=params) as response:
                    response.raise_for_status()
                    return await response.json()

            frames = await fetch_all(request)

    frames = [frame for frame in frames if not frame.empty]
    if not frames:
        return pd.DataFrame(columns=["ticker", "date", "close", "volume"])
    prices = pd.concat(frames, ignore_index=True)
    prices = prices.drop_duplicates(subset=["ticker", "date"], keep="last")
    return prices.sort_values(["ticker", "date"]).reset_index(drop=True)


def migrate_legacy_ru_ticker(conn: sqlite3.Connection) -> None:
    """Preserve TCSG history and favorites after its ticker changed to T."""
    conn.execute(
        """
        INSERT INTO prices (ticker, date, close, volume, market)
        SELECT 'T', date, close, volume, market
        FROM prices
        WHERE market = 'ru' AND ticker = 'TCSG'
        ON CONFLICT(ticker, date) DO NOTHING
        """
    )
    conn.execute("DELETE FROM prices WHERE market = 'ru' AND ticker = 'TCSG'")

    favorite_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'favorites'"
    ).fetchone()
    if favorite_table:
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(favorites)").fetchall()
        }
        market_assignment = ", market = 'ru'" if "market" in columns else ""
        conn.execute(
            f"""
            UPDATE favorites
            SET ticker_a = CASE WHEN ticker_a = 'TCSG' THEN 'T' ELSE ticker_a END,
                ticker_b = CASE WHEN ticker_b = 'TCSG' THEN 'T' ELSE ticker_b END,
                pair = REPLACE(pair, 'TCSG', 'T')
                {market_assignment}
            WHERE ticker_a = 'TCSG' OR ticker_b = 'TCSG'
            """
        )
    conn.commit()


def latest_ru_start_dates(
    conn: sqlite3.Connection,
    tickers: Sequence[str] | None = None,
    overlap_days: int = 7,
) -> dict[str, str]:
    """Return per-ticker refresh starts with overlap for corrected candles."""
    selected = list(tickers or RU_TICKERS)
    rows = conn.execute(
        """
        SELECT ticker, MAX(date)
        FROM prices
        WHERE market = 'ru'
        GROUP BY ticker
        """
    ).fetchall()
    latest = {ticker: max_date for ticker, max_date in rows if max_date}
    starts = {}
    for ticker in selected:
        max_date = latest.get(ticker)
        if max_date:
            starts[ticker] = (
                date.fromisoformat(max_date) - timedelta(days=overlap_days)
            ).isoformat()
    return starts


def upsert_ru_prices(
    conn: sqlite3.Connection,
    prices: pd.DataFrame,
    active_tickers: Sequence[str] | None = None,
) -> int:
    """Upsert MOEX candles while preserving history outside the refresh window."""
    if prices.empty:
        return 0

    if active_tickers:
        placeholders = ",".join("?" for _ in active_tickers)
        conn.execute(
            f"DELETE FROM prices WHERE market = 'ru' AND ticker NOT IN ({placeholders})",
            tuple(active_tickers),
        )

    rows = [
        (
            str(row.ticker),
            str(row.date),
            float(row.close),
            float(row.volume or 0),
            "ru",
        )
        for row in prices.itertuples(index=False)
    ]
    conn.executemany(
        """
        INSERT INTO prices (ticker, date, close, volume, market)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(ticker, date) DO UPDATE SET
            close = excluded.close,
            volume = excluded.volume,
            market = excluded.market
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def reprice_active_ru_favorites(conn: sqlite3.Connection) -> None:
    """Repair entry prices for RU favorites created while RU data was stale."""
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'favorites'"
    ).fetchone()
    if not table:
        return
    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(favorites)").fetchall()
    }
    if "market" not in columns:
        return

    conn.execute(
        """
        UPDATE favorites
        SET price_a_entry = COALESCE((
                SELECT close
                FROM prices
                WHERE market = 'ru'
                  AND ticker = favorites.ticker_a
                  AND date <= substr(favorites.entry_time, 1, 10)
                ORDER BY date DESC
                LIMIT 1
            ), price_a_entry),
            price_b_entry = COALESCE((
                SELECT close
                FROM prices
                WHERE market = 'ru'
                  AND ticker = favorites.ticker_b
                  AND date <= substr(favorites.entry_time, 1, 10)
                ORDER BY date DESC
                LIMIT 1
            ), price_b_entry)
        WHERE market = 'ru' AND status = 'active'
        """
    )
    conn.commit()
