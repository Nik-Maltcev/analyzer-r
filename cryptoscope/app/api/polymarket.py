"""Polymarket direction forecast API and UI routes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from app.data.polymarket import get_polymarket_forecasts
from app.ui.templates import templates

api_router = APIRouter(prefix="/api/polymarket", tags=["polymarket"])
ui_router = APIRouter(prefix="/tab/polymarket", tags=["polymarket-ui"])
MSK = timezone(timedelta(hours=3))


def _format_price(value) -> str:
    try:
        price = float(value)
    except (TypeError, ValueError):
        return "—"
    if price >= 1000:
        return f"{price:,.2f}".replace(",", " ")
    if price >= 1:
        return f"{price:.2f}"
    return f"{price:.4f}"


def _template_context(result: dict) -> dict:
    forecasts = []
    for item in result["forecasts"]:
        record = dict(item)
        record["price_display"] = _format_price(record.get("latest_price"))
        timestamp = record.get("latest_timestamp")
        if timestamp:
            record["price_time"] = (
                datetime.fromtimestamp(
                    timestamp,
                    timezone.utc,
                )
                .astimezone(MSK)
                .strftime("%d.%m.%Y")
            )
        else:
            record["price_time"] = None
        forecasts.append(record)

    leader = dict(result["leader"]) if result.get("leader") else None
    updated_at = result.get("updated_at")
    return {
        "forecasts": forecasts,
        "leader": leader,
        "available_count": result.get("available_count", 0),
        "total_count": result.get("total_count", len(forecasts)),
        "cached": result.get("cached", False),
        "updated_at": (
            updated_at.astimezone(MSK).strftime("%d.%m.%Y, %H:%M МСК")
            if updated_at
            else None
        ),
    }


@ui_router.get("", response_class=HTMLResponse)
async def polymarket_tab(request: Request):
    return templates.TemplateResponse(
        request,
        "components/polymarket_tab.html",
        {"request": request},
    )


@ui_router.get("/results", response_class=HTMLResponse)
async def polymarket_results(
    request: Request,
    refresh: bool = Query(False),
):
    try:
        result = await get_polymarket_forecasts(force=refresh)
        context = _template_context(result)
        return templates.TemplateResponse(
            request,
            "components/polymarket_results.html",
            {"request": request, **context},
        )
    except Exception as exc:
        return templates.TemplateResponse(
            request,
            "components/polymarket_results.html",
            {
                "request": request,
                "forecasts": [],
                "leader": None,
                "available_count": 0,
                "total_count": 0,
                "error": str(exc) or "Не удалось рассчитать прогнозы",
            },
        )


@api_router.get("")
async def polymarket_forecast_api(refresh: bool = Query(False)):
    result = await get_polymarket_forecasts(force=refresh)
    payload = _template_context(result)
    return {
        **payload,
        "leader": result.get("leader"),
    }
