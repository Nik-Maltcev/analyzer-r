"""Раздел «Новые стратегии» — admin-only, только крипта."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.access import is_admin_user
from app.auth import get_current_user
from app.core.new_strategies import build_new_strategies_report
from app.ui.templates import templates

router = APIRouter(prefix="/tab/new-strategies", tags=["new-strategies"])


async def _require_admin(request: Request) -> None:
    user = getattr(request.state, "current_user", None)
    if user is None:
        user = await get_current_user(request)
    if not is_admin_user(user):
        raise HTTPException(status_code=404, detail="Not found")


@router.get("", response_class=HTMLResponse)
async def new_strategies_tab(request: Request):
    await _require_admin(request)
    report = await asyncio.to_thread(build_new_strategies_report)
    return templates.TemplateResponse(
        request,
        "components/new_strategies_tab.html",
        {"report": report},
    )
