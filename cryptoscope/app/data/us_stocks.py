"""US stocks and ETFs daily price ingestion through Yahoo Finance."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence

import pandas as pd

from app.data.tickers import STOCK_TICKERS
from app.data.yahoo_market import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_HISTORY_YEARS,
    fetch_yahoo_prices,
    normalize_download,
    upsert_market_prices,
)


def normalize_us_download(
    raw: pd.DataFrame,
    symbol_to_ticker: dict[str, str],
) -> pd.DataFrame:
    return normalize_download(raw, symbol_to_ticker)


def fetch_us_stock_prices(
    tickers: Sequence[str] | None = None,
    history_years: int = DEFAULT_HISTORY_YEARS,
    batch_size: int = DEFAULT_BATCH_SIZE,
    retries: int = 2,
    download_fn: Callable | None = None,
) -> pd.DataFrame:
    """Download adjusted US stock prices in rate-limit-friendly batches."""
    return fetch_yahoo_prices(
        tickers=list(tickers or STOCK_TICKERS),
        suffix="",
        market_label="US",
        history_years=history_years,
        batch_size=batch_size,
        retries=retries,
        download_fn=download_fn,
    )


def upsert_us_stock_prices(
    conn: sqlite3.Connection,
    prices: pd.DataFrame,
    active_tickers: Sequence[str] | None = None,
) -> int:
    return upsert_market_prices(conn, prices, "stocks", active_tickers)
