#!/usr/bin/env python3
"""Refresh MOEX prices and recompute Russian equity pairs."""

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from compute_analysis import compute_market_pairs
from daily_update import update_ru_market

DB_PATH = os.environ.get("DB_PATH", "/data/market.db")


def main():
    conn = sqlite3.connect(DB_PATH)
    try:
        rows_written = update_ru_market(conn)
        if rows_written:
            compute_market_pairs("ru", conn)
    except Exception as exc:
        print(f"[RU] Startup refresh failed; startup will continue: {exc}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
