#!/usr/bin/env python3
"""Daily market price refresh and pair-analysis recomputation."""

import asyncio
import os
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.api.public_extension import refresh_extension_feed_snapshots
from app.core.crypto_v2 import sync_crypto_v2_journal
from app.core.market_regime import sync_market_regime_snapshots
from app.core.momentum_portfolio import sync_momentum_portfolio_journal
from app.core.scanner_history import sync_all_scanner_states
from app.data.brazil import fetch_brazil_prices, upsert_brazil_prices
from app.data.indonesia import fetch_indonesia_prices, upsert_indonesia_prices
from app.data.international import (
    INTERNATIONAL_MARKETS,
    fetch_international_prices,
    upsert_international_prices,
)
from app.data.mexc import refresh_mexc_crypto_market
from app.data.moex import (
    fetch_ru_prices,
    latest_ru_start_dates,
    migrate_legacy_ru_ticker,
    reprice_active_ru_favorites,
    upsert_ru_prices,
)
from app.data.tickers import (
    ALL_MARKETS,
    BRAZIL_TICKERS,
    CRYPTO_TICKERS,
    INDONESIA_TICKERS,
    RU_TICKERS,
    STOCK_TICKERS,
)
from app.data.us_stocks import fetch_us_stock_prices, upsert_us_stock_prices

DB_PATH = os.environ.get("DB_PATH", "/data/market.db")
ENABLED_MARKETS = {
    market.strip()
    for market in os.environ.get(
        "ENABLED_MARKETS",
        "crypto,stocks,ru,br,id,au",
    ).split(",")
    if market.strip() in ALL_MARKETS
}
BRAZIL_HISTORY_YEARS = int(os.environ.get("BRAZIL_HISTORY_YEARS", "3"))
INDONESIA_HISTORY_YEARS = int(os.environ.get("INDONESIA_HISTORY_YEARS", "3"))
US_HISTORY_YEARS = int(os.environ.get("US_HISTORY_YEARS", "3"))
SCRIPT_DIR = Path(__file__).resolve().parent


def update_crypto_market(conn: sqlite3.Connection) -> int:
    """Refresh completed MEXC daily candles and atomically activate them."""
    try:
        result = asyncio.run(
            refresh_mexc_crypto_market(conn, CRYPTO_TICKERS)
        )
        rows_written = int(result["staged_rows_written"])
        tickers_ok = int(result["tickers"])
        unavailable = list(result["unavailable_tickers"])
        status = "ok"
        message = (
            f"MEXC active: {result['tickers']} tickers, "
            f"{result['rows']} rows through {result['latest_date']}; "
            f"unavailable={','.join(unavailable) or 'none'}"
        )
        print(
            f"  [crypto/MEXC] ACTIVE={result['tickers']} "
            f"ROWS={result['rows']} LATEST={result['latest_date']}"
        )
        if unavailable:
            print(f"  [crypto/MEXC] unavailable: {', '.join(unavailable)}")
    except Exception as exc:
        rows_written = 0
        tickers_ok = 0
        unavailable = list(CRYPTO_TICKERS)
        status = "error"
        message = f"MEXC update/cutover failed; legacy data retained: {exc}"
        print(f"  [crypto/MEXC] {message}")

    conn.execute(
        """
        INSERT INTO update_log (
            market, tickers_ok, tickers_fail, rows_added, status, message
        ) VALUES ('crypto', ?, ?, ?, ?, ?)
        """,
        (
            tickers_ok,
            max(0, len(CRYPTO_TICKERS) - tickers_ok),
            rows_written,
            status,
            message,
        ),
    )
    conn.commit()
    if status != "ok":
        raise RuntimeError(message)
    return rows_written


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


def update_international_market(
    conn: sqlite3.Connection,
    market: str,
) -> int:
    """Refresh a rolling adjusted history for an administrator-only market."""
    config = INTERNATIONAL_MARKETS[market]
    return update_adjusted_market(
        conn,
        config.market,
        config.label,
        config.tickers,
        int(os.environ.get("INTERNATIONAL_HISTORY_YEARS", "3")),
        lambda **kwargs: fetch_international_prices(
            config.market,
            **kwargs,
        ),
        lambda database, prices, tickers: upsert_international_prices(
            database,
            config.market,
            prices,
            tickers,
        ),
    )


def main() -> int:
    conn = sqlite3.connect(DB_PATH)

    total = 0
    if "crypto" in ENABLED_MARKETS:
        total += update_crypto_market(conn)
    if "stocks" in ENABLED_MARKETS:
        total += update_us_market(conn)
    if "ru" in ENABLED_MARKETS:
        total += update_ru_market(conn)
    if "br" in ENABLED_MARKETS:
        total += update_brazil_market(conn)
    if "id" in ENABLED_MARKETS:
        total += update_indonesia_market(conn)
    for market in INTERNATIONAL_MARKETS:
        if market in ENABLED_MARKETS:
            total += update_international_market(conn, market)

    conn.close()

    print(f"\nTotal inserted or refreshed rows: {total}")

    try:
        asyncio.run(sync_all_scanner_states(DB_PATH, ENABLED_MARKETS))
        print("Scanner signal history updated")
    except Exception as exc:
        print(f"Scanner signal history update failed: {exc}")
        return 1

    if "crypto" in ENABLED_MARKETS:
        try:
            portfolio_result = asyncio.run(
                sync_momentum_portfolio_journal(DB_PATH)
            )
            print(f"Momentum portfolio journal: {portfolio_result}")
        except Exception as exc:
            print(f"Momentum portfolio journal update failed: {exc}")
            return 1
        try:
            crypto_v2_result = asyncio.run(sync_crypto_v2_journal(DB_PATH))
            print(f"Crypto V2 journal: {crypto_v2_result}")
        except Exception as exc:
            print(f"Crypto V2 journal update failed: {exc}")
            return 1
        try:
            regime_result = asyncio.run(
                sync_market_regime_snapshots(DB_PATH)
            )
            print(f"Market regime snapshots: {regime_result}")
        except Exception as exc:
            print(f"Market regime snapshot update failed: {exc}")
            return 1

    # Recompute even when providers returned no new rows. This verifies the
    # existing dataset instead of reporting success with stale/broken pairs.
    print("Recomputing pair analysis...")
    compute_script = os.environ.get("COMPUTE_ANALYSIS_PATH") or str(SCRIPT_DIR / "compute_analysis.py")
    result = subprocess.run([sys.executable, compute_script], check=False)
    if result.returncode != 0:
        print(f"compute_analysis failed with exit code {result.returncode}")
        return result.returncode

    try:
        refreshed = asyncio.run(
            refresh_extension_feed_snapshots(
                ENABLED_MARKETS,
                db_path=DB_PATH,
            )
        )
        print(f"Extension feed snapshots refreshed: {refreshed}")
    except Exception as exc:
        print(f"Extension feed snapshot refresh failed: {exc}")
        return 1

    print("Running crypto content automation...")
    content_script = SCRIPT_DIR / "run_content_automation.py"
    result = subprocess.run(
        [sys.executable, str(content_script), "--main-only"],
        check=False,
    )
    if result.returncode != 0:
        print(f"Content automation failed with exit code {result.returncode}")
        return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
