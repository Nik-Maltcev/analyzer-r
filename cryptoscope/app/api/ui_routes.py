"""Tab routes — serve HTML partials for HTMX tab/content swaps."""

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

router = APIRouter(prefix="/tab", tags=["ui"])

templates = Jinja2Templates(directory="app/templates")


@router.get("/signals", response_class=HTMLResponse)
async def tab_signals(
    request: Request,
    mode: str = Query("all", description="all|forecast|short"),
    coint_only: bool = Query(False),
    min_corr: float = Query(0.5),
    max_days: int = Query(30),
):
    """Signals tab content — returns HTML fragment."""
    return templates.TemplateResponse("components/signals_all.html", {
        "request": request,
        "mode": mode,
        "coint_only": coint_only,
        "min_corr": min_corr,
        "max_days": max_days,
    })


@router.get("/portfolio", response_class=HTMLResponse)
async def tab_portfolio(request: Request):
    """Portfolio tab content."""
    return templates.TemplateResponse("components/portfolio_tab.html", {
        "request": request,
    })


@router.get("/scanners", response_class=HTMLResponse)
async def tab_scanners(request: Request):
    """Scanners tab content."""
    return templates.TemplateResponse("components/scanners_tab.html", {
        "request": request,
    })


@router.get("/scanner/{scanner_type}", response_class=HTMLResponse)
async def tab_scanner_content(request: Request, scanner_type: str):
    """Individual scanner content partial."""
    template_map = {
        "corrbreak": "components/scanner_corrbreak.html",
        "momentum": "components/scanner_momentum.html",
        "drawdown": "components/scanner_drawdown.html",
    }
    template = template_map.get(scanner_type, "components/scanner_corrbreak.html")
    return templates.TemplateResponse(template, {
        "request": request,
        "scanner": scanner_type,
    })


@router.get("/favorites", response_class=HTMLResponse)
async def tab_favorites(request: Request):
    """Favorites tab content."""
    return templates.TemplateResponse("components/favorites_tab.html", {
        "request": request,
    })


@router.get("/data", response_class=HTMLResponse)
async def tab_data(request: Request):
    """Data tab content."""
    return templates.TemplateResponse("components/data_tab.html", {
        "request": request,
    })


@router.get("/ai", response_class=HTMLResponse)
async def tab_ai(request: Request):
    """AI Analyst tab content."""
    return templates.TemplateResponse("components/ai_tab.html", {
        "request": request,
    })
