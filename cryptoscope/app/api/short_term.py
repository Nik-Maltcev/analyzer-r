"""Admin-only routes for the short-term research lab."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from app.access import is_admin_user
from app.auth import get_current_user
from app.core.equity_short_term_lab import (
    get_equity_short_term_report,
    refresh_equity_short_term_lab,
)
from app.core.short_term_lab import (
    build_scan_cards,
    build_strategy_cards_for_report,
    get_short_term_report,
    recalc_short_term_report,
    refresh_short_term_lab,
    scan_short_term_sl_tp_slice,
)
from app.db import database
from app.ui.templates import templates

router = APIRouter(prefix="/tab/short-term", tags=["short-term"])
_SUPPORTED_MARKETS = {"crypto", "ru", "stocks"}
_REFRESH_TASKS: dict[str, asyncio.Task] = {}


async def _require_admin(request: Request) -> None:
    user = getattr(request.state, "current_user", None)
    if user is None:
        user = await get_current_user(request)
    if not is_admin_user(user):
        raise HTTPException(status_code=404, detail="Not found")


async def _run_refresh(market: str) -> None:
    try:
        if market == "crypto":
            result = await refresh_short_term_lab(database.DB_PATH)
        else:
            result = await asyncio.to_thread(
                refresh_equity_short_term_lab,
                database.DB_PATH,
                market,
            )
        print(f"Short-Term Lab refresh complete: market={market} run={result.get('run_id')}")
    except Exception as exc:
        print(f"Short-Term Lab refresh failed: market={market} error={exc!r}")


def _start_refresh(market: str) -> None:
    task = _REFRESH_TASKS.get(market)
    if task is None or task.done():
        _REFRESH_TASKS[market] = asyncio.create_task(_run_refresh(market))


@router.get("", response_class=HTMLResponse)
async def short_term_tab(
    request: Request,
    refresh: bool = Query(False),
    market: str = Query("crypto"),
):
    await _require_admin(request)
    market = market.strip().lower()
    if market not in _SUPPORTED_MARKETS:
        raise HTTPException(status_code=400, detail="Unsupported Short-Term Lab market")
    if market == "crypto":
        report = await asyncio.to_thread(get_short_term_report, database.DB_PATH)
    else:
        report = await asyncio.to_thread(
            get_equity_short_term_report,
            database.DB_PATH,
            market,
        )
    latest = report.get("latest") or {}
    latest_status = latest.get("status")
    persisted_run_active = latest_status == "running" and not latest.get("is_stale")
    migration_refresh = bool(report.get("needs_optimizer_refresh"))
    if refresh or ((not report["is_ready"] or migration_refresh) and not persisted_run_active):
        _start_refresh(market)
        await asyncio.sleep(0)
    task = _REFRESH_TASKS.get(market)
    report["running"] = bool(
        (task and not task.done())
        or persisted_run_active
    )
    return templates.TemplateResponse(
        request,
        (
            "components/short_term_tab.html"
            if market == "crypto"
            else "components/equity_short_term_tab.html"
        ),
        {"report": report},
    )


@router.get("/recalc-strategies", response_class=HTMLResponse)
async def recalc_strategies(
    request: Request,
    stop: float = Query(default=4.0, ge=0),
    target: float = Query(default=6.0, ge=0),
    stake: float = Query(default=100.0, gt=0),
):
    await _require_admin(request)
    try:
        result = await asyncio.to_thread(
            recalc_short_term_report,
            database.DB_PATH,
            stop_pct=stop,
            target_pct=target,
        )
        strategy_metrics = result.get("strategies") or {}
        strategy_cards = build_strategy_cards_for_report(strategy_metrics)
        return templates.TemplateResponse(
            request,
            "components/short_term_strategies.html",
            {
                "strategies": strategy_cards,
                "stake": stake,
                "stop": stop,
                "target": target,
            },
        )
    except Exception as exc:
        import traceback
        traceback.print_exc()
        return templates.TemplateResponse(
            request,
            "components/short_term_strategies.html",
            {
                "strategies": [],
                "error": str(exc)[:500],
                "stake": stake,
                "stop": stop,
                "target": target,
            },
        )


@router.get("/scan-strategies", response_class=HTMLResponse)
async def scan_strategies(request: Request):
    await _require_admin(request)
    try:
        result = await asyncio.to_thread(scan_short_term_sl_tp_slice, database.DB_PATH)
        strategy_cards = build_scan_cards(result.get("strategies") or {})
        return templates.TemplateResponse(
            request,
            "components/short_term_scan.html",
            {
                "scan_cards": strategy_cards,
                "coverage_days": result.get("coverage_days") or 0,
                "selection_days": result.get("selection_days") or 0,
                "test_days": result.get("test_days") or 0,
                "split_time": result.get("split_time") or 0,
            },
        )
    except Exception as exc:
        import traceback
        traceback.print_exc()
        return templates.TemplateResponse(
            request,
            "components/short_term_scan.html",
            {
                "scan_cards": [],
                "coverage_days": 0,
                "selection_days": 0,
                "test_days": 0,
                "split_time": 0,
                "error": str(exc)[:500],
            },
        )
