#!/usr/bin/env python3
"""Advance the Reversal Lab forward test using newly completed candles."""

import asyncio
import os

from app.core.reversal_lab import refresh_reversal_forward
from app.content.reversal_notifications import dispatch_reversal_notifications


if __name__ == "__main__":
    db_path = os.getenv("DB_PATH", "/data/market.db")
    refresh_error = None
    try:
        result = asyncio.run(refresh_reversal_forward(db_path))
        print(f"Reversal forward result: {result}")
    except Exception as exc:
        refresh_error = exc
        print(f"Reversal forward refresh failed: {exc}")
    notifications = dispatch_reversal_notifications(db_path)
    print(f"Reversal Telegram notifications: {notifications}")
    if refresh_error is not None:
        raise refresh_error
