"""Crypto Alpha Command Center routes."""

from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from app.access import is_admin_user
from app.core.market_regime import (
    fetch_market_regime_report,
    sync_market_regime_snapshots,
)
from app.data.mexc_market import (
    get_crypto_live_snapshot,
    refresh_crypto_live_prices,
)
from app.data.tickers import CRYPTO_TICKERS
from app.db import database
from app.db.database import get_connection
from app.ui.templates import templates

router = APIRouter(tags=["market-regime"])
MOSCOW_TZ = ZoneInfo("Europe/Moscow")


async def _alpha_live_context(
    *,
    force_refresh: bool,
) -> tuple[dict[str, float], str | None]:
    if force_refresh:
        result = await refresh_crypto_live_prices(
            CRYPTO_TICKERS,
            ttl_seconds=0,
        )
        prices = result.get("prices") or {}
        updated_at = result.get("updated_at")
    else:
        prices, updated_at = get_crypto_live_snapshot(CRYPTO_TICKERS)
    if not prices:
        return {}, None
    label = "live MEXC"
    if updated_at is not None:
        label = (
            "live MEXC · "
            f"{updated_at.astimezone(MOSCOW_TZ).strftime('%d.%m %H:%M')}"
        )
    return prices, label


@router.get("/tab/alpha", response_class=HTMLResponse)
async def market_regime_tab(
    request: Request,
    refresh: bool = Query(False),
):
    user = getattr(request.state, "current_user", None)
    is_admin = is_admin_user(user)
    refresh_result = None
    refresh_error = None
    live_prices: dict[str, float] = {}
    live_price_source_label = None

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

    try:
        live_prices, live_price_source_label = await _alpha_live_context(
            force_refresh=refresh,
        )
    except Exception as exc:
        print(f"Alpha MEXC live refresh failed: {exc}")
        if refresh and not refresh_error:
            refresh_error = (
                "Режим пересчитан, но live-котировки MEXC недоступны. "
                "Активные позиции показаны по последней дневной цене."
            )

    async with get_connection() as conn:
        report = await fetch_market_regime_report(
            conn,
            live_prices=live_prices,
            live_price_source_label=live_price_source_label,
        )

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
    live_prices, live_price_source_label = await _alpha_live_context(
        force_refresh=False,
    )
    async with get_connection() as conn:
        report = await fetch_market_regime_report(
            conn,
            live_prices=live_prices,
            live_price_source_label=live_price_source_label,
        )
    if not report["is_ready"]:
        raise HTTPException(
            status_code=503,
            detail="Market regime snapshot is not ready",
        )
    return {
        "calculation_version": report["calculation_version"],
        "latest": report["latest"],
        "trade_plan": report["trade_plan"],
        "statistics": report["statistics"],
        "history": report["history"],
    }
