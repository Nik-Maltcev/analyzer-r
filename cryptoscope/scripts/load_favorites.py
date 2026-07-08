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
                position_kind TEXT DEFAULT 'pair',
                source TEXT DEFAULT 'signal',
                ticker_a TEXT NOT NULL, ticker_b TEXT NOT NULL,
                signal TEXT, signal_type TEXT, z_at_entry REAL,
                hedge_ratio_entry REAL, spread_mean_entry REAL,
                spread_sd_entry REAL,
                price_a_entry REAL, price_b_entry REAL,
                entry_time TEXT, exit_time TEXT, exit_price_a REAL,
                exit_price_b REAL, exit_pnl_pct REAL,
                exit_net_pnl REAL, exit_net_return_pct REAL,
                exit_pair_move_pct REAL, exit_total_cost REAL,
                close_capital REAL,
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
    for column in (
        "hedge_ratio_entry",
        "spread_mean_entry",
        "spread_sd_entry",
        "exit_net_pnl",
        "exit_net_return_pct",
        "exit_pair_move_pct",
        "exit_total_cost",
        "close_capital",
    ):
        if column not in columns:
            conn.execute(
                f"ALTER TABLE favorites ADD COLUMN {column} REAL"
            )
    if "position_kind" not in columns:
        conn.execute("ALTER TABLE favorites ADD COLUMN position_kind TEXT DEFAULT 'pair'")
    if "source" not in columns:
        conn.execute("ALTER TABLE favorites ADD COLUMN source TEXT DEFAULT 'signal'")

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
