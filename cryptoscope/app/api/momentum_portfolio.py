"""Admin-only forward test for the Momentum risk portfolio."""

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from app.access import is_admin_user
from app.auth import get_current_user
from app.core.momentum_portfolio import (
    apply_momentum_live_prices,
    fetch_momentum_entry_status,
    fetch_momentum_portfolio_report,
    sync_momentum_portfolio_journal,
)
from app.data.binance_ws import refresh_crypto_live_prices
from app.db import database
from app.db.database import get_connection
from app.ui.templates import templates

router = APIRouter(
    prefix="/tab/momentum-portfolio",
    tags=["momentum-portfolio"],
)

MOSCOW_TZ = ZoneInfo("Europe/Moscow")


async def _require_admin(request: Request) -> None:
    user = getattr(request.state, "current_user", None)
    if user is None:
        user = await get_current_user(request)
    if not is_admin_user(user):
        raise HTTPException(status_code=404, detail="Not found")


@router.get("/status", response_class=HTMLResponse)
async def momentum_portfolio_status(request: Request):
    await _require_admin(request)
    async with get_connection() as conn:
        status = await fetch_momentum_entry_status(conn)
    return templates.TemplateResponse(
        request,
        "components/momentum_nav_status.html",
        {"momentum_entry_status": status},
    )


@router.get("", response_class=HTMLResponse)
async def momentum_portfolio_tab(
    request: Request,
    refresh: bool = Query(False),
):
    await _require_admin(request)
    sync_error = None
    refresh_result = None
    refresh_error = None
    try:
        await sync_momentum_portfolio_journal(database.DB_PATH)
    except Exception as exc:
        sync_error = "Не удалось создать свежий срез Momentum 3."
        print(f"Momentum portfolio bootstrap failed: {exc}")
    async with get_connection() as conn:
        report = await fetch_momentum_portfolio_report(conn)
    current = report.get("current")
    tickers = [
        str(item.get("ticker") or "")
        for item in (current or {}).get("allocations") or []
        if float(item.get("allocation") or 0) > 0
    ]
    live_prices = {}
    if tickers:
        try:
            live_result = await refresh_crypto_live_prices(
                tickers,
                ttl_seconds=0 if refresh else 30,
            )
            live_prices = live_result.get("prices") or {}
            if refresh:
                if not live_prices:
                    raise RuntimeError("Binance не вернул актуальные котировки")
                updated_at = live_result.get("updated_at")
                refresh_result = {
                    "updated": len(live_prices),
                    "updated_at": (
                        updated_at.astimezone(MOSCOW_TZ).strftime("%d.%m.%Y %H:%M")
                        if updated_at
                        else datetime.now(MOSCOW_TZ).strftime("%d.%m.%Y %H:%M")
                    ),
                }
        except Exception as exc:
            print(f"Momentum portfolio live prices failed: {exc}")
            if refresh:
                refresh_error = (
                    "Не удалось обновить котировки Momentum 3. "
                    "Попробуйте ещё раз через несколько секунд."
                )
    elif refresh:
        refresh_result = {
            "updated": 0,
            "updated_at": datetime.now(MOSCOW_TZ).strftime("%d.%m.%Y %H:%M"),
        }
    apply_momentum_live_prices(report, live_prices)
    return templates.TemplateResponse(
        request,
        "components/momentum_portfolio_tab.html",
        {
            "report": report,
            "current": report["current"],
            "sync_error": sync_error,
            "refresh_result": refresh_result,
            "refresh_error": refresh_error,
        },
    )
