#!/usr/bin/env python3
"""Daily price update from Twelve Data API + recompute analysis (port of daily_update.R)."""

import asyncio
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.data.tickers import CRYPTO_TICKERS, STOCK_TICKERS
from app.data.fetcher import fetch_batch

DB_PATH = os.environ.get("DB_PATH", "/data/market.db")
API_KEY = os.environ.get("TWELVEDATA_API_KEY", "")
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


def main():
    if not API_KEY:
        print("TWELVEDATA_API_KEY not set, skipping update")
        return
    
    conn = sqlite3.connect(DB_PATH)
    
    total = 0
    total += update_market(CRYPTO_TICKERS, "crypto", conn, API_KEY)
    total += update_market(STOCK_TICKERS, "stocks", conn, API_KEY)
    
    conn.close()
    
    print(f"\nTotal new rows: {total}")
    
    # Recompute analysis
    if total > 0:
        print("Recomputing pair analysis...")
        compute_script = os.environ.get("COMPUTE_ANALYSIS_PATH") or str(SCRIPT_DIR / "compute_analysis.py")
        result = subprocess.run([sys.executable, compute_script], check=False)
        if result.returncode != 0:
            print(f"compute_analysis failed with exit code {result.returncode}")


if __name__ == "__main__":
    main()
