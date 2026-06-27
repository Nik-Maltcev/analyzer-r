"""Shared daily price ingestion for Yahoo-backed equity markets."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta

import pandas as pd

DEFAULT_HISTORY_YEARS = 3
DEFAULT_BATCH_SIZE = 10


def provider_symbol(ticker: str, suffix: str) -> str:
    return f"{ticker}{suffix}"


def _ticker_frame(raw: pd.DataFrame, symbol: str, single_symbol: bool) -> pd.DataFrame:
    if raw.empty:
        return pd.DataFrame()
    if not isinstance(raw.columns, pd.MultiIndex):
        return raw.copy() if single_symbol else pd.DataFrame()

    for level in range(raw.columns.nlevels):
        if symbol in raw.columns.get_level_values(level):
            frame = raw.xs(symbol, axis=1, level=level, drop_level=True)
            if isinstance(frame, pd.Series):
                frame = frame.to_frame()
            return frame
    return pd.DataFrame()


def normalize_download(
    raw: pd.DataFrame,
    symbol_to_ticker: dict[str, str],
) -> pd.DataFrame:
    """Convert a yfinance batch response into the application's price schema."""
    records = []
    single_symbol = len(symbol_to_ticker) == 1

    for symbol, ticker in symbol_to_ticker.items():
        frame = _ticker_frame(raw, symbol, single_symbol)
        if frame.empty:
            continue

        columns = {str(column).lower(): column for column in frame.columns}
        close_column = columns.get("close")
        if close_column is None:
            continue
        volume_column = columns.get("volume")

        dates = pd.to_datetime(frame.index, errors="coerce", utc=True)
        closes = pd.to_numeric(frame[close_column], errors="coerce")
        if volume_column is None:
            volumes = pd.Series(0.0, index=frame.index)
        else:
            volumes = pd.to_numeric(frame[volume_column], errors="coerce").fillna(0)

        ticker_frame = pd.DataFrame({
            "ticker": ticker,
            "date": dates.strftime("%Y-%m-%d"),
            "close": closes.to_numpy(),
            "volume": volumes.to_numpy(),
        })
        ticker_frame = ticker_frame[
            ticker_frame["date"].notna()
            & ticker_frame["close"].notna()
            & (ticker_frame["close"] > 0)
        ]
        records.append(ticker_frame)

    if not records:
        return pd.DataFrame(columns=["ticker", "date", "close", "volume"])

    prices = pd.concat(records, ignore_index=True)
    prices = prices.drop_duplicates(subset=["ticker", "date"], keep="last")
    return prices.sort_values(["ticker", "date"]).reset_index(drop=True)


def fetch_yahoo_prices(
    tickers: Sequence[str],
    suffix: str,
    market_label: str,
    history_years: int = DEFAULT_HISTORY_YEARS,
    batch_size: int = DEFAULT_BATCH_SIZE,
    retries: int = 2,
    download_fn: Callable | None = None,
) -> pd.DataFrame:
    """Download adjusted daily prices in small batches to limit throttling."""
    selected = list(tickers)
    end_date = datetime.now(UTC).date()
    start_date = (
        end_date - timedelta(days=366 * history_years)
    ).isoformat()
    if download_fn is None:
        import yfinance as yf

        download_fn = yf.download

    frames = []
    for offset in range(0, len(selected), batch_size):
        batch = selected[offset:offset + batch_size]
        pending = list(batch)

        for attempt in range(retries + 1):
            symbol_to_ticker = {
                provider_symbol(ticker, suffix): ticker
                for ticker in pending
            }
            try:
                raw = download_fn(
                    tickers=list(symbol_to_ticker),
                    start=start_date,
                    end=end_date.isoformat(),
                    interval="1d",
                    group_by="ticker",
                    auto_adjust=True,
                    repair=True,
                    actions=False,
                    threads=min(4, len(batch)),
                    progress=False,
                    timeout=30,
                )
                normalized = normalize_download(raw, symbol_to_ticker)
                if not normalized.empty:
                    frames.append(normalized)
                    fetched = set(normalized["ticker"])
                    pending = [ticker for ticker in pending if ticker not in fetched]
                if not pending:
                    break
            except Exception as exc:
                if attempt == retries:
                    print(f"[{market_label}] Failed batch {pending}: {exc}")
            if pending and attempt < retries:
                time.sleep(2 ** attempt)
        if pending:
            print(f"[{market_label}] No data after retries: {pending}")

    if not frames:
        return pd.DataFrame(columns=["ticker", "date", "close", "volume"])
    prices = pd.concat(frames, ignore_index=True)
    prices = prices.drop_duplicates(subset=["ticker", "date"], keep="last")
    return prices.sort_values(["ticker", "date"]).reset_index(drop=True)


def upsert_market_prices(
    conn: sqlite3.Connection,
    prices: pd.DataFrame,
    market: str,
    active_tickers: Sequence[str] | None = None,
) -> int:
    """Replace the rolling adjusted history for each successfully fetched ticker."""
    if prices.empty:
        return 0

    if active_tickers:
        placeholders = ",".join("?" for _ in active_tickers)
        conn.execute(
            f"DELETE FROM prices WHERE market = ? AND ticker NOT IN ({placeholders})",
            (market, *active_tickers),
        )

    for ticker, ticker_prices in prices.groupby("ticker"):
        oldest_date = str(ticker_prices["date"].min())
        conn.execute(
            "DELETE FROM prices WHERE market = ? AND ticker = ? AND date < ?",
            (market, ticker, oldest_date),
        )

    rows = [
        (
            str(row.ticker),
            str(row.date),
            float(row.close),
            float(row.volume or 0),
            market,
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
