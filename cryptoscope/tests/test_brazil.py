"""Tests for Brazil B3 price ingestion."""

import sqlite3

import pandas as pd

from app.data.brazil import (
    fetch_brazil_prices,
    normalize_brazil_download,
    upsert_brazil_prices,
    yahoo_symbol,
)


def _multi_ticker_download() -> pd.DataFrame:
    dates = pd.to_datetime(["2026-06-25", "2026-06-26"])
    columns = pd.MultiIndex.from_tuples([
        ("PETR4.SA", "Close"),
        ("PETR4.SA", "Volume"),
        ("VALE3.SA", "Close"),
        ("VALE3.SA", "Volume"),
    ])
    return pd.DataFrame(
        [
            [31.5, 1000, 55.2, 2000],
            [32.0, 1100, 56.1, 2200],
        ],
        index=dates,
        columns=columns,
    )


def test_yahoo_symbol_uses_sao_paulo_suffix():
    assert yahoo_symbol("PETR4") == "PETR4.SA"


def test_normalize_brazil_download_maps_provider_symbols():
    result = normalize_brazil_download(
        _multi_ticker_download(),
        {"PETR4.SA": "PETR4", "VALE3.SA": "VALE3"},
    )

    assert list(result.columns) == ["ticker", "date", "close", "volume"]
    assert set(result["ticker"]) == {"PETR4", "VALE3"}
    assert len(result) == 4
    assert result.iloc[-1].to_dict() == {
        "ticker": "VALE3",
        "date": "2026-06-26",
        "close": 56.1,
        "volume": 2200,
    }


def test_fetch_brazil_prices_batches_and_normalizes():
    calls = []

    def fake_download(**kwargs):
        calls.append(kwargs)
        return _multi_ticker_download()

    result = fetch_brazil_prices(
        tickers=["PETR4", "VALE3"],
        batch_size=2,
        retries=0,
        download_fn=fake_download,
    )

    assert len(calls) == 1
    assert calls[0]["tickers"] == ["PETR4.SA", "VALE3.SA"]
    assert calls[0]["end"] > calls[0]["start"]
    assert calls[0]["auto_adjust"] is True
    assert calls[0]["repair"] is True
    assert len(result) == 4


def test_fetch_retries_only_missing_tickers(monkeypatch):
    calls = []
    full_download = _multi_ticker_download()

    def fake_download(**kwargs):
        calls.append(kwargs["tickers"])
        if len(calls) == 1:
            return full_download.loc[:, pd.IndexSlice[["PETR4.SA"], :]]
        return full_download

    monkeypatch.setattr("app.data.yahoo_market.time.sleep", lambda _: None)
    result = fetch_brazil_prices(
        tickers=["PETR4", "VALE3"],
        batch_size=2,
        retries=1,
        download_fn=fake_download,
    )

    assert calls == [["PETR4.SA", "VALE3.SA"], ["VALE3.SA"]]
    assert set(result["ticker"]) == {"PETR4", "VALE3"}


def test_upsert_brazil_prices_replaces_adjusted_window():
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
            ("PETR4", "2023-01-01", 10, 1, "br"),
            ("PETR4", "2026-06-25", 30, 1, "br"),
            ("OLD3", "2026-06-25", 5, 1, "br"),
            ("AAPL", "2026-06-25", 200, 1, "stocks"),
        ],
    )

    prices = pd.DataFrame([
        {"ticker": "PETR4", "date": "2026-06-25", "close": 31.5, "volume": 1000},
        {"ticker": "PETR4", "date": "2026-06-26", "close": 32.0, "volume": 1100},
    ])
    rows_written = upsert_brazil_prices(conn, prices, ["PETR4"])

    assert rows_written == 2
    brazil_rows = conn.execute(
        "SELECT date, close FROM prices WHERE ticker = 'PETR4' ORDER BY date"
    ).fetchall()
    assert brazil_rows == [("2026-06-25", 31.5), ("2026-06-26", 32.0)]
    assert conn.execute(
        "SELECT COUNT(*) FROM prices WHERE ticker = 'OLD3'"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT market FROM prices WHERE ticker = 'AAPL'"
    ).fetchone()[0] == "stocks"
    conn.close()
