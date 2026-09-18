"""Walk-forward research for a fixed crypto pairs-trading strategy."""

from __future__ import annotations

import argparse
import json
import math
from itertools import combinations
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from app.core.backtest import estimate_roundtrip_cost_pct
from app.core.cointegration import engle_granger
from app.core.risk import assess_cointegration_stability


ENTRY_Z = 2.0
EXIT_Z = 0.5
STOP_Z = 3.5
FORMATION_DAYS = 365
TRADING_DAYS = 90
MAX_PVALUE = 0.01
MIN_CORRELATION = 0.50
MIN_HALFLIFE = 2
MAX_HALFLIFE = 60
TAKER_FEE_PCT = 0.02
CONSERVATIVE_FUNDING_8H_PCT = 0.01
BARS_PER_DAY = 1


def _model(train_a: np.ndarray, train_b: np.ndarray) -> dict[str, Any] | None:
    result = engle_granger(
        train_a,
        train_b,
        min_obs=252 * BARS_PER_DAY,
        max_pvalue=MAX_PVALUE,
    )
    half_life = result.get("halflife")
    if (
        not result.get("is_coint")
        or half_life is None
        or not MIN_HALFLIFE * BARS_PER_DAY <= int(half_life) <= MAX_HALFLIFE * BARS_PER_DAY
    ):
        return None

    stability = assess_cointegration_stability(
        train_a,
        train_b,
        windows=(120 * BARS_PER_DAY, 252 * BARS_PER_DAY, 365 * BARS_PER_DAY),
        minimum_passed=2,
        require_recent=True,
        enforce_ratio_stability=True,
        max_pvalue=MAX_PVALUE,
    )
    if not stability["is_coint_stable"]:
        return None

    returns_a = np.diff(np.log(train_a))
    returns_b = np.diff(np.log(train_b))
    correlation = float(np.corrcoef(returns_a, returns_b)[0, 1])
    if not np.isfinite(correlation) or correlation < MIN_CORRELATION:
        return None

    hedge_ratio = float(result["hedge_ratio"])
    spread = np.log(train_a) - hedge_ratio * np.log(train_b)
    spread_mean = float(np.mean(spread))
    spread_sd = float(np.std(spread, ddof=0))
    if not np.isfinite(spread_sd) or spread_sd <= 0:
        return None

    return {
        "hedge_ratio": hedge_ratio,
        "spread_mean": spread_mean,
        "spread_sd": spread_sd,
        "p_value": float(result["p_value"]),
        "half_life": int(half_life),
        "half_life_days": round(float(half_life) / BARS_PER_DAY, 2),
        "correlation": correlation,
        "stability_pct": int(stability["coint_stability"]),
    }


def _trade_return(
    kind: str,
    entry_a: float,
    exit_a: float,
    entry_b: float,
    exit_b: float,
    hedge_ratio: float,
) -> float:
    return_a = (exit_a / entry_a - 1) * 100
    return_b = (exit_b / entry_b - 1) * 100
    hedge_weight = abs(hedge_ratio)
    weight_a = 1 / (1 + hedge_weight)
    weight_b = hedge_weight / (1 + hedge_weight)
    if kind == "long":
        leg_a = return_a
        leg_b = return_b if hedge_ratio < 0 else -return_b
    else:
        leg_a = -return_a
        leg_b = -return_b if hedge_ratio < 0 else return_b
    return float(leg_a * weight_a + leg_b * weight_b)


def _run_window(
    dates: pd.DatetimeIndex,
    price_a: np.ndarray,
    price_b: np.ndarray,
    model: dict[str, Any],
    pair: str,
    window_number: int,
) -> list[dict[str, Any]]:
    spread = (
        np.log(price_a)
        - model["hedge_ratio"] * np.log(price_b)
    )
    zscores = (spread - model["spread_mean"]) / model["spread_sd"]
    trades: list[dict[str, Any]] = []
    position: dict[str, Any] | None = None
    pending_entry: dict[str, Any] | None = None
    pending_exit_reason: str | None = None
    entry_armed = True
    previous_z: float | None = None

    for index, zscore in enumerate(zscores):
        if not np.isfinite(zscore):
            continue
        if pending_exit_reason is not None and position is not None:
            entry_index = int(position["entry_index"])
            holding_days = max(
                1 / BARS_PER_DAY,
                float((dates[index] - dates[entry_index]).total_seconds() / 86400),
            )
            gross = _trade_return(
                str(position["kind"]),
                float(price_a[entry_index]),
                float(price_a[index]),
                float(price_b[entry_index]),
                float(price_b[index]),
                float(model["hedge_ratio"]),
            )
            trades.append({
                "window": window_number,
                "pair": pair,
                "entry_date": dates[entry_index].date().isoformat(),
                "exit_date": dates[index].date().isoformat(),
                "days": round(holding_days, 3),
                "direction": position["kind"],
                "entry_z": float(position["entry_z"]),
                "exit_z": float(zscore),
                "exit_reason": pending_exit_reason,
                "gross_return_pct": gross,
                "net_fee_only_pct": gross - estimate_roundtrip_cost_pct(
                    holding_days,
                    taker_fee_pct=TAKER_FEE_PCT,
                ),
                "net_conservative_pct": gross - estimate_roundtrip_cost_pct(
                    holding_days,
                    taker_fee_pct=TAKER_FEE_PCT,
                    funding_rate_8h_pct=CONSERVATIVE_FUNDING_8H_PCT,
                ),
            })
            position = None
            if pending_exit_reason == "stop":
                entry_armed = False
            pending_exit_reason = None
            previous_z = float(zscore)
            continue

        if pending_entry is not None:
            position = {
                "entry_index": index,
                "entry_z": float(zscore),
                "kind": pending_entry["kind"],
            }
            pending_entry = None
            previous_z = float(zscore)
            continue

        if position is None:
            if abs(zscore) < ENTRY_Z:
                entry_armed = True
            turning = (
                previous_z is not None
                and np.sign(zscore) == np.sign(previous_z)
                and abs(zscore) < abs(previous_z)
            )
            if entry_armed and turning and zscore >= ENTRY_Z:
                pending_entry = {"kind": "short"}
            elif entry_armed and turning and zscore <= -ENTRY_Z:
                pending_entry = {"kind": "long"}
            previous_z = float(zscore)
            continue

        if position["kind"] == "short":
            if zscore <= EXIT_Z:
                pending_exit_reason = "mean_reversion"
            elif zscore >= STOP_Z or abs(zscore) >= STOP_Z:
                pending_exit_reason = "stop"
        else:
            if zscore >= -EXIT_Z:
                pending_exit_reason = "mean_reversion"
            elif zscore <= -STOP_Z or abs(zscore) >= STOP_Z:
                pending_exit_reason = "stop"
        previous_z = float(zscore)

    if position is not None:
        index = len(zscores) - 1
        entry_index = int(position["entry_index"])
        if index > entry_index:
            holding_days = max(
                1 / BARS_PER_DAY,
                float((dates[index] - dates[entry_index]).total_seconds() / 86400),
            )
            gross = _trade_return(
                str(position["kind"]),
                float(price_a[entry_index]),
                float(price_a[index]),
                float(price_b[entry_index]),
                float(price_b[index]),
                float(model["hedge_ratio"]),
            )
            trades.append({
                "window": window_number,
                "pair": pair,
                "entry_date": dates[entry_index].date().isoformat(),
                "exit_date": dates[index].date().isoformat(),
                "days": round(holding_days, 3),
                "direction": position["kind"],
                "entry_z": float(position["entry_z"]),
                "exit_z": float(zscores[index]),
                "exit_reason": "rebalance",
                "gross_return_pct": gross,
                "net_fee_only_pct": gross - estimate_roundtrip_cost_pct(
                    holding_days,
                    taker_fee_pct=TAKER_FEE_PCT,
                ),
                "net_conservative_pct": gross - estimate_roundtrip_cost_pct(
                    holding_days,
                    taker_fee_pct=TAKER_FEE_PCT,
                    funding_rate_8h_pct=CONSERVATIVE_FUNDING_8H_PCT,
                ),
            })
    return trades


def _summary(trades: pd.DataFrame, column: str, initial_capital: float) -> dict[str, Any]:
    if trades.empty:
        return {
            "trades": 0,
            "win_rate_pct": None,
            "total_return_pct": 0,
            "ending_capital": initial_capital,
        }
    ordered = trades.sort_values(["exit_date", "entry_date"]).copy()
    returns = ordered[column].astype(float)
    equity = initial_capital * (1 + returns / 100).cumprod()
    running_peak = np.maximum.accumulate(np.r_[initial_capital, equity.to_numpy()])
    equity_path = np.r_[initial_capital, equity.to_numpy()]
    drawdown = equity_path / running_peak - 1
    return {
        "trades": int(len(ordered)),
        "wins": int((returns > 0).sum()),
        "win_rate_pct": round(float((returns > 0).mean() * 100), 2),
        "average_trade_pct": round(float(returns.mean()), 3),
        "median_trade_pct": round(float(returns.median()), 3),
        "best_trade_pct": round(float(returns.max()), 3),
        "worst_trade_pct": round(float(returns.min()), 3),
        "average_holding_days": round(float(ordered["days"].mean()), 1),
        "total_return_pct": round(float((equity.iloc[-1] / initial_capital - 1) * 100), 2),
        "ending_capital": round(float(equity.iloc[-1]), 2),
        "max_closed_trade_drawdown_pct": round(float(drawdown.min() * 100), 2),
    }


def research(prices: pd.DataFrame, initial_capital: float = 1000) -> tuple[dict[str, Any], pd.DataFrame]:
    frame = prices.copy()
    frame["date"] = pd.to_datetime(frame["date"])
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    matrix = (
        frame.pivot_table(index="date", columns="ticker", values="close", aggfunc="last")
        .sort_index()
        .dropna(axis=0, how="any")
    )
    tickers = list(matrix.columns)
    all_pairs = list(combinations(tickers, 2))
    selected_windows: list[dict[str, Any]] = []
    portfolio_trades: list[dict[str, Any]] = []
    pooled_trades: list[dict[str, Any]] = []

    formation_bars = FORMATION_DAYS * BARS_PER_DAY
    trading_bars = TRADING_DAYS * BARS_PER_DAY
    start = formation_bars
    window_number = 0
    while start + 2 < len(matrix):
        stop = min(start + trading_bars, len(matrix))
        train = matrix.iloc[start - formation_bars:start]
        test = matrix.iloc[start:stop]
        candidates: list[dict[str, Any]] = []
        for ticker_a, ticker_b in all_pairs:
            fitted = _model(
                train[ticker_a].to_numpy(dtype=float),
                train[ticker_b].to_numpy(dtype=float),
            )
            if fitted is not None:
                candidates.append({
                    **fitted,
                    "ticker_a": ticker_a,
                    "ticker_b": ticker_b,
                    "pair": f"{ticker_a}_{ticker_b}",
                })
        candidates.sort(
            key=lambda row: (
                row["p_value"],
                -row["correlation"],
                row["half_life_days"],
                row["pair"],
            )
        )

        for candidate in candidates:
            pooled_trades.extend(
                _run_window(
                    test.index,
                    test[candidate["ticker_a"]].to_numpy(dtype=float),
                    test[candidate["ticker_b"]].to_numpy(dtype=float),
                    candidate,
                    candidate["pair"],
                    window_number,
                )
            )

        if candidates:
            chosen = candidates[0]
            window_trades = _run_window(
                test.index,
                test[chosen["ticker_a"]].to_numpy(dtype=float),
                test[chosen["ticker_b"]].to_numpy(dtype=float),
                chosen,
                chosen["pair"],
                window_number,
            )
            portfolio_trades.extend(window_trades)
            selected_windows.append({
                "window": window_number,
                "formation_end": train.index[-1].date().isoformat(),
                "trading_start": test.index[0].date().isoformat(),
                "trading_end": test.index[-1].date().isoformat(),
                "eligible_pairs": len(candidates),
                "selected_pair": chosen["pair"],
                "p_value": chosen["p_value"],
                "correlation": chosen["correlation"],
                "half_life_days": chosen["half_life_days"],
                "trades": len(window_trades),
            })
        else:
            selected_windows.append({
                "window": window_number,
                "formation_end": train.index[-1].date().isoformat(),
                "trading_start": test.index[0].date().isoformat(),
                "trading_end": test.index[-1].date().isoformat(),
                "eligible_pairs": 0,
                "selected_pair": None,
                "trades": 0,
            })
        window_number += 1
        start = stop

    portfolio = pd.DataFrame(portfolio_trades)
    pooled = pd.DataFrame(pooled_trades)
    report = {
        "method": "fixed-rules-quarterly-walk-forward-v1",
        "source": "Binance daily closes",
        "data_start": matrix.index.min().date().isoformat(),
        "data_end": matrix.index.max().date().isoformat(),
        "assets": tickers,
        "possible_pairs": len(all_pairs),
        "formation_days": FORMATION_DAYS,
        "trading_days": TRADING_DAYS,
        "bars_per_day": BARS_PER_DAY,
        "rules": {
            "entry_z": ENTRY_Z,
            "exit_z": EXIT_Z,
            "stop_z": STOP_Z,
            "maximum_cointegration_p_value": MAX_PVALUE,
            "minimum_return_correlation": MIN_CORRELATION,
            "half_life_days": [MIN_HALFLIFE, MAX_HALFLIFE],
            "taker_fee_pct_per_operation": TAKER_FEE_PCT,
            "conservative_funding_8h_pct": CONSERVATIVE_FUNDING_8H_PCT,
        },
        "windows": len(selected_windows),
        "windows_with_pair": sum(bool(row["selected_pair"]) for row in selected_windows),
        "windows_with_trade": sum(bool(row["trades"]) for row in selected_windows),
        "portfolio_one_pair_at_a_time": {
            "fee_only": _summary(portfolio, "net_fee_only_pct", initial_capital),
            "conservative_funding": _summary(
                portfolio,
                "net_conservative_pct",
                initial_capital,
            ),
        },
        "all_eligible_pair_opportunities": {
            "fee_only": _summary(pooled, "net_fee_only_pct", initial_capital),
            "conservative_funding": _summary(
                pooled,
                "net_conservative_pct",
                initial_capital,
            ),
            "note": "Each pooled trade is a separate $1000 opportunity; total return is not a deployable portfolio result.",
        },
        "selected_windows": selected_windows,
    }
    return report, portfolio


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input",
        default="data/long_term_7y/binance_top_daily.csv",
    )
    parser.add_argument(
        "--output",
        default="data/long_term_7y/pair_trading_report.json",
    )
    parser.add_argument(
        "--trades-output",
        default="data/long_term_7y/pair_trading_trades.csv",
    )
    parser.add_argument("--capital", type=float, default=1000)
    parser.add_argument("--bars-per-day", type=int, default=1)
    args = parser.parse_args()

    global BARS_PER_DAY
    BARS_PER_DAY = args.bars_per_day

    prices = pd.read_csv(args.input)
    report, trades = research(prices, initial_capital=args.capital)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    trades.to_csv(args.trades_output, index=False)
    print(json.dumps({
        "report": str(output),
        "trades": str(args.trades_output),
        "portfolio": report["portfolio_one_pair_at_a_time"],
        "windows": report["windows"],
        "windows_with_pair": report["windows_with_pair"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
