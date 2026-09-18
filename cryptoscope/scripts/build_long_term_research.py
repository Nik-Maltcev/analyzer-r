#!/usr/bin/env python3
"""Download a Binance top-asset dataset and run long-term feasibility tests."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.long_term import (
    evaluate_long_term_deposit_strategies,
    evaluate_long_term_strategies,
)
from app.data.binance_history import build_binance_top_dataset


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--years", type=int, default=7)
    parser.add_argument("--horizon-days", type=int, default=365)
    parser.add_argument("--target-return-pct", type=float, default=15.0)
    parser.add_argument("--probability-threshold-pct", type=float, default=80.0)
    parser.add_argument("--minimum-events", type=int, default=20)
    parser.add_argument(
        "--reuse-data",
        action="store_true",
        help="Recalculate from the saved CSV without downloading it again",
    )
    parser.add_argument(
        "--refresh-universe",
        action="store_true",
        help="Replace the frozen universe instead of reusing it",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data") / "long_term_7y",
    )
    return parser.parse_args()


async def run(args: argparse.Namespace) -> dict:
    output_dir = args.output_dir.resolve()
    dataset_path = output_dir / "binance_top_daily.csv"
    events_path = output_dir / "long_term_events.csv"
    summary_path = output_dir / "strategy_summary.csv"
    report_path = output_dir / "long_term_report.json"
    universe_path = output_dir / "universe.json"
    if args.reuse_data:
        if not dataset_path.exists():
            raise FileNotFoundError(f"Saved dataset does not exist: {dataset_path}")
        dataset = pd.read_csv(dataset_path)
        metadata = {
            "selection_method": "saved_binance_dataset",
            "start_date": str(dataset["date"].min()),
            "end_date": str(dataset["date"].max()),
            "top_n": int(dataset["ticker"].nunique()),
            "rows": int(len(dataset)),
        }
    else:
        fixed_candidates = None
        if universe_path.exists() and not args.refresh_universe:
            universe_payload = json.loads(universe_path.read_text(encoding="utf-8"))
            fixed_candidates = list(universe_payload.get("assets") or [])
            if not fixed_candidates:
                raise RuntimeError(f"Frozen universe is empty: {universe_path}")
        dataset, metadata = await build_binance_top_dataset(
            top_n=args.top,
            years=args.years,
            fixed_candidates=fixed_candidates,
        )
    events, research = evaluate_long_term_strategies(
        dataset,
        horizon_days=args.horizon_days,
        target_return=args.target_return_pct / 100,
        probability_threshold=args.probability_threshold_pct / 100,
        minimum_independent_events=args.minimum_events,
    )
    deposit_research = evaluate_long_term_deposit_strategies(
        dataset,
        probability_threshold=args.probability_threshold_pct / 100,
        minimum_independent_events=args.minimum_events,
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    if not args.reuse_data and (not universe_path.exists() or args.refresh_universe):
        universe_path.write_text(
            json.dumps(
                {
                    "version": "long-term-universe-v1",
                    "frozen_on": metadata.get("selection_date"),
                    "assets": metadata.get("selected", []),
                    "policy": "fixed_until_explicit_refresh",
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    dataset.to_csv(dataset_path, index=False)
    events.to_csv(events_path, index=False)
    pd.DataFrame(research["strategies"]).sort_values(
        ["qualifies", "asset_probability_pct", "asset_events"],
        ascending=[False, False, False],
    ).to_csv(summary_path, index=False)
    payload = {
        "dataset": metadata,
        "research": research,
        "deposit_research": deposit_research,
        "files": {
            "dataset": str(dataset_path),
            "events": str(events_path),
            "strategy_summary": str(summary_path),
            "report": str(report_path),
            "universe": str(universe_path),
        },
    }
    report_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return payload


def main() -> int:
    args = parse_args()
    if args.top < 2 or args.years < 1 or args.horizon_days < 30:
        raise SystemExit("top>=2, years>=1 and horizon-days>=30 are required")
    payload = asyncio.run(run(args))
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
