"""Forward paper-trade journal for the time-series relative-strength scheme.

Each week, for every crypto coin: long if its own trailing ``LOOKBACK``-day
return is positive (paired with a short BTC hedge), short if negative (long BTC
hedge). Equal weight, gross exposure normalized to 1. Per-leg PnL is the
direction-signed excess return versus BTC: ``direction * (coin_ret - btc_ret)``.

This is the construct that stayed positive across 2023-2026 INCLUDING the 2026
regime that broke cross-sectional momentum (see project memory
``ts-momentum-relative-strength``). It is a relative-strength bet versus BTC, not
a bet on market direction.

Forward-only: the journal records each weekly basket at the DB close on the
rebalance date and closes it ``HOLD_DAYS`` later at the then-current close. No
historical backfill — the point is live out-of-sample confirmation.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any

import pandas as pd

from app.db.schema import (
    CREATE_TS_MOMENTUM_FORWARD_INDICES,
    CREATE_TS_MOMENTUM_FORWARD_JOURNAL,
)

CALCULATION_VERSION = "tsmom-relstrength-v1"
LOOKBACK = 30
HOLD_DAYS = 7
MIN_UNIVERSE = 8
STAKE = 100.0


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _btc_column(columns: list[str]) -> str | None:
    return next(
        (c for c in columns if str(c).upper().split("/", 1)[0] == "BTC"),
        None,
    )


def compute_relative_strength_basket(
    prices: pd.DataFrame,
    lookback: int = LOOKBACK,
) -> dict[str, Any]:
    """Rank each coin by its own trailing return; long positives, short negatives.

    ``prices`` — DataFrame[ticker, date, close] (market='crypto'). Pure function:
    no network, no DB. Returns the weekly basket with equal weights and the BTC
    hedge reference price.
    """
    if prices is None or prices.empty:
        return {"status": "no_data"}

    wide = prices.pivot(index="date", columns="ticker", values="close").sort_index()
    if len(wide) < lookback + 1:
        return {"status": "insufficient_history", "rows": int(len(wide))}

    btc_ticker = _btc_column(list(wide.columns))
    if btc_ticker is None:
        return {"status": "no_btc"}

    as_of = str(wide.index[-1])[:10]
    last = wide.iloc[-1]
    base = wide.iloc[-1 - lookback]

    btc_entry = _finite(last.get(btc_ticker))
    btc_base = _finite(base.get(btc_ticker))
    if btc_entry <= 0 or btc_base <= 0:
        return {"status": "no_btc_price"}

    legs: list[dict[str, Any]] = []
    for ticker in wide.columns:
        older = _finite(base.get(ticker))
        newer = _finite(last.get(ticker))
        if older <= 0 or newer <= 0:
            continue
        own_return = newer / older - 1.0
        legs.append({
            "ticker": str(ticker),
            "direction": "long" if own_return > 0 else "short",
            "own_return_pct": round(own_return * 100.0, 4),
            "entry_price": newer,
        })

    if len(legs) < MIN_UNIVERSE:
        return {"status": "insufficient_universe", "universe": len(legs)}

    weight = 1.0 / len(legs)
    for leg in legs:
        leg["weight"] = weight

    try:
        next_rebalance = (
            date.fromisoformat(as_of) + timedelta(days=HOLD_DAYS)
        ).isoformat()
    except ValueError:
        next_rebalance = ""

    return {
        "status": "ok",
        "as_of": as_of,
        "lookback": lookback,
        "hold_days": HOLD_DAYS,
        "universe": len(legs),
        "btc_ticker": str(btc_ticker),
        "btc_entry_price": btc_entry,
        "legs": legs,
        "next_rebalance": next_rebalance,
        "long_legs": sum(1 for leg in legs if leg["direction"] == "long"),
        "short_legs": sum(1 for leg in legs if leg["direction"] == "short"),
    }


async def ensure_ts_momentum_schema(conn) -> None:
    await conn.execute(CREATE_TS_MOMENTUM_FORWARD_JOURNAL)
    for statement in CREATE_TS_MOMENTUM_FORWARD_INDICES:
        await conn.execute(statement)


def _last_close_by_ticker(prices: pd.DataFrame) -> dict[str, float]:
    """Most recent finite close per ticker (prices may be unsorted)."""
    out: dict[str, float] = {}
    if prices is None or prices.empty:
        return out
    frame = prices.copy()
    frame["date"] = frame["date"].astype(str)
    frame = frame.sort_values("date")
    for row in frame.itertuples(index=False):
        close = _finite(getattr(row, "close"))
        if close > 0:
            out[str(getattr(row, "ticker"))] = close
    return out


async def _close_due_positions(
    conn,
    today: str,
    last_close: dict[str, float],
    btc_ticker: str,
) -> int:
    """Close active baskets whose hold has elapsed, at the current close."""
    today_d = date.fromisoformat(today)
    cursor = await conn.execute(
        """
        SELECT id, rebalance_date, ticker, direction, weight,
               entry_price, btc_entry_price
        FROM ts_momentum_forward_journal
        WHERE calculation_version = ? AND status = 'active'
        """,
        (CALCULATION_VERSION,),
    )
    rows = [dict(r) for r in await cursor.fetchall()]

    btc_exit = last_close.get(btc_ticker, 0.0)
    closed = 0
    for row in rows:
        try:
            opened = date.fromisoformat(str(row["rebalance_date"])[:10])
        except ValueError:
            continue
        if (today_d - opened).days < HOLD_DAYS:
            continue
        exit_price = last_close.get(str(row["ticker"]), 0.0)
        btc_entry = _finite(row["btc_entry_price"])
        entry = _finite(row["entry_price"])
        if exit_price <= 0 or btc_exit <= 0 or entry <= 0 or btc_entry <= 0:
            continue
        excess = (exit_price / entry) - (btc_exit / btc_entry)
        signed = excess if row["direction"] == "long" else -excess
        return_pct = signed * 100.0
        cash = _finite(row.get("weight"), 0.0) * STAKE * signed
        await conn.execute(
            """
            UPDATE ts_momentum_forward_journal
            SET closed_on = ?, exit_price = ?, btc_exit_price = ?,
                return_pct = ?, cash_result = ?, status = 'closed',
                updated_at = datetime('now')
            WHERE id = ?
            """,
            (today, exit_price, btc_exit, round(return_pct, 6),
             round(cash, 6), row["id"]),
        )
        closed += 1
    return closed


async def _open_new_basket(conn, basket: dict[str, Any]) -> int:
    """Insert this week's basket if it was not already opened (idempotent)."""
    as_of = basket["as_of"]
    btc_ticker = basket["btc_ticker"]
    btc_entry = basket["btc_entry_price"]
    opened = 0
    for leg in basket["legs"]:
        cursor = await conn.execute(
            """
            INSERT OR IGNORE INTO ts_momentum_forward_journal (
                calculation_version, rebalance_date, ticker, direction, weight,
                entry_price, btc_ticker, btc_entry_price, own_return_pct, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
            """,
            (CALCULATION_VERSION, as_of, leg["ticker"], leg["direction"],
             leg["weight"], leg["entry_price"], btc_ticker, btc_entry,
             leg["own_return_pct"]),
        )
        opened += cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
    return opened


async def sync_ts_momentum_forward(
    db_path: str | None = None,
    as_of: str | None = None,
) -> dict[str, Any]:
    """Advance the forward journal: close due baskets, open a new one weekly.

    Safe to call daily. A new basket is opened only when at least ``HOLD_DAYS``
    have passed since the last opened rebalance (or none exists yet).
    """
    from app.db.database import fetch_prices, get_connection

    async with get_connection(db_path) as conn:
        await ensure_ts_momentum_schema(conn)
        prices = await fetch_prices(conn, "crypto")
        basket = compute_relative_strength_basket(prices)
        if basket.get("status") != "ok":
            return {
                "status": basket.get("status"),
                "opened": 0,
                "closed": 0,
            }

        today = as_of or basket["as_of"]
        last_close = _last_close_by_ticker(prices)

        closed = await _close_due_positions(
            conn, today, last_close, basket["btc_ticker"]
        )

        cursor = await conn.execute(
            """
            SELECT MAX(rebalance_date) AS last_date
            FROM ts_momentum_forward_journal
            WHERE calculation_version = ?
            """,
            (CALCULATION_VERSION,),
        )
        row = await cursor.fetchone()
        last_date = row["last_date"] if row else None

        should_open = True
        if last_date:
            try:
                should_open = (
                    date.fromisoformat(today)
                    >= date.fromisoformat(str(last_date)[:10])
                    + timedelta(days=HOLD_DAYS)
                )
            except ValueError:
                should_open = True

        opened = await _open_new_basket(conn, basket) if should_open else 0
        await conn.commit()

        return {
            "status": "ok",
            "as_of": today,
            "universe": basket["universe"],
            "long_legs": basket["long_legs"],
            "short_legs": basket["short_legs"],
            "opened": opened,
            "closed": closed,
            "next_rebalance": basket["next_rebalance"],
        }
