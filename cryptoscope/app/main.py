"""MEANX FastAPI application entry point."""

import asyncio
import os
import sys
import time
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.access import get_access_state, is_admin_user
from app.api.auth import router as auth_router
from app.api.charts import router as charts_router
from app.api.crypto_picks import router as crypto_picks_router
from app.api.crypto_v2 import router as crypto_v2_router
from app.api.data_view import router as data_router
from app.api.favorites import router as favorites_router
from app.api.health import router as health_router
from app.api.locale import router as locale_router
from app.api.market_regime import router as market_regime_router
from app.api.momentum_portfolio import router as momentum_portfolio_router
from app.api.payments import router as payments_router
from app.api.portfolio import router as portfolio_router
from app.api.public_content import router as public_content_router
from app.api.public_extension import router as public_extension_router
from app.api.scanners import router as scanners_router
from app.api.signals import router as signals_router
from app.api.ui_routes import router as ui_router
from app.auth import get_current_user
from app.config import get_settings
from app.db.database import db_status, fetch_pairs, get_connection, init_db, set_db_path
from app.product import (
    BASE_PRODUCT_NAME,
    get_product_profile,
    get_user_enabled_markets,
    normalize_market,
)
from app.ui.templates import templates

# Ensure cryptoscope is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

settings = get_settings()
set_db_path(settings.db_path)

START_TIME = time.time()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await init_db(settings.db_path)
    print(f"{get_product_profile(settings).name} starting on {settings.host}:{settings.port}")
    print(f"DB path: {settings.db_path}")

    # Keep MEXC public spot prices warm for user-triggered refreshes.
    ws_task = None
    try:
        from app.data.mexc_market import connect_mexc_market
        ws_task = asyncio.create_task(connect_mexc_market())
        print("[MEXC] Public spot price poller started in background")
    except ImportError:
        print("[MEXC] Live prices unavailable")

    yield
    # Shutdown
    print(f"{get_product_profile(settings).name} shutting down")
    if ws_task:
        ws_task.cancel()
        with suppress(asyncio.CancelledError):
            await ws_task


app = FastAPI(
    title=BASE_PRODUCT_NAME,
    description="Crypto/stock/forex pairs trading analysis terminal",
    version="1.0.0",
    lifespan=lifespan,
)


@app.middleware("http")
async def enforce_subscription_access(request: Request, call_next):
    path = request.url.path
    protected = (
        path.startswith("/tab/")
        or (
            path.startswith("/api/")
            and not path.startswith((
                "/api/auth/",
                "/api/locale",
                "/api/payments/",
                "/api/public/",
            ))
        )
    )
    if not protected:
        return await call_next(request)

    user = getattr(request.state, "current_user", None)
    if user is None:
        user = await get_current_user(request)
    access = await get_access_state(user)
    if access.has_access:
        return await call_next(request)

    if path.startswith("/tab/"):
        return templates.TemplateResponse(
            request,
            "components/access_gate.html",
            {
                "request": request,
                "access": access.as_dict(),
            },
        )

    status_code = 401 if access.status == "unauthenticated" else 402
    return JSONResponse(
        status_code=status_code,
        content={
            "detail": (
                "Authentication required"
                if status_code == 401
                else "Subscription required"
            ),
            "access": access.as_dict(),
        },
    )


@app.middleware("http")
async def enforce_product_markets(request: Request, call_next):
    if request.url.path.startswith("/static/"):
        return await call_next(request)

    profile = get_product_profile()
    user = await get_current_user(request)
    enabled_markets = get_user_enabled_markets(
        profile,
        is_admin=is_admin_user(user),
    )
    request.state.current_user = user
    request.state.enabled_markets = enabled_markets

    market = request.query_params.get("market")
    if (
        market
        and request.url.path.startswith(("/api/", "/tab/"))
        and market not in enabled_markets
    ):
        return JSONResponse(
            status_code=404,
            content={"detail": "Market is not available"},
        )
    return await call_next(request)


# Static files
app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.include_router(health_router)
app.include_router(auth_router, prefix="/api")
app.include_router(signals_router, prefix="/api")
app.include_router(portfolio_router, prefix="/api")
app.include_router(scanners_router, prefix="/api")
app.include_router(favorites_router, prefix="/api")
app.include_router(data_router, prefix="/api")
app.include_router(charts_router, prefix="/api")
app.include_router(locale_router, prefix="/api")
app.include_router(public_extension_router, prefix="/api")
app.include_router(public_content_router, prefix="/api")
app.include_router(payments_router)
app.include_router(ui_router)
app.include_router(crypto_picks_router)
app.include_router(crypto_v2_router)
app.include_router(momentum_portfolio_router)
app.include_router(market_regime_router)


async def _get_dashboard_context(market: str = "crypto"):
    try:
        async with get_connection() as conn:
            pairs = await fetch_pairs(conn, market, 0.5)
            st = await db_status(conn)
    except Exception:
        return {"n_active": 0, "n_total": 0, "best_signal": None,
                "volatility": "Низкая", "last_analysis": None}

    if pairs.empty:
        return {"n_active": 0, "n_total": 0, "best_signal": None,
                "volatility": "Низкая", "last_analysis": None}

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
        best = {"pair": f"{br['ticker_a']}/{br['ticker_b']}",
                "z_now": round(float(br.get("z_now", 0) or 0), 2),
                "strength": br.get("strength", "Нет")}

    return {
        "n_active": n_active,
        "n_total": len(pairs),
        "best_signal": best,
        "volatility": volatility,
        "last_analysis": st.get("last_analysis"),
        "db_tickers": st.get("n_tickers", 0),
        "db_rows": st.get("n_rows", 0),
    }


@app.get("/", response_class=HTMLResponse)
async def landing(request: Request):
    """Public product landing page."""
    return templates.TemplateResponse(request, "landing.html", {
        "request": request,
    })


@app.get("/privacy/extension", response_class=HTMLResponse)
async def extension_privacy(request: Request):
    """Public localized privacy policy for regional browser extensions."""
    return templates.TemplateResponse(request, "extension_privacy.html", {
        "request": request,
        "profile": get_product_profile(),
    })


@app.get("/app", response_class=HTMLResponse)
async def app_page(
    request: Request,
    market: str | None = Query(None),
):
    """Full app page."""
    user = getattr(request.state, "current_user", None)
    if user is None:
        user = await get_current_user(request)
    enabled_markets = getattr(
        request.state,
        "enabled_markets",
        get_product_profile().enabled_markets,
    )
    market = normalize_market(market, enabled_markets=enabled_markets)
    access = await get_access_state(user)
    dash = (
        await _get_dashboard_context(market)
        if access.has_access
        else {
            "n_active": 0,
            "n_total": 0,
            "best_signal": None,
            "volatility": "—",
            "last_analysis": None,
        }
    )
    return templates.TemplateResponse(request, "index.html", {
        "request": request,
        "settings": settings,
        "market": market,
        "access": access.as_dict(),
        **dash,
    })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=settings.host, port=settings.port)
