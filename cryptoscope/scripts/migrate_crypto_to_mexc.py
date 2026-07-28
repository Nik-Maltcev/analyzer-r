#!/usr/bin/env python3
"""One-time, validated crypto history migration to MEXC spot candles."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.scanner_history import sync_all_scanner_states
from app.data.mexc import (
    LEGACY_PROVIDER,
    refresh_mexc_crypto_market,
    rollback_crypto_provider,
)
from app.data.tickers import CRYPTO_TICKERS

DB_PATH = os.environ.get("DB_PATH", "/data/market.db")
SCRIPT_DIR = Path(__file__).resolve().parent


def _active_provider(conn: sqlite3.Connection) -> str:
    try:
        row = conn.execute(
            """
            SELECT active_provider
            FROM market_data_state
            WHERE market = 'crypto'
            """
        ).fetchone()
    except sqlite3.OperationalError:
        return ""
    return str(row[0] or "").lower() if row else ""


async def migrate() -> dict:
    conn = sqlite3.connect(DB_PATH)
    try:
        if _active_provider(conn) == "mexc":
            return {"status": "already_active", "provider": "mexc"}
        result = await refresh_mexc_crypto_market(conn, CRYPTO_TICKERS)
    finally:
        conn.close()

    try:
        subprocess.run(
            [sys.executable, str(SCRIPT_DIR / "compute_analysis.py")],
            check=True,
            env=os.environ.copy(),
        )
    except Exception:
        rollback_conn = sqlite3.connect(DB_PATH)
        try:
            rollback_crypto_provider(rollback_conn, LEGACY_PROVIDER)
        finally:
            rollback_conn.close()
        subprocess.run(
            [sys.executable, str(SCRIPT_DIR / "compute_analysis.py")],
            check=True,
            env=os.environ.copy(),
        )
        raise
    await sync_all_scanner_states(DB_PATH, {"crypto"})
    return {"status": "migrated", **result}


if __name__ == "__main__":
    migration = asyncio.run(migrate())
    print(
        "[MEXC] Migration result: "
        f"status={migration['status']} "
        f"provider={migration.get('provider')} "
        f"tickers={migration.get('tickers', '-')} "
        f"rows={migration.get('rows', '-')} "
        f"latest={migration.get('latest_date', '-')}"
    )
