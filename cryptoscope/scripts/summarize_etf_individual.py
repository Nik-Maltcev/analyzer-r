"""Summarize long-term ETF strategy outcomes for each ETF separately."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import NormalDist

import numpy as np
import pandas as pd

from app.core.long_term import STRATEGIES, wilson_interval


TARGET_RETURN_PCT = 10.0
REQUIRED_PROBABILITY = 0.80
MINIMUM_EVENTS = 20


def _json_default(value):
    if isinstance(value, np.generic):
        return value.item()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _summary(group: pd.DataFrame, familywise_z: float) -> dict:
    returns = pd.to_numeric(group["net_return_pct"], errors="coerce").dropna()
    successes = int((returns >= TARGET_RETURN_PCT).sum())
    observations = int(len(returns))
    probability = successes / observations if observations else 0.0
    low, high = wilson_interval(successes, observations)
    familywise_low, familywise_high = wilson_interval(
        successes,
        observations,
        z=familywise_z,
    )
    return {
        "events": observations,
        "successes": successes,
        "probability_pct": round(probability * 100, 2),
        "confidence_95_low_pct": round(low * 100, 2),
        "confidence_95_high_pct": round(high * 100, 2),
        "familywise_confidence_low_pct": round(familywise_low * 100, 2),
        "familywise_confidence_high_pct": round(familywise_high * 100, 2),
        "median_return_pct": round(float(returns.median()), 2),
        "average_return_pct": round(float(returns.mean()), 2),
        "quartile_25_return_pct": round(float(returns.quantile(0.25)), 2),
        "worst_return_pct": round(float(returns.min()), 2),
        "best_return_pct": round(float(returns.max()), 2),
        "qualifies": bool(
            observations >= MINIMUM_EVENTS
            and probability > REQUIRED_PROBABILITY
            and familywise_low > REQUIRED_PROBABILITY
        ),
    }


def run(args: argparse.Namespace) -> dict:
    events = pd.read_csv(args.events)
    strategy_labels = {strategy.key: strategy.label for strategy in STRATEGIES}
    tickers = sorted(events["ticker"].dropna().unique())
    horizons = sorted(pd.to_numeric(events["horizon_months"]).dropna().astype(int).unique())
    combination_count = len(tickers) * len(STRATEGIES) * len(horizons)
    familywise_z = NormalDist().inv_cdf(1 - 0.05 / (2 * combination_count))
    rows = []
    for (months, ticker, strategy), group in events.groupby(
        ["horizon_months", "ticker", "strategy"],
        sort=True,
    ):
        rows.append({
            "horizon_months": int(months),
            "ticker": str(ticker),
            "strategy": str(strategy),
            "strategy_label": strategy_labels.get(str(strategy), str(strategy)),
            **_summary(group, familywise_z),
        })
    summary = pd.DataFrame(rows)

    plans = []
    for ticker in tickers:
        ticker_plans = []
        for months in horizons:
            subset = summary.loc[
                (summary["ticker"] == ticker)
                & (summary["horizon_months"] == months)
            ].copy()
            sufficiently_sampled = subset.loc[subset["events"] >= MINIMUM_EVENTS]
            ranking_pool = sufficiently_sampled if not sufficiently_sampled.empty else subset
            ranking_pool = ranking_pool.sort_values(
                ["qualifies", "probability_pct", "events", "median_return_pct"],
                ascending=[False, False, False, False],
            )
            best = ranking_pool.iloc[0].to_dict() if not ranking_pool.empty else None
            benchmark_rows = subset.loc[subset["strategy"] == "buy_hold"]
            benchmark = (
                benchmark_rows.iloc[0].to_dict()
                if not benchmark_rows.empty
                else None
            )
            ticker_plans.append({
                "horizon_months": months,
                "best_strategy": best,
                "buy_hold_benchmark": benchmark,
            })
        plans.append({"ticker": ticker, "plans": ticker_plans})

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    summary_path = Path(args.summary)
    summary.to_csv(summary_path, index=False)
    report = {
        "research_version": "etf-long-term-individual-v1",
        "source_events": str(args.events),
        "target_definition": "individual ETF net return greater than or equal to 10%",
        "required_probability_pct": REQUIRED_PROBABILITY * 100,
        "minimum_events": MINIMUM_EVENTS,
        "tickers": tickers,
        "strategy_count": len(STRATEGIES),
        "horizon_count": len(horizons),
        "combination_count": combination_count,
        "multiple_testing_control": (
            "Bonferroni-adjusted Wilson interval across every ETF, strategy and horizon"
        ),
        "qualified_combination_count": int(summary["qualifies"].sum()),
        "etfs": plans,
        "files": {
            "report": str(output),
            "summary": str(summary_path),
        },
    }
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--events",
        default="data/etf_long_term/etf_strategy_events.csv",
    )
    parser.add_argument(
        "--output",
        default="data/etf_long_term/etf_individual_report.json",
    )
    parser.add_argument(
        "--summary",
        default="data/etf_long_term/etf_individual_summary.csv",
    )
    args = parser.parse_args()
    report = run(args)
    print(json.dumps({
        "tickers": report["tickers"],
        "combination_count": report["combination_count"],
        "qualified_combination_count": report["qualified_combination_count"],
        "twelve_months": [
            {
                "ticker": etf["ticker"],
                "best_strategy": next(
                    plan for plan in etf["plans"] if plan["horizon_months"] == 12
                )["best_strategy"],
                "buy_hold": next(
                    plan for plan in etf["plans"] if plan["horizon_months"] == 12
                )["buy_hold_benchmark"],
            }
            for etf in report["etfs"]
        ],
    }, ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()
