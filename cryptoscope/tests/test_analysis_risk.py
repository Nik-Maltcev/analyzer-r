"""Integration test for guarded pair analysis."""

import sqlite3
from datetime import date, timedelta

import numpy as np

from scripts.compute_analysis import compute_market_pairs


def test_ru_analysis_migrates_and_persists_risk_fields(tmp_path):
    db_path = tmp_path / "risk-analysis.db"
    conn = sqlite3.connect(db_path)
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
    conn.execute(
        """
        CREATE TABLE pairs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market TEXT NOT NULL,
            ticker_a TEXT NOT NULL,
            ticker_b TEXT NOT NULL,
            corr REAL,
            halflife INTEGER,
            t_stat REAL,
            is_coint INTEGER,
            hedge_ratio REAL,
            score REAL,
            z_now REAL,
            z_forecast REAL,
            signal TEXT,
            signal_type TEXT,
            strength TEXT,
            computed_at TEXT,
            UNIQUE (market, ticker_a, ticker_b)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            ticker_a TEXT,
            ticker_b TEXT,
            z_score REAL,
            z_forecast REAL,
            signal TEXT,
            strength TEXT,
            is_coint INTEGER,
            corr REAL
        )
        """
    )

    rng = np.random.default_rng(7)
    common = np.cumsum(rng.normal(0, 0.01, 300))
    residual = np.zeros(300)
    for index in range(1, 300):
        residual[index] = 0.2 * residual[index - 1] + rng.normal(0, 0.003)
    prices_a = 100 * np.exp(common + residual)
    prices_b = 80 * np.exp(common)
    prices_a[-1] *= 0.95
    prices_b[-1] *= 0.95

    first_day = date(2025, 1, 1)
    rows = []
    for index, (price_a, price_b) in enumerate(zip(prices_a, prices_b)):
        day = (first_day + timedelta(days=index)).isoformat()
        rows.extend([
            ("AAA", day, float(price_a), 1.0, "ru"),
            ("BBB", day, float(price_b), 1.0, "ru"),
        ])
    conn.executemany(
        "INSERT INTO prices (ticker, date, close, volume, market) VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()

    compute_market_pairs("ru", conn)

    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(pairs)").fetchall()
    }
    assert "is_coint_stable" in columns
    assert "market_regime" in columns
    assert "risk_reason" in columns

    row = conn.execute(
        """
        SELECT market_regime, market_volatility, coint_stability,
               signal_eligible, z_forecast_low, z_forecast_high
        FROM pairs
        WHERE market = 'ru' AND ticker_a = 'AAA' AND ticker_b = 'BBB'
        """
    ).fetchone()
    conn.close()

    assert row is not None
    assert row[0] == "stress"
    assert row[1] is not None
    assert row[2] is not None
    assert row[3] in (0, 1)
