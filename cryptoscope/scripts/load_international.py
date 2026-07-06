#!/usr/bin/env python3
"""Load initial histories for administrator-only Yahoo equity markets."""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.data.international import (
    INTERNATIONAL_MARKETS,
    fetch_international_prices,
    upsert_international_prices,
)


DB_PATH = os.environ.get("DB_PATH", "/data/market.db")
HISTORY_YEARS = int(os.environ.get("INTERNATIONAL_HISTORY_YEARS", "3"))
SCRIPT_DIR = Path(__file__).resolve().parent
ENABLED_MARKETS = {
    market.strip()
    for market in os.environ.get("ENABLED_MARKETS", "").split(",")
    if market.strip()
}


def main():
    enabled = [
        config
        for market, config in INTERNATIONAL_MARKETS.items()
        if market in ENABLED_MARKETS
    ]
    if not enabled:
        return

    conn = sqlite3.connect(DB_PATH)
    needs_analysis = False
    try:
        for config in enabled:
            try:
                row = conn.execute(
                    """
                    SELECT COUNT(*) AS rows_count,
                           COUNT(DISTINCT ticker) AS ticker_count
                    FROM prices
                    WHERE market = ?
                    """,
                    (config.market,),
                ).fetchone()
                rows_count, ticker_count = row if row else (0, 0)
                minimum_coverage = int(len(config.tickers) * 0.8)

                if rows_count > 0 and ticker_count >= minimum_coverage:
                    print(
                        f"[{config.label}] Already loaded: "
                        f"{rows_count} rows, {ticker_count} tickers"
                    )
                else:
                    print(
                        f"[{config.label}] Downloading {HISTORY_YEARS} years "
                        f"for {len(config.tickers)} tickers..."
                    )
                    prices = fetch_international_prices(
                        config.market,
                        history_years=HISTORY_YEARS,
                    )
                    rows_written = upsert_international_prices(
                        conn,
                        config.market,
                        prices,
                    )
                    tickers_written = (
                        prices["ticker"].nunique()
                        if not prices.empty
                        else 0
                    )
                    print(
                        f"[{config.label}] Loaded {rows_written} rows "
                        f"for {tickers_written} tickers"
                    )

                pair_count = conn.execute(
                    "SELECT COUNT(*) FROM pairs WHERE market = ?",
                    (config.market,),
                ).fetchone()[0]
                price_count = conn.execute(
                    "SELECT COUNT(*) FROM prices WHERE market = ?",
                    (config.market,),
                ).fetchone()[0]
                needs_analysis = (
                    needs_analysis
                    or (price_count > 0 and pair_count < 1)
                )
            except Exception as exc:
                print(
                    f"[{config.label}] Initial load failed; "
                    f"startup will continue: {exc}"
                )
    finally:
        conn.close()

    if needs_analysis:
        result = subprocess.run(
            [sys.executable, str(SCRIPT_DIR / "compute_analysis.py")],
            check=False,
        )
        if result.returncode != 0:
            print(
                "Initial international analysis failed with "
                f"exit code {result.returncode}"
            )


if __name__ == "__main__":
    main()
