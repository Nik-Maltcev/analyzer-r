#!/usr/bin/env python3
"""Build public extension payloads from already computed database data."""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.api.public_extension import refresh_extension_feed_snapshots
from app.data.tickers import ALL_MARKETS


def main() -> int:
    markets = {
        market.strip()
        for market in os.environ.get(
            "ENABLED_MARKETS",
            "crypto,stocks,ru,br,id,au",
        ).split(",")
        if market.strip() in ALL_MARKETS
    }
    if not markets:
        print("Extension feed refresh failed: no enabled markets")
        return 1
    refreshed = asyncio.run(
        refresh_extension_feed_snapshots(
            markets,
            db_path=os.environ.get("DB_PATH"),
        )
    )
    missing = sorted(set(markets) - set(refreshed))
    if missing:
        print(
            "Extension feed snapshot refresh missed markets: "
            + ", ".join(missing)
        )
        return 1
    print(f"Extension feed snapshots refreshed: {refreshed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
