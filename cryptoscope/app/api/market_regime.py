"""Crypto Alpha Command Center routes."""

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from app.access import is_admin_user
from app.core.market_regime import (
    fetch_market_regime_report,
    sync_market_regime_snapshots,
)
from app.db import database
from app.db.database import get_connection
from app.ui.templates import templates

router = APIRouter(tags=["market-regime"])


@router.get("/tab/alpha", response_class=HTMLResponse)
async def market_regime_tab(
    request: Request,
    refresh: bool = Query(False),
):
    user = getattr(request.state, "current_user", None)
    is_admin = is_admin_user(user)
    refresh_result = None
    refresh_error = None

    if refresh and not is_admin:
        raise HTTPException(status_code=404, detail="Not found")
    if refresh:
        try:
            refresh_result = await sync_market_regime_snapshots(
                database.DB_PATH,
            )
        except Exception as exc:
            refresh_error = (
                "Не удалось пересчитать режим рынка. "
                "Сохранённый снимок не изменён."
            )
            print(f"Market regime refresh failed: {exc}")

    async with get_connection() as conn:
        report = await fetch_market_regime_report(conn)

    return templates.TemplateResponse(
        request,
        "components/market_regime_tab.html",
        {
            "report": report,
            "latest": report.get("latest"),
            "refresh_result": refresh_result,
            "refresh_error": refresh_error,
            "is_admin": is_admin,
        },
    )


@router.get("/api/market-regime")
async def market_regime_api():
    async with get_connection() as conn:
        report = await fetch_market_regime_report(conn)
    if not report["is_ready"]:
        raise HTTPException(
            status_code=503,
            detail="Market regime snapshot is not ready",
        )
    return {
        "calculation_version": report["calculation_version"],
        "latest": report["latest"],
        "trade_plan": report["trade_plan"],
        "history": report["history"],
    }
