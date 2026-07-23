"""Yahoo-backed equity markets available to the global administrator."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import pandas as pd

from app.data.tickers import (
    AUSTRALIA_TICKERS,
    CANADA_TICKERS,
    MALAYSIA_TICKERS,
)
from app.data.yahoo_market import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_HISTORY_YEARS,
    fetch_yahoo_prices,
    upsert_market_prices,
)


@dataclass(frozen=True)
class YahooEquityMarket:
    market: str
    label: str
    tickers: tuple[str, ...]


INTERNATIONAL_MARKETS = {
    "au": YahooEquityMarket(
        market="au",
        label="AU / ASX",
        tickers=tuple(AUSTRALIA_TICKERS),
    ),
    "ca": YahooEquityMarket(
        market="ca",
        label="CA / TSX",
        tickers=tuple(CANADA_TICKERS),
    ),
    "my": YahooEquityMarket(
        market="my",
        label="MY / Bursa",
        tickers=tuple(MALAYSIA_TICKERS),
    ),
}


def get_international_market(market: str) -> YahooEquityMarket:
    try:
        return INTERNATIONAL_MARKETS[market]
    except KeyError as exc:
        raise ValueError(f"Unknown international market: {market}") from exc


def fetch_international_prices(
    market: str,
    tickers: Sequence[str] | None = None,
    history_years: int = DEFAULT_HISTORY_YEARS,
    batch_size: int = DEFAULT_BATCH_SIZE,
    retries: int = 2,
    download_fn: Callable | None = None,
) -> pd.DataFrame:
    config = get_international_market(market)
    return fetch_yahoo_prices(
        tickers=list(tickers or config.tickers),
        suffix="",
        market_label=config.label,
        history_years=history_years,
        batch_size=batch_size,
        retries=retries,
        download_fn=download_fn,
    )


def upsert_international_prices(
    conn: sqlite3.Connection,
    market: str,
    prices: pd.DataFrame,
    active_tickers: Sequence[str] | None = None,
) -> int:
    config = get_international_market(market)
    return upsert_market_prices(
        conn,
        prices,
        config.market,
        active_tickers or config.tickers,
    )
