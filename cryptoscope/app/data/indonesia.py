"""Indonesia IDX daily price ingestion through Yahoo Finance."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence

import pandas as pd

from app.data.tickers import INDONESIA_TICKERS
from app.data.yahoo_market import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_HISTORY_YEARS,
    fetch_yahoo_prices,
    normalize_download,
    provider_symbol,
    upsert_market_prices,
)

YAHOO_SUFFIX = ".JK"


def yahoo_symbol(ticker: str) -> str:
    return provider_symbol(ticker, YAHOO_SUFFIX)


def normalize_indonesia_download(
    raw: pd.DataFrame,
    symbol_to_ticker: dict[str, str],
) -> pd.DataFrame:
    """Convert a yfinance batch response into the application's price schema."""
    return normalize_download(raw, symbol_to_ticker)


def fetch_indonesia_prices(
    tickers: Sequence[str] | None = None,
    history_years: int = DEFAULT_HISTORY_YEARS,
    batch_size: int = DEFAULT_BATCH_SIZE,
    retries: int = 2,
    download_fn: Callable | None = None,
) -> pd.DataFrame:
    """Download adjusted IDX prices in small batches to limit Yahoo throttling."""
    return fetch_yahoo_prices(
        tickers=list(tickers or INDONESIA_TICKERS),
        suffix=YAHOO_SUFFIX,
        market_label="ID",
        history_years=history_years,
        batch_size=batch_size,
        retries=retries,
        download_fn=download_fn,
    )


def upsert_indonesia_prices(
    conn: sqlite3.Connection,
    prices: pd.DataFrame,
    active_tickers: Sequence[str] | None = None,
) -> int:
    """Replace the rolling adjusted history for each successfully fetched ticker."""
    return upsert_market_prices(conn, prices, "id", active_tickers)
