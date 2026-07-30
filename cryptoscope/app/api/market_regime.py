"""Crypto Alpha Command Center routes."""

import asyncio
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from app.access import is_admin_user
from app.core.scanner_history import sync_all_scanner_states
from app.core.market_regime import (
    expire_alpha_trade_journal,
    fetch_market_regime_report,
    sync_market_regime_snapshots,
)
from app.data.mexc import refresh_mexc_crypto_market
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
_ALPHA_REFRESH_LOCK = asyncio.Lock()


async def _refresh_alpha_inputs() -> dict:
    """Refresh completed crypto candles and scanner state before Alpha."""
    async with _ALPHA_REFRESH_LOCK:
        conn = sqlite3.connect(database.DB_PATH, timeout=60)
        try:
            market_result = await refresh_mexc_crypto_market(
                conn,
                CRYPTO_TICKERS,
            )
        finally:
            conn.close()

        await sync_all_scanner_states(
            database.DB_PATH,
            ("crypto",),
        )
        regime_result = await sync_market_regime_snapshots(
            database.DB_PATH,
        )
        regime_result["market_latest_data_date"] = market_result.get(
            "latest_date"
        )
        regime_result["market_tickers"] = market_result.get("tickers")
        return regime_result


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
            refresh_result = await _refresh_alpha_inputs()
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

    evaluation_date = datetime.now(MOSCOW_TZ).date().isoformat()
    async with get_connection() as conn:
        expiration_result = await expire_alpha_trade_journal(
            conn,
            as_of_date=evaluation_date,
            live_prices=live_prices,
        )
        if refresh_result is not None:
            refresh_result["alpha_expired"] = expiration_result["closed"]
            refresh_result["alpha_expiration_skipped"] = expiration_result["skipped"]
        report = await fetch_market_regime_report(
            conn,
            live_prices=live_prices,
            live_price_source_label=live_price_source_label,
            evaluation_date=evaluation_date,
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
    evaluation_date = datetime.now(MOSCOW_TZ).date().isoformat()
    async with get_connection() as conn:
        await expire_alpha_trade_journal(
            conn,
            as_of_date=evaluation_date,
            live_prices=live_prices,
        )
        report = await fetch_market_regime_report(
            conn,
            live_prices=live_prices,
            live_price_source_label=live_price_source_label,
            evaluation_date=evaluation_date,
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
