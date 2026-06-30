"""Tab routes — serve HTML partials for HTMX tab/content swaps with real data."""

import numpy as np
import pandas as pd
from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse
from zoneinfo import ZoneInfo

from app.auth import get_current_or_legacy_user
from app.core.cointegration import compute_fixed_zscore, compute_zscore, engle_granger
from app.core.scanners import corr_breakdown_scan, drawdown_scan, momentum_scan
from app.core.signals import estimate_signal_timing
from app.data.moex import get_ru_live_snapshot
from app.db.database import (
    db_status,
    ensure_favorite_z_model,
    fetch_favorites,
    fetch_favorites_history,
    fetch_pairs,
    fetch_prices,
    get_connection,
)
from app.ui.templates import templates

router = APIRouter(prefix="/tab", tags=["ui"])


def _finite_float(value, default=None):
    """Return a float only for real finite values; DB rows may contain NaN."""
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
        "z_tomorrow": round(z_tomorrow, 2),
        "z_tomorrow_delta": round(delta, 2),
        "z_tomorrow_reversion_pct": round((1.0 - decay) * 100, 1),
    }


def _df_records(df, limit=None):
    if df.empty:
        return []
    if limit:
        df = df.head(limit)
    df = df.replace({np.nan: None})
    return df.to_dict(orient="records")


def _make_signal_cards(pairs, market="crypto", fav_pairs=None):
    """Convert pairs DataFrame to list of dicts for template rendering."""
    fav_set = set(fav_pairs) if fav_pairs else set()
    signals = []
    for _, row in pairs.iterrows():
        corr_val = _finite_float(row.get("corr"))
        z_now = _finite_float(row.get("z_now"))
        zf = _finite_float(row.get("z_forecast"))
        zf_low = _finite_float(row.get("z_forecast_low"))
        zf_high = _finite_float(row.get("z_forecast_high"))
        hl = _finite_int(row.get("halflife"))
        score = _finite_float(row.get("score"))
        tomorrow_move = _project_tomorrow_move(z_now, hl)
        signal_type = row.get("signal_type", "wait")
        timing = estimate_signal_timing(
            row.get("signal_started_at"),
            hl,
            fallback_started_at=row.get("computed_at"),
        ) if signal_type != "wait" else estimate_signal_timing(None, hl)
        pair_id = f"{row['ticker_a']}_{row['ticker_b']}"
        signals.append({
            "pair_id": pair_id,
            "ticker_a": row["ticker_a"],
            "ticker_b": row["ticker_b"],
            "corr": round(corr_val, 2) if corr_val is not None else None,
            "corr_pct": round(corr_val * 100) if corr_val is not None else None,
            "is_coint": _finite_bool(row.get("is_coint")),
            "halflife": hl,
            "score": round(score, 3) if score is not None else None,
            "z_now": round(z_now, 2) if z_now is not None else None,
            "z_forecast": round(zf, 2) if zf is not None else None,
            "z_forecast_low": round(zf_low, 2) if zf_low is not None else None,
            "z_forecast_high": round(zf_high, 2) if zf_high is not None else None,
            "signal_eligible": _finite_bool(row.get("signal_eligible")),
            "is_coint_stable": _finite_bool(row.get("is_coint_stable")),
            "coint_stability": _finite_float(row.get("coint_stability")),
            "coint_windows": row.get("coint_windows"),
            "market_regime": row.get("market_regime") or "normal",
            "market_volatility": _finite_float(row.get("market_volatility")),
            "event_risk": _finite_bool(row.get("event_risk")),
            "risk_reason": row.get("risk_reason"),
            **tomorrow_move,
            **timing,
            "signal": row.get("signal", "Ждать"),
            "signal_type": signal_type,
            "strength": row.get("strength", "Нет"),
            "is_favorite": pair_id in fav_set,
        })
    return signals


def _make_forecast_trades(pairs, fav_pairs=None):
    """Build forecast trade cards from active signals."""
    fav_set = set(fav_pairs) if fav_pairs else set()
    trades = []
    for _, row in pairs.iterrows():
        z_now = _finite_float(row.get("z_now"), 0.0)
        hl = _finite_int(row.get("halflife"), 30)
        pnl_est = abs(z_now) - 0.5
        pnl_pct = max(0, round(float(pnl_est), 2))
        win_rate = 65 if _finite_bool(row.get("is_coint")) else 50
        corr_val = _finite_float(row.get("corr"))
        z_forecast = _finite_float(row.get("z_forecast"))
        zf_low = _finite_float(row.get("z_forecast_low"))
        zf_high = _finite_float(row.get("z_forecast_high"))
        tomorrow_move = _project_tomorrow_move(z_now, hl)
        timing = estimate_signal_timing(
            row.get("signal_started_at"),
            hl,
            fallback_started_at=row.get("computed_at"),
        )
        pair_id = f"{row['ticker_a']}_{row['ticker_b']}"

        trades.append({
            "pair": f"{row['ticker_a']}/{row['ticker_b']}",
            "pair_id": pair_id,
            "ticker_a": row["ticker_a"],
            "ticker_b": row["ticker_b"],
            "signal": row.get("signal", ""),
            "signal_type": row.get("signal_type", "wait"),
            "strength": row.get("strength", "Нет"),
            "corr": round(corr_val, 2) if corr_val is not None else None,
            "is_coint": _finite_bool(row.get("is_coint")),
            "halflife": hl,
            "z_now": round(float(z_now), 2) if z_now else None,
            "z_forecast": round(z_forecast, 2) if z_forecast is not None else None,
            "z_forecast_low": round(zf_low, 2) if zf_low is not None else None,
            "z_forecast_high": round(zf_high, 2) if zf_high is not None else None,
            "is_coint_stable": _finite_bool(row.get("is_coint_stable")),
            "coint_stability": _finite_float(row.get("coint_stability")),
            "market_regime": row.get("market_regime") or "normal",
            "market_volatility": _finite_float(row.get("market_volatility")),
            "event_risk": _finite_bool(row.get("event_risk")),
            "risk_reason": row.get("risk_reason"),
            **tomorrow_move,
            **timing,
            "win_rate": round(float(win_rate), 1),
            "avg_pnl_pct": round(float(pnl_pct), 2),
            "avg_hold_days": round(float(min(hl, 30)), 1),
            "is_favorite": pair_id in fav_set,
        })

    trades.sort(key=lambda x: abs(x.get("z_now", 0) or 0), reverse=True)
    return trades


async def _dash_data(conn, market):
    """Get dashboard summary."""
    pairs = await fetch_pairs(conn, market, 0.5)
    if pairs.empty:
        return {"n_active": 0, "n_total": 0, "best_signal": None, "volatility": "Низкая"}

    active = pairs[pairs["signal_type"] != "wait"]
    n_active = len(active)
    regime = pairs.iloc[0].get("market_regime") or "normal"
    volatility = {
        "stress": "Стрессовая",
        "elevated": "Повышенная",
        "normal": "Обычная",
    }.get(regime, "Обычная")

    best = None
    if not active.empty:
        br = active.iloc[0]
        best = {
            "pair": f"{br['ticker_a']}/{br['ticker_b']}",
            "z_now": round(float(br.get("z_now", 0) or 0), 2),
            "strength": br.get("strength", "Нет"),
        }

    return {
        "n_active": n_active,
        "n_total": len(pairs),
        "best_signal": best,
        "volatility": volatility,
    }


@router.get("/signals", response_class=HTMLResponse)
async def tab_signals(
    request: Request,
    market: str = Query("crypto"),
    mode: str = Query("all"),
    min_corr: float = Query(0.5),
    min_coint: bool = Query(False),
    max_days: int = Query(30),
):
    ctx = {
        "request": request,
        "mode": mode,
        "market": market,
        "min_corr": min_corr,
        "min_coint": min_coint,
        "max_days": max_days,
    }

    try:
        async with get_connection() as conn:
            pairs = await fetch_pairs(conn, market, min_corr)

        if pairs.empty:
            return templates.TemplateResponse(request, "components/signals_all.html", {
                **ctx, "signals": [], "total": 0, "active": [],
            })

        if min_coint:
            coint_column = (
                "is_coint_stable"
                if market == "ru" and "is_coint_stable" in pairs.columns
                else "is_coint"
            )
            pairs = pairs[pairs[coint_column] == 1]

        # Filter by half-life
        pairs = pairs[(pairs["halflife"].isna()) | (pairs["halflife"] <= max_days)]

        # Fetch existing favorite pair IDs to render star state
        # Wrapped in try/except so favorites query failure doesn't kill signals
        fav_pair_ids = set()
        try:
            user = await get_current_or_legacy_user(request)
            if user:
                async with get_connection() as conn:
                    cursor = await conn.execute(
                        """
                        SELECT pair FROM favorites
                        WHERE status = 'active'
                          AND user_id = ?
                          AND COALESCE(market, 'crypto') = ?
                        """,
                        (user.id, market),
                    )
                    fav_rows = await cursor.fetchall()
                    fav_pair_ids = set(r[0] for r in fav_rows)
        except Exception:
            pass

        signals = _make_signal_cards(pairs, market, fav_pair_ids)
        active = [s for s in signals if s["signal_type"] != "wait"]
        market_regime = signals[0]["market_regime"] if signals else "normal"
        market_volatility = signals[0]["market_volatility"] if signals else None

        if mode == "forecast":
            active_pairs = pairs[pairs["signal_type"] != "wait"]
            if active_pairs.empty:
                return templates.TemplateResponse(request, "components/signals_forecast.html", {
                    **ctx, "trades": [], "total": 0,
                    "market_regime": market_regime,
                    "market_volatility": market_volatility,
                })
            trades = _make_forecast_trades(active_pairs, fav_pair_ids)
            return templates.TemplateResponse(request, "components/signals_forecast.html", {
                **ctx, "trades": trades, "total": len(trades),
                "market_regime": market_regime,
                "market_volatility": market_volatility,
            })

        if mode == "short":
            active_pairs = pairs[pairs["signal_type"] != "wait"]
            active_pairs = active_pairs[(active_pairs["halflife"].isna()) | (active_pairs["halflife"] <= 7)]
            if active_pairs.empty:
                return templates.TemplateResponse(request, "components/signals_forecast.html", {
                    **ctx, "trades": [], "total": 0, "is_short": True,
                    "market_regime": market_regime,
                    "market_volatility": market_volatility,
                })
            trades = _make_forecast_trades(active_pairs, fav_pair_ids)
            return templates.TemplateResponse(request, "components/signals_forecast.html", {
                **ctx, "trades": trades, "total": len(trades), "is_short": True,
                "market_regime": market_regime,
                "market_volatility": market_volatility,
            })

        return templates.TemplateResponse(request, "components/signals_all.html", {
            **ctx, "signals": signals, "active": active, "total": len(signals),
            "n_active": len(active), "min_corr": min_corr, "max_days": max_days,
            "market_regime": market_regime,
            "market_volatility": market_volatility,
        })

    except Exception as e:
        return templates.TemplateResponse(request, "components/signals_all.html", {
            **ctx, "signals": [], "total": 0, "active": [], "error": str(e),
        })


@router.get("/dashboard", response_class=HTMLResponse)
async def tab_dashboard(
    request: Request,
    market: str = Query("crypto"),
):
    try:
        async with get_connection() as conn:
            dash = await _dash_data(conn, market)
    except Exception:
        dash = {"n_active": 0, "n_total": 0, "best_signal": None, "volatility": "Низкая"}

    return templates.TemplateResponse(request, "components/dashboard_partial.html", {
        "request": request, "market": market, **dash,
    })


@router.get("/portfolio", response_class=HTMLResponse)
async def tab_portfolio(request: Request, market: str = Query("crypto")):
    try:
        async with get_connection() as conn:
            pairs = await fetch_pairs(conn, market, 0.0)
            prices_df = await fetch_prices(conn, market)
    except Exception:
        return templates.TemplateResponse(request, "components/portfolio_tab.html", {
            "request": request, "pairs": [], "tickers": [], "n_coint": 0, "total": 0,
        })

    tickers = sorted(prices_df["ticker"].unique().tolist()) if not prices_df.empty else []
    top_pairs = pairs.head(6).to_dict(orient="records")
    all_pairs = pairs.to_dict(orient="records")
    n_coint = int(pairs["is_coint"].sum()) if not pairs.empty else 0

    return templates.TemplateResponse(request, "components/portfolio_tab.html", {
        "request": request, "market": market,
        "top_pairs": top_pairs, "all_pairs": all_pairs,
        "tickers": tickers, "n_coint": n_coint, "total": len(all_pairs),
    })


@router.get("/scanners", response_class=HTMLResponse)
async def tab_scanners(request: Request):
    return templates.TemplateResponse(
        request,
        "components/scanners_tab.html",
        {"request": request},
    )


@router.get("/scanner/{scanner_type}", response_class=HTMLResponse)
async def tab_scanner_content(
    request: Request,
    scanner_type: str,
    market: str = Query("crypto"),
    min_deviation: float = Query(0.2),
    limit: int = Query(20),
    min_drawdown: float = Query(10.0),
):
    template_map = {
        "corrbreak": "components/scanner_corrbreak.html",
        "momentum": "components/scanner_momentum.html",
        "drawdown": "components/scanner_drawdown.html",
    }
    template = template_map.get(scanner_type, "components/scanner_corrbreak.html")
    if scanner_type not in template_map:
        scanner_type = "corrbreak"

    ctx = {
        "request": request,
        "scanner": scanner_type,
        "market": market,
        "results": [],
        "total": 0,
    }

    try:
        async with get_connection() as conn:
            prices_df = await fetch_prices(conn, market)

        if prices_df.empty:
            return templates.TemplateResponse(request, template, ctx)

        wide = prices_df.pivot(index="date", columns="ticker", values="close")
        tickers_list = list(wide.columns)

        if scanner_type == "momentum":
            dates_list = list(wide.index.astype(str))
            df = momentum_scan(wide.values, tickers_list, dates_list)
            results = _df_records(df, limit)
        elif scanner_type == "drawdown":
            df = drawdown_scan(wide.values, tickers_list)
            df = df[df["drawdown_pct"] >= min_drawdown] if not df.empty else df
            results = _df_records(df)
        else:
            if len(tickers_list) < 2:
                df = pd.DataFrame()
            else:
                df = corr_breakdown_scan(wide, tickers_list)
                df = df[df["deviation"] >= min_deviation] if not df.empty else df
            results = _df_records(df)

        return templates.TemplateResponse(request, template, {
            **ctx,
            "results": results,
            "total": len(results),
        })
    except Exception as e:
        return templates.TemplateResponse(request, template, {
            **ctx,
            "error": str(e) if str(e) else "Scanner unavailable",
        })


def _forecast_status(z_now, z_entry, signal_type, days_held, hl, pnl_pct):
    """Compute forecast/recommendation status for a favorite position."""
    # Default
    status = "Держать"
    status_color = "green"
    status_detail = ""
    recommendation = " holding"

    hl = _finite_int(hl)
    z_now = _finite_float(z_now)
    z_entry = _finite_float(z_entry)
    abs_z_now = abs(z_now) if z_now is not None else 0
    abs_z_entry = abs(z_entry) if z_entry is not None else 0

    # HL remaining
    hl_remaining = max(0, int(hl) - days_held) if hl else None
    is_expired = bool(hl and days_held >= int(hl))

    # Where on the Z-path are we?
    # Entry at |Z|>=2, exit target at |Z|<=0.5
    progress_pct = 0
    if abs_z_entry > 0:
        progress_pct = round((abs_z_entry - abs_z_now) / (abs_z_entry - 0.5) * 100) if abs_z_entry > 0.5 else 100
        progress_pct = max(0, min(100, progress_pct))

    # Status determination
    if z_now is not None and abs_z_now <= 0.5:
        status = "Цель достигнута"
        status_color = "green"
        status_detail = f"Z={z_now:.2f}, цель ±0.5 достигнута"
        recommendation = "close"
    elif is_expired:
        status = "Просрочен"
        status_color = "red"
        status_detail = f"Держали {days_held} дн. при HL={hl}. Цена могла уйти"
        recommendation = "close_warn"
    elif hl_remaining is not None and hl_remaining <= 2:
        status = "Скоро закрытие"
        status_color = "orange"
        status_detail = f"Осталось {hl_remaining} дн. до конца HL"
        recommendation = "hold_warn"
    elif abs_z_now >= 3.5:
        status = "Стоп-лосс"
        status_color = "red"
        status_detail = f"Z={z_now:.2f} — пробит стоп ±3.5"
        recommendation = "close"
    else:
        # Normal holding
        if hl_remaining is not None:
            status_detail = f"Держать ещё {hl_remaining} дн. (осталось из HL={hl})"
        else:
            status_detail = "Ожидание возврата Z к среднему"

    # Anomaly check: Z went wrong direction (increased beyond entry)
    if abs_z_now > abs_z_entry * 1.3 and abs_z_entry > 0:
        status = "Отклонение"
        status_color = "red"
        status_detail = f"Z растёт не в ту сторону: вход {z_entry}, сейчас {z_now}"
        recommendation = "close_warn"

    # Progress display
    progress_str = f"{progress_pct}% пути к цели"

    return {
        "status": status,
        "status_color": status_color,
        "status_detail": status_detail,
        "recommendation": recommendation,
        "progress_pct": progress_pct,
        "progress_str": progress_str,
        "hl_remaining": hl_remaining,
        "is_expired": is_expired,
        "z_now": z_now,
        "z_entry": z_entry,
        "days_held": days_held,
    }


@router.get("/favorites", response_class=HTMLResponse)
async def tab_favorites(request: Request):
    active = []
    user = await get_current_or_legacy_user(request)
    if user is None:
        return templates.TemplateResponse(request, "components/favorites_tab.html", {
            "request": request,
            "favorites": [],
            "auth_required": True,
            "has_ru_favorites": False,
        })
    try:
        async with get_connection() as conn:
            favs = await fetch_favorites(conn, user.id)
            model_backfilled = False
            for _, favorite in favs.iterrows():
                fixed_sd = _finite_float(favorite.get("spread_sd_entry"))
                if (
                    _finite_float(favorite.get("hedge_ratio_entry")) is None
                    or _finite_float(favorite.get("spread_mean_entry")) is None
                    or fixed_sd is None
                    or fixed_sd <= 0
                ):
                    if await ensure_favorite_z_model(conn, favorite):
                        model_backfilled = True
            if model_backfilled:
                favs = await fetch_favorites(conn, user.id)

        if favs.empty:
            return templates.TemplateResponse(request, "components/favorites_tab.html", {
                "request": request,
                "favorites": [],
                "has_ru_favorites": False,
            })

        # Fetch latest price per ticker + price history for Z-score recalculation
        ticker_keys = set()
        has_ru_favorites = False
        for _, favorite in favs.iterrows():
            market = favorite.get("market") or "crypto"
            has_ru_favorites = has_ru_favorites or market == "ru"
            ticker_keys.add((market, favorite["ticker_a"]))
            ticker_keys.add((market, favorite["ticker_b"]))
        latest_prices = {}
        price_history = {}  # (market, ticker) -> np.array of close prices
        pair_risks = {}
        ru_live_prices, ru_live_updated_at = get_ru_live_snapshot()

        async with get_connection() as conn:
            for market, ticker in ticker_keys:
                cursor = await conn.execute(
                    """
                    SELECT close FROM prices
                    WHERE ticker = ? AND market = ?
                    ORDER BY date
                    """,
                    (ticker, market)
                )
                rows = await cursor.fetchall()
                if rows:
                    prices = np.array([float(r[0]) for r in rows if r[0] and r[0] > 0])
                    price_history[(market, ticker)] = prices
                    latest_prices[(market, ticker)] = (
                        float(prices[-1]) if len(prices) > 0 else 0
                    )

                # Binance live prices
                if market == "crypto":
                    try:
                        from app.data.binance_ws import get_live_price
                        live = get_live_price(ticker)
                        if live is not None and live > 0:
                            latest_prices[(market, ticker)] = float(live)
                    except ImportError:
                        pass
                elif market == "ru":
                    live = ru_live_prices.get(ticker)
                    if live is not None and live > 0:
                        latest_prices[(market, ticker)] = float(live)
            cursor = await conn.execute("SELECT * FROM pairs")
            for pair_row in await cursor.fetchall():
                pair_data = dict(pair_row)
                pair_risks[(
                    pair_data.get("market"),
                    pair_data.get("ticker_a"),
                    pair_data.get("ticker_b"),
                )] = pair_data

        for _, row in favs.iterrows():
            market = row.get("market") or "crypto"
            entry_a = row.get("price_a_entry")
            entry_b = row.get("price_b_entry")

            # Backfill entry from DB if 0
            if not entry_a or entry_a == 0:
                entry_a = latest_prices.get((market, row["ticker_a"]), 0)
            if not entry_b or entry_b == 0:
                entry_b = latest_prices.get((market, row["ticker_b"]), 0)

            p_a = latest_prices.get((market, row["ticker_a"]), 0)
            p_b = latest_prices.get((market, row["ticker_b"]), 0)
            pnl_a = (p_a / entry_a - 1) * 100 if entry_a and entry_a > 0 and p_a else 0
            pnl_b = (p_b / entry_b - 1) * 100 if entry_b and entry_b > 0 and p_b else 0
            st = row.get("signal_type", "wait")
            total_pnl = (-pnl_a + pnl_b) if st == "short_a" else (pnl_a - pnl_b) if st == "long_a" else 0

            hl = _finite_int(row.get("halflife"))
            z_at_entry = _finite_float(row.get("z_at_entry"))
            entry_time = row.get("entry_time")
            holding_timing = estimate_signal_timing(entry_time, hl)
            days_held = holding_timing["signal_days_elapsed"]

            # Recalculate current Z-score from price history
            z_now_live = compute_fixed_zscore(
                p_a,
                p_b,
                row.get("hedge_ratio_entry"),
                row.get("spread_mean_entry"),
                row.get("spread_sd_entry"),
            )
            if z_now_live is not None:
                z_now_live = round(z_now_live, 2)
            ta_hist = price_history.get((market, row["ticker_a"]))
            tb_hist = price_history.get((market, row["ticker_b"]))
            if (
                z_now_live is None
                and ta_hist is not None
                and tb_hist is not None
                and len(ta_hist) >= 60
                and len(tb_hist) >= 60
            ):
                min_len = min(len(ta_hist), len(tb_hist))
                pa_arr = ta_hist[-min_len:]
                pb_arr = tb_hist[-min_len:]
                if market == "ru":
                    pa_arr = pa_arr[-252:]
                    pb_arr = pb_arr[-252:]
                cg = engle_granger(pa_arr, pb_arr)
                zres = compute_zscore(pa_arr, pb_arr, cg["hedge_ratio"])
                if zres["z_now"] is not None:
                    z_now_live = round(float(zres["z_now"]), 2)
                    # Use dynamic HL if original is missing
                    if not hl and cg["halflife"]:
                        hl = _finite_int(cg["halflife"])

            # If we couldn't recalc, use the stored z_at_entry as fallback
            z_now_for_status = z_now_live if z_now_live is not None else z_at_entry

            # Compute forecast status
            fc = _forecast_status(
                z_now=z_now_for_status,
                z_entry=z_at_entry,
                signal_type=st,
                days_held=days_held,
                hl=hl,
                pnl_pct=total_pnl,
            )
            pair_risk = pair_risks.get(
                (market, row["ticker_a"], row["ticker_b"]),
                {},
            )
            signal_eligible = _finite_bool(
                pair_risk.get(
                    "signal_eligible",
                    0 if market == "ru" else 1,
                )
            )
            risk_reason = pair_risk.get("risk_reason")
            if market == "ru" and not pair_risk:
                risk_reason = "Пара отсутствует в свежем анализе"
            if market == "ru" and not signal_eligible:
                fc.update({
                    "status": "Перепроверить",
                    "status_color": "orange",
                    "status_detail": (
                        risk_reason
                        or "Текущая модель больше не подтверждает сигнал"
                    ),
                    "recommendation": "hold_warn",
                })

            active.append({
                "id": int(row["id"]),
                "pair": row["pair"],
                "market": market,
                "ticker_a": row["ticker_a"],
                "ticker_b": row["ticker_b"],
                "signal": row.get("signal", ""),
                "signal_type": st,
                "pnl_total_pct": round(float(total_pnl), 2),
                "price_a_entry": round(float(entry_a), 4) if entry_a else None,
                "price_b_entry": round(float(entry_b), 4) if entry_b else None,
                "price_a_now": round(float(p_a), 4) if p_a else None,
                "price_b_now": round(float(p_b), 4) if p_b else None,
                "entry_time": entry_time,
                "corr": row.get("corr"),
                "halflife": hl,
                "days_held": days_held,
                "z_at_entry": round(float(z_at_entry), 2) if z_at_entry else None,
                "z_now_live": z_now_live,
                "signal_eligible": signal_eligible,
                "is_coint_stable": _finite_bool(
                    pair_risk.get("is_coint_stable")
                ),
                "coint_stability": _finite_float(
                    pair_risk.get("coint_stability")
                ),
                "market_regime": pair_risk.get("market_regime") or "normal",
                "market_volatility": _finite_float(
                    pair_risk.get("market_volatility")
                ),
                "event_risk": _finite_bool(pair_risk.get("event_risk")),
                "risk_reason": risk_reason,
                **holding_timing,
                **fc,
            })

    except Exception as e:
        return templates.TemplateResponse(request, "components/favorites_tab.html", {
            "request": request,
            "favorites": [],
            "has_ru_favorites": False,
            "error": str(e) if str(e) else "DB unavailable",
        })

    ru_live_time = None
    if ru_live_updated_at is not None:
        ru_live_time = ru_live_updated_at.astimezone(
            ZoneInfo("Europe/Moscow")
        ).strftime("%d.%m, %H:%M МСК")
    return templates.TemplateResponse(request, "components/favorites_tab.html", {
        "request": request,
        "favorites": active,
        "has_ru_favorites": has_ru_favorites,
        "ru_live_updated_at": ru_live_time,
    })


@router.get("/favorites/history", response_class=HTMLResponse)
async def tab_favorites_history(request: Request, limit: int = Query(10)):
    user = await get_current_or_legacy_user(request)
    if user is None:
        return templates.TemplateResponse(request, "components/favorites_history.html", {
            "request": request,
            "history": [],
        })
    try:
        async with get_connection() as conn:
            hist = await fetch_favorites_history(conn, user_id=user.id, limit=limit)
        history = hist.to_dict(orient="records") if not hist.empty else []
    except Exception as e:
        return templates.TemplateResponse(request, "components/favorites_history.html", {
            "request": request,
            "history": [],
            "error": str(e) if str(e) else "DB unavailable",
        })

    return templates.TemplateResponse(request, "components/favorites_history.html", {
        "request": request,
        "history": history,
    })


@router.get("/data", response_class=HTMLResponse)
async def tab_data(request: Request):
    try:
        async with get_connection() as conn:
            status = await db_status(conn)
    except Exception:
        status = {"n_tickers": 0, "n_rows": 0, "date_min": None, "date_max": None,
                  "n_pairs": 0, "last_analysis": None, "last_update": None}

    return templates.TemplateResponse(request, "components/data_tab.html", {
        "request": request, "status": status,
    })


@router.get("/ai", response_class=HTMLResponse)
async def tab_ai(request: Request, market: str = Query("crypto")):
    return templates.TemplateResponse(request, "components/ai_tab.html", {
        "request": request,
        "market": market,
    })
