"""Signals API endpoints."""

import numpy as np
import pandas as pd
from fastapi import APIRouter, Query, Request
from app.db.database import get_connection, fetch_prices, fetch_pairs
from app.core.cointegration import compute_zscore, forecast_zscore
from app.core.signals import determine_signal, determine_strength
from app.core.calculator import calc_signal_pnl

router = APIRouter(prefix="/signals", tags=["signals"])


def _finite_float(value, default=None):
    if value is None:
        return default
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    return f if np.isfinite(f) else default


def _finite_int(value, default=None):
    f = _finite_float(value)
    return int(f) if f is not None else default


def _finite_bool(value) -> bool:
    f = _finite_float(value)
    return bool(f) if f is not None else False


@router.get("")
async def get_signals(
    market: str = Query("crypto", description="Market: crypto, stocks, ru"),
    min_corr: float = Query(0.5, ge=0, le=1, description="Minimum correlation"),
    min_coint: bool = Query(False, description="Only cointegrated pairs"),
    max_days: int = Query(30, ge=1, le=60, description="Max days for quick signals"),
):
    """Get active trading signals."""
    async with get_connection() as conn:
        pairs = await fetch_pairs(conn, market, min_corr)
    
    if pairs.empty:
        return {"signals": [], "total": 0, "active": 0}
    
    # Filter cointegrated only
    if min_coint:
        pairs = pairs[pairs["is_coint"] == 1]
    
    # Filter by half-life (fast signals)
    pairs = pairs[(pairs["halflife"].isna()) | (pairs["halflife"] <= max_days)]
    
    # Convert to dict records
    signals = []
    for _, row in pairs.iterrows():
        corr = _finite_float(row.get("corr"))
        score = _finite_float(row.get("score"))
        z_now = _finite_float(row.get("z_now"))
        zf = _finite_float(row.get("z_forecast"))
        
        signals.append({
            "pair_id": f"{row['ticker_a']}_{row['ticker_b']}",
            "ticker_a": row["ticker_a"],
            "ticker_b": row["ticker_b"],
            "corr": round(corr, 4) if corr is not None else None,
            "is_coint": _finite_bool(row.get("is_coint")),
            "halflife": _finite_int(row.get("halflife")),
            "score": round(score, 4) if score is not None else None,
            "z_now": round(z_now, 4) if z_now is not None else None,
            "z_forecast": round(zf, 4) if zf is not None else None,
            "signal": row["signal"],
            "signal_type": row["signal_type"],
            "strength": row["strength"],
        })
    
    active = [s for s in signals if s["signal_type"] != "wait"]
    
    return {
        "signals": signals,
        "active_signals": active,
        "total": len(signals),
        "active": len(active),
        "market": market,
    }


@router.get("/forecast")
async def get_forecast_trades(
    market: str = Query("crypto"),
    min_corr: float = Query(0.5),
    max_days: int = Query(30, description="Max hold days for forecast"),
):
    """Get forecast trades (Прогноз mode)."""
    async with get_connection() as conn:
        pairs = await fetch_pairs(conn, market, min_corr)
    
    if pairs.empty:
        return {"trades": [], "total": 0}
    
    # Filter active signals
    active = pairs[pairs["signal_type"] != "wait"].copy()
    if active.empty:
        return {"trades": [], "total": 0}
    
    trades = []
    for _, row in active.iterrows():
        z_now = _finite_float(row.get("z_now"), 0.0)
        hl = _finite_int(row.get("halflife"), 30)
        avg_hold = min(hl, max_days)
        
        # Estimate P&L based on Z-score move
        pnl_est = abs(z_now) - 0.5  # expected move back to ±0.5
        pnl_pct = max(0, round(float(pnl_est), 2))
        win_rate = 65 if _finite_bool(row.get("is_coint")) else 50
        z_forecast = _finite_float(row.get("z_forecast"))
        
        trades.append({
            "pair": f"{row['ticker_a']}/{row['ticker_b']}",
            "ticker_a": row["ticker_a"],
            "ticker_b": row["ticker_b"],
            "signal": row["signal"],
            "signal_type": row["signal_type"],
            "strength": row.get("strength", "Нет"),
            "z_now": round(float(z_now), 4) if z_now else None,
            "z_forecast": round(z_forecast, 4) if z_forecast is not None else None,
            "win_rate": round(float(win_rate), 1),
            "n_similar": 0,
            "avg_pnl_pct": round(float(pnl_pct), 2),
            "avg_hold_days": round(float(avg_hold), 1),
            "best_pnl": round(float(pnl_pct * 1.5), 2),
            "worst_pnl": round(float(-pnl_pct), 2),
        })
    
    trades.sort(key=lambda x: abs(x.get("z_now", 0) or 0), reverse=True)
    
    return {
        "trades": trades,
        "total": len(trades),
        "market": market,
    }


@router.get("/short")
async def get_short_trades(
    market: str = Query("crypto"),
    min_corr: float = Query(0.5),
    max_days: int = Query(7, le=7),
):
    """Get fast short-term forecast trades (Быстрые <7д)."""
    result = await get_forecast_trades(market=market, min_corr=min_corr, max_days=max_days)
    return result


@router.get("/dashboard")
async def get_dashboard(
    market: str = Query("crypto"),
):
    """Get signals dashboard summary."""
    async with get_connection() as conn:
        pairs = await fetch_pairs(conn, market, 0.5)
        prices_df = await fetch_prices(conn, market)
    
    if pairs.empty:
        return {"n_active": 0, "n_total": 0, "best_signal": None}
    
    active = pairs[pairs["signal_type"] != "wait"]
    
    # Market volatility (7-day)
    volatility_str = "Низкая"
    if not prices_df.empty:
        try:
            wide = prices_df.pivot(index="date", columns="ticker", values="close")
            latest = wide.iloc[-1]
            week_ago = wide.iloc[-min(8, len(wide))]
            avg_change = float(abs(latest / week_ago - 1).mean() * 100)
            if avg_change > 10:
                volatility_str = "Высокая"
            elif avg_change > 5:
                volatility_str = "Средняя"
        except Exception:
            pass
    
    # Best signal
    best = None
    if not active.empty:
        best_row = active.iloc[0]
        best = {
            "pair": f"{best_row['ticker_a']}/{best_row['ticker_b']}",
            "signal": best_row["signal"],
            "z_now": round(float(best_row.get("z_now", 0) or 0), 2),
            "strength": best_row.get("strength", "Нет"),
        }
    
    return {
        "n_active": len(active),
        "n_total": len(pairs),
        "best_signal": best,
        "volatility": volatility_str,
        "last_analysis": str(pairs["computed_at"].max()) if "computed_at" in pairs.columns and not pairs["computed_at"].isna().all() else None,
    }


@router.get("/pnl")
async def calculate_pnl(
    capital: float = Query(1000.0, ge=10),
    leverage: float = Query(3.0, ge=1, le=20),
    taker_fee: float = Query(0.02),
    funding_rate: float = Query(0.01),
    hold_days: int = Query(5, ge=1),
    z_move: float = Query(2.0, ge=0.1, le=5),
    spread_sd: float = Query(0.05),
):
    """P&L calculator endpoint."""
    signal_info = {"spread_sd_pct": spread_sd, "signal": "Manual", "signal_type": "manual"}
    result = calc_signal_pnl(
        signal_info,
        capital=capital,
        leverage=leverage,
        taker_fee_pct=taker_fee,
        funding_rate_8h_pct=funding_rate,
        hold_days=hold_days,
        avg_pnl_z=z_move,
    )
    return result
