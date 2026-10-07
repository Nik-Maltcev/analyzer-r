#!/usr/bin/env python3
"""One-time repair of MEXC crypto rows whose close diverges from Binance spot.

Thin MEXC markets (historically EGLD, ONE, ZIL) print closes far from the broad
market. This script re-reads the authoritative Binance daily close for affected
tickers, backs the old value up into crypto_price_repairs, and overwrites both
the staging source (crypto_price_versions) and the live prices table so the fix
is immediate and survives the next provider activation.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx

from app.data.binance_history import fetch_binance_daily_klines
from app.data.mexc import (
    BINANCE_RECONCILE_THRESHOLD,
    MEXC_PROVIDER,
    normalize_mexc_symbol,
)
from app.data.tickers import CRYPTO_TICKERS

DB_PATH = os.environ.get("DB_PATH", "/data/market.db")
DEFAULT_TICKERS = ["EGLD/USD", "ONE/USD", "ZIL/USD"]

CREATE_REPAIRS = """
CREATE TABLE IF NOT EXISTS crypto_price_repairs (
    provider     TEXT NOT NULL,
    ticker       TEXT NOT NULL,
    date         TEXT NOT NULL,
    old_close    REAL NOT NULL,
    new_close    REAL NOT NULL,
    repaired_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (provider, ticker, date)
)
"""


async def _binance_closes(
    ticker: str,
    start: date,
    end: date,
) -> dict[str, float]:
    symbol = normalize_mexc_symbol(ticker)
    if not symbol:
        return {}
    timeout = httpx.Timeout(30, connect=15)
    async with httpx.AsyncClient(timeout=timeout) as client:
        frame = await fetch_binance_daily_klines(client, symbol, start, end)
    if frame is None or frame.empty:
        return {}
    out: dict[str, float] = {}
    for row in frame.itertuples(index=False):
        try:
            close = float(row.close)
        except (TypeError, ValueError):
            continue
        if close > 0:
            out[str(row.date)[:10]] = close
    return out


async def _load_reference(
    staging: dict[str, list[tuple[str, float]]],
) -> dict[str, dict[str, float]]:
    semaphore = asyncio.Semaphore(4)
    reference: dict[str, dict[str, float]] = {}

    async def worker(ticker: str, rows: list[tuple[str, float]]) -> None:
        dates = sorted(d for d, _ in rows)
        if not dates:
            return
        try:
            async with semaphore:
                reference[ticker] = await _binance_closes(
                    ticker,
                    date.fromisoformat(dates[0]),
                    date.fromisoformat(dates[-1]),
                )
        except Exception as exc:
            print(f"[repair] Binance fetch failed for {ticker}: {exc}")
            reference[ticker] = {}

    await asyncio.gather(*(worker(t, r) for t, r in staging.items()))
    return reference


def repair(tickers: list[str], *, dry_run: bool) -> dict:
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(CREATE_REPAIRS)
        conn.commit()

        staging: dict[str, list[tuple[str, float]]] = {}
        for ticker in tickers:
            rows = conn.execute(
                """
                SELECT date, close
                FROM crypto_price_versions
                WHERE provider = ? AND ticker = ?
                ORDER BY date
                """,
                (MEXC_PROVIDER, ticker),
            ).fetchall()
            if rows:
                staging[ticker] = [(str(r[0])[:10], float(r[1])) for r in rows]

        reference = asyncio.run(_load_reference(staging))

        fixes: list[tuple[str, str, float, float]] = []
        for ticker, rows in staging.items():
            ref = reference.get(ticker, {})
            for day, close in rows:
                target = ref.get(day)
                if not target or target <= 0 or close <= 0:
                    continue
                if abs(close / target - 1.0) > BINANCE_RECONCILE_THRESHOLD:
                    fixes.append((ticker, day, close, target))

        per_ticker: dict[str, int] = {}
        for ticker, _, _, _ in fixes:
            per_ticker[ticker] = per_ticker.get(ticker, 0) + 1

        if dry_run:
            return {
                "status": "dry_run",
                "repairs": len(fixes),
                "per_ticker": per_ticker,
                "sample": fixes[:10],
            }

        for ticker, day, old, new in fixes:
            conn.execute(
                """
                INSERT INTO crypto_price_repairs (
                    provider, ticker, date, old_close, new_close
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(provider, ticker, date) DO UPDATE SET
                    old_close = excluded.old_close,
                    new_close = excluded.new_close,
                    repaired_at = datetime('now')
                """,
                (MEXC_PROVIDER, ticker, day, old, new),
            )
            conn.execute(
                """
                UPDATE crypto_price_versions
                SET close = ?, imported_at = datetime('now')
                WHERE provider = ? AND ticker = ? AND date = ?
                """,
                (new, MEXC_PROVIDER, ticker, day),
            )
            conn.execute(
                """
                UPDATE prices
                SET close = ?
                WHERE market = 'crypto' AND ticker = ? AND date = ?
                """,
                (new, ticker, day),
            )
        conn.commit()
        return {
            "status": "repaired",
            "repairs": len(fixes),
            "per_ticker": per_ticker,
        }
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--all",
        action="store_true",
        help="repair every crypto ticker instead of the known-thin default set",
    )
    parser.add_argument(
        "--tickers",
        nargs="*",
        help="explicit ticker list (e.g. EGLD/USD ONE/USD)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report divergent rows without writing anything",
    )
    args = parser.parse_args()

    if args.tickers:
        tickers = args.tickers
    elif args.all:
        tickers = sorted({str(t).upper() for t in CRYPTO_TICKERS})
    else:
        tickers = DEFAULT_TICKERS

    result = repair(tickers, dry_run=args.dry_run)
    print(f"[repair] {result}")


if __name__ == "__main__":
    main()
