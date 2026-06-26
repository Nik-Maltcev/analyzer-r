"""Tab routes — serve HTML partials for HTMX tab/content swaps with real data."""

import numpy as np
import pandas as pd
from datetime import datetime, timezone
from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from app.db.database import (
    get_connection, fetch_pairs, fetch_prices, fetch_favorites,
    fetch_favorites_history, db_status,
)
from app.core.cointegration import engle_granger, compute_zscore, forecast_zscore

router = APIRouter(prefix="/tab", tags=["ui"])
templates = Jinja2Templates(directory="app/templates")


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


def _make_signal_cards(pairs, market="crypto", fav_pairs=None):
    """Convert pairs DataFrame to list of dicts for template rendering."""
    fav_set = set(fav_pairs) if fav_pairs else set()
    signals = []
    for _, row in pairs.iterrows():
        corr_val = _finite_float(row.get("corr"))
        z_now = _finite_float(row.get("z_now"))
        zf = _finite_float(row.get("z_forecast"))
        hl = _finite_int(row.get("halflife"))
        score = _finite_float(row.get("score"))
        tomorrow_move = _project_tomorrow_move(z_now, hl)
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
            **tomorrow_move,
            "signal": row.get("signal", "Ждать"),
            "signal_type": row.get("signal_type", "wait"),
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
        tomorrow_move = _project_tomorrow_move(z_now, hl)
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
            **tomorrow_move,
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
        "volatility": "Средняя",
    }


@router.get("/signals", response_class=HTMLResponse)
async def tab_signals(
    request: Request,
    market: str = Query("crypto"),
    mode: str = Query("all"),
    min_corr: float = Query(0.5),
    max_days: int = Query(30),
):
    ctx = {
        "request": request,
        "mode": mode,
        "market": market,
        "min_corr": min_corr,
        "max_days": max_days,
    }

    try:
        async with get_connection() as conn:
            pairs = await fetch_pairs(conn, market, min_corr)

        if pairs.empty:
            return templates.TemplateResponse("components/signals_all.html", {
                **ctx, "signals": [], "total": 0, "active": [],
            })

        # Filter by half-life
        pairs = pairs[(pairs["halflife"].isna()) | (pairs["halflife"] <= max_days)]

        # Fetch existing favorite pair IDs to render star state
        # Wrapped in try/except so favorites query failure doesn't kill signals
        fav_pair_ids = set()
        try:
            async with get_connection() as conn:
                cursor = await conn.execute("SELECT pair FROM favorites WHERE status = 'active'")
                fav_rows = await cursor.fetchall()
                fav_pair_ids = set(r[0] for r in fav_rows)
        except Exception:
            pass

        signals = _make_signal_cards(pairs, market, fav_pair_ids)
        active = [s for s in signals if s["signal_type"] != "wait"]

        if mode == "forecast":
            active_pairs = pairs[pairs["signal_type"] != "wait"]
            if active_pairs.empty:
                return templates.TemplateResponse("components/signals_forecast.html", {
                    **ctx, "trades": [], "total": 0,
                })
            trades = _make_forecast_trades(active_pairs, fav_pair_ids)
            return templates.TemplateResponse("components/signals_forecast.html", {
                **ctx, "trades": trades, "total": len(trades),
            })

        if mode == "short":
            active_pairs = pairs[pairs["signal_type"] != "wait"]
            active_pairs = active_pairs[(active_pairs["halflife"].isna()) | (active_pairs["halflife"] <= 7)]
            if active_pairs.empty:
                return templates.TemplateResponse("components/signals_forecast.html", {
                    **ctx, "trades": [], "total": 0, "is_short": True,
                })
            trades = _make_forecast_trades(active_pairs, fav_pair_ids)
            return templates.TemplateResponse("components/signals_forecast.html", {
                **ctx, "trades": trades, "total": len(trades), "is_short": True,
            })

        return templates.TemplateResponse("components/signals_all.html", {
            **ctx, "signals": signals, "active": active, "total": len(signals),
            "n_active": len(active), "min_corr": min_corr, "max_days": max_days,
        })

    except Exception as e:
        return templates.TemplateResponse("components/signals_all.html", {
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

    return templates.TemplateResponse("components/dashboard_partial.html", {
        "request": request, "market": market, **dash,
    })


@router.get("/portfolio", response_class=HTMLResponse)
async def tab_portfolio(request: Request, market: str = Query("crypto")):
    try:
        async with get_connection() as conn:
            pairs = await fetch_pairs(conn, market, 0.0)
            prices_df = await fetch_prices(conn, market)
    except Exception:
        return templates.TemplateResponse("components/portfolio_tab.html", {
            "request": request, "pairs": [], "tickers": [], "n_coint": 0, "total": 0,
        })

    tickers = sorted(prices_df["ticker"].unique().tolist()) if not prices_df.empty else []
    top_pairs = pairs.head(6).to_dict(orient="records")
    all_pairs = pairs.to_dict(orient="records")
    n_coint = int(pairs["is_coint"].sum()) if not pairs.empty else 0

    return templates.TemplateResponse("components/portfolio_tab.html", {
        "request": request, "market": market,
        "top_pairs": top_pairs, "all_pairs": all_pairs,
        "tickers": tickers, "n_coint": n_coint, "total": len(all_pairs),
    })


@router.get("/scanners", response_class=HTMLResponse)
async def tab_scanners(request: Request):
    return templates.TemplateResponse("components/scanners_tab.html", {"request": request})


@router.get("/scanner/{scanner_type}", response_class=HTMLResponse)
async def tab_scanner_content(request: Request, scanner_type: str, market: str = Query("crypto")):
    template_map = {
        "corrbreak": "components/scanner_corrbreak.html",
        "momentum": "components/scanner_momentum.html",
        "drawdown": "components/scanner_drawdown.html",
    }
    template = template_map.get(scanner_type, "components/scanner_corrbreak.html")
    return templates.TemplateResponse(template, {"request": request, "scanner": scanner_type, "market": market})


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
    if abs_z_now <= 0.5:
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
    try:
        async with get_connection() as conn:
            favs = await fetch_favorites(conn)

        if favs.empty:
            return templates.TemplateResponse("components/favorites_tab.html", {
                "request": request, "favorites": [],
            })

        # Fetch latest price per ticker + price history for Z-score recalculation
        tickers_set = set(favs["ticker_a"].tolist() + favs["ticker_b"].tolist())
        latest_prices = {}
        price_history = {}  # ticker -> np.array of close prices

        async with get_connection() as conn:
            for ticker in tickers_set:
                cursor = await conn.execute(
                    "SELECT close FROM prices WHERE ticker = ? ORDER BY date",
                    (ticker,)
                )
                rows = await cursor.fetchall()
                if rows:
                    prices = np.array([float(r[0]) for r in rows if r[0] and r[0] > 0])
                    price_history[ticker] = prices
                    latest_prices[ticker] = float(prices[-1]) if len(prices) > 0 else 0

                # Binance live prices
                try:
                    from app.data.binance_ws import get_live_price
                    live = get_live_price(ticker)
                    if live is not None and live > 0:
                        latest_prices[ticker] = float(live)
                except ImportError:
                    pass

        for _, row in favs.iterrows():
            entry_a = row.get("price_a_entry")
            entry_b = row.get("price_b_entry")

            # Backfill entry from DB if 0
            if not entry_a or entry_a == 0:
                entry_a = latest_prices.get(row["ticker_a"], 0)
            if not entry_b or entry_b == 0:
                entry_b = latest_prices.get(row["ticker_b"], 0)

            p_a = latest_prices.get(row["ticker_a"], 0)
            p_b = latest_prices.get(row["ticker_b"], 0)
            pnl_a = (p_a / entry_a - 1) * 100 if entry_a and entry_a > 0 and p_a else 0
            pnl_b = (p_b / entry_b - 1) * 100 if entry_b and entry_b > 0 and p_b else 0
            st = row.get("signal_type", "wait")
            total_pnl = (-pnl_a + pnl_b) if st == "short_a" else (pnl_a - pnl_b) if st == "long_a" else 0

            hl = _finite_int(row.get("halflife"))
            z_at_entry = _finite_float(row.get("z_at_entry"))
            entry_time = row.get("entry_time")
            days_held = 0
            if entry_time:
                try:
                    entry_dt = datetime.fromisoformat(entry_time.replace(" ", "T"))
                    if entry_dt.tzinfo is None:
                        entry_dt = entry_dt.replace(tzinfo=timezone.utc)
                    days_held = max(0, (datetime.now(timezone.utc) - entry_dt).days)
                except Exception:
                    pass

            # Recalculate current Z-score from price history
            z_now_live = None
            ta_hist = price_history.get(row["ticker_a"])
            tb_hist = price_history.get(row["ticker_b"])
            if ta_hist is not None and tb_hist is not None and len(ta_hist) >= 60 and len(tb_hist) >= 60:
                min_len = min(len(ta_hist), len(tb_hist))
                pa_arr = ta_hist[-min_len:]
                pb_arr = tb_hist[-min_len:]
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

            active.append({
                "id": int(row["id"]),
                "pair": row["pair"],
                "ticker_a": row["ticker_a"],
                "ticker_b": row["ticker_b"],
                "signal": row.get("signal", ""),
                "signal_type": st,
                "pnl_total_pct": round(float(total_pnl), 2),
                "entry_time": entry_time,
                "corr": row.get("corr"),
                "halflife": hl,
                "days_held": days_held,
                "z_at_entry": round(float(z_at_entry), 2) if z_at_entry else None,
                "z_now_live": z_now_live,
                **fc,
            })

    except Exception as e:
        return templates.TemplateResponse("components/favorites_tab.html", {
            "request": request, "favorites": [], "error": str(e) if str(e) else "DB unavailable",
        })

    return templates.TemplateResponse("components/favorites_tab.html", {
        "request": request, "favorites": active,
    })


@router.get("/favorites/history", response_class=HTMLResponse)
async def tab_favorites_history(request: Request, limit: int = Query(10)):
    try:
        async with get_connection() as conn:
            hist = await fetch_favorites_history(conn, limit=limit)
        history = hist.to_dict(orient="records") if not hist.empty else []
    except Exception as e:
        return templates.TemplateResponse("components/favorites_history.html", {
            "request": request,
            "history": [],
            "error": str(e) if str(e) else "DB unavailable",
        })

    return templates.TemplateResponse("components/favorites_history.html", {
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

    return templates.TemplateResponse("components/data_tab.html", {
        "request": request, "status": status,
    })


@router.get("/ai", response_class=HTMLResponse)
async def tab_ai(request: Request):
    return templates.TemplateResponse("components/ai_tab.html", {"request": request})
