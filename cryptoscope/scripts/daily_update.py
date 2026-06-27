#!/usr/bin/env python3
"""Daily market price refresh and pair-analysis recomputation."""

import asyncio
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.data.brazil import fetch_brazil_prices, upsert_brazil_prices
from app.data.fetcher import fetch_batch
from app.data.tickers import BRAZIL_TICKERS, CRYPTO_TICKERS, STOCK_TICKERS

DB_PATH = os.environ.get("DB_PATH", "/data/market.db")
API_KEY = os.environ.get("TWELVEDATA_API_KEY", "")
BRAZIL_HISTORY_YEARS = int(os.environ.get("BRAZIL_HISTORY_YEARS", "3"))
SCRIPT_DIR = Path(__file__).resolve().parent


def update_market(tickers: list, market_name: str, conn: sqlite3.Connection, api_key: str) -> int:
    """Update one market's prices from Twelve Data."""
    if not api_key:
        print(f"  No API key, skipping {market_name}")
        return 0
    
    rows_added = 0
    tickers_ok = 0
    tickers_fail = 0
    
    batches = [tickers[i:i + 8] for i in range(0, len(tickers), 8)]
    for idx, batch in enumerate(batches):
        try:
            df = asyncio.run(fetch_batch(batch, api_key, outputsize=5))
            
            if df.empty:
                tickers_fail += len(batch)
                continue
            
            for ticker in df["ticker"].unique():
                sub = df[df["ticker"] == ticker]
                
                # Check existing dates
                existing = pd.read_sql_query(
                    "SELECT date FROM prices WHERE ticker = ? ORDER BY date DESC LIMIT 10",
                    conn, params=(ticker,)
                )
                existing_dates = set(existing["date"].values) if not existing.empty else set()
                
                # Insert new rows
                new_rows = sub[~sub["date"].isin(existing_dates) & sub["close"].notna() & (sub["close"] > 0)]
                if not new_rows.empty:
                    new_rows = new_rows.copy()
                    new_rows["market"] = market_name
                    new_rows["volume"] = new_rows.get("volume", 0)
                    new_rows[["ticker", "date", "close", "volume", "market"]].to_sql(
                        "prices", conn, if_exists="append", index=False
                    )
                    rows_added += len(new_rows)
                
                tickers_ok += 1
            
            if idx < len(batches) - 1:
                time.sleep(75)
            
        except Exception as e:
            print(f"  Error batch {batch}: {e}")
            tickers_fail += len(batch)
            time.sleep(10)
    
    # Log to update_log
    conn.execute("""
        INSERT INTO update_log (market, tickers_ok, tickers_fail, rows_added, status, message)
        VALUES (?, ?, ?, ?, 'ok', 'Daily update complete')
    """, (market_name, tickers_ok, tickers_fail, rows_added))
    conn.commit()
    
    print(f"  [{market_name}] OK={tickers_ok} FAIL={tickers_fail} ROWS={rows_added}")
    return rows_added


def update_brazil_market(conn: sqlite3.Connection) -> int:
    """Refresh a rolling adjusted history for Brazil B3 equities."""
    try:
        prices = fetch_brazil_prices(history_years=BRAZIL_HISTORY_YEARS)
        rows_written = upsert_brazil_prices(conn, prices, BRAZIL_TICKERS)
        tickers_ok = prices["ticker"].nunique() if not prices.empty else 0
        status = "ok" if rows_written else "error"
        message = "Brazil adjusted history refreshed" if rows_written else "No Brazil data returned"
    except Exception as exc:
        rows_written = 0
        tickers_ok = 0
        status = "error"
        message = f"Brazil update failed: {exc}"

    conn.execute(
        """
        INSERT INTO update_log (market, tickers_ok, tickers_fail, rows_added, status, message)
        VALUES ('br', ?, ?, ?, ?, ?)
        """,
        (
            tickers_ok,
            max(0, len(BRAZIL_TICKERS) - tickers_ok),
            rows_written,
            status,
            message,
        ),
    )
    conn.commit()
    print(
        f"  [br] OK={tickers_ok} "
        f"FAIL={max(0, len(BRAZIL_TICKERS) - tickers_ok)} ROWS={rows_written}"
    )
    return rows_written


def main():
    conn = sqlite3.connect(DB_PATH)
    
    total = 0
    if API_KEY:
        total += update_market(CRYPTO_TICKERS, "crypto", conn, API_KEY)
        total += update_market(STOCK_TICKERS, "stocks", conn, API_KEY)
    else:
        print("TWELVEDATA_API_KEY not set, skipping crypto and stocks")
    total += update_brazil_market(conn)
    
    conn.close()
    
    print(f"\nTotal inserted or refreshed rows: {total}")
    
    # Recompute analysis
    if total > 0:
        print("Recomputing pair analysis...")
        compute_script = os.environ.get("COMPUTE_ANALYSIS_PATH") or str(SCRIPT_DIR / "compute_analysis.py")
        result = subprocess.run([sys.executable, compute_script], check=False)
        if result.returncode != 0:
            print(f"compute_analysis failed with exit code {result.returncode}")


if __name__ == "__main__":
    main()
