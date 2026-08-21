"""Signals API endpoints."""

import numpy as np
import pandas as pd
from fastapi import APIRouter, Query

from app.core.calculator import calc_signal_pnl
from app.core.signals import estimate_signal_timing, is_actionable_signal
from app.db.database import fetch_active_pairs, fetch_pairs, fetch_prices, get_connection

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


def _is_validated_pair(row) -> bool:
    if "is_coint_stable" in row:
        return _finite_bool(row.get("is_coint_stable"))
    return _finite_bool(row.get("is_coint"))


def _project_tomorrow_move(z_now, halflife):
    """Project one-day Z-score mean reversion from the pair half-life."""
    z = _finite_float(z_now)
    hl = _finite_int(halflife)
    if z is None or hl is None or hl <= 0:
        return {
            "z_tomorrow": None,
            "z_tomorrow_delta": None,
            "z_tomorrow_reversion_pct": None,
        }

    decay = 0.5 ** (1.0 / hl)
    z_tomorrow = z * decay
    delta = z_tomorrow - z
    return {
        "z_tomorrow": round(z_tomorrow, 4),
        "z_tomorrow_delta": round(delta, 4),
        "z_tomorrow_reversion_pct": round((1.0 - decay) * 100, 2),
    }


def _backtest_metrics(row) -> dict:
    """Return only statistically usable out-of-sample metrics."""
    n_trades = _finite_int(row.get("backtest_trades"), 0)
    win_rate = _finite_float(row.get("backtest_win_rate"))
    avg_pnl_pct = _finite_float(row.get("backtest_avg_pnl_pct"))
    avg_net_pnl_pct = _finite_float(row.get("backtest_avg_net_pnl_pct"))
    avg_hold_days = _finite_float(row.get("backtest_avg_hold_days"))
    validated = bool(
        _finite_bool(row.get("backtest_validated"))
        and n_trades >= 5
        and win_rate is not None
        and avg_pnl_pct is not None
        and avg_hold_days is not None
    )
    return {
        "backtest_validated": validated,
        "n_similar": n_trades,
        "win_rate": (
            win_rate if validated else None
        ),
        "avg_pnl_pct": (
            avg_pnl_pct if validated else None
        ),
        "avg_net_pnl_pct": (
            avg_net_pnl_pct if validated else None
        ),
        "avg_hold_days": (
            avg_hold_days if validated else None
        ),
    }


@router.get("")
async def get_signals(
    market: str = Query("crypto", description="Market: crypto, stocks, ru, br, id"),
    min_corr: float = Query(0.5, ge=0, le=1, description="Minimum correlation"),
    min_coint: bool = Query(False, description="Only cointegrated pairs"),
    max_days: int = Query(30, ge=1, le=60, description="Max days for quick signals"),
):
    """Get active trading signals."""
    async with get_connection() as conn:
        pairs = await fetch_pairs(conn, market, min_corr)
        active_pairs = await fetch_active_pairs(conn, market)

    if pairs.empty:
        pairs = active_pairs.copy()
    elif not active_pairs.empty:
        pairs = pd.concat([active_pairs, pairs], ignore_index=True).drop_duplicates(
            subset=["market", "ticker_a", "ticker_b"],
            keep="first",
        )

    if pairs.empty:
        return {"signals": [], "total": 0, "active": 0}

    # Filter cointegrated only
    if min_coint:
        coint_column = (
            "is_coint_stable"
            if "is_coint_stable" in pairs.columns
            else "is_coint"
        )
        pairs = pairs[pairs[coint_column] == 1]

    # Keep stored active signals visible even when their correlation or horizon
    # lies outside optional candidate filters.
    stored_active = pairs["signal_type"] != "wait"
    within_horizon = pairs["halflife"].notna() & (pairs["halflife"] <= max_days)
    pairs = pairs[stored_active | within_horizon]

    # Convert to dict records
    signals = []
    for _, row in pairs.iterrows():
        corr = _finite_float(row.get("corr"))
        score = _finite_float(row.get("score"))
        z_now = _finite_float(row.get("z_now"))
        zf = _finite_float(row.get("z_forecast"))
        zf_low = _finite_float(row.get("z_forecast_low"))
        zf_high = _finite_float(row.get("z_forecast_high"))
        hl = _finite_int(row.get("halflife"))
        tomorrow_move = _project_tomorrow_move(z_now, hl)
        is_coint_stable = _is_validated_pair(row)
        signal_type = row.get("signal_type", "wait")
        signal = row.get("signal", "Ждать")
        strength = row.get("strength", "Нет")
        risk_reason = row.get("risk_reason")
        signal_eligible = is_actionable_signal(signal_type, is_coint_stable)
        if signal_type != "wait" and not signal_eligible:
            signal_type = "wait"
            signal = "Наблюдение: коинтеграция не подтверждена"
            strength = "Наблюдение"
            risk_reason = risk_reason or "Коинтеграция пары не подтверждена"
        timing = estimate_signal_timing(
            row.get("signal_started_at"),
            hl,
            fallback_started_at=row.get("computed_at"),
        ) if signal_type != "wait" else estimate_signal_timing(None, hl)

        signals.append({
            "pair_id": f"{row['ticker_a']}_{row['ticker_b']}",
            "ticker_a": row["ticker_a"],
            "ticker_b": row["ticker_b"],
            "corr": round(corr, 4) if corr is not None else None,
            "is_coint": _finite_bool(row.get("is_coint")),
            "halflife": hl,
            "score": round(score, 4) if score is not None else None,
            "z_now": round(z_now, 4) if z_now is not None else None,
            "z_forecast": round(zf, 4) if zf is not None else None,
            "z_forecast_low": round(zf_low, 4) if zf_low is not None else None,
            "z_forecast_high": round(zf_high, 4) if zf_high is not None else None,
            "signal_eligible": signal_eligible,
            "is_coint_stable": is_coint_stable,
            "coint_stability": _finite_float(row.get("coint_stability")),
            "coint_windows": row.get("coint_windows"),
            "market_regime": row.get("market_regime") or "normal",
            "market_volatility": _finite_float(row.get("market_volatility")),
            "event_risk": _finite_bool(row.get("event_risk")),
            "risk_reason": risk_reason,
            **tomorrow_move,
            **timing,
            "signal": signal,
            "signal_type": signal_type,
            "strength": strength,
        })

    active = [s for s in signals if s["signal_eligible"]]

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
    active = active[
        active["halflife"].notna()
        & (active["halflife"] <= max_days)
    ]
    if active.empty:
        return {"trades": [], "total": 0}

    trades = []
    for _, row in active.iterrows():
        if not is_actionable_signal(
            row.get("signal_type"),
            _is_validated_pair(row),
        ):
            continue
        z_now = _finite_float(row.get("z_now"), 0.0)
        hl = _finite_int(row.get("halflife"), 30)
        backtest = _backtest_metrics(row)
        tomorrow_move = _project_tomorrow_move(z_now, hl)
        timing = estimate_signal_timing(
            row.get("signal_started_at"),
            hl,
            fallback_started_at=row.get("computed_at"),
        )

        z_forecast = _finite_float(row.get("z_forecast"))
        zf_low = _finite_float(row.get("z_forecast_low"))
        zf_high = _finite_float(row.get("z_forecast_high"))
        is_stable = _finite_bool(row.get("is_coint_stable"))

        trades.append({
            "pair": f"{row['ticker_a']}/{row['ticker_b']}",
            "ticker_a": row["ticker_a"],
            "ticker_b": row["ticker_b"],
            "signal": row["signal"],
            "signal_type": row["signal_type"],
            "strength": row.get("strength", "Нет"),
            "z_now": round(float(z_now), 4) if z_now else None,
            "z_forecast": round(z_forecast, 4) if z_forecast is not None else None,
            "z_forecast_low": round(zf_low, 4) if zf_low is not None else None,
            "z_forecast_high": round(zf_high, 4) if zf_high is not None else None,
            "is_coint_stable": is_stable,
            "coint_stability": _finite_float(row.get("coint_stability")),
            "market_regime": row.get("market_regime") or "normal",
            "market_volatility": _finite_float(row.get("market_volatility")),
            "event_risk": _finite_bool(row.get("event_risk")),
            "risk_reason": row.get("risk_reason"),
            **tomorrow_move,
            **timing,
            **backtest,
            "best_pnl": None,
            "worst_pnl": None,
        })

    trades.sort(key=lambda x: abs(x.get("z_now", 0) or 0), reverse=True)

    return {
        "trades": trades,
        "total": len(trades),
        "market": market,
        "market_regime": (
            trades[0]["market_regime"] if trades else "normal"
        ),
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

    coint_column = (
        "is_coint_stable" if "is_coint_stable" in pairs.columns else "is_coint"
    )
    active = pairs[
        (pairs["signal_type"] != "wait")
        & (pairs[coint_column].fillna(0) == 1)
    ]

    # Market volatility (7-day)
    volatility_str = "Низкая"
    stored_regime = pairs.iloc[0].get("market_regime") or "normal"
    if stored_regime == "stress":
        volatility_str = "Стрессовая"
    elif stored_regime == "elevated":
        volatility_str = "Повышенная"
    elif stored_regime == "normal":
        volatility_str = "Обычная"
    if not prices_df.empty:
        try:
            wide = prices_df.pivot(index="date", columns="ticker", values="close")
            latest = wide.iloc[-1]
            week_ago = wide.iloc[-min(8, len(wide))]
            avg_change = float(abs(latest / week_ago - 1).mean() * 100)
            if stored_regime == "normal" and avg_change > 10:
                volatility_str = "Высокая"
            elif stored_regime == "normal" and avg_change > 5:
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
    market: str = Query("crypto"),
    ticker_a: str | None = Query(None),
    ticker_b: str | None = Query(None),
    capital: float = Query(1000.0, ge=10),
    leverage: float = Query(3.0, ge=1, le=20),
    taker_fee: float = Query(0.02),
    funding_rate: float = Query(0.01),
    hold_days: int = Query(5, ge=1),
    z_move: float | None = Query(None, ge=0.0, le=10),
    spread_sd: float | None = Query(None, ge=0.0),
):
    """Calculate expected P&L from the pair's stored statistical model."""
    signal_info = {
        "spread_sd_pct": spread_sd,
        "signal": "Manual",
        "signal_type": "manual",
    }
    if ticker_a and ticker_b:
        async with get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM pairs
                WHERE market = ? AND ticker_a = ? AND ticker_b = ?
                LIMIT 1
                """,
                (market, ticker_a, ticker_b),
            )
            row = await cursor.fetchone()
        if row:
            signal_info.update(dict(row))
        else:
            return {
                "complete": False,
                "error": "Пара отсутствует в свежем анализе",
            }

    if spread_sd is not None:
        signal_info["spread_sd_pct"] = spread_sd

    if market != "crypto":
        funding_rate = 0.0
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
