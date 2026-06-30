#!/usr/bin/env python3
"""Compute pair analysis for all markets (port of compute_analysis.R)."""

import os
import sqlite3
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.cointegration import compute_zscore, engle_granger, forecast_zscore
from app.core.risk import (
    assess_cointegration_stability,
    assess_market_regime,
    detect_recent_event_gap,
    forecast_scenario,
    guard_signal,
)
from app.core.signals import (
    compute_pair_score,
    correlation_matrix,
    determine_signal,
    determine_strength,
    resolve_signal_started_at,
)
from app.db.schema import PAIR_COLUMN_MIGRATIONS

DB_PATH = os.environ.get("DB_PATH", "/data/market.db")
ENABLED_MARKETS = {
    market.strip()
    for market in os.environ.get(
        "ENABLED_MARKETS",
        "crypto,stocks,ru,br,id",
    ).split(",")
    if market.strip()
}


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
    for column, definition in PAIR_COLUMN_MIGRATIONS.items():
        if column not in pair_columns:
            conn.execute(f"ALTER TABLE pairs ADD COLUMN {column} {definition}")

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
    market_risk = assess_market_regime(prices_mat)
    results = []

    for i in range(n):
        for j in range(i + 1, n):
            ta, tb = tickers[i], tickers[j]
            corr_val = float(corr_mat[i, j])

            pa = prices_mat[:, i]
            pb = prices_mat[:, j]

            # A one-year model reacts faster to regime changes on MOEX.
            model_pa = pa[-252:] if market_name == "ru" else pa
            model_pb = pb[-252:] if market_name == "ru" else pb
            cg = engle_granger(model_pa, model_pb)
            if market_name == "ru":
                stability = assess_cointegration_stability(pa, pb)
                event_gap = detect_recent_event_gap(
                    pa,
                    pb,
                    market_returns=market_risk["market_returns"],
                    ticker_a=ta,
                    ticker_b=tb,
                )
            else:
                is_coint = bool(cg["is_coint"])
                stability = {
                    "is_coint_stable": is_coint,
                    "coint_stability": 100 if is_coint else 0,
                    "coint_windows": (
                        '{"model":true}' if is_coint else '{"model":false}'
                    ),
                    "coint_stability_reason": (
                        "Коинтеграция подтверждена моделью"
                        if is_coint
                        else "Коинтеграция не подтверждена моделью"
                    ),
                }
                event_gap = {
                    "event_risk": False,
                    "event_risk_reason": None,
                }

            # Z-score
            zres = compute_zscore(model_pa, model_pb, cg["hedge_ratio"])
            z_now_val = zres["z_now"]

            # AR(1) forecast
            z_forecast_val = None
            forecast_resid_sd = None
            if zres["zscores"] is not None:
                fc = forecast_zscore(zres["zscores"])
                z_forecast_val = fc["z_forecast"]
                forecast_resid_sd = fc["resid_sd"]
            scenario = forecast_scenario(
                z_forecast_val,
                forecast_resid_sd,
                market_risk["market_regime"],
            )

            # Signal
            sig = determine_signal(z_now_val, z_forecast_val, ta, tb)
            coint_for_strength = (
                stability["is_coint_stable"]
                if market_name == "ru"
                else cg["is_coint"]
            )
            strength = determine_strength(coint_for_strength, z_now_val, z_forecast_val)
            guarded = guard_signal(
                market_name,
                sig,
                strength,
                stability,
                event_gap,
                market_risk["market_regime"],
            )
            score = compute_pair_score(corr_val, coint_for_strength, cg["halflife"])
            previous = previous_pairs.get((ta, tb), {})
            signal_started_at = resolve_signal_started_at(
                current_signal_type=guarded["signal_type"],
                previous_signal_type=previous.get("signal_type"),
                previous_started_at=previous.get("signal_started_at"),
                previous_computed_at=previous.get("computed_at"),
                now=computed_at,
            )
            risk_reason = guarded["risk_reason"]
            if risk_reason is None and event_gap["event_risk"]:
                risk_reason = event_gap["event_risk_reason"]
            if (
                risk_reason is None
                and market_name == "ru"
                and not stability["is_coint_stable"]
            ):
                risk_reason = stability["coint_stability_reason"]
            if (
                risk_reason is None
                and market_risk["market_regime"] != "normal"
            ):
                risk_reason = market_risk["market_regime_reason"]

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
                **scenario,
                "signal": guarded["signal"],
                "signal_type": guarded["signal_type"],
                "strength": guarded["strength"],
                "signal_eligible": int(guarded["signal_eligible"]),
                "is_coint_stable": int(stability["is_coint_stable"]),
                "coint_stability": stability["coint_stability"],
                "coint_windows": stability["coint_windows"],
                "market_regime": market_risk["market_regime"],
                "market_volatility": market_risk["market_volatility"],
                "event_risk": int(event_gap["event_risk"]),
                "risk_reason": risk_reason,
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

    for market in ["crypto", "stocks", "ru", "br", "id"]:
        if market not in ENABLED_MARKETS:
            print(f"Skipping disabled market: {market}")
            continue
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
