"""Tests for MOEX ISS price ingestion."""

import sqlite3
from datetime import date, timedelta

import pandas as pd
import pytest

from app.data.moex import (
    fetch_ru_prices,
    latest_ru_start_dates,
    migrate_legacy_ru_ticker,
    normalize_moex_candles,
    reprice_active_ru_favorites,
    upsert_ru_prices,
)


def _payload(rows):
    return {
        "candles": {
            "columns": [
                "open", "close", "high", "low",
                "value", "volume", "begin", "end",
            ],
            "data": rows,
        }
    }


def _candle(day: str, close: float, volume: float = 1000):
    return [
        close - 1,
        close,
        close + 1,
        close - 2,
        close * volume,
        volume,
        f"{day} 00:00:00",
        f"{day} 23:59:59",
    ]


def test_normalize_moex_candles_maps_daily_prices():
    payload = _payload([
        _candle("2026-06-25", 295.16, 100),
        _candle("2026-06-26", 299.18, 200),
    ])

    result = normalize_moex_candles(payload, "SBER")

    assert result.to_dict(orient="records") == [
        {"ticker": "SBER", "date": "2026-06-25", "close": 295.16, "volume": 100},
        {"ticker": "SBER", "date": "2026-06-26", "close": 299.18, "volume": 200},
    ]


@pytest.mark.asyncio
async def test_fetch_ru_prices_paginates_and_excludes_current_day(monkeypatch):
    monkeypatch.setattr("app.data.moex.PAGE_SIZE", 2)
    today = date.today()
    day_1 = (today - timedelta(days=2)).isoformat()
    day_2 = (today - timedelta(days=1)).isoformat()
    calls = []

    async def fake_request(ticker, params):
        calls.append((ticker, params["start"]))
        if params["start"] == 0:
            return _payload([
                _candle(day_1, 100),
                _candle(day_2, 101),
            ])
        return _payload([_candle(today.isoformat(), 102)])

    result = await fetch_ru_prices(
        tickers=["SBER"],
        start_dates={"SBER": day_1},
        retries=0,
        request_fn=fake_request,
    )

    assert calls == [("SBER", 0), ("SBER", 2)]
    assert list(result["date"]) == [day_1, day_2]


def test_ru_storage_migrates_ticker_and_repairs_favorite_entry():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE prices (
            ticker TEXT NOT NULL,
            date TEXT NOT NULL,
            close REAL NOT NULL,
            volume REAL,
            market TEXT,
            PRIMARY KEY (ticker, date)
        );
        CREATE TABLE favorites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pair TEXT NOT NULL,
            market TEXT DEFAULT 'crypto',
            ticker_a TEXT NOT NULL,
            ticker_b TEXT NOT NULL,
            price_a_entry REAL,
            price_b_entry REAL,
            entry_time TEXT,
            status TEXT DEFAULT 'active'
        );
        """
    )
    conn.executemany(
        "INSERT INTO prices (ticker, date, close, volume, market) VALUES (?, ?, ?, ?, ?)",
        [
            ("TCSG", "2024-11-27", 2500, 10, "ru"),
            ("SBER", "2026-06-25", 295, 10, "ru"),
            ("SBER", "2026-06-26", 299, 10, "ru"),
        ],
    )
    conn.execute(
        """
        INSERT INTO favorites (
            pair, market, ticker_a, ticker_b,
            price_a_entry, price_b_entry, entry_time, status
        )
        VALUES ('TCSG_SBER', 'ru', 'TCSG', 'SBER', 0, 0, '2026-06-25 12:00:00', 'active')
        """
    )

    migrate_legacy_ru_ticker(conn)
    new_prices = pd.DataFrame([
        {"ticker": "T", "date": "2026-06-26", "close": 3200, "volume": 20},
    ])
    assert upsert_ru_prices(conn, new_prices, ["T", "SBER"]) == 1
    reprice_active_ru_favorites(conn)

    assert conn.execute(
        "SELECT COUNT(*) FROM prices WHERE ticker = 'TCSG'"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT close FROM prices WHERE ticker = 'T' AND date = '2024-11-27'"
    ).fetchone()[0] == 2500
    assert conn.execute(
        "SELECT pair, ticker_a, market, price_b_entry FROM favorites"
    ).fetchone() == ("T_SBER", "T", "ru", 295)
    assert latest_ru_start_dates(conn, ["SBER"], overlap_days=1) == {
        "SBER": "2026-06-25"
    }
    conn.close()
