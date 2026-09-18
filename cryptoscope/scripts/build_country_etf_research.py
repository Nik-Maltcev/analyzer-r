"""Download country ETF histories and test each ETF independently."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import NormalDist

import numpy as np
import pandas as pd

from app.core.long_term import STRATEGIES, _month_end_rows, _prepare_features, wilson_interval
from app.data.us_stocks import fetch_us_stock_prices


COUNTRY_ETFS = {
    "EWA": "Australia",
    "EWC": "Canada",
    "EWG": "Germany",
    "EWU": "United Kingdom",
    "EWQ": "France",
    "EWL": "Switzerland",
    "EWJ": "Japan",
    "MCHI": "China",
    "INDA": "India",
    "EWY": "South Korea",
    "EWT": "Taiwan",
    "EWH": "Hong Kong",
    "EWS": "Singapore",
    "EWM": "Malaysia",
    "EWZ": "Brazil",
    "EWW": "Mexico",
    "EZA": "South Africa",
    "EWI": "Italy",
    "EWP": "Spain",
    "EWN": "Netherlands",
}
MONTH_HORIZONS = {
    1: 30, 2: 61, 3: 91, 4: 122, 5: 152, 6: 183,
    7: 213, 8: 244, 9: 274, 10: 305, 11: 335, 12: 365,
}
TARGET_RETURN = 0.10
ROUND_TRIP_COST = 0.001
REQUIRED_PROBABILITY = 0.80
MINIMUM_EVENTS = 20


def _strategy_events(
    history: pd.DataFrame,
    monthly: pd.DataFrame,
    strategy_key: str,
    horizon_days: int,
) -> list[dict]:
    dates = history["date"].to_numpy(dtype="datetime64[ns]")
    closes = history["close"].to_numpy(dtype=float)
    signal_rows = monthly.loc[monthly[f"signal_{strategy_key}"]]
    events = []
    next_allowed_entry: pd.Timestamp | None = None
    for signal in signal_rows.itertuples(index=False):
        signal_date = pd.Timestamp(signal.date)
        signal_index = int(np.searchsorted(dates, np.datetime64(signal_date), side="left"))
        entry_index = signal_index + 1
        if entry_index >= len(history):
            continue
        entry_date = pd.Timestamp(dates[entry_index])
        if next_allowed_entry is not None and entry_date < next_allowed_entry:
            continue
        desired_exit = entry_date + pd.Timedelta(days=horizon_days)
        exit_index = int(np.searchsorted(dates, np.datetime64(desired_exit), side="left"))
        if exit_index >= len(history):
            continue
        net_return = closes[exit_index] / closes[entry_index] - 1 - ROUND_TRIP_COST
        path = pd.Series(closes[entry_index:exit_index + 1])
        drawdown = float((path / path.cummax() - 1).min())
        events.append({
            "signal_date": signal_date.date().isoformat(),
            "entry_date": entry_date.date().isoformat(),
            "exit_date": pd.Timestamp(dates[exit_index]).date().isoformat(),
            "net_return_pct": float(net_return * 100),
            "success": bool(net_return >= TARGET_RETURN),
            "max_drawdown_pct": float(drawdown * 100),
        })
        next_allowed_entry = desired_exit
    return events


def _summarize(events: list[dict], familywise_z: float) -> dict:
    if not events:
        return {
            "events": 0,
            "successes": 0,
            "probability_pct": 0.0,
            "confidence_95_low_pct": 0.0,
            "familywise_confidence_low_pct": 0.0,
            "median_return_pct": None,
            "average_return_pct": None,
            "quartile_25_return_pct": None,
            "worst_return_pct": None,
            "worst_drawdown_pct": None,
            "qualifies": False,
        }
    frame = pd.DataFrame(events)
    observations = len(frame)
    successes = int(frame["success"].sum())
    probability = successes / observations
    low, _ = wilson_interval(successes, observations)
    familywise_low, _ = wilson_interval(successes, observations, z=familywise_z)
    return {
        "events": observations,
        "successes": successes,
        "probability_pct": round(probability * 100, 2),
        "confidence_95_low_pct": round(low * 100, 2),
        "familywise_confidence_low_pct": round(familywise_low * 100, 2),
        "median_return_pct": round(float(frame["net_return_pct"].median()), 2),
        "average_return_pct": round(float(frame["net_return_pct"].mean()), 2),
        "quartile_25_return_pct": round(float(frame["net_return_pct"].quantile(0.25)), 2),
        "worst_return_pct": round(float(frame["net_return_pct"].min()), 2),
        "worst_drawdown_pct": round(float(frame["max_drawdown_pct"].min()), 2),
        "qualifies": bool(
            observations >= MINIMUM_EVENTS
            and probability > REQUIRED_PROBABILITY
            and familywise_low > REQUIRED_PROBABILITY
        ),
    }


def run(args: argparse.Namespace) -> dict:
    output_dir = Path(args.output_dir)
    data_path = output_dir / "country_etf_adjusted_daily.csv"
    if args.reuse_data:
        prices = pd.read_csv(data_path)
    else:
        prices = fetch_us_stock_prices(
            list(COUNTRY_ETFS),
            history_years=args.history_years,
            batch_size=5,
            retries=3,
        )
        missing = sorted(set(COUNTRY_ETFS) - set(prices["ticker"].unique()))
        if missing:
            raise RuntimeError(f"Missing country ETF histories: {missing}")
    output_dir.mkdir(parents=True, exist_ok=True)
    prices.to_csv(data_path, index=False)

    prices["date"] = pd.to_datetime(prices["date"])
    history_meta = {
        ticker: {
            "country": COUNTRY_ETFS[ticker],
            "start": group["date"].min().date().isoformat(),
            "end": group["date"].max().date().isoformat(),
            "rows": int(len(group)),
        }
        for ticker, group in prices.groupby("ticker")
    }
    features = _prepare_features(prices)
    monthly = _month_end_rows(features)
    combination_count = len(COUNTRY_ETFS) * len(STRATEGIES) * len(MONTH_HORIZONS)
    familywise_z = NormalDist().inv_cdf(1 - 0.05 / (2 * combination_count))
    rows = []
    event_rows = []

    for ticker in sorted(COUNTRY_ETFS):
        history = features.loc[features["ticker"] == ticker].sort_values("date").reset_index(drop=True)
        ticker_monthly = monthly.loc[monthly["ticker"] == ticker]
        for strategy in STRATEGIES:
            for months, horizon_days in MONTH_HORIZONS.items():
                events = _strategy_events(
                    history,
                    ticker_monthly,
                    strategy.key,
                    horizon_days,
                )
                summary = _summarize(events, familywise_z)
                rows.append({
                    "ticker": ticker,
                    "country": COUNTRY_ETFS[ticker],
                    "horizon_months": months,
                    "strategy": strategy.key,
                    "strategy_label": strategy.label,
                    **summary,
                })
                event_rows.extend({
                    "ticker": ticker,
                    "country": COUNTRY_ETFS[ticker],
                    "horizon_months": months,
                    "strategy": strategy.key,
                    **event,
                } for event in events)

    summary_frame = pd.DataFrame(rows)
    events_frame = pd.DataFrame(event_rows)
    best_by_country = []
    for ticker in sorted(COUNTRY_ETFS):
        subset = summary_frame.loc[
            (summary_frame["ticker"] == ticker)
            & (summary_frame["events"] >= MINIMUM_EVENTS)
        ].sort_values(
            ["qualifies", "probability_pct", "events", "median_return_pct"],
            ascending=[False, False, False, False],
        )
        best = subset.iloc[0].to_dict() if not subset.empty else None
        buy_hold = summary_frame.loc[
            (summary_frame["ticker"] == ticker)
            & (summary_frame["strategy"] == "buy_hold")
            & (summary_frame["horizon_months"] == 12)
        ].iloc[0].to_dict()
        best_by_country.append({
            "ticker": ticker,
            "country": COUNTRY_ETFS[ticker],
            "history": history_meta[ticker],
            "best": best,
            "buy_hold_12m": buy_hold,
        })

    summary_path = output_dir / "country_etf_summary.csv"
    events_path = output_dir / "country_etf_events.csv"
    report_path = output_dir / "country_etf_report.json"
    summary_frame.to_csv(summary_path, index=False)
    events_frame.to_csv(events_path, index=False)
    report = {
        "research_version": "country-etf-individual-fixed-rules-v1",
        "source": "Yahoo Finance auto-adjusted daily prices",
        "target_definition": "individual ETF net return greater than or equal to 10%",
        "required_probability_pct": REQUIRED_PROBABILITY * 100,
        "minimum_events": MINIMUM_EVENTS,
        "round_trip_cost_pct": ROUND_TRIP_COST * 100,
        "strategy_count": len(STRATEGIES),
        "horizon_count": len(MONTH_HORIZONS),
        "country_count": len(COUNTRY_ETFS),
        "combination_count": combination_count,
        "multiple_testing_control": (
            "Bonferroni-adjusted Wilson interval across every country ETF, strategy and horizon"
        ),
        "qualified_combination_count": int(summary_frame["qualifies"].sum()),
        "history": history_meta,
        "best_by_country": best_by_country,
        "files": {
            "dataset": str(data_path),
            "summary": str(summary_path),
            "events": str(events_path),
            "report": str(report_path),
        },
    }
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=lambda value: value.item()),
        encoding="utf-8",
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--history-years", type=int, default=40)
    parser.add_argument("--reuse-data", action="store_true")
    parser.add_argument("--output-dir", default="data/country_etf_long_term")
    args = parser.parse_args()
    report = run(args)
    print(json.dumps({
        "country_count": report["country_count"],
        "combination_count": report["combination_count"],
        "qualified_combination_count": report["qualified_combination_count"],
        "best_by_country": report["best_by_country"],
    }, ensure_ascii=False, indent=2, default=lambda value: value.item()))


if __name__ == "__main__":
    main()
