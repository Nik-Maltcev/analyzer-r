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
from app.core.backtest import compute_spread_sd_pct, out_of_sample_backtest
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
from app.data.tickers import ALL_MARKETS
from app.db.schema import PAIR_COLUMN_MIGRATIONS

DB_PATH = os.environ.get("DB_PATH", "/data/market.db")
# Trailing window for the spread mean/sd used by the Z-score. A full-history
# normalization goes stale after a regime shift; 120 days tracks the current
# regime while staying statistically meaningful.
Z_SCORE_WINDOW = 120
# Significance threshold for cointegration tests. Thousands of pairs per
# market at p<=0.05 would yield hundreds of false positives by chance alone.
COINT_MAX_PVALUE = 0.01
ENABLED_MARKETS = {
    market.strip()
    for market in os.environ.get(
        "ENABLED_MARKETS",
        "crypto,stocks,ru,br,id,au",
    ).split(",")
    if market.strip() in ALL_MARKETS
}


def compute_market_pairs(
    market_name: str,
    conn: sqlite3.Connection,
) -> int:
    """Compute all pair analysis for one market."""
    print(f"\n{'='*60}")
    print(f"Computing pairs for market: {market_name}")
    print(f"{'='*60}")

    df = pd.read_sql_query(
        "SELECT ticker, date, close FROM prices WHERE market = ? ORDER BY ticker, date",
        conn, params=(market_name,)
    )

    if df.empty:
        raise RuntimeError(f"No price data for market '{market_name}'")

    wide = df.pivot(index="date", columns="ticker", values="close")
    tickers = list(wide.columns)
    n = len(tickers)
    print(f"  {n} tickers, {len(wide)} days")

    if n < 2:
        raise RuntimeError(
            f"Market '{market_name}' has fewer than two tickers"
        )

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
    # Execution cost assumptions match the calculator defaults: 0.02% taker
    # per leg transaction; perpetual funding 0.01% per 8h for crypto only.
    fee_pct, funding_pct = (0.02, 0.01) if market_name == "crypto" else (0.02, 0.0)
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
            cg = engle_granger(model_pa, model_pb, max_pvalue=COINT_MAX_PVALUE)
            if cg["is_coint"]:
                if market_name in {"crypto", "ru"}:
                    stability = assess_cointegration_stability(
                        pa, pb, max_pvalue=COINT_MAX_PVALUE
                    )
                else:
                    # Daily equities need a medium-term confirmation. Requiring
                    # the 60-day test as well eliminated every equity signal.
                    stability = assess_cointegration_stability(
                        pa,
                        pb,
                        windows=(120, 252),
                        minimum_passed=1,
                        require_recent=False,
                        enforce_ratio_stability=False,
                        max_pvalue=COINT_MAX_PVALUE,
                    )
            else:
                stability = {
                    "is_coint_stable": False,
                    "coint_stability": 0,
                    "coint_windows": '{"model":false}',
                    "coint_stability_reason": (
                        "Коинтеграция не подтверждена на полном окне"
                    ),
                }

            if market_name == "ru":
                event_gap = detect_recent_event_gap(
                    pa,
                    pb,
                    market_returns=market_risk["market_returns"],
                    ticker_a=ta,
                    ticker_b=tb,
                )
            else:
                event_gap = {
                    "event_risk": False,
                    "event_risk_reason": None,
                }

            # Z-score normalized on the trailing window (current regime).
            # The hedge ratio is also refit on that window: the long-window
            # test above validates the relationship, while the recent beta
            # keeps the spread anchored to how the pair trades now.
            z_hedge = cg["hedge_ratio"]
            if len(model_pa) >= 60:
                recent_fit = engle_granger(
                    model_pa[-Z_SCORE_WINDOW:], model_pb[-Z_SCORE_WINDOW:], min_obs=60
                )
                if recent_fit["hedge_ratio"] is not None:
                    z_hedge = recent_fit["hedge_ratio"]
            zres = compute_zscore(
                model_pa, model_pb, z_hedge, window=Z_SCORE_WINDOW
            )
            z_now_val = zres["z_now"]
            z_prev_val = zres["z_prev"]
            spread_sd_pct = (
                compute_spread_sd_pct(
                    model_pa, model_pb, z_hedge, window=Z_SCORE_WINDOW
                )
                if z_hedge is not None
                else None
            )
            backtest = (
                out_of_sample_backtest(
                    pa,
                    pb,
                    taker_fee_pct=fee_pct,
                    funding_rate_8h_pct=funding_pct,
                )
                if stability["is_coint_stable"]
                else {"n_trades": 0, "validated": False}
            )
            backtest_validated = bool(
                backtest.get("validated") and backtest.get("model_is_coint")
            )

            # AR(1) forecast fitted on the same regime window as the Z-score
            z_forecast_val = None
            forecast_resid_sd = None
            if zres["zscores"] is not None:
                fc = forecast_zscore(zres["zscores"][-Z_SCORE_WINDOW:])
                z_forecast_val = fc["z_forecast"]
                forecast_resid_sd = fc["resid_sd"]
            scenario = forecast_scenario(
                z_forecast_val,
                forecast_resid_sd,
                market_risk["market_regime"],
            )

            # Signal
            sig = determine_signal(
                z_now_val,
                z_forecast_val,
                ta,
                tb,
                hedge_ratio=z_hedge,
                z_prev=z_prev_val,
            )
            coint_for_strength = stability["is_coint_stable"]
            strength = determine_strength(
                coint_for_strength, z_now_val, z_forecast_val, sig.get("z_turning")
            )
            guarded = guard_signal(
                market_name,
                sig,
                strength,
                stability,
                event_gap,
                market_risk["market_regime"],
            )
            score = compute_pair_score(
                corr_val,
                coint_for_strength,
                cg["halflife"],
                coint_stability=stability["coint_stability"],
                backtest_win_rate=backtest.get("win_rate"),
                backtest_validated=backtest_validated,
            )
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
                and not stability["is_coint_stable"]
            ):
                risk_reason = stability["coint_stability_reason"]
            if (
                risk_reason is None
                and market_risk["market_regime"] != "normal"
            ):
                risk_reason = market_risk["market_regime_reason"]
            if (
                risk_reason is None
                and guarded["signal_type"] != "wait"
                and sig.get("z_turning") is False
            ):
                risk_reason = "Спред ещё расширяется — разворот Z не подтверждён"

            results.append({
                "market": market_name,
                "ticker_a": ta,
                "ticker_b": tb,
                "corr": round(corr_val, 4),
                "halflife": cg["halflife"],
                "t_stat": round(cg["t_stat"], 4) if cg["t_stat"] is not None else None,
                "coint_pvalue": round(cg["p_value"], 6) if cg["p_value"] is not None else None,
                "coint_critical_5pct": round(cg["critical_5pct"], 4) if cg["critical_5pct"] is not None else None,
                "is_coint": int(cg["is_coint"]),
                "hedge_ratio": round(z_hedge, 4) if z_hedge is not None else None,
                "ar_phi": round(cg["ar_phi"], 6) if cg["ar_phi"] is not None else None,
                "spread_sd_pct": round(spread_sd_pct, 8) if spread_sd_pct is not None else None,
                "backtest_trades": int(backtest.get("n_trades", 0)),
                "backtest_win_rate": backtest.get("win_rate"),
                "backtest_avg_pnl_pct": backtest.get("avg_pnl_pct"),
                "backtest_avg_net_pnl_pct": backtest.get("avg_net_pnl_pct"),
                "backtest_avg_pnl_sigma": backtest.get("avg_pnl_sigma"),
                "backtest_avg_hold_days": backtest.get("avg_hold"),
                "backtest_validated": int(backtest_validated),
                "score": round(float(score), 4),
                "z_now": round(z_now_val, 4) if z_now_val is not None else None,
                "z_prev": round(z_prev_val, 4) if z_prev_val is not None else None,
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

    if not results:
        raise RuntimeError(
            f"Analysis produced no pairs for market '{market_name}'"
        )

    # Replace a market only after a complete, non-empty calculation.
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
    return len(results)


def main() -> int:
    conn = sqlite3.connect(DB_PATH)
    attempted = 0
    failed: list[str] = []

    for market in ALL_MARKETS:
        if market not in ENABLED_MARKETS:
            print(f"Skipping disabled market: {market}")
            continue
        attempted += 1
        try:
            compute_market_pairs(market, conn)
        except Exception as e:
            conn.rollback()
            print(f"Error computing {market}: {e}")
            import traceback
            traceback.print_exc()
            failed.append(market)

    conn.close()
    if attempted == 0:
        print("Analysis failed: no enabled markets were selected")
        return 1
    if failed:
        print(
            "Analysis failed for required markets: "
            + ", ".join(sorted(failed))
        )
        return 1
    print("\nAnalysis complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
