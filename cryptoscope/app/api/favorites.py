"""Favorites API endpoints."""

import pandas as pd
from fastapi import APIRouter, Query, HTTPException
from app.db.database import (
    get_connection, fetch_favorites, fetch_favorites_history,
    toggle_favorite, close_favorite, delete_favorite, fetch_prices,
)

router = APIRouter(prefix="/favorites", tags=["favorites"])


@router.get("")
async def get_favorites(user_id: str = Query("local")):
    """Get active favorites with live P&L."""
    async with get_connection() as conn:
        favs = await fetch_favorites(conn, user_id)
        prices_df = await fetch_prices(conn)
    
    if favs.empty:
        return {"favorites": [], "total": 0}
    
    # Compute live P&L
    if not prices_df.empty:
        try:
            wide = prices_df.pivot(index="date", columns="ticker", values="close")
            latest_prices = wide.iloc[-1].to_dict()
        except Exception:
            latest_prices = {}
    else:
        latest_prices = {}
    
    active_positions = []
    for _, row in favs.iterrows():
        price_a_now = latest_prices.get(row["ticker_a"], row.get("price_a_entry", 0) or 0)
        price_b_now = latest_prices.get(row["ticker_b"], row.get("price_b_entry", 0) or 0)
        
        pnl_a = (price_a_now / row["price_a_entry"] - 1) * 100 if row.get("price_a_entry") else 0
        pnl_b = (price_b_now / row["price_b_entry"] - 1) * 100 if row.get("price_b_entry") else 0
        
        # For pairs, P&L depends on position direction
        sig_type = row.get("signal_type", "wait")
        if sig_type == "short_a":
            pnl_total = -pnl_a + pnl_b
        elif sig_type == "long_a":
            pnl_total = pnl_a - pnl_b
        else:
            pnl_total = 0
        
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
            "entry_time": row.get("entry_time"),
            "halflife": row.get("halflife"),
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
    z_at_entry: float = Query(0),
    price_a_entry: float = Query(0),
    price_b_entry: float = Query(0),
    halflife: int = Query(None),
    corr: float = Query(0),
    user_id: str = Query("local"),
):
    """Toggle favorite (add/remove)."""
    async with get_connection() as conn:
        result = await toggle_favorite(
            conn, pair, ticker_a, ticker_b, user_id,
            signal=signal, signal_type=signal_type,
            z_at_entry=z_at_entry, price_a_entry=price_a_entry,
            price_b_entry=price_b_entry, halflife=halflife, corr=corr,
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
