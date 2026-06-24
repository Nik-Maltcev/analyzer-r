"""Scanners API endpoints."""

import numpy as np
import pandas as pd
from fastapi import APIRouter, Query
from app.db.database import get_connection, fetch_prices
from app.data.tickers import ALL_MARKETS
from app.core.scanners import corr_breakdown_scan, momentum_scan, drawdown_scan

router = APIRouter(prefix="/scanners", tags=["scanners"])


@router.get("/corrbreak")
async def scanner_corrbreak(
    market: str = Query("crypto"),
    min_deviation: float = Query(0.2, ge=0.05, le=0.5),
):
    """Correlation breakdown scanner."""
    async with get_connection() as conn:
        prices_df = await fetch_prices(conn, market)
    
    if prices_df.empty:
        return {"results": [], "total": 0}
    
    wide = prices_df.pivot(index="date", columns="ticker", values="close")
    tickers_list = list(wide.columns)
    
    if len(tickers_list) < 2:
        return {"results": [], "total": 0}
    
    df = corr_breakdown_scan(wide, tickers_list)
    
    # Filter by min deviation
    df = df[df["deviation"] >= min_deviation]
    
    return {
        "results": df.to_dict(orient="records"),
        "total": len(df),
        "market": market,
    }


@router.get("/momentum")
async def scanner_momentum(
    market: str = Query("crypto"),
    limit: int = Query(20, le=50),
):
    """Momentum scanner."""
    async with get_connection() as conn:
        prices_df = await fetch_prices(conn, market)
    
    if prices_df.empty:
        return {"results": [], "total": 0}
    
    wide = prices_df.pivot(index="date", columns="ticker", values="close")
    tickers_list = list(wide.columns)
    dates_list = list(wide.index.astype(str))
    
    prices_mat = wide.values
    df = momentum_scan(prices_mat, tickers_list, dates_list)
    
    return {
        "results": df.head(limit).to_dict(orient="records"),
        "total": len(df),
        "market": market,
    }


@router.get("/drawdown")
async def scanner_drawdown(
    market: str = Query("crypto"),
    min_drawdown: float = Query(10.0, ge=5, le=50),
):
    """Drawdown scanner."""
    async with get_connection() as conn:
        prices_df = await fetch_prices(conn, market)
    
    if prices_df.empty:
        return {"results": [], "total": 0}
    
    wide = prices_df.pivot(index="date", columns="ticker", values="close")
    tickers_list = list(wide.columns)
    
    prices_mat = wide.values
    df = drawdown_scan(prices_mat, tickers_list)
    
    # Filter by min drawdown
    df = df[df["drawdown_pct"] >= min_drawdown]
    
    return {
        "results": df.to_dict(orient="records"),
        "total": len(df),
        "market": market,
    }
