#!/usr/bin/env python3
"""Compute pair analysis for all markets (port of compute_analysis.R)."""

import sqlite3
import numpy as np
import pandas as pd
import time
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.cointegration import engle_granger, compute_zscore, forecast_zscore
from app.core.signals import (
    compute_pair_score,
    determine_signal,
    determine_strength,
    resolve_signal_started_at,
)
from app.core.scanners import correlation_matrix

DB_PATH = os.environ.get("DB_PATH", "/data/market.db")


def compute_market_pairs(market_name: str, conn: sqlite3.Connection):
    """Compute all pair analysis for one market."""
    print(f"\n{'='*60}")
    print(f"Computing pairs for market: {market_name}")
    print(f"{'='*60}")
    
    df = pd.read_sql_query(
        "SELECT ticker, date, close FROM prices WHERE market = ? ORDER BY ticker, date",
        conn, params=(market_name,)
    )
    
    if df.empty:
        print(f"  No data for market '{market_name}'")
        return
    
    wide = df.pivot(index="date", columns="ticker", values="close")
    tickers = list(wide.columns)
    n = len(tickers)
    print(f"  {n} tickers, {len(wide)} days")
    
    if n < 2:
        return

    pair_columns = {row[1] for row in conn.execute("PRAGMA table_info(pairs)")}
    if "signal_started_at" not in pair_columns:
        conn.execute("ALTER TABLE pairs ADD COLUMN signal_started_at TEXT")

    previous_rows = conn.execute(
        """
        SELECT ticker_a, ticker_b, signal_type, signal_started_at, computed_at
        FROM pairs
        WHERE market = ?
        """,
        (market_name,),
    ).fetchall()
    previous_pairs = {
        (row[0], row[1]): {
            "signal_type": row[2],
            "signal_started_at": row[3],
            "computed_at": row[4],
        }
        for row in previous_rows
    }
    computed_at = time.strftime("%Y-%m-%d %H:%M:%S")
    
    # Log returns and correlation matrix
    log_rets = np.log(wide / wide.shift(1)).values
    corr_mat = correlation_matrix(log_rets)
    
    prices_mat = wide.values
    results = []
    
    for i in range(n):
        for j in range(i + 1, n):
            ta, tb = tickers[i], tickers[j]
            corr_val = float(corr_mat[i, j])
            
            # Engle-Granger test
            cg = engle_granger(prices_mat[:, i], prices_mat[:, j])
            
            # Z-score
            zres = compute_zscore(prices_mat[:, i], prices_mat[:, j], cg["hedge_ratio"])
            z_now_val = zres["z_now"]
            
            # AR(1) forecast
            z_forecast_val = None
            if zres["zscores"] is not None:
                fc = forecast_zscore(zres["zscores"])
                z_forecast_val = fc["z_forecast"]
            
            # Signal
            sig = determine_signal(z_now_val, z_forecast_val, ta, tb)
            strength = determine_strength(cg["is_coint"], z_now_val, z_forecast_val)
            score = compute_pair_score(corr_val, cg["is_coint"], cg["halflife"])
            previous = previous_pairs.get((ta, tb), {})
            signal_started_at = resolve_signal_started_at(
                current_signal_type=sig["signal_type"],
                previous_signal_type=previous.get("signal_type"),
                previous_started_at=previous.get("signal_started_at"),
                previous_computed_at=previous.get("computed_at"),
                now=computed_at,
            )
            
            results.append({
                "market": market_name,
                "ticker_a": ta,
                "ticker_b": tb,
                "corr": round(corr_val, 4),
                "halflife": cg["halflife"],
                "t_stat": round(cg["t_stat"], 4) if cg["t_stat"] is not None else None,
                "is_coint": int(cg["is_coint"]),
                "hedge_ratio": round(cg["hedge_ratio"], 4) if cg["hedge_ratio"] is not None else None,
                "score": round(float(score), 4),
                "z_now": round(z_now_val, 4) if z_now_val is not None else None,
                "z_forecast": round(z_forecast_val, 4) if z_forecast_val is not None else None,
                "signal": sig["signal"],
                "signal_type": sig["signal_type"],
                "strength": strength,
                "signal_started_at": signal_started_at,
                "computed_at": computed_at,
            })
    
    # Write to DB
    conn.execute("DELETE FROM pairs WHERE market = ?", (market_name,))
    
    if results:
        result_df = pd.DataFrame(results)
        result_df.to_sql("pairs", conn, if_exists="append", index=False)
        print(f"  Written {len(results)} pair analyses")
    
    # Log active signals
    active = [r for r in results if r["signal_type"] != "wait"]
    if active:
        today = time.strftime("%Y-%m-%d")
        conn.execute("DELETE FROM signals WHERE date = ?", (today,))
        
        for r in active:
            conn.execute("""
                INSERT INTO signals (date, ticker_a, ticker_b, z_score, z_forecast, signal, strength, is_coint, corr)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (today, r["ticker_a"], r["ticker_b"], r["z_now"], r["z_forecast"],
                  r["signal"], r["strength"], r["is_coint"], r["corr"]))
        
        print(f"  Logged {len(active)} active signals")
    
    conn.commit()


def main():
    conn = sqlite3.connect(DB_PATH)
    
    for market in ["crypto", "stocks", "ru"]:
        try:
            compute_market_pairs(market, conn)
        except Exception as e:
            print(f"Error computing {market}: {e}")
            import traceback
            traceback.print_exc()
    
    conn.close()
    print("\nAnalysis complete.")


if __name__ == "__main__":
    main()
