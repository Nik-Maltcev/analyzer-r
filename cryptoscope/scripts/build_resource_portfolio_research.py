"""Walk-forward portfolios for the non-oil resource universe.

Weights are calculated at a month end and applied only to the following month.
The universe and rules are fixed in advance; there is no parameter optimisation.
"""

from __future__ import annotations

import json
from pathlib import Path
from statistics import NormalDist

import numpy as np
import pandas as pd

from analyze_country_etf_rub_real import (
    STARTING_RUB,
    cpi_for_dates,
    load_cbr_usd_rub,
    load_rosstat_month_start_cpi,
    values_asof,
    wilson_lower,
)
from build_resource_strategy_research import ASSETS, HORIZONS


START_DATE = pd.Timestamp("2012-01-01")
ONE_WAY_COST_PCT = 0.10
DEPOSIT_RATE = 0.10
TARGET_RETURN = 0.10

CORE = ["GLD", "SLV", "PPLT", "PALL", "DBB", "DBA", "NLR", "WOOD", "PHO"]
BARBELL = {
    "GLD": 0.30,
    "SLV": 0.10,
    "DBB": 0.15,
    "DBA": 0.15,
    "NLR": 0.15,
    "PHO": 0.15,
}

PORTFOLIO_LABELS = {
    "equal_weight_core": "Equal-weight diversified resources",
    "fixed_macro_barbell": "Precious / real-assets barbell",
    "top5_trend_equal": "Top-5 positive 10-month trends",
    "top5_trend_inverse_vol": "Top-5 trends, inverse volatility",
    "top3_rotation": "Top-3 resource momentum rotation",
    "defensive_trend_barbell": "Barbell with trend-to-cash filter",
    "core_plus_trend": "50% macro core / 50% trend",
    "defensive_plus_trend": "50% defensive / 50% trend",
}


def _cap_weights(raw: pd.Series, cap: float = 0.30) -> pd.Series:
    """Normalise non-negative weights while respecting a practical position cap."""
    weights = raw.clip(lower=0).fillna(0).astype(float)
    if weights.sum() <= 0:
        return weights
    weights /= weights.sum()
    for _ in range(20):
        excess = (weights - cap).clip(lower=0)
        if excess.sum() < 1e-12:
            break
        weights = weights.clip(upper=cap)
        room = (cap - weights).clip(lower=0)
        if room.sum() <= 0:
            break
        weights += excess.sum() * room / room.sum()
    return weights / weights.sum()


def _portfolio_weights(monthly: pd.DataFrame) -> dict[str, pd.DataFrame]:
    momentum = monthly.pct_change(10, fill_method=None)
    ma10 = monthly.rolling(10, min_periods=10).mean()
    trend = (momentum > 0) & (monthly > ma10)
    monthly_vol = monthly.pct_change(fill_method=None).rolling(6, min_periods=6).std()
    portfolios: dict[str, pd.DataFrame] = {}

    equal = pd.DataFrame(0.0, index=monthly.index, columns=monthly.columns)
    active_core = monthly[CORE].notna()
    equal[CORE] = active_core.div(active_core.sum(axis=1), axis=0).fillna(0)
    portfolios["equal_weight_core"] = equal

    fixed = pd.DataFrame(0.0, index=monthly.index, columns=monthly.columns)
    for ticker, weight in BARBELL.items():
        fixed[ticker] = np.where(monthly[ticker].notna(), weight, 0.0)
    fixed = fixed.div(fixed.sum(axis=1), axis=0).fillna(0)
    portfolios["fixed_macro_barbell"] = fixed

    trend_equal = pd.DataFrame(0.0, index=monthly.index, columns=monthly.columns)
    trend_iv = trend_equal.copy()
    rotation = trend_equal.copy()
    for date in monthly.index:
        scores = momentum.loc[date].where(trend.loc[date]).dropna().sort_values(ascending=False)
        top5 = scores.head(5).index
        if len(top5):
            trend_equal.loc[date, top5] = 1.0 / len(top5)
            inverse_vol = (1 / monthly_vol.loc[date, top5]).replace([np.inf, -np.inf], np.nan).dropna()
            if len(inverse_vol):
                trend_iv.loc[date, inverse_vol.index] = _cap_weights(inverse_vol, 0.30)
        top3 = scores.head(3).index
        if len(top3):
            rotation.loc[date, top3] = 1.0 / len(top3)
    portfolios["top5_trend_equal"] = trend_equal
    portfolios["top5_trend_inverse_vol"] = trend_iv
    portfolios["top3_rotation"] = rotation

    defensive = pd.DataFrame(0.0, index=monthly.index, columns=monthly.columns)
    for ticker, base_weight in BARBELL.items():
        defensive[ticker] = np.where(trend[ticker], base_weight, 0.0)
    portfolios["defensive_trend_barbell"] = defensive
    portfolios["core_plus_trend"] = (fixed + trend_iv) / 2
    portfolios["defensive_plus_trend"] = (defensive + trend_iv) / 2
    return portfolios


def _return_series(
    monthly: pd.DataFrame,
    weights: dict[str, pd.DataFrame],
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    asset_returns = monthly.pct_change(fill_method=None)
    portfolio_returns = pd.DataFrame(index=monthly.index)
    turnover = pd.DataFrame(index=monthly.index)
    cash = pd.DataFrame(index=monthly.index)
    for name, frame in weights.items():
        held = frame.shift(1).fillna(0)
        gross = (held * asset_returns.fillna(0)).sum(axis=1)
        traded = frame.diff().abs().sum(axis=1).fillna(frame.abs().sum(axis=1))
        cost = traded.shift(1).fillna(0) * ONE_WAY_COST_PCT / 100
        portfolio_returns[name] = gross - cost
        turnover[name] = traded
        cash[name] = (1 - frame.sum(axis=1)).clip(lower=0, upper=1)
    return portfolio_returns, turnover, cash


def _drawdown(series: pd.Series) -> float:
    wealth = (1 + series).cumprod()
    return float((wealth / wealth.cummax() - 1).min())


def _full_period_metrics(
    returns: pd.DataFrame,
    turnover: pd.DataFrame,
    fx: pd.Series,
    cpi: pd.Series,
) -> pd.DataFrame:
    rows = []
    for name in returns:
        series = returns[name].dropna()
        years = len(series) / 12
        factor = float((1 + series).prod())
        cagr = factor ** (1 / years) - 1 if years > 0 and factor > 0 else np.nan
        annual_vol = float(series.std(ddof=1) * np.sqrt(12))
        annual = (1 + series).groupby(series.index.year).prod() - 1
        start_date = series.index[0] - pd.offsets.MonthEnd(1)
        end_date = series.index[-1]
        start_fx = values_asof(fx, pd.Series([start_date]))[0]
        end_fx = values_asof(fx, pd.Series([end_date]))[0]
        start_cpi = cpi_for_dates(cpi, pd.Series([start_date]))[0]
        end_cpi = cpi_for_dates(cpi, pd.Series([end_date]))[0]
        rub_factor = factor * end_fx / start_fx
        real_factor = rub_factor / (end_cpi / start_cpi)
        deposit_factor = (1 + DEPOSIT_RATE) ** years
        deposit_real_factor = deposit_factor / (end_cpi / start_cpi)
        rows.append({
            "portfolio": name,
            "portfolio_label": PORTFOLIO_LABELS[name],
            "months": len(series),
            "usd_cagr_pct": cagr * 100,
            "annualized_volatility_pct": annual_vol * 100,
            "max_drawdown_pct": _drawdown(series) * 100,
            "positive_calendar_years_pct": (annual > 0).mean() * 100,
            "worst_calendar_year_pct": annual.min() * 100,
            "best_calendar_year_pct": annual.max() * 100,
            "average_annual_turnover_pct": turnover.loc[series.index, name].mean() * 12 * 100,
            "usd_strategy_factor": factor,
            "rub_cagr_pct": (rub_factor ** (1 / years) - 1) * 100,
            "real_rub_cagr_pct": (real_factor ** (1 / years) - 1) * 100,
            "final_rub_from_100k": STARTING_RUB * rub_factor,
            "real_value_in_start_rub": STARTING_RUB * real_factor,
            "fixed_10pct_deposit_final_rub": STARTING_RUB * deposit_factor,
            "fixed_10pct_deposit_real_value": STARTING_RUB * deposit_real_factor,
        })
    return pd.DataFrame(rows)


def _window_results(
    returns: pd.DataFrame,
    fx: pd.Series,
    cpi: pd.Series,
) -> pd.DataFrame:
    rows: list[dict] = []
    for name in returns:
        series = returns[name].dropna()
        dates = series.index
        for horizon in HORIZONS:
            for end_pos in range(horizon, len(series) + 1):
                start_pos = end_pos - horizon
                start_date = dates[start_pos] - pd.offsets.MonthEnd(1)
                end_date = dates[end_pos - 1]
                usd_factor = float((1 + series.iloc[start_pos:end_pos]).prod())
                start_fx = values_asof(fx, pd.Series([start_date]))[0]
                end_fx = values_asof(fx, pd.Series([end_date]))[0]
                start_cpi = cpi_for_dates(cpi, pd.Series([start_date]))[0]
                end_cpi = cpi_for_dates(cpi, pd.Series([end_date]))[0]
                if not all(np.isfinite([start_fx, end_fx, start_cpi, end_cpi])):
                    continue
                rub_factor = usd_factor * end_fx / start_fx
                real_factor = rub_factor / (end_cpi / start_cpi)
                days = max((end_date - start_date).days, 1)
                deposit_factor = 1 + DEPOSIT_RATE * days / 365
                rows.append({
                    "portfolio": name,
                    "portfolio_label": PORTFOLIO_LABELS[name],
                    "horizon_months": horizon,
                    "entry_date": start_date,
                    "exit_date": end_date,
                    "usd_return_pct": (usd_factor - 1) * 100,
                    "rub_return_pct": (rub_factor - 1) * 100,
                    "real_return_pct": (real_factor - 1) * 100,
                    "deposit_return_pct": (deposit_factor - 1) * 100,
                    "final_rub": STARTING_RUB * rub_factor,
                    "usd_plus_10": usd_factor >= 1 + TARGET_RETURN,
                    "rub_plus_10": rub_factor >= 1 + TARGET_RETURN,
                    "real_plus_10": real_factor >= 1 + TARGET_RETURN,
                    "beats_deposit": rub_factor > deposit_factor,
                })
    return pd.DataFrame(rows)


def _window_summary(windows: pd.DataFrame) -> pd.DataFrame:
    rows = []
    keys = ["portfolio", "portfolio_label", "horizon_months"]
    for key, group in windows.groupby(keys, sort=False):
        horizon = int(key[2])
        successes = int(group["usd_plus_10"].sum())
        observations = len(group)
        # Rolling windows overlap. floor(months/horizon) is used as a conservative
        # effective sample size for the confidence interval.
        effective_n = max(1, observations // horizon)
        effective_successes = int(round(group["usd_plus_10"].mean() * effective_n))
        rows.append({
            "portfolio": key[0],
            "portfolio_label": key[1],
            "horizon_months": horizon,
            "rolling_windows": observations,
            "effective_independent_windows": effective_n,
            "usd_plus_10_count": successes,
            "usd_plus_10_pct": group["usd_plus_10"].mean() * 100,
            "rub_plus_10_pct": group["rub_plus_10"].mean() * 100,
            "real_plus_10_pct": group["real_plus_10"].mean() * 100,
            "positive_usd_pct": (group["usd_return_pct"] > 0).mean() * 100,
            "beats_deposit_pct": group["beats_deposit"].mean() * 100,
            "median_usd_return_pct": group["usd_return_pct"].median(),
            "median_rub_return_pct": group["rub_return_pct"].median(),
            "median_real_return_pct": group["real_return_pct"].median(),
            "median_final_rub": group["final_rub"].median(),
            "worst_usd_return_pct": group["usd_return_pct"].min(),
            "best_usd_return_pct": group["usd_return_pct"].max(),
            "usd_plus_10_wilson_low_pct": wilson_lower(
                effective_successes, effective_n, 1.96
            ) * 100,
        })
    result = pd.DataFrame(rows)
    family_z = NormalDist().inv_cdf(1 - 0.05 / (2 * len(result)))
    result["usd_plus_10_familywise_low_pct"] = result.apply(
        lambda row: wilson_lower(
            int(round(row.usd_plus_10_pct / 100 * row.effective_independent_windows)),
            int(row.effective_independent_windows),
            family_z,
        ) * 100,
        axis=1,
    )
    result["formally_over_80"] = (
        (result["effective_independent_windows"] >= 20)
        & (result["usd_plus_10_pct"] >= 80)
        & (result["usd_plus_10_familywise_low_pct"] >= 80)
    )
    numeric = result.select_dtypes(include="number").columns
    result[numeric] = result[numeric].round(2)
    return result.sort_values(
        ["usd_plus_10_pct", "median_usd_return_pct"], ascending=[False, False]
    )


def _latest_allocations(
    prices: pd.DataFrame,
    portfolios: dict[str, pd.DataFrame],
    cash: pd.DataFrame,
) -> pd.DataFrame:
    latest_date = prices["date"].max()
    rows = []
    for name, weights in portfolios.items():
        last = weights.iloc[-1]
        for ticker, weight in last[last > 0.0001].sort_values(ascending=False).items():
            rows.append({
                "as_of": latest_date.date().isoformat(),
                "portfolio": name,
                "portfolio_label": PORTFOLIO_LABELS[name],
                "ticker": ticker,
                "asset": ASSETS[ticker],
                "weight_pct": round(weight * 100, 2),
            })
        cash_weight = float(cash[name].iloc[-1])
        if cash_weight > 0.0001:
            rows.append({
                "as_of": latest_date.date().isoformat(),
                "portfolio": name,
                "portfolio_label": PORTFOLIO_LABELS[name],
                "ticker": "USD_CASH",
                "asset": "USD cash reserve",
                "weight_pct": round(cash_weight * 100, 2),
            })
    return pd.DataFrame(rows)


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    output = root / "data" / "resource_portfolio"
    output.mkdir(parents=True, exist_ok=True)
    prices = pd.read_csv(root / "data" / "resource_prices.csv")
    prices["date"] = pd.to_datetime(prices["date"])
    prices = prices.loc[prices["ticker"].isin(ASSETS)].copy()
    pivot = prices.pivot(index="date", columns="ticker", values="close").sort_index()
    monthly = pivot.resample("ME").last()
    # The last resampled row may be an unfinished month. Keep it for today's
    # allocation but exclude it from realised performance.
    current_monthly = monthly.copy()
    last_market_date = prices["date"].max()
    if monthly.index[-1] > last_market_date:
        backtest_monthly = monthly.iloc[:-1]
    else:
        backtest_monthly = monthly
    backtest_monthly = backtest_monthly.loc[backtest_monthly.index >= START_DATE]

    backtest_weights = _portfolio_weights(backtest_monthly)
    returns, turnover, cash = _return_series(backtest_monthly, backtest_weights)
    returns = returns.loc[returns.index > START_DATE]
    turnover = turnover.loc[returns.index]

    rub_dir = root / "data" / "rub_real_return"
    fx = load_cbr_usd_rub(rub_dir / "cbr_usd_rub.xml")
    cpi = load_rosstat_month_start_cpi(rub_dir / "rosstat_cpi_monthly.xlsx")
    windows = _window_results(returns, fx, cpi)
    summary = _window_summary(windows)
    full = _full_period_metrics(returns, turnover, fx, cpi)

    current_weights = _portfolio_weights(current_monthly)
    _, _, current_cash = _return_series(current_monthly, current_weights)
    latest = _latest_allocations(prices, current_weights, current_cash)

    returns.reset_index(names="date").to_csv(output / "portfolio_monthly_returns.csv", index=False)
    windows.to_csv(output / "portfolio_rolling_windows.csv", index=False)
    summary.to_csv(output / "portfolio_horizon_summary.csv", index=False)
    full.to_csv(output / "portfolio_full_period.csv", index=False)
    latest.to_csv(output / "portfolio_latest_allocations.csv", index=False)

    best_by_horizon = (
        summary.sort_values(["horizon_months", "usd_plus_10_pct"], ascending=[True, False])
        .groupby("horizon_months", as_index=False)
        .first()
    )
    report = {
        "method": {
            "backtest_start": START_DATE.date().isoformat(),
            "last_realised_month": returns.index.max().date().isoformat(),
            "last_market_date": last_market_date.date().isoformat(),
            "execution": "month-end signal, next-month return; long-only, no leverage",
            "cash_return": "0% USD, deliberately conservative",
            "one_way_cost_pct_per_turnover": ONE_WAY_COST_PCT,
            "oil_and_gasoline": "excluded",
            "target": "at least +10%, not a fixed +10% take-profit",
            "confidence": "rolling rates shown descriptively; confidence uses floor(months/horizon) effective windows",
        },
        "coverage": {
            "portfolios": len(PORTFOLIO_LABELS),
            "horizon_tests": len(summary),
            "rolling_windows": len(windows),
            "formally_over_80_count": int(summary["formally_over_80"].sum()),
        },
        "full_period": full.round(2).replace({np.nan: None}).to_dict(orient="records"),
        "best_by_horizon": best_by_horizon.replace({np.nan: None}).to_dict(orient="records"),
        "latest_allocations": latest.replace({np.nan: None}).to_dict(orient="records"),
    }
    (output / "resource_portfolio_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report["coverage"], ensure_ascii=False))
    print("\nFull period:")
    print(full.round(2).to_string(index=False))
    print("\nBest by horizon:")
    print(best_by_horizon[[
        "horizon_months", "portfolio", "rolling_windows", "effective_independent_windows",
        "usd_plus_10_pct", "positive_usd_pct", "median_usd_return_pct",
        "real_plus_10_pct", "beats_deposit_pct", "worst_usd_return_pct",
    ]].to_string(index=False))


if __name__ == "__main__":
    main()
