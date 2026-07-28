"""Admin-only Crypto V2 strategy workspace."""

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from app.access import is_admin_user
from app.auth import get_current_user
from app.core.crypto_v2 import (
    apply_crypto_v2_live_prices,
    fetch_crypto_v2_report,
    sync_crypto_v2_journal,
)
from app.data.mexc_market import refresh_crypto_live_prices
from app.db import database
from app.db.database import get_connection
from app.ui.templates import templates

router = APIRouter(prefix="/tab/crypto-v2", tags=["crypto-v2"])
MOSCOW_TZ = ZoneInfo("Europe/Moscow")


async def _require_admin(request: Request) -> None:
    user = getattr(request.state, "current_user", None)
    if user is None:
        user = await get_current_user(request)
    if not is_admin_user(user):
        raise HTTPException(status_code=404, detail="Not found")


@router.get("", response_class=HTMLResponse)
async def crypto_v2_tab(
    request: Request,
    refresh: bool = Query(False),
):
    await _require_admin(request)
    sync_error = None
    refresh_error = None
    refresh_result = None
    try:
        await sync_crypto_v2_journal(database.DB_PATH)
    except Exception as exc:
        sync_error = (
            "Не удалось обновить независимый журнал Crypto V2. "
            "Старый раздел «Крипта» не затронут."
        )
        print(f"Crypto V2 sync failed: {exc}")

    async with get_connection() as conn:
        report = await fetch_crypto_v2_report(conn)

    tickers = [
        str(item.get("ticker") or "")
        for item in report.get("active") or []
    ]
    live_prices = {}
    if tickers:
        try:
            live_result = await refresh_crypto_live_prices(
                tickers,
                ttl_seconds=0 if refresh else 30,
            )
            live_prices = live_result.get("prices") or {}
            if refresh and not live_prices:
                raise RuntimeError("MEXC returned no current prices")
            if refresh:
                updated_at = live_result.get("updated_at")
                refresh_result = {
                    "updated": len(live_prices),
                    "updated_at": (
                        updated_at.astimezone(MOSCOW_TZ).strftime(
                            "%d.%m.%Y %H:%M"
                        )
                        if updated_at
                        else datetime.now(MOSCOW_TZ).strftime("%d.%m.%Y %H:%M")
                    ),
                }
        except Exception as exc:
            print(f"Crypto V2 live refresh failed: {exc}")
            if refresh:
                refresh_error = (
                    "MEXC не вернул свежие цены. Журнал не изменён, "
                    "попробуйте снова через несколько секунд."
                )
    elif refresh:
        refresh_result = {
            "updated": 0,
            "updated_at": datetime.now(MOSCOW_TZ).strftime("%d.%m.%Y %H:%M"),
        }

    apply_crypto_v2_live_prices(report, live_prices)
    return templates.TemplateResponse(
        request,
        "components/crypto_v2_tab.html",
        {
            "report": report,
            "latest": report.get("latest"),
            "sync_error": sync_error,
            "refresh_error": refresh_error,
            "refresh_result": refresh_result,
        },
    )
