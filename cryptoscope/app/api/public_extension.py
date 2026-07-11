"""Small public market feed used by the browser extension."""

from datetime import UTC, datetime

import numpy as np
from fastapi import APIRouter, Query, Response

from app.core.signals import estimate_signal_timing, is_actionable_signal
from app.db.database import fetch_pairs, get_connection
from app.product import get_product_profile, require_market_enabled

router = APIRouter(prefix="/public/extension", tags=["public-extension"])

MARKET_LABELS = {
    "br": "B3",
    "stocks": "Ações EUA",
    "crypto": "Cripto",
}


def _finite_float(value, default=None):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if np.isfinite(number) else default


def _finite_int(value, default=None):
    number = _finite_float(value)
    return int(number) if number is not None else default


def _is_validated(row) -> bool:
    value = row.get("is_coint_stable")
    if value is None:
        value = row.get("is_coint")
    return bool(_finite_int(value, 0))


def _clean_ticker(ticker: str) -> str:
    return str(ticker).replace("/USD", "")


def _present_pair(row, market: str) -> dict:
    signal_type = str(row.get("signal_type") or "wait")
    ticker_a = str(row["ticker_a"])
    ticker_b = str(row["ticker_b"])
    a = _clean_ticker(ticker_a)
    b = _clean_ticker(ticker_b)
    long_a = signal_type == "long_a"
    timing = estimate_signal_timing(
        row.get("signal_started_at"),
        _finite_int(row.get("halflife")),
        fallback_started_at=row.get("computed_at"),
    )

    return {
        "id": f"{market}:{ticker_a}:{ticker_b}",
        "market": market,
        "market_label": MARKET_LABELS.get(market, market.upper()),
        "ticker_a": ticker_a,
        "ticker_b": ticker_b,
        "pair": f"{a} / {b}",
        "direction": signal_type,
        "recommendation": (
            f"Comprar {a} / Vender {b}"
            if long_a
            else f"Vender {a} / Comprar {b}"
        ),
        "z_score": round(_finite_float(row.get("z_now"), 0.0), 2),
        "correlation_pct": round(_finite_float(row.get("corr"), 0.0) * 100),
        "estimated_days": _finite_int(row.get("halflife")),
        "signal_days": timing["signal_days_elapsed"],
        "review_in_days": timing["signal_days_remaining"],
        "started_at": timing["signal_started_at"],
    }


@router.get("/feed")
async def extension_feed(
    response: Response,
    market: str | None = Query(None),
):
    """Return a deliberately small, read-only preview of actionable signals."""
    profile = get_product_profile()
    selected_market = require_market_enabled(market or profile.default_market, profile)

    async with get_connection() as conn:
        pairs = await fetch_pairs(conn, selected_market, min_corr=0.5)

    items = []
    if not pairs.empty:
        candidates = []
        for _, row in pairs.iterrows():
            signal_type = row.get("signal_type")
            halflife = _finite_int(row.get("halflife"))
            validated = _is_validated(row)
            if (
                halflife is None
                or halflife <= 0
                or halflife > 30
                or not is_actionable_signal(signal_type, validated)
            ):
                continue
            candidates.append(row)

        candidates.sort(
            key=lambda row: (
                abs(_finite_float(row.get("z_now"), 0.0)),
                _finite_float(row.get("score"), 0.0),
            ),
            reverse=True,
        )
        items = [_present_pair(row, selected_market) for row in candidates[:4]]

    response.headers["Cache-Control"] = "public, max-age=300"
    return {
        "market": selected_market,
        "market_label": MARKET_LABELS.get(selected_market, selected_market.upper()),
        "updated_at": datetime.now(UTC).isoformat(),
        "items": items,
        "total": len(items),
        "full_analysis_url": f"/app?market={selected_market}",
    }
