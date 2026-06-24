#!/usr/bin/env python3
"""Load hourly candle data into existing DB (idempotent, port of load_hourly.R)."""

import sqlite3
import pandas as pd
import os

DB_PATH = os.environ.get("DB_PATH", "/data/market.db")
HOURLY_PATH = os.environ.get("HOURLY_PATH", "/opt/seed/hourly_6coins_2yr.csv")


def main():
    conn = sqlite3.connect(DB_PATH)
    
    # Check if already loaded
    try:
        count = conn.execute("SELECT COUNT(*) FROM hourly_prices").fetchone()[0]
        if count > 0:
            print(f"Hourly data already loaded: {count} rows")
            conn.close()
            return
    except sqlite3.OperationalError:
        count = 0
    
    if not os.path.exists(HOURLY_PATH):
        print(f"Hourly CSV not found: {HOURLY_PATH}")
        conn.close()
        return
    
    print(f"Loading {HOURLY_PATH}...")
    df = pd.read_csv(HOURLY_PATH)
    
    cols = ["ticker", "timestamp", "date", "hour", "open", "high", "low", "close", "volume"]
    df = df[[c for c in cols if c in df.columns]]
    df = df.drop_duplicates(subset=["ticker", "timestamp"])
    df = df.dropna(subset=["ticker", "timestamp", "close"])
    
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
    
    df.to_sql("hourly_prices", conn, if_exists="append", index=False)
    conn.commit()
    
    n = conn.execute("SELECT COUNT(*) FROM hourly_prices").fetchone()[0]
    n_tickers = conn.execute("SELECT COUNT(DISTINCT ticker) FROM hourly_prices").fetchone()[0]
    print(f"Loaded {n} rows, {n_tickers} distinct tickers")
    conn.close()


if __name__ == "__main__":
    main()
