#!/usr/bin/env python3
"""Ensure favorites table exists on existing DB (idempotent, port of load_favorites.R)."""

import os
import sqlite3

DB_PATH = os.environ.get("DB_PATH", "/data/market.db")


def main():
    conn = sqlite3.connect(DB_PATH)

    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='favorites'")
    if not cursor.fetchone():
        conn.execute("""
            CREATE TABLE favorites (
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

    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(favorites)").fetchall()
    }
    if "market" not in columns:
        conn.execute(
            "ALTER TABLE favorites ADD COLUMN market TEXT DEFAULT 'crypto'"
        )

    conn.execute(
        """
        UPDATE favorites
        SET market = COALESCE((
            SELECT market
            FROM pairs
            WHERE pairs.ticker_a = favorites.ticker_a
              AND pairs.ticker_b = favorites.ticker_b
            LIMIT 1
        ), market, 'crypto')
        """
    )
    conn.commit()
    conn.close()
    print("Favorites table ready")


if __name__ == "__main__":
    main()
