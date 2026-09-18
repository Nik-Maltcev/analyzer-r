"""Long Term Deals HTML and JSON routes."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pandas as pd
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.config import get_settings
from app.core.long_term import build_long_term_investment_report
from app.ui.templates import templates

router = APIRouter(tags=["long-term"])

RESOURCE_ASSET_LABELS = {
    "Agricultural commodities": "Сельхозсырьё",
    "Base metals": "Базовые металлы",
    "Copper": "Медь",
    "Gold": "Золото",
    "Nuclear-energy chain": "Ядерная энергетика",
    "Silver": "Серебро",
    "Steel producers": "Производители стали",
    "USD cash reserve": "Резерв в USD",
    "Water infrastructure": "Водная инфраструктура",
}


def _resource_risk_decision(
    positive_rate: float,
    target_rate: float,
    max_drawdown: float,
    independent_windows: int,
) -> tuple[str, str, int]:
    strong = (
        positive_rate >= 70
        and target_rate >= 55
        and max_drawdown >= -25
        and independent_windows >= 20
    )
    limited = (
        positive_rate >= 65
        and target_rate >= 40
        and max_drawdown >= -35
        and independent_windows >= 10
    )
    if strong:
        return "consider", "Можно рассматривать", 100_000
    if limited:
        return "limited", "Только небольшой суммой", 25_000
    return "wait", "Сейчас лучше не покупать", 0


def _load_resource_portfolio_report() -> dict | None:
    data_dir = Path(__file__).resolve().parents[2] / "data" / "resource_portfolio"
    path = data_dir / "resource_portfolio_report.json"
    if not path.exists():
        return None
    resource = json.loads(path.read_text(encoding="utf-8"))
    full = {row["portfolio"]: row for row in resource.get("full_period", [])}
    horizon_path = data_dir / "portfolio_horizon_summary.csv"
    horizon_rows: dict[tuple[str, int], dict] = {}
    if horizon_path.exists():
        horizon_frame = pd.read_csv(horizon_path)
        horizon_rows = {
            (str(row.portfolio), int(row.horizon_months)): row._asdict()
            for row in horizon_frame.itertuples(index=False)
        }
    allocations: dict[str, list[dict]] = {}
    for row in resource.get("latest_allocations", []):
        item = dict(row)
        item["asset_label"] = RESOURCE_ASSET_LABELS.get(item["asset"], item["asset"])
        item["amount_from_100k"] = round(float(item["weight_pct"]) * 1_000)
        allocations.setdefault(item["portfolio"], []).append(item)
    cards = []
    for name in ("core_plus_trend", "defensive_trend_barbell", "top5_trend_inverse_vol"):
        if name not in full:
            continue
        year = horizon_rows.get((name, 12), {})
        positive_rate = float(year.get("positive_usd_pct", 0))
        target_rate = float(year.get("usd_plus_10_pct", 0))
        independent_windows = int(year.get("effective_independent_windows", 0))
        max_drawdown = float(full[name]["max_drawdown_pct"])
        risk_status, risk_label, portfolio_budget = _resource_risk_decision(
            positive_rate, target_rate, max_drawdown, independent_windows
        )
        reserve = 100_000 - portfolio_budget
        card_allocations = []
        for item in allocations.get(name, []):
            scaled = dict(item)
            scaled["suggested_amount_rub"] = round(
                portfolio_budget * float(item["weight_pct"]) / 100
            )
            card_allocations.append(scaled)
        if card_allocations and portfolio_budget:
            rounding_delta = portfolio_budget - sum(
                int(item["suggested_amount_rub"]) for item in card_allocations
            )
            card_allocations[0]["suggested_amount_rub"] += rounding_delta
        cards.append({
            **full[name],
            "allocations": card_allocations,
            "risk_status": risk_status,
            "risk_label": risk_label,
            "portfolio_budget_rub": portfolio_budget,
            "reserve_rub": reserve,
            "example_year_end_rub": round(
                reserve + portfolio_budget * (1 + float(full[name]["usd_cagr_pct"]) / 100)
            ),
            "example_drawdown_value_rub": round(
                reserve + portfolio_budget * (1 + max_drawdown / 100)
            ),
            "year_goal_hits": int(year.get("usd_plus_10_count", 0)),
            "year_windows": int(year.get("rolling_windows", 0)),
            "positive_years_pct": positive_rate,
            "target_years_pct": target_rate,
            "effective_independent_windows": independent_windows,
        })
    resource["cards"] = cards
    resource["selected_horizons"] = [
        row for row in resource.get("best_by_horizon", [])
        if int(row.get("horizon_months", 0)) in (3, 6, 12)
    ]
    return resource


def _load_long_term_report() -> dict:
    settings = get_settings()
    data_path = Path(settings.long_term_data_path)
    report_path = Path(settings.long_term_report_path)
    if not data_path.exists():
        return {
            "is_ready": False,
            "error": f"Файл истории не найден: {data_path}",
            "active": [],
        }
    prices = pd.read_csv(data_path)
    research = {}
    if report_path.exists():
        research = json.loads(report_path.read_text(encoding="utf-8"))
    result = build_long_term_investment_report(prices, research)
    result["resource_portfolio"] = _load_resource_portfolio_report()
    return result


async def _report() -> dict:
    return await asyncio.to_thread(_load_long_term_report)


@router.get("/tab/long-term", response_class=HTMLResponse)
async def long_term_tab(request: Request):
    report = await _report()
    return templates.TemplateResponse(
        request,
        "components/long_term_tab.html",
        {"report": report},
    )


@router.get("/api/long-term")
async def long_term_api():
    report = await _report()
    if not report.get("is_ready"):
        raise HTTPException(status_code=503, detail=report.get("error"))
    return report
