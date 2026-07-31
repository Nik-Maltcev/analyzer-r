"""Admin-only intraday reversal research workspace."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from app.access import is_admin_user
from app.auth import get_current_user
from app.core.reversal_lab import get_reversal_report, refresh_and_backtest
from app.db import database
from app.ui.templates import templates

router = APIRouter(prefix="/tab/reversal", tags=["reversal"])
_REFRESH_TASK: asyncio.Task | None = None


async def _require_admin(request: Request) -> None:
    user = getattr(request.state, "current_user", None)
    if user is None:
        user = await get_current_user(request)
    if not is_admin_user(user):
        raise HTTPException(status_code=404, detail="Not found")


async def _run_refresh() -> None:
    try:
        result = await refresh_and_backtest(database.DB_PATH)
        print(f"Reversal Lab refresh complete: {result}")
    except Exception as exc:
        print(f"Reversal Lab refresh failed: {exc!r}")


def _start_refresh() -> None:
    global _REFRESH_TASK
    if _REFRESH_TASK is None or _REFRESH_TASK.done():
        _REFRESH_TASK = asyncio.create_task(_run_refresh())


@router.get("", response_class=HTMLResponse)
async def reversal_tab(
    request: Request,
    refresh: bool = Query(False),
):
    await _require_admin(request)
    if refresh:
        _start_refresh()
        await asyncio.sleep(0)
    report = await asyncio.to_thread(get_reversal_report, database.DB_PATH)
    if refresh and not report["running"]:
        report["running"] = True
    return templates.TemplateResponse(
        request,
        "components/reversal_tab.html",
        {"report": report},
    )
