"""Tests for US stock price ingestion."""

import sqlite3

import pandas as pd

from app.data.us_stocks import fetch_us_stock_prices, upsert_us_stock_prices


def _download() -> pd.DataFrame:
    dates = pd.to_datetime(["2026-06-25", "2026-06-26"])
    columns = pd.MultiIndex.from_tuples([
        ("AAPL", "Close"),
        ("AAPL", "Volume"),
        ("MSFT", "Close"),
        ("MSFT", "Volume"),
    ])
    return pd.DataFrame(
        [
            [200.0, 1000, 508.0, 2000],
            [201.5, 1100, 510.2, 2200],
        ],
        index=dates,
        columns=columns,
    )


def test_fetch_us_stocks_uses_adjusted_yahoo_prices():
    calls = []

    def fake_download(**kwargs):
        calls.append(kwargs)
        return _download()

    result = fetch_us_stock_prices(
        tickers=["AAPL", "MSFT"],
        batch_size=2,
        retries=0,
        download_fn=fake_download,
    )

    assert calls[0]["tickers"] == ["AAPL", "MSFT"]
    assert calls[0]["auto_adjust"] is True
    assert set(result["ticker"]) == {"AAPL", "MSFT"}
    assert len(result) == 4


def test_upsert_us_stocks_preserves_other_markets():
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
            ("AAPL", "2026-06-25", 199, 1, "stocks"),
            ("OLD", "2026-06-25", 10, 1, "stocks"),
            ("SBER", "2026-06-25", 300, 1, "ru"),
        ],
    )
    prices = pd.DataFrame([
        {"ticker": "AAPL", "date": "2026-06-25", "close": 200, "volume": 1000},
        {"ticker": "AAPL", "date": "2026-06-26", "close": 201.5, "volume": 1100},
    ])

    rows = upsert_us_stock_prices(conn, prices, ["AAPL"])

    assert rows == 2
    assert conn.execute(
        "SELECT close FROM prices WHERE ticker = 'AAPL' ORDER BY date DESC LIMIT 1"
    ).fetchone()[0] == 201.5
    assert conn.execute(
        "SELECT COUNT(*) FROM prices WHERE ticker = 'OLD'"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT market FROM prices WHERE ticker = 'SBER'"
    ).fetchone()[0] == "ru"
    conn.close()
