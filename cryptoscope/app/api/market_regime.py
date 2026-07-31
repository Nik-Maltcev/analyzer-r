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
_ALPHA_REFRESH_TASK: asyncio.Task | None = None
_ALPHA_REFRESH_STATE: dict = {
    "status": "idle",
    "result": None,
    "error": None,
}


async def _refresh_alpha_inputs() -> dict:
    """Refresh completed crypto candles and scanner state before Alpha."""
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


def _refresh_alpha_inputs_in_worker() -> dict:
    """Run the async refresh pipeline on a dedicated worker thread."""
    return asyncio.run(_refresh_alpha_inputs())


async def _run_alpha_refresh() -> None:
    try:
        result = await asyncio.to_thread(_refresh_alpha_inputs_in_worker)
    except Exception as exc:
        _ALPHA_REFRESH_STATE.update({
            "status": "error",
            "result": None,
            "error": str(exc)[:500],
        })
        print(f"Market regime background refresh failed: {exc!r}")
        return
    _ALPHA_REFRESH_STATE.update({
        "status": "success",
        "result": result,
        "error": None,
    })


def _start_alpha_refresh() -> None:
    global _ALPHA_REFRESH_TASK
    if _ALPHA_REFRESH_TASK is not None and not _ALPHA_REFRESH_TASK.done():
        return
    _ALPHA_REFRESH_STATE.update({
        "status": "running",
        "result": None,
        "error": None,
    })
    _ALPHA_REFRESH_TASK = asyncio.create_task(_run_alpha_refresh())


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
    refresh_status: bool = Query(False),
):
    user = getattr(request.state, "current_user", None)
    is_admin = is_admin_user(user)
    refresh_result = None
    refresh_error = None
    live_prices: dict[str, float] = {}
    live_price_source_label = None

    if (refresh or refresh_status) and not is_admin:
        raise HTTPException(status_code=404, detail="Not found")

    refresh_state = dict(_ALPHA_REFRESH_STATE)
    refresh_in_progress = refresh_state["status"] == "running"
    if refresh or refresh_status:
        if refresh_state["status"] == "success":
            refresh_result = refresh_state["result"]
        elif refresh_state["status"] == "error":
            detail = refresh_state.get("error") or "неизвестная ошибка"
            refresh_error = (
                "Не удалось пересчитать режим рынка. "
                f"Причина: {detail}"
            )

    try:
        live_prices, live_price_source_label = await _alpha_live_context(
            force_refresh=(
                refresh_status
                and refresh_state["status"] == "success"
            ),
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
        expiration_result = {
            "closed": 0,
            "skipped": [],
        }
        if not refresh_in_progress:
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

    if refresh:
        _start_alpha_refresh()
        refresh_result = None
        refresh_error = None
        refresh_in_progress = True

    return templates.TemplateResponse(
        request,
        "components/market_regime_tab.html",
        {
            "report": report,
            "latest": report.get("latest"),
            "refresh_result": refresh_result,
            "refresh_error": refresh_error,
            "refresh_in_progress": refresh_in_progress,
            "is_admin": is_admin,
        },
    )


@router.get("/tab/alpha/refresh-status", response_class=HTMLResponse)
async def market_regime_refresh_status(request: Request):
    user = getattr(request.state, "current_user", None)
    if not is_admin_user(user):
        raise HTTPException(status_code=404, detail="Not found")
    return templates.TemplateResponse(
        request,
        "components/alpha_refresh_status.html",
        {
            "refresh_state": dict(_ALPHA_REFRESH_STATE),
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
