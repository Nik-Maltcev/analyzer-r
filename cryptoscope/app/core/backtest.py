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
    
    A threshold is known only after that session closes. Entry and exit are
    therefore executed at the next finite observation, never at the same
    close that generated the decision.

    Entry decision: |Z| >= entry_threshold
    Exit decision:  |Z| <= exit_threshold OR |Z| >= stop_threshold
    
    Returns DataFrame with trades: entry_idx, exit_idx, entry_z, exit_z, pnl_sigma, days
    """
    trades = []
    in_position = False
    entry_idx = None
    entry_z = None
    position_type = None  # 'long' (buy spread) or 'short' (sell spread)
    entry_signal_idx = None
    pending_entry = None
    pending_exit_idx = None
    
    for i in range(len(zscores)):
        z = zscores[i]
        if np.isnan(z):
            continue

        if pending_exit_idx is not None:
            pnl_sigma = (
                entry_z - z
                if position_type == "short"
                else z - entry_z
            )
            trades.append({
                "entry_signal_idx": int(entry_signal_idx),
                "exit_signal_idx": int(pending_exit_idx),
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
            entry_signal_idx = None
            position_type = None
            pending_exit_idx = None
            continue

        if pending_entry is not None:
            in_position = True
            entry_idx = i
            entry_z = z
            entry_signal_idx = pending_entry["signal_idx"]
            position_type = pending_entry["type"]
            pending_entry = None
            continue

        if not in_position:
            if z >= entry_threshold:
                pending_entry = {"signal_idx": i, "type": "short"}
            elif z <= -entry_threshold:
                pending_entry = {"signal_idx": i, "type": "long"}
        else:
            exit_signal = False
            if position_type == 'short':
                if z <= exit_threshold or z >= stop_threshold or abs(z) >= stop_threshold:
                    exit_signal = True
            else:
                if z >= -exit_threshold or z <= -stop_threshold or abs(z) >= stop_threshold:
                    exit_signal = True
            
            if exit_signal:
                pending_exit_idx = i

    return pd.DataFrame(
        trades,
        columns=[
            "entry_signal_idx",
            "exit_signal_idx",
            "entry_idx",
            "exit_idx",
            "entry_z",
            "exit_z",
            "pnl_sigma",
            "days",
            "type",
        ],
    )


def backtest_stats(
    trades: pd.DataFrame,
    spread_sd_pct: float | None = None,
) -> Dict[str, Any]:
    """Compute summary statistics from backtest trades."""
    if trades.empty:
        return {
            "n_trades": 0, "win_rate": None, "avg_pnl_pct": None,
            "avg_net_pnl_pct": None,
            "avg_hold": None, "avg_win": None, "avg_loss": None,
            "has_history": False, "total_pnl_sigma": 0.0,
            "avg_pnl_sigma": None, "validated": False,
        }
    
    n = len(trades)
    result_column = (
        "return_pct"
        if "return_pct" in trades.columns
        else "pnl_sigma"
    )
    wins = trades[trades[result_column] > 0]
    losses = trades[trades[result_column] < 0]
    
    win_rate = len(wins) / n if n > 0 else 0
    avg_pnl_sigma = float(trades["pnl_sigma"].mean())
    if "return_pct" in trades.columns:
        avg_pnl_pct = float(trades["return_pct"].mean())
    else:
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
        "avg_net_pnl_pct": (
            round(float(trades["net_return_pct"].mean()), 2)
            if "net_return_pct" in trades.columns
            and len(trades["net_return_pct"].dropna()) > 0
            else None
        ),
        "avg_pnl_sigma": round(avg_pnl_sigma, 4),
        "avg_hold": round(float(trades["days"].mean()), 1),
        "avg_win": (
            round(float(wins[result_column].mean()), 4)
            if len(wins) > 0
            else None
        ),
        "avg_loss": (
            round(float(losses[result_column].mean()), 4)
            if len(losses) > 0
            else None
        ),
        "has_history": True,
        "validated": n >= MIN_VALIDATED_TRADES,
        "total_pnl_sigma": round(float(trades["pnl_sigma"].sum()), 4),
    }


def compute_spread_sd_pct(
    pa: np.ndarray,
    pb: np.ndarray,
    hedge_ratio: float,
    window: int | None = None,
) -> float | None:
    """Compute spread standard deviation as percentage.

    With ``window`` set, only the trailing observations are used so the
    estimate matches the current regime (same normalization as the Z-score).
    """
    try:
        ok = (~np.isnan(pa)) & (~np.isnan(pb)) & (pa > 0) & (pb > 0)
        if ok.sum() < 30:
            return None
        hr = hedge_ratio if not np.isnan(hedge_ratio) else 1.0
        spread = np.log(pa[ok]) - hr * np.log(pb[ok])
        if window:
            spread = spread[-window:]
        return float(np.std(spread, ddof=0))
    except Exception:
        return None


def estimate_roundtrip_cost_pct(
    days: float,
    taker_fee_pct: float = 0.02,
    funding_rate_8h_pct: float = 0.0,
) -> float:
    """Round-trip cost of a pair position in percent of gross notional.

    Two legs x (entry + exit) taker commissions, plus perpetual funding
    charged every 8 hours while the position is open (crypto only).
    """
    fee_cost = 4.0 * float(taker_fee_pct)
    funding_cost = 3.0 * float(funding_rate_8h_pct) * max(0.0, float(days))
    return fee_cost + funding_cost


def attach_pair_returns(
    trades: pd.DataFrame,
    price_a: np.ndarray,
    price_b: np.ndarray,
    hedge_ratio: float,
    taker_fee_pct: float = 0.0,
    funding_rate_8h_pct: float = 0.0,
) -> pd.DataFrame:
    """Attach exact hedge-weighted price returns to backtest trades."""
    if trades.empty:
        result = trades.copy()
        result["return_pct"] = pd.Series(dtype=float)
        result["net_return_pct"] = pd.Series(dtype=float)
        return result

    a = np.asarray(price_a, dtype=float)
    b = np.asarray(price_b, dtype=float)
    hedge = float(hedge_ratio)
    hedge_weight = abs(hedge)
    weight_a = 1.0 / (1.0 + hedge_weight)
    weight_b = hedge_weight / (1.0 + hedge_weight)
    returns: list[float] = []
    net_returns: list[float] = []

    for row in trades.itertuples(index=False):
        entry_idx = int(row.entry_idx)
        exit_idx = int(row.exit_idx)
        return_a = (a[exit_idx] / a[entry_idx] - 1) * 100
        return_b = (b[exit_idx] / b[entry_idx] - 1) * 100
        if row.type == "long":
            leg_a = return_a
            leg_b = return_b if hedge < 0 else -return_b
        else:
            leg_a = -return_a
            leg_b = -return_b if hedge < 0 else return_b
        gross = leg_a * weight_a + leg_b * weight_b
        returns.append(gross)
        net_returns.append(
            gross
            - estimate_roundtrip_cost_pct(
                int(row.days), taker_fee_pct, funding_rate_8h_pct
            )
        )

    result = trades.copy()
    result["return_pct"] = returns
    result["net_return_pct"] = net_returns
    return result


def out_of_sample_backtest(
    pa: np.ndarray,
    pb: np.ndarray,
    train_fraction: float = 0.7,
    min_train: int = 120,
    min_test: int = 60,
    taker_fee_pct: float = 0.0,
    funding_rate_8h_pct: float = 0.0,
) -> Dict[str, Any]:
    """Evaluate a pair on prices excluded from model fitting.

    The hedge ratio, spread mean and spread deviation are fitted on the first
    part of the history. Trades are then simulated only on the held-out tail.
    Net returns subtract taker commissions and (for perpetuals) funding.
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
    trades = attach_pair_returns(
        trades,
        test_a,
        test_b,
        hedge_ratio,
        taker_fee_pct=taker_fee_pct,
        funding_rate_8h_pct=funding_rate_8h_pct,
    )

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
