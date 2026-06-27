"""Tests for Indonesia IDX price ingestion."""

import sqlite3

import pandas as pd

from app.data.indonesia import (
    fetch_indonesia_prices,
    normalize_indonesia_download,
    upsert_indonesia_prices,
    yahoo_symbol,
)
from app.data.tickers import ALL_MARKETS, INDONESIA_TICKERS


def _multi_ticker_download() -> pd.DataFrame:
    dates = pd.to_datetime(["2026-06-25", "2026-06-26"])
    columns = pd.MultiIndex.from_tuples([
        ("BBCA.JK", "Close"),
        ("BBCA.JK", "Volume"),
        ("TLKM.JK", "Close"),
        ("TLKM.JK", "Volume"),
    ])
    return pd.DataFrame(
        [
            [6175, 1000, 2480, 2000],
            [6200, 1100, 2500, 2200],
        ],
        index=dates,
        columns=columns,
    )


def test_indonesia_universe_is_registered_without_ticker_collisions():
    other_tickers = {
        ticker
        for market, tickers in ALL_MARKETS.items()
        if market != "id"
        for ticker in tickers
    }

    assert len(INDONESIA_TICKERS) == 45
    assert not set(INDONESIA_TICKERS) & other_tickers
    assert ALL_MARKETS["id"] is INDONESIA_TICKERS


def test_yahoo_symbol_uses_jakarta_suffix():
    assert yahoo_symbol("BBCA") == "BBCA.JK"


def test_normalize_indonesia_download_maps_provider_symbols():
    result = normalize_indonesia_download(
        _multi_ticker_download(),
        {"BBCA.JK": "BBCA", "TLKM.JK": "TLKM"},
    )

    assert list(result.columns) == ["ticker", "date", "close", "volume"]
    assert set(result["ticker"]) == {"BBCA", "TLKM"}
    assert len(result) == 4


def test_fetch_indonesia_prices_uses_adjusted_batches():
    calls = []

    def fake_download(**kwargs):
        calls.append(kwargs)
        return _multi_ticker_download()

    result = fetch_indonesia_prices(
        tickers=["BBCA", "TLKM"],
        batch_size=2,
        retries=0,
        download_fn=fake_download,
    )

    assert calls[0]["tickers"] == ["BBCA.JK", "TLKM.JK"]
    assert calls[0]["auto_adjust"] is True
    assert calls[0]["repair"] is True
    assert len(result) == 4


def test_upsert_indonesia_prices_only_updates_id_market():
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
        "INSERT INTO prices (ticker, date, close, volume, market) VALUES (?, ?, ?, ?, ?)",
        [
            ("BBCA", "2023-01-01", 7000, 1, "id"),
            ("BBCA", "2026-06-25", 6100, 1, "id"),
            ("OLD", "2026-06-25", 100, 1, "id"),
            ("AAPL", "2026-06-25", 200, 1, "stocks"),
        ],
    )
    prices = pd.DataFrame([
        {"ticker": "BBCA", "date": "2026-06-25", "close": 6175, "volume": 1000},
        {"ticker": "BBCA", "date": "2026-06-26", "close": 6200, "volume": 1100},
    ])

    rows_written = upsert_indonesia_prices(conn, prices, ["BBCA"])

    assert rows_written == 2
    assert conn.execute(
        "SELECT date, close FROM prices WHERE ticker = 'BBCA' ORDER BY date"
    ).fetchall() == [("2026-06-25", 6175), ("2026-06-26", 6200)]
    assert conn.execute(
        "SELECT COUNT(*) FROM prices WHERE ticker = 'OLD'"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT market FROM prices WHERE ticker = 'AAPL'"
    ).fetchone()[0] == "stocks"
    conn.close()
