#!/usr/bin/env python3
"""Load Russian stock data into existing DB (idempotent, port of load_ru.R)."""

import sqlite3
import pandas as pd
import os

DB_PATH = os.environ.get("DB_PATH", "/data/market.db")
RU_CSV_PATH = os.environ.get("RU_CSV_PATH", "/opt/seed/tinkoff_ru_2yr.csv")


def main():
    conn = sqlite3.connect(DB_PATH)
    
    # Check if already loaded
    try:
        count = conn.execute("SELECT COUNT(*) FROM prices WHERE market = 'ru'").fetchone()[0]
        if count > 0:
            print(f"RU data already loaded: {count} rows")
            conn.close()
            return
    except sqlite3.OperationalError:
        # Prices table might not exist yet
        conn.execute("""
            CREATE TABLE IF NOT EXISTS prices (
                ticker TEXT NOT NULL, date TEXT NOT NULL, close REAL NOT NULL,
                volume REAL, market TEXT, PRIMARY KEY (ticker, date)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_prices_ticker ON prices(ticker)")
        count = 0
    
    if not os.path.exists(RU_CSV_PATH):
        print(f"RU CSV not found: {RU_CSV_PATH}")
        conn.close()
        return
    
    print(f"Loading {RU_CSV_PATH}...")
    df = pd.read_csv(RU_CSV_PATH)
    
    if "market" not in df.columns:
        df["market"] = "ru"
    
    df = df[["ticker", "date", "close", "volume", "market"]]
    df = df.drop_duplicates(subset=["ticker", "date"])
    df = df[(df["close"].notna()) & (df["close"] > 0)]
    
    df.to_sql("prices", conn, if_exists="append", index=False)
    conn.commit()
    
    n = conn.execute("SELECT COUNT(*) FROM prices WHERE market = 'ru'").fetchone()[0]
    n_tickers = conn.execute("SELECT COUNT(DISTINCT ticker) FROM prices WHERE market = 'ru'").fetchone()[0]
    print(f"Loaded {n} rows, {n_tickers} distinct RU tickers")
    conn.close()


if __name__ == "__main__":
    main()
