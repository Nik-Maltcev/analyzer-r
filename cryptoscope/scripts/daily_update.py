#!/usr/bin/env python3
"""Daily market price refresh and pair-analysis recomputation."""

import asyncio
import os
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.data.brazil import fetch_brazil_prices, upsert_brazil_prices
from app.data.fetcher import fetch_batch
from app.data.indonesia import fetch_indonesia_prices, upsert_indonesia_prices
from app.data.moex import (
    fetch_ru_prices,
    latest_ru_start_dates,
    migrate_legacy_ru_ticker,
    reprice_active_ru_favorites,
    upsert_ru_prices,
)
from app.data.us_stocks import fetch_us_stock_prices, upsert_us_stock_prices
from app.data.tickers import (
    BRAZIL_TICKERS,
    CRYPTO_TICKERS,
    INDONESIA_TICKERS,
    RU_TICKERS,
    STOCK_TICKERS,
)

DB_PATH = os.environ.get("DB_PATH", "/data/market.db")
API_KEY = os.environ.get("TWELVEDATA_API_KEY", "")
ENABLED_MARKETS = {
    market.strip()
    for market in os.environ.get(
        "ENABLED_MARKETS",
        "crypto,stocks,ru,br,id",
    ).split(",")
    if market.strip()
}
BRAZIL_HISTORY_YEARS = int(os.environ.get("BRAZIL_HISTORY_YEARS", "3"))
INDONESIA_HISTORY_YEARS = int(os.environ.get("INDONESIA_HISTORY_YEARS", "3"))
US_HISTORY_YEARS = int(os.environ.get("US_HISTORY_YEARS", "3"))
SCRIPT_DIR = Path(__file__).resolve().parent


def update_market(tickers: list, market_name: str, conn: sqlite3.Connection, api_key: str) -> int:
    """Update one market's prices from Twelve Data."""
    if not api_key:
        print(f"  No API key, skipping {market_name}")
        return 0

    rows_added = 0
    tickers_ok = 0
    tickers_fail = 0

    batches = [tickers[i:i + 8] for i in range(0, len(tickers), 8)]
    for idx, batch in enumerate(batches):
        try:
            df = asyncio.run(fetch_batch(batch, api_key, outputsize=5))

            if df.empty:
                tickers_fail += len(batch)
                continue

            for ticker in df["ticker"].unique():
                sub = df[df["ticker"] == ticker]

                # Check existing dates
                existing = pd.read_sql_query(
                    "SELECT date FROM prices WHERE ticker = ? ORDER BY date DESC LIMIT 10",
                    conn, params=(ticker,)
                )
                existing_dates = set(existing["date"].values) if not existing.empty else set()

                # Insert new rows
                new_rows = sub[~sub["date"].isin(existing_dates) & sub["close"].notna() & (sub["close"] > 0)]
                if not new_rows.empty:
                    new_rows = new_rows.copy()
                    new_rows["market"] = market_name
                    new_rows["volume"] = new_rows.get("volume", 0)
                    new_rows[["ticker", "date", "close", "volume", "market"]].to_sql(
                        "prices", conn, if_exists="append", index=False
                    )
                    rows_added += len(new_rows)

                tickers_ok += 1

            if idx < len(batches) - 1:
                time.sleep(75)

        except Exception as e:
            print(f"  Error batch {batch}: {e}")
            tickers_fail += len(batch)
            time.sleep(10)

    # Log to update_log
    conn.execute("""
        INSERT INTO update_log (market, tickers_ok, tickers_fail, rows_added, status, message)
        VALUES (?, ?, ?, ?, 'ok', 'Daily update complete')
    """, (market_name, tickers_ok, tickers_fail, rows_added))
    conn.commit()

    print(f"  [{market_name}] OK={tickers_ok} FAIL={tickers_fail} ROWS={rows_added}")
    return rows_added


def update_adjusted_market(
    conn: sqlite3.Connection,
    market: str,
    display_name: str,
    tickers: list[str],
    history_years: int,
    fetch_fn: Callable,
    upsert_fn: Callable,
) -> int:
    """Refresh a rolling adjusted history for one Yahoo-backed market."""
    try:
        prices = fetch_fn(history_years=history_years)
        rows_written = upsert_fn(conn, prices, tickers)
        tickers_ok = prices["ticker"].nunique() if not prices.empty else 0
        status = "ok" if rows_written else "error"
        message = (
            f"{display_name} adjusted history refreshed"
            if rows_written
            else f"No {display_name} data returned"
        )
    except Exception as exc:
        rows_written = 0
        tickers_ok = 0
        status = "error"
        message = f"{display_name} update failed: {exc}"

    conn.execute(
        """
        INSERT INTO update_log (market, tickers_ok, tickers_fail, rows_added, status, message)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            market,
            tickers_ok,
            max(0, len(tickers) - tickers_ok),
            rows_written,
            status,
            message,
        ),
    )
    conn.commit()
    print(
        f"  [{market}] OK={tickers_ok} "
        f"FAIL={max(0, len(tickers) - tickers_ok)} ROWS={rows_written}"
    )
    return rows_written


def update_ru_market(conn: sqlite3.Connection) -> int:
    """Refresh completed daily candles for MOEX equities."""
    try:
        migrate_legacy_ru_ticker(conn)
        start_dates = latest_ru_start_dates(conn, RU_TICKERS)
        prices = asyncio.run(
            fetch_ru_prices(tickers=RU_TICKERS, start_dates=start_dates)
        )
        rows_written = upsert_ru_prices(conn, prices, RU_TICKERS)
        reprice_active_ru_favorites(conn)
        tickers_ok = prices["ticker"].nunique() if not prices.empty else 0
        status = "ok" if rows_written else "error"
        message = (
            "MOEX delayed daily candles refreshed"
            if rows_written
            else "No MOEX data returned"
        )
    except Exception as exc:
        rows_written = 0
        tickers_ok = 0
        status = "error"
        message = f"MOEX update failed: {exc}"

    conn.execute(
        """
        INSERT INTO update_log (market, tickers_ok, tickers_fail, rows_added, status, message)
        VALUES ('ru', ?, ?, ?, ?, ?)
        """,
        (
            tickers_ok,
            max(0, len(RU_TICKERS) - tickers_ok),
            rows_written,
            status,
            message,
        ),
    )
    conn.commit()
    print(
        f"  [ru] OK={tickers_ok} "
        f"FAIL={max(0, len(RU_TICKERS) - tickers_ok)} ROWS={rows_written}"
    )
    return rows_written


def update_us_market(conn: sqlite3.Connection) -> int:
    """Refresh a rolling adjusted history for US stocks and ETFs."""
    return update_adjusted_market(
        conn,
        "stocks",
        "US",
        STOCK_TICKERS,
        US_HISTORY_YEARS,
        fetch_us_stock_prices,
        upsert_us_stock_prices,
    )


def update_brazil_market(conn: sqlite3.Connection) -> int:
    """Refresh a rolling adjusted history for Brazil B3 equities."""
    return update_adjusted_market(
        conn,
        "br",
        "Brazil",
        BRAZIL_TICKERS,
        BRAZIL_HISTORY_YEARS,
        fetch_brazil_prices,
        upsert_brazil_prices,
    )


def update_indonesia_market(conn: sqlite3.Connection) -> int:
    """Refresh a rolling adjusted history for Indonesia IDX equities."""
    return update_adjusted_market(
        conn,
        "id",
        "Indonesia",
        INDONESIA_TICKERS,
        INDONESIA_HISTORY_YEARS,
        fetch_indonesia_prices,
        upsert_indonesia_prices,
    )


def main():
    conn = sqlite3.connect(DB_PATH)

    total = 0
    if "crypto" in ENABLED_MARKETS and API_KEY:
        total += update_market(CRYPTO_TICKERS, "crypto", conn, API_KEY)
    elif "crypto" in ENABLED_MARKETS:
        print("TWELVEDATA_API_KEY not set, skipping crypto")
    if "stocks" in ENABLED_MARKETS:
        total += update_us_market(conn)
    if "ru" in ENABLED_MARKETS:
        total += update_ru_market(conn)
    if "br" in ENABLED_MARKETS:
        total += update_brazil_market(conn)
    if "id" in ENABLED_MARKETS:
        total += update_indonesia_market(conn)

    conn.close()

    print(f"\nTotal inserted or refreshed rows: {total}")

    # Recompute analysis
    if total > 0:
        print("Recomputing pair analysis...")
        compute_script = os.environ.get("COMPUTE_ANALYSIS_PATH") or str(SCRIPT_DIR / "compute_analysis.py")
        result = subprocess.run([sys.executable, compute_script], check=False)
        if result.returncode != 0:
            print(f"compute_analysis failed with exit code {result.returncode}")


if __name__ == "__main__":
    main()
