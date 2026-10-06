"""Раздел «Live-сигналы» — admin-only, только крипта.

Кросс-секционный моментум (long winners / short losers) живьём: дневная L/S
корзина + живые цены MEXC для интрадей-тайминга входа.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.access import is_admin_user
from app.auth import get_current_user
from app.core.live_momentum import (
    ALLOWED_LOOKBACKS,
    DEFAULT_LOOKBACK,
    build_live_momentum_report,
)
from app.ui.templates import templates

router = APIRouter(prefix="/tab/live-signals", tags=["live-signals"])


def _normalize_lookback(value: int) -> int:
    return value if value in ALLOWED_LOOKBACKS else DEFAULT_LOOKBACK


async def _require_admin(request: Request) -> None:
    user = getattr(request.state, "current_user", None)
    if user is None:
        user = await get_current_user(request)
    if not is_admin_user(user):
        raise HTTPException(status_code=404, detail="Not found")


@router.get("", response_class=HTMLResponse)
async def live_signals_tab(request: Request, lookback: int = DEFAULT_LOOKBACK):
    await _require_admin(request)
    lookback = _normalize_lookback(lookback)
    report = await build_live_momentum_report(lookback)
    return templates.TemplateResponse(
        request,
        "components/live_signals_tab.html",
        {"report": report, "lookback": lookback},
    )


@router.get("/board", response_class=HTMLResponse)
async def live_signals_board(request: Request, lookback: int = DEFAULT_LOOKBACK):
    """Partial для авто-обновления (HTMX every 30s) — только доска сигналов."""
    await _require_admin(request)
    lookback = _normalize_lookback(lookback)
    report = await build_live_momentum_report(lookback)
    return templates.TemplateResponse(
        request,
        "components/live_signals_board.html",
        {"report": report, "lookback": lookback},
    )
