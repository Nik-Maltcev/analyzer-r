#!/usr/bin/env python3
"""Refresh persisted Reversal Lab data without blocking the web process."""

import asyncio
import os

from app.core.reversal_lab import refresh_and_backtest


if __name__ == "__main__":
    db_path = os.getenv("DB_PATH", "/data/market.db")
    result = asyncio.run(refresh_and_backtest(db_path))
    print(f"Reversal Lab result: {result}")
