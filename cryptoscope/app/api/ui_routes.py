"""Tab routes — serve HTML partials for HTMX tab/content swaps with real data."""

from datetime import datetime, timezone
from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from app.db.database import get_connection, fetch_pairs, fetch_prices, fetch_favorites, db_status

router = APIRouter(prefix="/tab", tags=["ui"])
templates = Jinja2Templates(directory="app/templates")


def _make_signal_cards(pairs, market="crypto", fav_pairs=None):
    """Convert pairs DataFrame to list of dicts for template rendering."""
    fav_set = set(fav_pairs) if fav_pairs else set()
    signals = []
    for _, row in pairs.iterrows():
        corr_val = row.get("corr")
        z_now = row.get("z_now")
        zf = row.get("z_forecast")
        hl = row.get("halflife")
        pair_id = f"{row['ticker_a']}_{row['ticker_b']}"
        signals.append({
            "pair_id": pair_id,
            "ticker_a": row["ticker_a"],
            "ticker_b": row["ticker_b"],
            "corr": round(float(corr_val), 2) if corr_val is not None else None,
            "corr_pct": round(float(corr_val) * 100) if corr_val is not None else None,
            "is_coint": bool(row.get("is_coint")),
            "halflife": int(hl) if hl is not None else None,
            "score": round(float(row.get("score", 0)), 3) if row.get("score") is not None else None,
            "z_now": round(float(z_now), 2) if z_now is not None else None,
            "z_forecast": round(float(zf), 2) if zf is not None else None,
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
        z_now = row.get("z_now", 0) or 0
        hl = row.get("halflife", 30) or 30
        pnl_est = abs(z_now) - 0.5
        pnl_pct = max(0, round(float(pnl_est), 2))
        win_rate = 65 if row.get("is_coint") else 50
        corr_val = row.get("corr")
        pair_id = f"{row['ticker_a']}_{row['ticker_b']}"

        trades.append({
            "pair": f"{row['ticker_a']}/{row['ticker_b']}",
            "pair_id": pair_id,
            "ticker_a": row["ticker_a"],
            "ticker_b": row["ticker_b"],
            "signal": row.get("signal", ""),
            "signal_type": row.get("signal_type", "wait"),
            "strength": row.get("strength", "Нет"),
            "corr": round(float(corr_val), 2) if corr_val is not None else None,
            "is_coint": bool(row.get("is_coint")),
            "halflife": int(hl) if hl is not None else None,
            "z_now": round(float(z_now), 2) if z_now else None,
            "z_forecast": round(float(row.get("z_forecast", 0) or 0), 2),
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
    ctx = {"request": request, "mode": mode, "market": market}

    try:
        async with get_connection() as conn:
            pairs = await fetch_pairs(conn, market, min_corr)

        if pairs.empty:
            return templates.TemplateResponse("components/signals_all.html", {
                **ctx, "signals": [], "total": 0, "active": 0,
            })

        # Filter by half-life
        pairs = pairs[(pairs["halflife"].isna()) | (pairs["halflife"] <= max_days)]

        # Fetch existing favorite pair IDs to render star state
        async with get_connection() as conn:
            cursor = await conn.execute("SELECT pair FROM favorites WHERE status = 'active'")
            fav_rows = await cursor.fetchall()
            fav_pair_ids = set(r[0] for r in fav_rows)

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
            **ctx, "signals": [], "total": 0, "active": 0, "error": str(e),
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

        # Fetch latest price per ticker — only for tickers in favorites
        tickers_set = set(favs["ticker_a"].tolist() + favs["ticker_b"].tolist())
        async with get_connection() as conn:
            latest_prices = {}
            for ticker in tickers_set:
                cursor = await conn.execute(
                    "SELECT close FROM prices WHERE ticker = ? ORDER BY date DESC LIMIT 1",
                    (ticker,)
                )
                row = await cursor.fetchone()
                if row:
                    latest_prices[ticker] = float(row[0])

            # Try Binance live prices
            for ticker in tickers_set:
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
            
            # Backfill entry from DB if 0 or missing
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

            hl = row.get("halflife")
            entry_time = row.get("entry_time")
            days_held = 0
            hl_remaining = hl
            is_expired = False
            if entry_time:
                try:
                    entry_dt = datetime.fromisoformat(entry_time.replace(" ", "T"))
                    if entry_dt.tzinfo is None:
                        entry_dt = entry_dt.replace(tzinfo=timezone.utc)
                    days_held = max(0, (datetime.now(timezone.utc) - entry_dt).days)
                    if hl:
                        hl_remaining = max(0, int(hl) - days_held)
                        if days_held >= int(hl):
                            is_expired = True
                except Exception:
                    pass

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
                "hl_remaining": hl_remaining,
                "is_expired": is_expired,
            })

    except Exception as e:
        return templates.TemplateResponse("components/favorites_tab.html", {
            "request": request, "favorites": [], "error": str(e) if str(e) else "DB unavailable",
        })

    return templates.TemplateResponse("components/favorites_tab.html", {
        "request": request, "favorites": active,
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
