"""Favorites API endpoints."""

import math

from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth import AuthUser, require_current_or_legacy_user
from app.core.signals import estimate_signal_timing
from app.db.database import (
    close_favorite,
    delete_favorite,
    fetch_favorites,
    fetch_favorites_history,
    get_connection,
    toggle_favorite,
)

router = APIRouter(prefix="/favorites", tags=["favorites"])


def _query_float(value, default=0.0):
    if value is None:
        return default
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def _query_int(value, default=None):
    f = _query_float(value, None)
    return int(f) if f is not None else default


def _get_current_price(ticker: str, db_prices: dict, market: str) -> float:
    """Get current price: try Binance WS first, fall back to DB."""
    if market == "crypto":
        try:
            from app.data.binance_ws import get_live_price
            live = get_live_price(ticker)
            if live is not None and live > 0:
                return float(live)
        except ImportError:
            pass
    return float(db_prices.get((market, ticker), 0) or 0)


def _pair_pnl(signal_type, entry_a, entry_b, price_a_now, price_b_now) -> float:
    pnl_a = (
        (price_a_now / entry_a - 1) * 100
        if entry_a and entry_a > 0 and price_a_now
        else 0
    )
    pnl_b = (
        (price_b_now / entry_b - 1) * 100
        if entry_b and entry_b > 0 and price_b_now
        else 0
    )
    if signal_type == "short_a":
        return -pnl_a + pnl_b
    if signal_type == "long_a":
        return pnl_a - pnl_b
    return 0


@router.get("/live-status")
async def live_prices_status():
    """Check Binance WS connection status."""
    try:
        from app.data.binance_ws import get_all_live_tickers, get_uptime, is_connected, live_prices
        return {
            "connected": is_connected(),
            "uptime_seconds": round(get_uptime(), 1),
            "symbols_tracked": len(live_prices),
            "tickers_tracked": len(get_all_live_tickers()),
        }
    except ImportError:
        return {"connected": False, "error": "websockets module not installed"}


@router.get("")
async def get_favorites(
    user: AuthUser = Depends(require_current_or_legacy_user),
):
    """Get active favorites with live P&L."""
    async with get_connection() as conn:
        favs = await fetch_favorites(conn, user.id)

    if favs.empty:
        return {"favorites": [], "total": 0}

    # Fetch latest price ONLY for tickers in favorites (not all 159k rows)
    ticker_keys = set()
    for _, favorite in favs.iterrows():
        market = favorite.get("market") or "crypto"
        ticker_keys.add((market, favorite["ticker_a"]))
        ticker_keys.add((market, favorite["ticker_b"]))

    latest_prices = {}
    pair_risks = {}
    async with get_connection() as conn:
        for market, ticker in ticker_keys:
            cursor = await conn.execute(
                """
                SELECT close FROM prices
                WHERE ticker = ? AND market = ?
                ORDER BY date DESC LIMIT 1
                """,
                (ticker, market)
            )
            row = await cursor.fetchone()
            if row:
                latest_prices[(market, ticker)] = float(row[0])
        cursor = await conn.execute("SELECT * FROM pairs")
        for pair_row in await cursor.fetchall():
            pair_data = dict(pair_row)
            pair_risks[(
                pair_data.get("market"),
                pair_data.get("ticker_a"),
                pair_data.get("ticker_b"),
            )] = pair_data

    active_positions = []
    for _, row in favs.iterrows():
        market = row.get("market") or "crypto"
        entry_a = row.get("price_a_entry")
        entry_b = row.get("price_b_entry")

        # Backfill missing entry prices from latest_prices
        if not entry_a or entry_a == 0:
            entry_a = float(latest_prices.get((market, row["ticker_a"]), 0) or 0)
        if not entry_b or entry_b == 0:
            entry_b = float(latest_prices.get((market, row["ticker_b"]), 0) or 0)

        price_a_now = _get_current_price(row["ticker_a"], latest_prices, market)
        price_b_now = _get_current_price(row["ticker_b"], latest_prices, market)

        # Fall back to DB prices if live price is 0 and DB has one
        if (price_a_now or 0) == 0:
            price_a_now = float(latest_prices.get((market, row["ticker_a"]), 0) or 0)
        if (price_b_now or 0) == 0:
            price_b_now = float(latest_prices.get((market, row["ticker_b"]), 0) or 0)

        sig_type = row.get("signal_type", "wait")
        pnl_total = _pair_pnl(
            sig_type,
            entry_a,
            entry_b,
            price_a_now,
            price_b_now,
        )

        hl = _query_int(row.get("halflife"), None)
        entry_time = row.get("entry_time")
        timing = estimate_signal_timing(entry_time, hl)
        days_held = timing["signal_days_elapsed"]
        hl_remaining = timing["signal_days_remaining"]
        is_expired = timing["signal_is_expired"]
        pair_risk = pair_risks.get(
            (market, row["ticker_a"], row["ticker_b"]),
            {},
        )
        default_eligible = 0 if market == "ru" else 1
        risk_reason = pair_risk.get("risk_reason")
        if market == "ru" and not pair_risk:
            risk_reason = "Пара отсутствует в свежем анализе"

        active_positions.append({
            "id": int(row["id"]),
            "pair": row["pair"],
            "market": market,
            "ticker_a": row["ticker_a"],
            "ticker_b": row["ticker_b"],
            "signal": row.get("signal", ""),
            "signal_type": sig_type,
            "z_at_entry": row.get("z_at_entry"),
            "price_a_entry": round(float(entry_a), 4) if entry_a else None,
            "price_b_entry": round(float(entry_b), 4) if entry_b else None,
            "price_a_now": round(float(price_a_now), 4) if price_a_now else None,
            "price_b_now": round(float(price_b_now), 4) if price_b_now else None,
            "pnl_total_pct": round(float(pnl_total), 2),
            "entry_time": entry_time,
            "halflife": hl,
            "days_held": days_held,
            "hl_remaining": hl_remaining,
            "is_expired": is_expired,
            "signal_eligible": _query_int(
                pair_risk.get("signal_eligible"),
                default_eligible,
            ) == 1,
            "is_coint_stable": _query_int(
                pair_risk.get("is_coint_stable"),
                0,
            ) == 1,
            "coint_stability": _query_float(
                pair_risk.get("coint_stability"),
                None,
            ),
            "market_regime": pair_risk.get("market_regime") or "normal",
            "market_volatility": _query_float(
                pair_risk.get("market_volatility"),
                None,
            ),
            "event_risk": _query_int(pair_risk.get("event_risk"), 0) == 1,
            "risk_reason": risk_reason,
            **timing,
            "corr": row.get("corr"),
            "status": row.get("status", "active"),
        })

    return {"favorites": active_positions, "total": len(active_positions)}


@router.get("/history")
async def get_favorites_history(
    limit: int = Query(10),
    user: AuthUser = Depends(require_current_or_legacy_user),
):
    """Get closed favorites history."""
    async with get_connection() as conn:
        hist = await fetch_favorites_history(conn, user.id, limit)

    return {
        "history": hist.to_dict(orient="records") if not hist.empty else [],
        "total": len(hist),
    }


@router.post("/toggle")
async def toggle_fav(
    pair: str = Query(...),
    ticker_a: str = Query(...),
    ticker_b: str = Query(...),
    signal: str = Query(""),
    signal_type: str = Query("wait"),
    z_at_entry: str | None = Query(None),
    price_a_entry: str | None = Query(None),
    price_b_entry: str | None = Query(None),
    halflife: str | None = Query(None),
    corr: str | None = Query(None),
    market: str = Query("crypto"),
    user: AuthUser = Depends(require_current_or_legacy_user),
):
    """Toggle favorite (add/remove)."""
    async with get_connection() as conn:
        result = await toggle_favorite(
            conn, pair, ticker_a, ticker_b, user.id, market,
            signal=signal, signal_type=signal_type,
            z_at_entry=_query_float(z_at_entry, 0),
            price_a_entry=_query_float(price_a_entry, 0),
            price_b_entry=_query_float(price_b_entry, 0),
            halflife=_query_int(halflife),
            corr=_query_float(corr, 0),
        )
    return result


@router.post("/close/{fav_id}")
async def close_fav(
    fav_id: int,
    exit_price_a: float = Query(0),
    exit_price_b: float = Query(0),
    exit_pnl_pct: float = Query(0),
    user: AuthUser = Depends(require_current_or_legacy_user),
):
    """Close an active favorite position."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT * FROM favorites
            WHERE id = ? AND status = 'active' AND user_id = ?
            """,
            (fav_id, user.id),
        )
        favorite = await cursor.fetchone()
        if not favorite:
            raise HTTPException(status_code=404, detail="Позиция не найдена")
        if favorite:
            market = favorite["market"] or "crypto"
            latest_prices = {}
            for ticker in (favorite["ticker_a"], favorite["ticker_b"]):
                price_cursor = await conn.execute(
                    """
                    SELECT close FROM prices
                    WHERE ticker = ? AND market = ?
                    ORDER BY date DESC LIMIT 1
                    """,
                    (ticker, market),
                )
                price_row = await price_cursor.fetchone()
                if price_row:
                    latest_prices[(market, ticker)] = float(price_row[0])

            if exit_price_a <= 0:
                exit_price_a = _get_current_price(
                    favorite["ticker_a"], latest_prices, market
                )
            if exit_price_b <= 0:
                exit_price_b = _get_current_price(
                    favorite["ticker_b"], latest_prices, market
                )
            if exit_pnl_pct == 0:
                exit_pnl_pct = round(
                    _pair_pnl(
                        favorite["signal_type"],
                        favorite["price_a_entry"],
                        favorite["price_b_entry"],
                        exit_price_a,
                        exit_price_b,
                    ),
                    4,
                )
        result = await close_favorite(
            conn,
            fav_id,
            exit_price_a,
            exit_price_b,
            exit_pnl_pct,
            user_id=user.id,
        )
    return result


@router.delete("/{fav_id}")
async def delete_fav(
    fav_id: int,
    user: AuthUser = Depends(require_current_or_legacy_user),
):
    """Delete a favorite from history."""
    async with get_connection() as conn:
        result = await delete_favorite(conn, fav_id, user_id=user.id)
    if not result["deleted"]:
        raise HTTPException(status_code=404, detail="Запись не найдена")
    return result
