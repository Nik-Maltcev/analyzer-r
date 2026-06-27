#!/usr/bin/env python3
"""Load an initial rolling history for liquid Brazil B3 equities."""

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.data.brazil import fetch_brazil_prices, upsert_brazil_prices
from app.data.tickers import BRAZIL_TICKERS


DB_PATH = os.environ.get("DB_PATH", "/data/market.db")
HISTORY_YEARS = int(os.environ.get("BRAZIL_HISTORY_YEARS", "3"))


def main():
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute(
            """
            SELECT COUNT(*) AS rows_count, COUNT(DISTINCT ticker) AS ticker_count
            FROM prices
            WHERE market = 'br'
            """
        ).fetchone()
        rows_count, ticker_count = row if row else (0, 0)
        minimum_coverage = int(len(BRAZIL_TICKERS) * 0.8)

        if rows_count > 0 and ticker_count >= minimum_coverage:
            print(f"[BR] Already loaded: {rows_count} rows, {ticker_count} tickers")
            return

        print(f"[BR] Downloading {HISTORY_YEARS} years for {len(BRAZIL_TICKERS)} B3 tickers...")
        prices = fetch_brazil_prices(history_years=HISTORY_YEARS)
        rows_written = upsert_brazil_prices(conn, prices, BRAZIL_TICKERS)
        tickers_written = prices["ticker"].nunique() if not prices.empty else 0
        print(f"[BR] Loaded {rows_written} rows for {tickers_written} tickers")
    except Exception as exc:
        print(f"[BR] Initial load failed; startup will continue: {exc}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
