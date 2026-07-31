#!/usr/bin/env python3
"""Advance the Reversal Lab forward test using newly completed candles."""

import asyncio
import os

from app.core.reversal_lab import refresh_reversal_forward


if __name__ == "__main__":
    db_path = os.getenv("DB_PATH", "/data/market.db")
    result = asyncio.run(refresh_reversal_forward(db_path))
    print(f"Reversal forward result: {result}")
