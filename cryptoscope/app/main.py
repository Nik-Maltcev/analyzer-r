"""CryptoScope — FastAPI application entry point."""

import asyncio
import os
import sys
import time
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse

from app.config import get_settings
from app.db.database import set_db_path, init_db, get_connection, fetch_pairs, db_status

# Ensure cryptoscope is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

settings = get_settings()
set_db_path(settings.db_path)

templates = Jinja2Templates(directory="app/templates")

START_TIME = time.time()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await init_db(settings.db_path)
    print(f"CryptoScope starting on {settings.host}:{settings.port}")
    print(f"DB path: {settings.db_path}")

    # Start Binance WebSocket for live prices
    ws_task = None
    try:
        from app.data.binance_ws import connect_binance_ws
        ws_task = asyncio.create_task(connect_binance_ws())
        print("[Binance] Price stream started in background")
    except ImportError:
        print("[Binance] websockets not available, live prices disabled")

    yield
    # Shutdown
    print("CryptoScope shutting down")
    if ws_task:
        ws_task.cancel()
        with suppress(asyncio.CancelledError):
            await ws_task


app = FastAPI(
    title="CryptoScope",
    description="Crypto/stock/forex pairs trading analysis terminal",
    version="1.0.0",
    lifespan=lifespan,
)

# Static files
app.mount("/static", StaticFiles(directory="app/static"), name="static")

# Import and include routers
from app.api.health import router as health_router
from app.api.signals import router as signals_router
from app.api.portfolio import router as portfolio_router
from app.api.scanners import router as scanners_router
from app.api.favorites import router as favorites_router
from app.api.data_view import router as data_router
from app.api.ai import router as ai_router
from app.api.ui_routes import router as ui_router
from app.api.charts import router as charts_router

app.include_router(health_router)
app.include_router(signals_router, prefix="/api")
app.include_router(portfolio_router, prefix="/api")
app.include_router(scanners_router, prefix="/api")
app.include_router(favorites_router, prefix="/api")
app.include_router(data_router, prefix="/api")
app.include_router(ai_router, prefix="/api")
app.include_router(charts_router, prefix="/api")
app.include_router(ui_router)


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
        "volatility": "Средняя",
        "last_analysis": st.get("last_analysis"),
        "db_tickers": st.get("n_tickers", 0),
        "db_rows": st.get("n_rows", 0),
    }


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Main page with pre-rendered signals data."""
    dash = await _get_dashboard_context("crypto")
    return templates.TemplateResponse("index.html", {
        "request": request,
        "settings": settings,
        "market": "crypto",
        **dash,
    })


@app.get("/app", response_class=HTMLResponse)
async def app_page(request: Request):
    """Full app page."""
    dash = await _get_dashboard_context("crypto")
    return templates.TemplateResponse("index.html", {
        "request": request,
        "settings": settings,
        "market": "crypto",
        **dash,
    })


@app.get("/onboarding", response_class=HTMLResponse)
async def onboarding(request: Request):
    """Onboarding wizard page."""
    return templates.TemplateResponse("onboarding.html", {
        "request": request,
        "settings": settings,
    })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=settings.host, port=settings.port)
