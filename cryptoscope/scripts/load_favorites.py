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
                capital_at_entry REAL, leverage_at_entry REAL,
                taker_fee_pct_at_entry REAL,
                funding_rate_pct_at_entry REAL,
                calculation_version TEXT,
                exit_spread_move_pp REAL,
                exit_unlevered_return_pct REAL,
                exit_gross_pnl REAL, exit_gross_return_pct REAL,
                exit_hold_days REAL, exit_leverage REAL,
                exit_taker_fee_pct REAL,
                exit_funding_rate_pct REAL,
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
        "capital_at_entry",
        "leverage_at_entry",
        "taker_fee_pct_at_entry",
        "funding_rate_pct_at_entry",
        "exit_spread_move_pp",
        "exit_unlevered_return_pct",
        "exit_gross_pnl",
        "exit_gross_return_pct",
        "exit_hold_days",
        "exit_leverage",
        "exit_taker_fee_pct",
        "exit_funding_rate_pct",
    ):
        if column not in columns:
            conn.execute(
                f"ALTER TABLE favorites ADD COLUMN {column} REAL"
            )
    if "calculation_version" not in columns:
        conn.execute("ALTER TABLE favorites ADD COLUMN calculation_version TEXT")
    if "position_kind" not in columns:
        conn.execute("ALTER TABLE favorites ADD COLUMN position_kind TEXT DEFAULT 'pair'")
    if "source" not in columns:
        conn.execute("ALTER TABLE favorites ADD COLUMN source TEXT DEFAULT 'signal'")

    conn.execute(
        """
        UPDATE favorites
        SET position_kind = CASE
                WHEN TRIM(COALESCE(ticker_b, '')) = '' THEN 'single'
                ELSE COALESCE(position_kind, 'pair')
            END,
            source = COALESCE(source, 'signal'),
            capital_at_entry = COALESCE(
                capital_at_entry,
                close_capital,
                1000.0
            ),
            leverage_at_entry = COALESCE(leverage_at_entry, 1.0),
            taker_fee_pct_at_entry = COALESCE(
                taker_fee_pct_at_entry,
                0.02
            ),
            funding_rate_pct_at_entry = COALESCE(
                funding_rate_pct_at_entry,
                CASE
                    WHEN COALESCE(market, 'crypto') = 'crypto' THEN 0.01
                    ELSE 0.0
                END
            ),
            calculation_version = COALESCE(
                calculation_version,
                'legacy-estimated-v1'
            )
        """
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
