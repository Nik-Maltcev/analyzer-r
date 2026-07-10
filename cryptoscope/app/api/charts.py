"""Chart data endpoints backed by focused database queries."""

from __future__ import annotations

from collections import defaultdict

import numpy as np
from fastapi import APIRouter, HTTPException, Query

from app.core.cointegration import compute_zscore, engle_granger, forecast_zscore
from app.db.database import get_connection

router = APIRouter(prefix="/charts", tags=["charts"])


def _aligned_pair(
    series: dict[str, list[tuple[str, float]]],
    ticker_a: str,
    ticker_b: str,
) -> tuple[list[str], np.ndarray, np.ndarray]:
    values_a = dict(series.get(ticker_a, []))
    values_b = dict(series.get(ticker_b, []))
    dates = sorted(set(values_a).intersection(values_b))
    return (
        dates,
        np.asarray([values_a[date] for date in dates], dtype=float),
        np.asarray([values_b[date] for date in dates], dtype=float),
    )


def _pair_model(
    pair_models: dict[tuple[str, str], dict],
    ticker_a: str,
    ticker_b: str,
) -> dict:
    direct = pair_models.get((ticker_a, ticker_b))
    if direct:
        return dict(direct)
    reverse = pair_models.get((ticker_b, ticker_a))
    if not reverse:
        return {}
    model = dict(reverse)
    hedge_ratio = model.get("hedge_ratio")
    if (
        hedge_ratio is not None
        and np.isfinite(hedge_ratio)
        and abs(hedge_ratio) > 1e-12
    ):
        model["hedge_ratio"] = 1.0 / float(hedge_ratio)
    return model


def _chart_series(
    dates: list[str],
    pa: np.ndarray,
    pb: np.ndarray,
    model: dict,
    points: int,
) -> dict | None:
    if len(dates) < 30:
        return None
    hedge_ratio = model.get("hedge_ratio")
    if hedge_ratio is None:
        model = engle_granger(pa, pb)
        hedge_ratio = model.get("hedge_ratio")
    zres = compute_zscore(pa, pb, hedge_ratio)
    zscores = zres.get("zscores")
    if zscores is None:
        return None

    n_show = min(points, len(zscores))
    z_show = zscores[-n_show:]
    return {
        "dates": dates[-n_show:],
        "values": [
            None if not np.isfinite(value) else round(float(value), 4)
            for value in z_show
        ],
        "z_now": (
            round(float(zres["z_now"]), 4)
            if zres.get("z_now") is not None
            else None
        ),
        "hedge_ratio": hedge_ratio,
        "cointegration": model,
        "zscores": zscores,
    }


async def _load_chart_inputs(
    market: str,
    tickers: set[str],
    load_models: bool = True,
) -> tuple[
    dict[str, list[tuple[str, float]]],
    dict[tuple[str, str], dict],
]:
    if not tickers:
        return {}, {}
    placeholders = ",".join("?" for _ in tickers)
    params = [market, *sorted(tickers)]
    async with get_connection() as conn:
        cursor = await conn.execute(
            f"""
            SELECT ticker, date, close
            FROM prices
            WHERE market = ? AND ticker IN ({placeholders})
            ORDER BY ticker, date
            """,
            params,
        )
        price_rows = await cursor.fetchall()
        pair_rows = []
        if load_models:
            cursor = await conn.execute(
                f"""
                SELECT ticker_a, ticker_b, hedge_ratio, is_coint, halflife,
                       t_stat, coint_pvalue
                FROM pairs
                WHERE market = ?
                  AND ticker_a IN ({placeholders})
                  AND ticker_b IN ({placeholders})
                """,
                [market, *sorted(tickers), *sorted(tickers)],
            )
            pair_rows = await cursor.fetchall()

    series: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for row in price_rows:
        series[row["ticker"]].append((row["date"], float(row["close"])))
    pair_models = {
        (row["ticker_a"], row["ticker_b"]): {
            "hedge_ratio": (
                float(row["hedge_ratio"])
                if row["hedge_ratio"] is not None
                else None
            ),
            "is_coint": bool(row["is_coint"]),
            "halflife": row["halflife"],
            "t_stat": row["t_stat"],
            "p_value": row["coint_pvalue"],
        }
        for row in pair_rows
    }
    return dict(series), pair_models


@router.get("/spread")
async def spread_chart_data(
    ticker_a: str = Query(...),
    ticker_b: str = Query(...),
    market: str = Query("crypto"),
    points: int = Query(180, ge=30, le=500),
):
    """Return a pair Z-score chart without altering ticker characters."""
    series, pair_models = await _load_chart_inputs(
        market, {ticker_a, ticker_b}
    )
    dates, pa, pb = _aligned_pair(series, ticker_a, ticker_b)
    model = _pair_model(pair_models, ticker_a, ticker_b)
    chart = _chart_series(dates, pa, pb, model, points)
    if chart is None:
        raise HTTPException(status_code=404, detail="Not enough pair data")

    forecast = forecast_zscore(chart.pop("zscores"))
    return {
        "dates": chart["dates"],
        "zscores": chart["values"],
        "z_now": chart["z_now"],
        "z_forecast": forecast.get("z_forecast"),
        "z_mean": 0,
        "z_sd": 1,
        "bands": {"pos2": 2.0, "pos1": 1.0, "neg1": -1.0, "neg2": -2.0},
        "cointegration": chart["cointegration"],
        "ticker_a": ticker_a,
        "ticker_b": ticker_b,
        "n_points": len(chart["values"]),
    }


@router.post("/sparklines")
async def sparklines_data(payload: dict):
    """Return every signal-card sparkline in one HTTP/database round trip."""
    market = str(payload.get("market") or "crypto")
    requested = payload.get("pairs") or []
    if not isinstance(requested, list) or len(requested) > 100:
        raise HTTPException(status_code=400, detail="Invalid pairs payload")

    pairs: list[tuple[str, str]] = []
    tickers: set[str] = set()
    for item in requested:
        if not isinstance(item, dict):
            continue
        ticker_a = str(item.get("ticker_a") or "").strip()
        ticker_b = str(item.get("ticker_b") or "").strip()
        if not ticker_a or not ticker_b or ticker_a == ticker_b:
            continue
        pairs.append((ticker_a, ticker_b))
        tickers.update((ticker_a, ticker_b))

    series, pair_models = await _load_chart_inputs(market, tickers)
    items = []
    for ticker_a, ticker_b in pairs:
        dates, pa, pb = _aligned_pair(series, ticker_a, ticker_b)
        model = _pair_model(pair_models, ticker_a, ticker_b)
        chart = _chart_series(dates, pa, pb, model, 30)
        items.append({
            "ticker_a": ticker_a,
            "ticker_b": ticker_b,
            "values": chart["values"] if chart else [],
            "z_now": chart["z_now"] if chart else None,
        })
    return {"items": items}


@router.get("/price")
async def price_chart_data(
    ticker: str = Query(...),
    market: str = Query("crypto"),
    points: int = Query(90, ge=30, le=365),
):
    """Return price history while preserving dots and slashes in tickers."""
    series, _ = await _load_chart_inputs(market, {ticker}, load_models=False)
    rows = series.get(ticker, [])[-points:]
    if not rows:
        raise HTTPException(status_code=404, detail=f"No data for {ticker}")
    return {
        "ticker": ticker,
        "dates": [row[0] for row in rows],
        "prices": [round(row[1], 4) for row in rows],
        "n_points": len(rows),
    }
