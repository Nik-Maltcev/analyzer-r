"""Admin-only crypto forward-test confirmation board."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.access import is_admin_user
from app.auth import get_current_user
from app.core.forward_confirmation import build_forward_confirmation_report
from app.db import database
from app.ui.templates import templates

router = APIRouter(prefix="/tab/forward-confirmation", tags=["forward-confirmation"])


async def _require_admin(request: Request) -> None:
    user = getattr(request.state, "current_user", None)
    if user is None:
        user = await get_current_user(request)
    if not is_admin_user(user):
        raise HTTPException(status_code=404, detail="Not found")


@router.get("", response_class=HTMLResponse)
async def forward_confirmation_tab(request: Request):
    await _require_admin(request)
    report = await asyncio.to_thread(
        build_forward_confirmation_report, database.DB_PATH
    )
    return templates.TemplateResponse(
        request,
        "components/forward_confirmation_tab.html",
        {"report": report},
    )
