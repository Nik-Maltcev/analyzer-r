"""Backtest engine for pair trading strategies."""

import numpy as np
import pandas as pd
from typing import Dict, Any, List


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
                pnl_sigma = abs(entry_z) - abs(z) if position_type == 'short' else abs(entry_z) - abs(z)
                pnl_sigma = pnl_sigma if position_type == 'long' else -pnl_sigma
                
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


def backtest_stats(trades: pd.DataFrame, spread_sd_pct: float = 0.05) -> Dict[str, Any]:
    """Compute summary statistics from backtest trades."""
    if trades.empty:
        return {
            "n_trades": 0, "win_rate": None, "avg_pnl_pct": None,
            "avg_hold": None, "avg_win": None, "avg_loss": None,
            "has_history": False, "total_pnl_sigma": 0.0,
        }
    
    n = len(trades)
    wins = trades[trades["pnl_sigma"] > 0]
    losses = trades[trades["pnl_sigma"] < 0]
    
    win_rate = len(wins) / n if n > 0 else 0
    avg_pnl_sigma = float(trades["pnl_sigma"].mean())
    avg_pnl_pct = avg_pnl_sigma * spread_sd_pct * 100
    
    return {
        "n_trades": n,
        "win_rate": round(float(win_rate) * 100, 1),
        "avg_pnl_pct": round(float(avg_pnl_pct), 2),
        "avg_pnl_sigma": round(avg_pnl_sigma, 4),
        "avg_hold": round(float(trades["days"].mean()), 1),
        "avg_win": round(float(wins["pnl_sigma"].mean()), 4) if len(wins) > 0 else None,
        "avg_loss": round(float(losses["pnl_sigma"].mean()), 4) if len(losses) > 0 else None,
        "has_history": True,
        "total_pnl_sigma": round(float(trades["pnl_sigma"].sum()), 4),
    }


def compute_spread_sd_pct(pa: np.ndarray, pb: np.ndarray, hedge_ratio: float) -> float:
    """Compute spread standard deviation as percentage."""
    try:
        ok = (~np.isnan(pa)) & (~np.isnan(pb)) & (pa > 0) & (pb > 0)
        if ok.sum() < 30:
            return 0.05
        hr = hedge_ratio if not np.isnan(hedge_ratio) else 1.0
        spread = np.log(pa[ok]) - hr * np.log(pb[ok])
        return float(np.std(spread, ddof=0))
    except Exception:
        return 0.05
