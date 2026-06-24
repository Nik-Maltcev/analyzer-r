"""Portfolio / Pairs Trading API endpoints."""

import numpy as np
import pandas as pd
from fastapi import APIRouter, Query, HTTPException
from app.db.database import get_connection, fetch_pairs, fetch_prices
from app.core.cointegration import compute_zscore, forecast_zscore, engle_granger
from app.core.backtest import run_backtest, backtest_stats, compute_spread_sd_pct

router = APIRouter(prefix="/portfolio", tags=["portfolio"])


@router.get("/pairs")
async def get_pairs(
    market: str = Query("crypto"),
    limit: int = Query(20, le=100),
):
    """Get top pairs analysis."""
    async with get_connection() as conn:
        pairs = await fetch_pairs(conn, market, min_corr=0.0)
    
    if pairs.empty:
        return {"pairs": [], "total": 0}
    
    top = pairs.head(limit)
    
    return {
        "pairs": top.to_dict(orient="records"),
        "total": len(pairs),
        "n_coint": int(pairs["is_coint"].sum()),
    }


@router.get("/spread/{ticker_a}/{ticker_b}")
async def get_spread(
    ticker_a: str,
    ticker_b: str,
    market: str = Query("crypto"),
):
    """Get spread chart data (Z-score series) for a specific pair."""
    async with get_connection() as conn:
        prices_df = await fetch_prices(conn, market)
    
    if prices_df.empty:
        raise HTTPException(status_code=404, detail="No price data")
    
    ta = prices_df[prices_df["ticker"] == ticker_a]
    tb = prices_df[prices_df["ticker"] == ticker_b]
    
    if ta.empty or tb.empty:
        raise HTTPException(status_code=404, detail=f"Ticker not found: {ticker_a} or {ticker_b}")
    
    # Align dates
    merged = pd.merge(ta[["date", "close"]], tb[["date", "close"]],
                      on="date", suffixes=("_a", "_b"))
    merged = merged.sort_values("date")
    
    if len(merged) < 30:
        raise HTTPException(status_code=400, detail="Not enough data points")
    
    pa = merged["close_a"].values
    pb = merged["close_b"].values
    dates = merged["date"].tolist()
    
    # Cointegration
    cg = engle_granger(pa, pb)
    
    # Z-score
    zres = compute_zscore(pa, pb, cg["hedge_ratio"])
    
    # Forecast
    zscores_arr = zres["zscores"]
    fc = forecast_zscore(zscores_arr) if zscores_arr is not None else {"z_forecast": None}
    
    # Backtest
    bt_df = run_backtest(zscores_arr) if zscores_arr is not None else pd.DataFrame()
    spread_sd = compute_spread_sd_pct(pa, pb, cg["hedge_ratio"] or 1.0)
    bt_stats = backtest_stats(bt_df, spread_sd)
    
    z_values = zscores_arr.tolist() if zscores_arr is not None else []
    
    return {
        "ticker_a": ticker_a,
        "ticker_b": ticker_b,
        "dates": dates[-len(z_values):] if z_values else dates,
        "z_values": z_values,
        "z_now": zres["z_now"],
        "z_mean": zres["mean"],
        "z_sd": zres["sd"],
        "z_forecast": fc["z_forecast"],
        "cointegration": {
            "is_coint": cg["is_coint"],
            "halflife": cg["halflife"],
            "t_stat": cg["t_stat"],
            "hedge_ratio": cg["hedge_ratio"],
        },
        "backtest": bt_stats,
        "n_obs": len(merged),
    }
