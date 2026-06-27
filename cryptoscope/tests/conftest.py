import os
import sqlite3
import tempfile
from typing import Optional

import numpy as np
import pandas as pd
import pytest


@pytest.fixture
def sample_prices():
    """Generate synthetic cointegrated price series."""
    np.random.seed(42)
    n = 500

    # Random walk (common trend)
    rw = np.cumsum(np.random.randn(n) * 0.01)

    # Asset A: random walk + small noise
    pa = 100 * np.exp(rw + np.random.randn(n) * 0.005)

    # Asset B: 0.5 * random_walk + larger noise (cointegrated with A)
    pb = 50 * np.exp(0.5 * rw + np.random.randn(n) * 0.008)

    return pa, pb


@pytest.fixture
def non_cointegrated_prices():
    """Generate non-cointegrated price series."""
    np.random.seed(123)
    n = 500
    pa = 100 * np.exp(np.cumsum(np.random.randn(n) * 0.01))
    pb = 80 * np.exp(np.cumsum(np.random.randn(n) * 0.012))
    return pa, pb


@pytest.fixture
def sample_zscore_series():
    """Generate a synthetic Z-score series with mean-reverting behavior."""
    np.random.seed(42)
    n = 200
    phi = 0.95
    z = np.zeros(n)
    z[0] = 1.5
    for i in range(1, n):
        z[i] = phi * z[i-1] + np.random.randn() * 0.1
    z[-1] = 2.5  # End at a signal level
    return z


@pytest.fixture
def temp_db():
    """Create a temporary SQLite database for testing."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    conn = sqlite3.connect(path)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS prices (
            ticker TEXT NOT NULL, date TEXT NOT NULL, close REAL NOT NULL,
            volume REAL, market TEXT, PRIMARY KEY (ticker, date)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS pairs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market TEXT NOT NULL, ticker_a TEXT NOT NULL, ticker_b TEXT NOT NULL,
            corr REAL, halflife INTEGER, t_stat REAL, is_coint INTEGER,
            hedge_ratio REAL, score REAL, z_now REAL, z_forecast REAL,
            signal TEXT, signal_type TEXT, strength TEXT,
            signal_started_at TEXT,
            computed_at TEXT, UNIQUE (market, ticker_a, ticker_b)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS favorites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pair TEXT NOT NULL, market TEXT DEFAULT 'crypto',
            ticker_a TEXT NOT NULL, ticker_b TEXT NOT NULL,
            signal TEXT, signal_type TEXT, z_at_entry REAL,
            price_a_entry REAL, price_b_entry REAL,
            entry_time TEXT, exit_time TEXT, exit_price_a REAL,
            exit_price_b REAL, exit_pnl_pct REAL,
            status TEXT DEFAULT 'active', halflife INTEGER, corr REAL,
            user_id TEXT DEFAULT 'local', created_at TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS update_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT DEFAULT (datetime('now')),
            market TEXT,
            tickers_ok INTEGER,
            tickers_fail INTEGER,
            rows_added INTEGER,
            status TEXT,
            message TEXT
        )
    """)

    # Seed some test data
    dates = [f'2024-01-{d:02d}' for d in range(1, 31)]
    for i, date in enumerate(dates):
        conn.execute(
            "INSERT OR IGNORE INTO prices (ticker, date, close, market) VALUES (?, ?, ?, ?)",
            ("BTC/USD", date, 40000 + i * 200 + np.random.randn() * 500, "crypto")
        )
        conn.execute(
            "INSERT OR IGNORE INTO prices (ticker, date, close, market) VALUES (?, ?, ?, ?)",
            ("ETH/USD", date, 2000 + i * 10 + np.random.randn() * 30, "crypto")
        )

    conn.execute("""
        INSERT OR IGNORE INTO pairs (market, ticker_a, ticker_b, corr, halflife, t_stat, is_coint, score, z_now, signal, signal_type, strength)
        VALUES ('crypto', 'BTC/USD', 'ETH/USD', 0.85, 30, -3.5, 1, 1.15, 2.3, 'Шорт BTC / Лонг ETH', 'short_a', 'Сильный')
    """)

    conn.commit()

    yield path
    conn.close()
    os.unlink(path)
