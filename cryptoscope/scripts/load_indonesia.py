#!/usr/bin/env python3
"""Load an initial rolling history for liquid Indonesia IDX equities."""

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.data.indonesia import fetch_indonesia_prices, upsert_indonesia_prices
from app.data.tickers import INDONESIA_TICKERS

DB_PATH = os.environ.get("DB_PATH", "/data/market.db")
HISTORY_YEARS = int(os.environ.get("INDONESIA_HISTORY_YEARS", "3"))


def main():
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute(
            """
            SELECT COUNT(*) AS rows_count, COUNT(DISTINCT ticker) AS ticker_count
            FROM prices
            WHERE market = 'id'
            """
        ).fetchone()
        rows_count, ticker_count = row if row else (0, 0)
        minimum_coverage = int(len(INDONESIA_TICKERS) * 0.8)

        if rows_count > 0 and ticker_count >= minimum_coverage:
            print(f"[ID] Already loaded: {rows_count} rows, {ticker_count} tickers")
            return

        print(
            f"[ID] Downloading {HISTORY_YEARS} years "
            f"for {len(INDONESIA_TICKERS)} IDX tickers..."
        )
        prices = fetch_indonesia_prices(history_years=HISTORY_YEARS)
        rows_written = upsert_indonesia_prices(conn, prices, INDONESIA_TICKERS)
        tickers_written = prices["ticker"].nunique() if not prices.empty else 0
        print(f"[ID] Loaded {rows_written} rows for {tickers_written} tickers")
    except Exception as exc:
        print(f"[ID] Initial load failed; startup will continue: {exc}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
