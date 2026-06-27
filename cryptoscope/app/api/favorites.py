"""Favorites API endpoints."""

import math
from typing import Optional
from fastapi import APIRouter, Query, HTTPException
from app.db.database import (
    get_connection, fetch_favorites, fetch_favorites_history,
    toggle_favorite, close_favorite, delete_favorite,
)
from app.core.signals import estimate_signal_timing

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


def _get_current_price(ticker: str, db_prices: dict) -> float:
    """Get current price: try Binance WS first, fall back to DB."""
    try:
        from app.data.binance_ws import get_live_price
        live = get_live_price(ticker)
        if live is not None and live > 0:
            return float(live)
    except ImportError:
        pass
    return float(db_prices.get(ticker, 0) or 0)


@router.get("/live-status")
async def live_prices_status():
    """Check Binance WS connection status."""
    try:
        from app.data.binance_ws import is_connected, get_uptime, live_prices, get_all_live_tickers
        return {
            "connected": is_connected(),
            "uptime_seconds": round(get_uptime(), 1),
            "symbols_tracked": len(live_prices),
            "tickers_tracked": len(get_all_live_tickers()),
        }
    except ImportError:
        return {"connected": False, "error": "websockets module not installed"}


@router.get("")
async def get_favorites(user_id: str = Query("local")):
    """Get active favorites with live P&L."""
    async with get_connection() as conn:
        favs = await fetch_favorites(conn, user_id)

    if favs.empty:
        return {"favorites": [], "total": 0}

    # Fetch latest price ONLY for tickers in favorites (not all 159k rows)
    tickers_set = set(favs["ticker_a"].tolist() + favs["ticker_b"].tolist())
    latest_prices = {}
    async with get_connection() as conn:
        for ticker in tickers_set:
            cursor = await conn.execute(
                "SELECT close FROM prices WHERE ticker = ? ORDER BY date DESC LIMIT 1",
                (ticker,)
            )
            row = await cursor.fetchone()
            if row:
                latest_prices[ticker] = float(row[0])
    
    active_positions = []
    for _, row in favs.iterrows():
        entry_a = row.get("price_a_entry")
        entry_b = row.get("price_b_entry")

        # Backfill missing entry prices from latest_prices
        if not entry_a or entry_a == 0:
            entry_a = float(latest_prices.get(row["ticker_a"], 0) or 0)
        if not entry_b or entry_b == 0:
            entry_b = float(latest_prices.get(row["ticker_b"], 0) or 0)

        price_a_now = _get_current_price(row["ticker_a"], latest_prices)
        price_b_now = _get_current_price(row["ticker_b"], latest_prices)
        
        # Fall back to DB prices if live price is 0 and DB has one
        if (price_a_now or 0) == 0:
            price_a_now = float(latest_prices.get(row["ticker_a"], 0) or 0)
        if (price_b_now or 0) == 0:
            price_b_now = float(latest_prices.get(row["ticker_b"], 0) or 0)
        
        pnl_a = (price_a_now / entry_a - 1) * 100 if entry_a and entry_a > 0 and price_a_now else 0
        pnl_b = (price_b_now / entry_b - 1) * 100 if entry_b and entry_b > 0 and price_b_now else 0
        
        # For pairs, P&L depends on position direction
        sig_type = row.get("signal_type", "wait")
        if sig_type == "short_a":
            pnl_total = -pnl_a + pnl_b
        elif sig_type == "long_a":
            pnl_total = pnl_a - pnl_b
        else:
            pnl_total = 0
        
        hl = _query_int(row.get("halflife"), None)
        entry_time = row.get("entry_time")
        timing = estimate_signal_timing(entry_time, hl)
        days_held = timing["signal_days_elapsed"]
        hl_remaining = timing["signal_days_remaining"]
        is_expired = timing["signal_is_expired"]

        active_positions.append({
            "id": int(row["id"]),
            "pair": row["pair"],
            "ticker_a": row["ticker_a"],
            "ticker_b": row["ticker_b"],
            "signal": row.get("signal", ""),
            "signal_type": sig_type,
            "z_at_entry": row.get("z_at_entry"),
            "price_a_entry": row.get("price_a_entry"),
            "price_b_entry": row.get("price_b_entry"),
            "price_a_now": round(float(price_a_now), 4) if price_a_now else None,
            "price_b_now": round(float(price_b_now), 4) if price_b_now else None,
            "pnl_total_pct": round(float(pnl_total), 2),
            "entry_time": entry_time,
            "halflife": hl,
            "days_held": days_held,
            "hl_remaining": hl_remaining,
            "is_expired": is_expired,
            **timing,
            "corr": row.get("corr"),
            "status": row.get("status", "active"),
        })
    
    return {"favorites": active_positions, "total": len(active_positions)}


@router.get("/history")
async def get_favorites_history(user_id: str = Query("local"), limit: int = Query(10)):
    """Get closed favorites history."""
    async with get_connection() as conn:
        hist = await fetch_favorites_history(conn, user_id, limit)
    
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
    z_at_entry: Optional[str] = Query(None),
    price_a_entry: Optional[str] = Query(None),
    price_b_entry: Optional[str] = Query(None),
    halflife: Optional[str] = Query(None),
    corr: Optional[str] = Query(None),
    user_id: str = Query("local"),
):
    """Toggle favorite (add/remove)."""
    async with get_connection() as conn:
        result = await toggle_favorite(
            conn, pair, ticker_a, ticker_b, user_id,
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
):
    """Close an active favorite position."""
    async with get_connection() as conn:
        result = await close_favorite(conn, fav_id, exit_price_a, exit_price_b, exit_pnl_pct)
    return result


@router.delete("/{fav_id}")
async def delete_fav(fav_id: int):
    """Delete a favorite from history."""
    async with get_connection() as conn:
        result = await delete_favorite(conn, fav_id)
    return result
