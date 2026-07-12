"""Async SQLite database layer."""

from contextlib import asynccontextmanager
from typing import Any

import aiosqlite
import numpy as np
import pandas as pd

from app.config import get_settings
from app.core.cointegration import fit_fixed_zscore_model
from app.db.schema import (
    ALL_INDICES_SQL,
    ALL_TABLES_SQL,
    FAVORITE_COLUMN_MIGRATIONS,
    PAIR_COLUMN_MIGRATIONS,
    PAYMENT_ORDER_COLUMN_MIGRATIONS,
)

DB_PATH = "/data/market.db"


def set_db_path(path: str):
    global DB_PATH
    DB_PATH = path


@asynccontextmanager
async def get_connection(db_path: str | None = None):
    """Get async SQLite connection context manager."""
    path = db_path or DB_PATH
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    try:
        yield conn
    finally:
        await conn.close()


def get_sync_connection(db_path: str | None = None):
    """Get synchronous SQLite connection (for scripts)."""
    import sqlite3
    path = db_path or DB_PATH
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


async def init_db(db_path: str | None = None):
    """Create all tables and indexes if they don't exist."""
    async with get_connection(db_path) as conn:
        for sql in ALL_TABLES_SQL:
            await conn.execute(sql)
        cursor = await conn.execute("PRAGMA table_info(pairs)")
        pair_columns = {row["name"] for row in await cursor.fetchall()}
        if "signal_started_at" not in pair_columns:
            await conn.execute("ALTER TABLE pairs ADD COLUMN signal_started_at TEXT")
        for column, definition in PAIR_COLUMN_MIGRATIONS.items():
            if column not in pair_columns:
                await conn.execute(
                    f"ALTER TABLE pairs ADD COLUMN {column} {definition}"
                )
        await conn.execute(
            """
            UPDATE pairs
            SET is_coint_stable = COALESCE(is_coint, 0),
                coint_stability = CASE WHEN is_coint = 1 THEN 100 ELSE 0 END,
                coint_windows = CASE
                    WHEN is_coint = 1 THEN '{"legacy":true}'
                    ELSE '{"legacy":false}'
                END
            WHERE market != 'ru' AND coint_windows IS NULL
            """
        )
        await conn.execute(
            """
            UPDATE pairs
            SET signal = 'Наблюдение: требуется свежий расчёт',
                signal_type = 'wait',
                strength = 'Наблюдение',
                signal_eligible = 0,
                risk_reason = 'Защитные метрики ещё не рассчитаны'
            WHERE market = 'ru' AND coint_windows IS NULL
            """
        )
        cursor = await conn.execute("PRAGMA table_info(favorites)")
        favorite_columns = {row["name"] for row in await cursor.fetchall()}
        if "market" not in favorite_columns:
            await conn.execute(
                "ALTER TABLE favorites ADD COLUMN market TEXT DEFAULT 'crypto'"
            )
        for column, definition in FAVORITE_COLUMN_MIGRATIONS.items():
            if column not in favorite_columns:
                await conn.execute(
                    f"ALTER TABLE favorites ADD COLUMN {column} {definition}"
                )
        await conn.execute(
            """
            UPDATE favorites
            SET position_kind = COALESCE(position_kind, 'pair'),
                source = COALESCE(source, 'signal')
            """
        )
        await conn.execute(
            """
            UPDATE favorites
            SET market = COALESCE((
                SELECT market
                FROM pairs
                WHERE pairs.ticker_a = favorites.ticker_a
                  AND pairs.ticker_b = favorites.ticker_b
                LIMIT 1
            ), market, 'crypto')
            """
        )
        cursor = await conn.execute("PRAGMA table_info(auth_users)")
        auth_user_columns = {row["name"] for row in await cursor.fetchall()}
        if "trial_started_at" not in auth_user_columns:
            await conn.execute(
                "ALTER TABLE auth_users ADD COLUMN trial_started_at TEXT"
            )
        if "trial_ends_at" not in auth_user_columns:
            await conn.execute(
                "ALTER TABLE auth_users ADD COLUMN trial_ends_at TEXT"
            )
        await conn.execute(
            """
            UPDATE auth_users
            SET trial_started_at = COALESCE(trial_started_at, datetime('now')),
                trial_ends_at = COALESCE(
                    trial_ends_at,
                    datetime('now', '+' || ? || ' days')
                )
            WHERE trial_started_at IS NULL OR trial_ends_at IS NULL
            """,
            (max(1, int(get_settings().trial_days)),),
        )
        cursor = await conn.execute("PRAGMA table_info(auth_magic_links)")
        magic_link_columns = {row["name"] for row in await cursor.fetchall()}
        if "redirect_path" not in magic_link_columns:
            await conn.execute(
                "ALTER TABLE auth_magic_links ADD COLUMN redirect_path TEXT"
            )
        cursor = await conn.execute("PRAGMA table_info(payment_orders)")
        payment_order_columns = {
            row["name"] for row in await cursor.fetchall()
        }
        for column, definition in PAYMENT_ORDER_COLUMN_MIGRATIONS.items():
            if column not in payment_order_columns:
                await conn.execute(
                    f"ALTER TABLE payment_orders ADD COLUMN {column} {definition}"
                )
        for sql in ALL_INDICES_SQL:
            await conn.execute(sql)
        await conn.commit()


async def fetch_prices(conn: aiosqlite.Connection, market: str | None = None) -> pd.DataFrame:
    """Fetch price data, optionally filtered by market."""
    if market:
        query = "SELECT ticker, date, close FROM prices WHERE market = ? ORDER BY ticker, date"
        cursor = await conn.execute(query, (market,))
    else:
        query = "SELECT ticker, date, close FROM prices ORDER BY ticker, date"
        cursor = await conn.execute(query)

    rows = await cursor.fetchall()
    if not rows:
        return pd.DataFrame(columns=["ticker", "date", "close"])

    return pd.DataFrame([dict(r) for r in rows], columns=["ticker", "date", "close"])


async def fetch_pairs(conn: aiosqlite.Connection, market: str | None = None, min_corr: float = 0.0) -> pd.DataFrame:
    """Fetch precomputed pair analysis."""
    if market:
        query = """
            SELECT * FROM pairs
            WHERE market = ? AND corr >= ?
            ORDER BY score DESC
        """
        cursor = await conn.execute(query, (market, min_corr))
    else:
        query = "SELECT * FROM pairs WHERE corr >= ? ORDER BY score DESC"
        cursor = await conn.execute(query, (min_corr,))

    rows = await cursor.fetchall()
    if not rows:
        return pd.DataFrame()

    return pd.DataFrame([dict(r) for r in rows])


async def fetch_active_pairs(
    conn: aiosqlite.Connection,
    market: str,
) -> pd.DataFrame:
    """Fetch stored active signals without applying UI candidate filters."""
    cursor = await conn.execute(
        """
        SELECT * FROM pairs
        WHERE market = ? AND signal_type != 'wait'
        ORDER BY score DESC
        """,
        (market,),
    )
    rows = await cursor.fetchall()
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame([dict(row) for row in rows])


async def fetch_favorites(conn: aiosqlite.Connection, user_id: str = "local") -> pd.DataFrame:
    """Fetch user's favorites."""
    cursor = await conn.execute(
        "SELECT * FROM favorites WHERE user_id = ? AND status = 'active' ORDER BY entry_time DESC",
        (user_id,)
    )
    rows = await cursor.fetchall()
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame([dict(r) for r in rows])


async def fetch_favorites_history(
    conn: aiosqlite.Connection,
    user_id: str = "local",
    limit: int | None = None,
) -> pd.DataFrame:
    """Fetch closed favorites history."""
    if limit is not None and int(limit) > 0:
        cursor = await conn.execute(
            """
            SELECT * FROM favorites
            WHERE user_id = ? AND status = 'closed'
            ORDER BY exit_time DESC
            LIMIT ?
            """,
            (user_id, int(limit)),
        )
    else:
        cursor = await conn.execute(
            """
            SELECT * FROM favorites
            WHERE user_id = ? AND status = 'closed'
            ORDER BY exit_time DESC
            """,
            (user_id,),
        )
    rows = await cursor.fetchall()
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame([dict(r) for r in rows])


def _valid_fixed_model(row) -> bool:
    try:
        values = (
            float(row.get("hedge_ratio_entry")),
            float(row.get("spread_mean_entry")),
            float(row.get("spread_sd_entry")),
        )
        return all(np.isfinite(value) for value in values) and values[2] > 0
    except (TypeError, ValueError):
        return False


async def build_favorite_z_model(
    conn: aiosqlite.Connection,
    market: str,
    ticker_a: str,
    ticker_b: str,
    z_at_entry=None,
    price_a_entry=None,
    price_b_entry=None,
) -> dict[str, Any]:
    """Build a fixed model from date-aligned prices and anchor it at entry."""
    cursor = await conn.execute(
        """
        SELECT hedge_ratio
        FROM pairs
        WHERE market = ? AND ticker_a = ? AND ticker_b = ?
        LIMIT 1
        """,
        (market, ticker_a, ticker_b),
    )
    pair_row = await cursor.fetchone()
    hedge_ratio = pair_row["hedge_ratio"] if pair_row else None

    cursor = await conn.execute(
        """
        SELECT pa.close AS close_a, pb.close AS close_b
        FROM prices AS pa
        JOIN prices AS pb
          ON pb.date = pa.date
         AND pb.market = pa.market
        WHERE pa.market = ?
          AND pa.ticker = ?
          AND pb.ticker = ?
        ORDER BY pa.date
        """,
        (market, ticker_a, ticker_b),
    )
    rows = await cursor.fetchall()
    if not rows:
        return {}

    prices_a = np.asarray([row["close_a"] for row in rows], dtype=float)
    prices_b = np.asarray([row["close_b"] for row in rows], dtype=float)
    if market == "ru":
        prices_a = prices_a[-252:]
        prices_b = prices_b[-252:]

    return fit_fixed_zscore_model(
        prices_a,
        prices_b,
        z_at_entry=z_at_entry,
        price_a_entry=price_a_entry,
        price_b_entry=price_b_entry,
        hedge_ratio=hedge_ratio,
    )


async def ensure_favorite_z_model(
    conn: aiosqlite.Connection,
    favorite,
) -> dict[str, Any]:
    """Backfill a fixed model for a favorite created before model snapshots."""
    if favorite.get("position_kind", "pair") == "single":
        return {}
    if _valid_fixed_model(favorite):
        return {
            key: favorite.get(key)
            for key in FAVORITE_COLUMN_MIGRATIONS
        }

    model = await build_favorite_z_model(
        conn,
        favorite.get("market") or "crypto",
        favorite["ticker_a"],
        favorite["ticker_b"],
        z_at_entry=favorite.get("z_at_entry"),
        price_a_entry=favorite.get("price_a_entry"),
        price_b_entry=favorite.get("price_b_entry"),
    )
    if not model:
        return {}

    await conn.execute(
        """
        UPDATE favorites
        SET z_at_entry = ?,
            hedge_ratio_entry = ?,
            spread_mean_entry = ?,
            spread_sd_entry = ?
        WHERE id = ?
        """,
        (
            model.get("z_at_entry"),
            model["hedge_ratio_entry"],
            model["spread_mean_entry"],
            model["spread_sd_entry"],
            int(favorite["id"]),
        ),
    )
    await conn.commit()
    return model


async def toggle_favorite(conn: aiosqlite.Connection, pair: str, ticker_a: str, ticker_b: str,
                          user_id: str = "local", market: str = "crypto", **kwargs) -> dict[str, Any]:
    """Toggle a favorite: add if not exists, remove if exists."""
    cursor = await conn.execute(
        """
        SELECT id FROM favorites
        WHERE pair = ? AND user_id = ? AND status = 'active'
          AND COALESCE(market, 'crypto') = ?
        """,
        (pair, user_id, market)
    )
    existing = await cursor.fetchone()

    if existing:
        await conn.execute("DELETE FROM favorites WHERE id = ?", (existing["id"],))
        await conn.commit()
        return {"action": "removed", "pair": pair}

    price_a = kwargs.get("price_a_entry", 0) or 0
    price_b = kwargs.get("price_b_entry", 0) or 0
    position_kind = kwargs.get("position_kind", "pair")
    source = kwargs.get("source", "signal")
    if position_kind not in {"pair", "single"}:
        position_kind = "pair"
    if position_kind == "single":
        ticker_b = ""
        price_b = 0

    # If entry prices are 0, look them up from DB
    price_lookups = []
    if price_a == 0:
        price_lookups.append((ticker_a, "price_a_entry"))
    if position_kind == "pair" and price_b == 0:
        price_lookups.append((ticker_b, "price_b_entry"))
    for ticker, key in price_lookups:
        if ticker:
            cursor2 = await conn.execute(
                """
                SELECT close FROM prices
                WHERE ticker = ? AND market = ?
                ORDER BY date DESC LIMIT 1
                """,
                (ticker, market)
            )
            row2 = await cursor2.fetchone()
            if row2 and row2[0]:
                if key == "price_a_entry":
                    price_a = float(row2[0])
                else:
                    price_b = float(row2[0])

    model = {}
    if position_kind == "pair":
        model = await build_favorite_z_model(
            conn,
            market,
            ticker_a,
            ticker_b,
            z_at_entry=kwargs.get("z_at_entry"),
            price_a_entry=price_a,
            price_b_entry=price_b,
        )
    z_at_entry = model.get("z_at_entry", kwargs.get("z_at_entry", 0))

    await conn.execute("""
        INSERT INTO favorites (pair, market, position_kind, source,
                              ticker_a, ticker_b, signal, signal_type, z_at_entry,
                              hedge_ratio_entry, spread_mean_entry, spread_sd_entry,
                              price_a_entry, price_b_entry, entry_time, status, halflife, corr, user_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), 'active', ?, ?, ?)
    """, (
        pair, market, position_kind, source, ticker_a, ticker_b,
        kwargs.get("signal", ""),
        kwargs.get("signal_type", "wait"),
        z_at_entry,
        model.get("hedge_ratio_entry"),
        model.get("spread_mean_entry"),
        model.get("spread_sd_entry"),
        price_a,
        price_b,
        kwargs.get("halflife"),
        kwargs.get("corr", 0),
        user_id,
    ))
    await conn.commit()
    return {"action": "added", "pair": pair, "entry_a": price_a, "entry_b": price_b}


async def close_favorite(conn: aiosqlite.Connection, fav_id: int, exit_price_a: float,
                         exit_price_b: float, exit_pnl_pct: float,
                         user_id: str | None = None,
                         exit_net_pnl: float | None = None,
                         exit_net_return_pct: float | None = None,
                         exit_pair_move_pct: float | None = None,
                         exit_total_cost: float | None = None,
                         close_capital: float | None = None) -> dict[str, Any]:
    """Close an active favorite position."""
    if user_id is None:
        cursor = await conn.execute("""
            UPDATE favorites SET status = 'closed', exit_time = datetime('now'),
                exit_price_a = ?, exit_price_b = ?, exit_pnl_pct = ?,
                exit_net_pnl = ?, exit_net_return_pct = ?,
                exit_pair_move_pct = ?, exit_total_cost = ?,
                close_capital = ?
            WHERE id = ?
        """, (
            exit_price_a, exit_price_b, exit_pnl_pct,
            exit_net_pnl, exit_net_return_pct, exit_pair_move_pct,
            exit_total_cost, close_capital, fav_id,
        ))
    else:
        cursor = await conn.execute("""
            UPDATE favorites SET status = 'closed', exit_time = datetime('now'),
                exit_price_a = ?, exit_price_b = ?, exit_pnl_pct = ?,
                exit_net_pnl = ?, exit_net_return_pct = ?,
                exit_pair_move_pct = ?, exit_total_cost = ?,
                close_capital = ?
            WHERE id = ? AND user_id = ?
        """, (
            exit_price_a, exit_price_b, exit_pnl_pct,
            exit_net_pnl, exit_net_return_pct, exit_pair_move_pct,
            exit_total_cost, close_capital, fav_id, user_id,
        ))
    await conn.commit()
    return {"action": "closed", "id": fav_id, "updated": cursor.rowcount == 1}


async def delete_favorite(
    conn: aiosqlite.Connection,
    fav_id: int,
    user_id: str | None = None,
) -> dict[str, Any]:
    """Delete a favorite from history."""
    if user_id is None:
        cursor = await conn.execute(
            "DELETE FROM favorites WHERE id = ?",
            (fav_id,),
        )
    else:
        cursor = await conn.execute(
            "DELETE FROM favorites WHERE id = ? AND user_id = ?",
            (fav_id, user_id),
        )
    await conn.commit()
    return {"action": "deleted", "id": fav_id, "deleted": cursor.rowcount == 1}


async def db_status(conn: aiosqlite.Connection) -> dict[str, Any]:
    """Get database status summary."""
    status = {}

    cursor = await conn.execute("SELECT COUNT(DISTINCT ticker) as n FROM prices")
    row = await cursor.fetchone()
    status["n_tickers"] = row["n"] if row else 0

    cursor = await conn.execute("SELECT COUNT(*) as n FROM prices")
    row = await cursor.fetchone()
    status["n_rows"] = row["n"] if row else 0

    cursor = await conn.execute("SELECT MIN(date) as d1, MAX(date) as d2 FROM prices")
    row = await cursor.fetchone()
    status["date_min"] = row["d1"]
    status["date_max"] = row["d2"]

    cursor = await conn.execute("SELECT COUNT(*) as n FROM pairs")
    row = await cursor.fetchone()
    status["n_pairs"] = row["n"] if row else 0

    cursor = await conn.execute("SELECT COUNT(*) as n FROM pairs WHERE is_coint = 1")
    row = await cursor.fetchone()
    status["n_coint"] = row["n"] if row else 0

    cursor = await conn.execute("SELECT COUNT(*) as n FROM pairs WHERE signal_type != 'wait'")
    row = await cursor.fetchone()
    status["n_active_signals"] = row["n"] if row else 0

    cursor = await conn.execute(
        """
        SELECT market, COUNT(*) AS n
        FROM pairs
        WHERE signal_type != 'wait'
        GROUP BY market
        ORDER BY market
        """
    )
    status["active_signals_by_market"] = {
        row["market"]: row["n"] for row in await cursor.fetchall()
    }

    cursor = await conn.execute(
        """
        SELECT market,
               COUNT(*) AS pairs,
               SUM(CASE WHEN is_coint = 1 THEN 1 ELSE 0 END) AS coint,
               SUM(CASE WHEN is_coint_stable = 1 THEN 1 ELSE 0 END) AS stable,
               SUM(CASE WHEN ABS(COALESCE(z_now, 0)) >= 2 THEN 1 ELSE 0 END) AS deviated,
               SUM(CASE
                   WHEN is_coint_stable = 1 AND ABS(COALESCE(z_now, 0)) >= 2
                   THEN 1 ELSE 0
               END) AS stable_deviated,
               SUM(CASE WHEN signal_type != 'wait' THEN 1 ELSE 0 END) AS active,
               MAX(computed_at) AS computed_at
        FROM pairs
        GROUP BY market
        ORDER BY market
        """
    )
    status["pair_diagnostics_by_market"] = {
        row["market"]: {
            "pairs": row["pairs"],
            "coint": row["coint"],
            "stable": row["stable"],
            "deviated": row["deviated"],
            "stable_deviated": row["stable_deviated"],
            "active": row["active"],
            "computed_at": row["computed_at"],
        }
        for row in await cursor.fetchall()
    }

    cursor = await conn.execute(
        """
        SELECT market, COALESCE(coint_windows, 'null') AS pattern, COUNT(*) AS n
        FROM pairs
        WHERE is_coint = 1
        GROUP BY market, COALESCE(coint_windows, 'null')
        ORDER BY market, n DESC
        """
    )
    stability_patterns: dict[str, dict[str, int]] = {}
    for row in await cursor.fetchall():
        stability_patterns.setdefault(row["market"], {})[row["pattern"]] = row["n"]
    status["stability_patterns_by_market"] = stability_patterns

    cursor = await conn.execute("SELECT MAX(computed_at) as last_analysis FROM pairs")
    row = await cursor.fetchone()
    status["last_analysis"] = row["last_analysis"]

    cursor = await conn.execute("SELECT timestamp, market FROM update_log ORDER BY id DESC LIMIT 1")
    row = await cursor.fetchone()
    status["last_update"] = row["timestamp"] if row else None
    status["last_market"] = row["market"] if row else None

    return status
