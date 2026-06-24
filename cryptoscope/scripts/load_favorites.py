#!/usr/bin/env python3
"""Ensure favorites table exists on existing DB (idempotent, port of load_favorites.R)."""

import sqlite3
import os

DB_PATH = os.environ.get("DB_PATH", "/data/market.db")


def main():
    conn = sqlite3.connect(DB_PATH)
    
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='favorites'")
    if cursor.fetchone():
        print("Favorites table already exists")
        conn.close()
        return
    
    conn.execute("""
        CREATE TABLE IF NOT EXISTS favorites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pair TEXT NOT NULL, ticker_a TEXT NOT NULL, ticker_b TEXT NOT NULL,
            signal TEXT, signal_type TEXT, z_at_entry REAL,
            price_a_entry REAL, price_b_entry REAL,
            entry_time TEXT, exit_time TEXT, exit_price_a REAL,
            exit_price_b REAL, exit_pnl_pct REAL,
            status TEXT DEFAULT 'active', halflife INTEGER, corr REAL,
            user_id TEXT DEFAULT 'local', created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    conn.close()
    print("Favorites table created")


if __name__ == "__main__":
    main()
