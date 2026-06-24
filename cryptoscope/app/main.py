"""CryptoScope — FastAPI application entry point."""

import os
import sys
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse

from app.config import get_settings
from app.db.database import set_db_path, init_db

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
    yield
    # Shutdown
    print("CryptoScope shutting down")


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

app.include_router(health_router)
app.include_router(signals_router, prefix="/api")
app.include_router(portfolio_router, prefix="/api")
app.include_router(scanners_router, prefix="/api")
app.include_router(favorites_router, prefix="/api")
app.include_router(data_router, prefix="/api")
app.include_router(ai_router, prefix="/api")
app.include_router(ui_router)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Main page."""
    return templates.TemplateResponse("index.html", {
        "request": request,
        "settings": settings,
    })


@app.get("/app", response_class=HTMLResponse)
async def app_page(request: Request):
    """Full app page."""
    return templates.TemplateResponse("index.html", {
        "request": request,
        "settings": settings,
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
