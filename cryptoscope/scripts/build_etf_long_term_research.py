"""Download adjusted ETF prices and evaluate fixed long-term strategies."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from app.core.long_term import STRATEGIES, evaluate_long_term_strategies
from app.data.us_stocks import fetch_us_stock_prices


ETF_UNIVERSE = (
    "SPY", "QQQ", "VTI", "DIA", "IWM",
    "XLK", "XLF", "XLV", "XLE", "XLP",
    "XLI", "XLY", "XLU", "XLB",
)
MONTH_HORIZONS = {
    1: 30,
    2: 61,
    3: 91,
    4: 122,
    5: 152,
    6: 183,
    7: 213,
    8: 244,
    9: 274,
    10: 305,
    11: 335,
    12: 365,
}
TARGET_RETURN = 0.10
REQUIRED_PROBABILITY = 0.80
ROUND_TRIP_COST = 0.001
MINIMUM_EPISODES = 20


def _common_history(prices: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    starts = prices.groupby("ticker")["date"].min().sort_values()
    ends = prices.groupby("ticker")["date"].max().sort_values()
    common_start = str(starts.max())
    common_end = str(ends.min())
    common = prices.loc[
        (prices["date"] >= common_start) & (prices["date"] <= common_end)
    ].copy()
    return common, {
        "individual_start_dates": starts.to_dict(),
        "individual_end_dates": ends.to_dict(),
        "common_start": common_start,
        "common_end": common_end,
    }


def _rank(strategies: list[dict]) -> list[dict]:
    return sorted(
        strategies,
        key=lambda row: (
            not bool(row["qualifies"]),
            -float(row["probability_pct"]),
            -int(row["independent_market_episodes"]),
            -float(row["median_net_return_pct"] or -999),
            row["strategy"],
        ),
    )


def run(args: argparse.Namespace) -> dict:
    output_dir = Path(args.output_dir)
    data_path = output_dir / "etf_adjusted_daily.csv"
    if args.reuse_data:
        prices = pd.read_csv(data_path)
    else:
        prices = fetch_us_stock_prices(
            ETF_UNIVERSE,
            history_years=args.history_years,
            batch_size=7,
            retries=3,
        )
        if prices["ticker"].nunique() != len(ETF_UNIVERSE):
            missing = sorted(set(ETF_UNIVERSE) - set(prices["ticker"].unique()))
            raise RuntimeError(f"Missing ETF histories: {missing}")

    prices, history = _common_history(prices)
    output_dir.mkdir(parents=True, exist_ok=True)
    prices.to_csv(data_path, index=False)

    familywise_tests = len(STRATEGIES) * len(MONTH_HORIZONS)
    plan_reports = []
    event_frames = []
    summary_rows = []
    for months, horizon_days in MONTH_HORIZONS.items():
        events, result = evaluate_long_term_strategies(
            prices,
            horizon_days=horizon_days,
            target_return=TARGET_RETURN,
            probability_threshold=REQUIRED_PROBABILITY,
            minimum_independent_events=MINIMUM_EPISODES,
            round_trip_cost=ROUND_TRIP_COST,
            familywise_test_count=familywise_tests,
        )
        if not events.empty:
            events.insert(0, "horizon_months", months)
            event_frames.append(events)
        ranked = _rank(result["strategies"])
        best = ranked[0]
        buy_hold = next(
            row for row in result["strategies"] if row["strategy"] == "buy_hold"
        )
        plan = {
            "horizon_months": months,
            "horizon_days": horizon_days,
            "target_return_pct": TARGET_RETURN * 100,
            "best_strategy": best,
            "buy_hold_benchmark": buy_hold,
            "qualified_strategies": result["qualified_strategies"],
            "strategies": ranked,
        }
        plan_reports.append(plan)
        for rank, strategy in enumerate(ranked, start=1):
            summary_rows.append({
                "horizon_months": months,
                "rank": rank,
                **strategy,
            })

    all_events = (
        pd.concat(event_frames, ignore_index=True)
        if event_frames
        else pd.DataFrame()
    )
    summary = pd.DataFrame(summary_rows)
    events_path = output_dir / "etf_strategy_events.csv"
    summary_path = output_dir / "etf_strategy_summary.csv"
    report_path = output_dir / "etf_long_term_report.json"
    all_events.to_csv(events_path, index=False)
    summary.to_csv(summary_path, index=False)

    report = {
        "research_version": "etf-long-term-fixed-rules-v1",
        "source": "Yahoo Finance auto-adjusted daily prices",
        "universe_policy": "fixed before testing",
        "universe": list(ETF_UNIVERSE),
        "history": history,
        "rows": int(len(prices)),
        "strategy_count": len(STRATEGIES),
        "horizon_count": len(MONTH_HORIZONS),
        "combination_count": familywise_tests,
        "target_definition": "net return greater than or equal to 10% at maturity",
        "target_return_pct": TARGET_RETURN * 100,
        "required_probability_pct": REQUIRED_PROBABILITY * 100,
        "round_trip_cost_pct": ROUND_TRIP_COST * 100,
        "minimum_independent_market_episodes": MINIMUM_EPISODES,
        "multiple_testing_control": (
            "Bonferroni-adjusted Wilson interval across 30 strategies and 12 horizons"
        ),
        "qualified_combination_count": sum(
            len(plan["qualified_strategies"]) for plan in plan_reports
        ),
        "plans": plan_reports,
        "files": {
            "dataset": str(data_path),
            "events": str(events_path),
            "summary": str(summary_path),
            "report": str(report_path),
        },
    }
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--history-years", type=int, default=40)
    parser.add_argument("--reuse-data", action="store_true")
    parser.add_argument("--output-dir", default="data/etf_long_term")
    args = parser.parse_args()
    report = run(args)
    print(json.dumps({
        "history": report["history"],
        "qualified_combination_count": report["qualified_combination_count"],
        "best_by_horizon": [
            {
                "months": plan["horizon_months"],
                "strategy": plan["best_strategy"]["label"],
                "probability_pct": plan["best_strategy"]["probability_pct"],
                "familywise_low_pct": plan["best_strategy"]["familywise_confidence_low_pct"],
                "episodes": plan["best_strategy"]["independent_market_episodes"],
                "median_return_pct": plan["best_strategy"]["median_net_return_pct"],
                "qualifies": plan["best_strategy"]["qualifies"],
            }
            for plan in report["plans"]
        ],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
