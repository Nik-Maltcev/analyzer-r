"""Download frozen-universe Binance candles for pair-trading research."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pandas as pd

from app.data.binance_history import fetch_binance_klines


def _start_date(end_date: date, years: int) -> date:
    try:
        return end_date.replace(year=end_date.year - years)
    except ValueError:
        return end_date.replace(year=end_date.year - years, day=28)


async def run(args: argparse.Namespace) -> dict:
    universe = json.loads(Path(args.universe).read_text(encoding="utf-8"))
    assets = list(universe.get("assets") or [])
    if not assets:
        raise RuntimeError("Frozen universe is empty")
    end_date = datetime.now(UTC).date() - timedelta(days=1)
    start_date = _start_date(end_date, args.years)
    timeout = httpx.Timeout(60, connect=20)
    headers = {"User-Agent": "MEANX-pair-research/1.0"}

    async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
        downloaded = await asyncio.gather(*[
            fetch_binance_klines(
                client,
                asset["binance_symbol"],
                start_date,
                end_date,
                interval=args.interval,
            )
            for asset in assets
        ])

    frames = []
    selected = []
    for asset, frame in zip(assets, downloaded, strict=True):
        if frame.empty:
            continue
        enriched = frame.copy()
        enriched.insert(0, "ticker", f"{asset['base_asset']}/USD")
        enriched.insert(1, "binance_symbol", asset["binance_symbol"])
        frames.append(enriched)
        selected.append({
            "ticker": f"{asset['base_asset']}/USD",
            "binance_symbol": asset["binance_symbol"],
            "rows": int(len(frame)),
        })
    if len(frames) < 2:
        raise RuntimeError("Fewer than two assets were downloaded")

    dataset = pd.concat(frames, ignore_index=True).sort_values(["ticker", "date"])
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    dataset.to_csv(output, index=False)
    result = {
        "source": "Binance",
        "interval": args.interval,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "rows": int(len(dataset)),
        "assets": selected,
        "output": str(output),
    }
    Path(args.metadata).write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", choices=("4h", "1d"), default="4h")
    parser.add_argument("--years", type=int, default=7)
    parser.add_argument("--universe", default="data/long_term_7y/universe.json")
    parser.add_argument("--output", default="data/pair_trading_7y/binance_top_4h.csv")
    parser.add_argument("--metadata", default="data/pair_trading_7y/dataset.json")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
