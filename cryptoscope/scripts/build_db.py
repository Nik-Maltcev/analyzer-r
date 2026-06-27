#!/usr/bin/env python3
"""Build SQLite database from seed CSVs (port of build_db.R)."""

import os
import sqlite3
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


CSV_PATH = os.environ.get("CSV_PATH", "/opt/seed/all_markets_3yr.csv")
RU_CSV_PATH = os.environ.get("RU_CSV_PATH", "/opt/seed/tinkoff_ru_2yr.csv")
HOURLY_PATH = os.environ.get("HOURLY_PATH", "/opt/seed/hourly_6coins_2yr.csv")
DB_PATH = os.environ.get("DB_PATH", "/data/market.db")


def main():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS prices (
            ticker TEXT NOT NULL,
            date   TEXT NOT NULL,
            close  REAL NOT NULL,
            volume REAL,
            market TEXT,
            PRIMARY KEY (ticker, date)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_prices_ticker ON prices(ticker)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_prices_date ON prices(date)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS pairs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market TEXT NOT NULL, ticker_a TEXT NOT NULL, ticker_b TEXT NOT NULL,
            corr REAL, halflife INTEGER, t_stat REAL, is_coint INTEGER,
            hedge_ratio REAL, score REAL, z_now REAL, z_forecast REAL,
            signal TEXT, signal_type TEXT, strength TEXT,
            signal_started_at TEXT,
            computed_at TEXT DEFAULT (datetime('now')),
            UNIQUE (market, ticker_a, ticker_b)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pairs_market ON pairs(market)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pairs_score ON pairs(score DESC)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT, ticker_a TEXT, ticker_b TEXT,
            z_score REAL, z_forecast REAL, signal TEXT,
            strength TEXT, is_coint INTEGER, corr REAL,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS update_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT DEFAULT (datetime('now')),
            market TEXT, tickers_ok INTEGER, tickers_fail INTEGER,
            rows_added INTEGER, status TEXT, message TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS hourly_prices (
            ticker TEXT NOT NULL, timestamp TEXT NOT NULL,
            date TEXT NOT NULL, hour INTEGER NOT NULL,
            open REAL, high REAL, low REAL, close REAL NOT NULL, volume REAL,
            PRIMARY KEY (ticker, timestamp)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_hourly_ticker ON hourly_prices(ticker)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_hourly_hour ON hourly_prices(hour)")

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
            user_id TEXT DEFAULT 'local', created_at TEXT DEFAULT (datetime('now'))
        )
    """)

    # Load main CSV
    if os.path.exists(CSV_PATH):
        print(f"Loading {CSV_PATH}...")
        df = pd.read_csv(CSV_PATH)
        if "market" not in df.columns:
            df["market"] = "unknown"
        df = df.drop_duplicates(subset=["ticker", "date"])
        df.to_sql("prices", conn, if_exists="append", index=False)
        print(f"  Inserted {len(df)} rows")
    else:
        print(f"CSV not found: {CSV_PATH}")

    # Load RU CSV
    if os.path.exists(RU_CSV_PATH):
        print(f"Loading {RU_CSV_PATH}...")
        df = pd.read_csv(RU_CSV_PATH)
        if "market" not in df.columns:
            df["market"] = "ru"
        df = df.drop_duplicates(subset=["ticker", "date"])
        df = df[["ticker", "date", "close", "volume", "market"]]
        df = df[(df["close"].notna()) & (df["close"] > 0)]
        df.to_sql("prices", conn, if_exists="append", index=False)
        print(f"  Inserted {len(df)} RU rows")

    # Load hourly CSV
    if os.path.exists(HOURLY_PATH):
        print(f"Loading {HOURLY_PATH}...")
        df = pd.read_csv(HOURLY_PATH)
        df = df.drop_duplicates(subset=["ticker", "timestamp"])
        cols = ["ticker", "timestamp", "date", "hour", "open", "high", "low", "close", "volume"]
        df = df[[c for c in cols if c in df.columns]]
        df.to_sql("hourly_prices", conn, if_exists="append", index=False)
        print(f"  Inserted {len(df)} hourly rows")

    # Log
    conn.execute("""
        INSERT INTO update_log (market, tickers_ok, rows_added, status, message)
        VALUES (?, ?, ?, ?, ?)
    """, ("all", conn.execute("SELECT COUNT(DISTINCT ticker) FROM prices").fetchone()[0],
          conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0],
          "ok", "Database built from seed CSVs"))

    conn.commit()

    n_tickers = conn.execute("SELECT COUNT(DISTINCT ticker) FROM prices").fetchone()[0]
    n_rows = conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
    print(f"\nDatabase built: {n_tickers} tickers, {n_rows} rows")
    conn.close()


if __name__ == "__main__":
    main()
