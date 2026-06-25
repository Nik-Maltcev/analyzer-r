"""Chart data API endpoints for Chart.js frontend."""

import numpy as np
import pandas as pd
from fastapi import APIRouter, Query, HTTPException
from app.db.database import get_connection, fetch_prices, fetch_pairs
from app.core.cointegration import compute_zscore, forecast_zscore, engle_granger

router = APIRouter(prefix="/charts", tags=["charts"])


@router.get("/spread/{ticker_a}/{ticker_b}")
async def spread_chart_data(
    ticker_a: str,
    ticker_b: str,
    market: str = Query("crypto"),
    points: int = Query(180, ge=30, le=500),
):
    """Get Z-score spread chart data for Chart.js.
    
    Returns JSON with:
    - dates: array of date strings
    - zscores: array of Z-score values
    - z_now: current Z
    - z_forecast: AR(1) forecast
    - bands: ±1σ, ±2σ reference lines
    - cointegration info
    """
    ticker_a = ticker_a.replace(".", "/")
    ticker_b = ticker_b.replace(".", "/")
    
    async with get_connection() as conn:
        prices_df = await fetch_prices(conn, market)
    
    if prices_df.empty:
        raise HTTPException(status_code=404, detail="No price data")
    
    ta = prices_df[prices_df["ticker"] == ticker_a]
    tb = prices_df[prices_df["ticker"] == ticker_b]
    
    if ta.empty or tb.empty:
        raise HTTPException(status_code=404, detail=f"Ticker not found: {ticker_a} or {ticker_b}")
    
    merged = pd.merge(ta[["date", "close"]], tb[["date", "close"]],
                      on="date", suffixes=("_a", "_b"))
    merged = merged.sort_values("date")
    
    if len(merged) < 30:
        raise HTTPException(status_code=400, detail="Not enough data points")
    
    pa = merged["close_a"].values
    pb = merged["close_b"].values
    dates_all = merged["date"].tolist()
    
    cg = engle_granger(pa, pb)
    zres = compute_zscore(pa, pb, cg["hedge_ratio"])
    
    if zres["zscores"] is None:
        raise HTTPException(status_code=400, detail="Cannot compute Z-scores")
    
    fc = forecast_zscore(zres["zscores"])
    
    z_arr = zres["zscores"]
    n = len(z_arr)
    
    # Take last N points
    n_show = min(points, n)
    z_show = z_arr[-n_show:]
    dates_show = dates_all[-n_show:]
    
    # Convert to lists for JSON
    z_list = [None if np.isnan(z) else round(float(z), 4) for z in z_show]
    
    return {
        "dates": dates_show,
        "zscores": z_list,
        "z_now": round(float(zres["z_now"]), 4) if zres["z_now"] else None,
        "z_forecast": round(float(fc["z_forecast"]), 4) if fc["z_forecast"] else None,
        "z_mean": 0,
        "z_sd": 1,
        "bands": {
            "pos2": 2.0,
            "pos1": 1.0,
            "neg1": -1.0,
            "neg2": -2.0,
        },
        "cointegration": {
            "is_coint": cg["is_coint"],
            "halflife": cg["halflife"],
            "t_stat": round(float(cg["t_stat"]), 4) if cg["t_stat"] else None,
            "hedge_ratio": round(float(cg["hedge_ratio"]), 4) if cg["hedge_ratio"] else None,
        },
        "ticker_a": ticker_a,
        "ticker_b": ticker_b,
        "n_points": n_show,
    }


@router.get("/sparkline/{ticker_a}/{ticker_b}")
async def sparkline_data(
    ticker_a: str,
    ticker_b: str,
    market: str = Query("crypto"),
    points: int = Query(30, ge=10, le=90),
):
    """Get mini sparkline data for signal cards.
    
    Returns last N Z-score values for a sparkline chart.
    """
    ticker_a = ticker_a.replace(".", "/")
    ticker_b = ticker_b.replace(".", "/")
    
    async with get_connection() as conn:
        prices_df = await fetch_prices(conn, market)
    
    if prices_df.empty:
        return {"values": [], "z_now": None}
    
    ta = prices_df[prices_df["ticker"] == ticker_a]
    tb = prices_df[prices_df["ticker"] == ticker_b]
    
    if ta.empty or tb.empty:
        return {"values": [], "z_now": None}
    
    merged = pd.merge(ta[["date", "close"]], tb[["date", "close"]],
                      on="date", suffixes=("_a", "_b"))
    merged = merged.sort_values("date")
    
    if len(merged) < 30:
        return {"values": [], "z_now": None}
    
    pa = merged["close_a"].values
    pb = merged["close_b"].values
    
    cg = engle_granger(pa, pb)
    zres = compute_zscore(pa, pb, cg["hedge_ratio"])
    
    if zres["zscores"] is None:
        return {"values": [], "z_now": None}
    
    z_arr = zres["zscores"]
    n_show = min(points, len(z_arr))
    z_show = z_arr[-n_show:]
    
    values = [None if np.isnan(z) else round(float(z), 3) for z in z_show]
    
    return {
        "values": values,
        "z_now": round(float(zres["z_now"]), 3) if zres["z_now"] else None,
    }


@router.get("/price/{ticker}")
async def price_chart_data(
    ticker: str,
    market: str = Query("crypto"),
    points: int = Query(90, ge=30, le=365),
):
    """Get price history for a single ticker chart."""
    ticker = ticker.replace(".", "/")
    
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT date, close FROM prices WHERE ticker = ? AND market = ? ORDER BY date",
            (ticker, market)
        )
        rows = await cursor.fetchall()
    
    if not rows:
        raise HTTPException(status_code=404, detail=f"No data for {ticker}")
    
    dates = [r[0] for r in rows[-points:]]
    prices = [round(float(r[1]), 4) for r in rows[-points:]]
    
    return {
        "ticker": ticker,
        "dates": dates,
        "prices": prices,
        "n_points": len(prices),
    }
