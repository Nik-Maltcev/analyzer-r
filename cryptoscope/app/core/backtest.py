"""Backtest engine for pair trading strategies."""

from typing import Any, Dict

import numpy as np
import pandas as pd

from app.core.cointegration import engle_granger

MIN_VALIDATED_TRADES = 5


def run_backtest(zscores: np.ndarray, entry_threshold: float = 2.0,
                 exit_threshold: float = 0.5, stop_threshold: float = 3.5) -> pd.DataFrame:
    """
    Simulate pair trading on Z-score series.
    
    Entry: |Z| >= entry_threshold
    Exit:  |Z| <= exit_threshold  OR  |Z| >= stop_threshold (stop loss)
    
    Returns DataFrame with trades: entry_idx, exit_idx, entry_z, exit_z, pnl_sigma, days
    """
    trades = []
    in_position = False
    entry_idx = None
    entry_z = None
    position_type = None  # 'long' (buy spread) or 'short' (sell spread)
    
    for i in range(len(zscores)):
        z = zscores[i]
        if np.isnan(z):
            continue
        
        if not in_position:
            if z >= entry_threshold:
                in_position = True
                entry_idx = i
                entry_z = z
                position_type = 'short'
            elif z <= -entry_threshold:
                in_position = True
                entry_idx = i
                entry_z = z
                position_type = 'long'
        else:
            exit_signal = False
            if position_type == 'short':
                if z <= exit_threshold or z >= stop_threshold or abs(z) >= stop_threshold:
                    exit_signal = True
            else:
                if z >= -exit_threshold or z <= -stop_threshold or abs(z) >= stop_threshold:
                    exit_signal = True
            
            if exit_signal:
                pnl_sigma = (
                    entry_z - z
                    if position_type == "short"
                    else z - entry_z
                )
                
                trades.append({
                    "entry_idx": int(entry_idx),
                    "exit_idx": int(i),
                    "entry_z": float(entry_z),
                    "exit_z": float(z),
                    "pnl_sigma": float(pnl_sigma),
                    "days": int(i - entry_idx),
                    "type": position_type,
                })
                in_position = False
                entry_idx = None
                entry_z = None
                position_type = None
    
    return pd.DataFrame(trades)


def backtest_stats(
    trades: pd.DataFrame,
    spread_sd_pct: float | None = None,
) -> Dict[str, Any]:
    """Compute summary statistics from backtest trades."""
    if trades.empty:
        return {
            "n_trades": 0, "win_rate": None, "avg_pnl_pct": None,
            "avg_hold": None, "avg_win": None, "avg_loss": None,
            "has_history": False, "total_pnl_sigma": 0.0,
            "avg_pnl_sigma": None, "validated": False,
        }
    
    n = len(trades)
    wins = trades[trades["pnl_sigma"] > 0]
    losses = trades[trades["pnl_sigma"] < 0]
    
    win_rate = len(wins) / n if n > 0 else 0
    avg_pnl_sigma = float(trades["pnl_sigma"].mean())
    avg_pnl_pct = (
        avg_pnl_sigma * spread_sd_pct * 100
        if spread_sd_pct is not None
        else None
    )
    
    return {
        "n_trades": n,
        "win_rate": round(float(win_rate) * 100, 1),
        "avg_pnl_pct": (
            round(float(avg_pnl_pct), 2)
            if avg_pnl_pct is not None
            else None
        ),
        "avg_pnl_sigma": round(avg_pnl_sigma, 4),
        "avg_hold": round(float(trades["days"].mean()), 1),
        "avg_win": round(float(wins["pnl_sigma"].mean()), 4) if len(wins) > 0 else None,
        "avg_loss": round(float(losses["pnl_sigma"].mean()), 4) if len(losses) > 0 else None,
        "has_history": True,
        "validated": n >= MIN_VALIDATED_TRADES,
        "total_pnl_sigma": round(float(trades["pnl_sigma"].sum()), 4),
    }


def compute_spread_sd_pct(
    pa: np.ndarray,
    pb: np.ndarray,
    hedge_ratio: float,
) -> float | None:
    """Compute spread standard deviation as percentage."""
    try:
        ok = (~np.isnan(pa)) & (~np.isnan(pb)) & (pa > 0) & (pb > 0)
        if ok.sum() < 30:
            return None
        hr = hedge_ratio if not np.isnan(hedge_ratio) else 1.0
        spread = np.log(pa[ok]) - hr * np.log(pb[ok])
        return float(np.std(spread, ddof=0))
    except Exception:
        return None


def out_of_sample_backtest(
    pa: np.ndarray,
    pb: np.ndarray,
    train_fraction: float = 0.7,
    min_train: int = 120,
    min_test: int = 60,
) -> Dict[str, Any]:
    """Evaluate a pair on prices excluded from model fitting.

    The hedge ratio, spread mean and spread deviation are fitted on the first
    part of the history. Trades are then simulated only on the held-out tail.
    """
    a = np.asarray(pa, dtype=float)
    b = np.asarray(pb, dtype=float)
    ok = np.isfinite(a) & np.isfinite(b) & (a > 0) & (b > 0)
    a = a[ok]
    b = b[ok]
    if len(a) < min_train + min_test:
        return backtest_stats(pd.DataFrame())

    split = max(min_train, int(len(a) * train_fraction))
    split = min(split, len(a) - min_test)
    train_a, test_a = a[:split], a[split:]
    train_b, test_b = b[:split], b[split:]

    model = engle_granger(train_a, train_b, min_obs=min_train)
    hedge_ratio = model.get("hedge_ratio")
    if hedge_ratio is None or not np.isfinite(float(hedge_ratio)):
        return backtest_stats(pd.DataFrame())

    hedge_ratio = float(hedge_ratio)
    train_spread = np.log(train_a) - hedge_ratio * np.log(train_b)
    spread_mean = float(np.mean(train_spread))
    spread_sd = float(np.std(train_spread, ddof=0))
    if not np.isfinite(spread_sd) or spread_sd <= 0:
        return backtest_stats(pd.DataFrame())

    test_spread = np.log(test_a) - hedge_ratio * np.log(test_b)
    test_zscores = (test_spread - spread_mean) / spread_sd
    trades = run_backtest(test_zscores)

    # Spread changes are normalized by total gross notional allocated to both
    # legs, so the percentage matches a hedge-ratio-weighted pair position.
    portfolio_spread_sd = spread_sd / (1.0 + abs(hedge_ratio))
    stats = backtest_stats(trades, portfolio_spread_sd)
    stats.update({
        "train_observations": int(len(train_a)),
        "test_observations": int(len(test_a)),
        "model_is_coint": bool(model.get("is_coint")),
    })
    return stats
