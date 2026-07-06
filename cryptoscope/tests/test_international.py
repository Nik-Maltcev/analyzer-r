"""Tests for administrator-only Yahoo equity markets."""

import sqlite3

import pandas as pd
import pytest

from app.data.international import (
    INTERNATIONAL_MARKETS,
    fetch_international_prices,
    get_international_market,
    upsert_international_prices,
)
from app.data.tickers import ALL_MARKETS


@pytest.mark.parametrize(
    ("market", "suffix"),
    [
        ("au", ".AX"),
        ("ca", ".TO"),
        ("my", ".KL"),
        ("za", ".JO"),
    ],
)
def test_international_universes_use_full_yahoo_symbols(market, suffix):
    config = get_international_market(market)

    assert len(config.tickers) == 20
    assert all(ticker.endswith(suffix) for ticker in config.tickers)
    assert ALL_MARKETS[market] == list(config.tickers)


def test_international_tickers_do_not_collide():
    all_tickers = [
        ticker
        for tickers in ALL_MARKETS.values()
        for ticker in tickers
    ]

    assert len(all_tickers) == len(set(all_tickers))


def test_fetch_international_prices_preserves_provider_symbols():
    dates = pd.to_datetime(["2026-07-02", "2026-07-03"])
    columns = pd.MultiIndex.from_tuples([
        ("BHP.AX", "Close"),
        ("BHP.AX", "Volume"),
        ("CBA.AX", "Close"),
        ("CBA.AX", "Volume"),
    ])
    raw = pd.DataFrame(
        [
            [48.1, 1000, 190.2, 2000],
            [48.4, 1100, 191.5, 2200],
        ],
        index=dates,
        columns=columns,
    )
    calls = []

    def fake_download(**kwargs):
        calls.append(kwargs)
        return raw

    result = fetch_international_prices(
        "au",
        tickers=["BHP.AX", "CBA.AX"],
        batch_size=2,
        retries=0,
        download_fn=fake_download,
    )

    assert calls[0]["tickers"] == ["BHP.AX", "CBA.AX"]
    assert set(result["ticker"]) == {"BHP.AX", "CBA.AX"}
    assert len(result) == 4


def test_upsert_international_prices_is_market_scoped():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE prices (
            ticker TEXT NOT NULL,
            date TEXT NOT NULL,
            close REAL NOT NULL,
            volume REAL,
            market TEXT,
            PRIMARY KEY (ticker, date)
        )
        """
    )
    conn.executemany(
        """
        INSERT INTO prices (ticker, date, close, volume, market)
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            ("BHP.AX", "2026-07-02", 48.1, 1, "au"),
            ("OLD.AX", "2026-07-02", 10, 1, "au"),
            ("AAPL", "2026-07-02", 200, 1, "stocks"),
        ],
    )
    prices = pd.DataFrame([
        {
            "ticker": "BHP.AX",
            "date": "2026-07-02",
            "close": 48.4,
            "volume": 1100,
        },
        {
            "ticker": "BHP.AX",
            "date": "2026-07-03",
            "close": 49.0,
            "volume": 1200,
        },
    ])

    rows = upsert_international_prices(
        conn,
        "au",
        prices,
        ["BHP.AX"],
    )

    assert rows == 2
    assert conn.execute(
        "SELECT COUNT(*) FROM prices WHERE ticker = 'OLD.AX'"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT market FROM prices WHERE ticker = 'AAPL'"
    ).fetchone()[0] == "stocks"
    conn.close()


def test_unknown_international_market_is_rejected():
    with pytest.raises(ValueError):
        get_international_market("unknown")


def test_registered_international_markets_are_expected():
    assert tuple(INTERNATIONAL_MARKETS) == ("au", "ca", "my", "za")
