"""Admin-only crypto buy ideas assembled from daily scanners."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.access import is_admin_user
from app.auth import get_current_user
from app.core.crypto_picks import (
    aggregate_crypto_long_picks,
    build_price_progress,
)
from app.core.scanner_history import (
    annotate_scanner_results,
    build_scanner_snapshot,
    format_scanner_date,
    is_scanner_signal_within_horizon,
    sync_scanner_periods,
)
from app.db.database import fetch_prices, get_connection
from app.ui.templates import templates

router = APIRouter(prefix="/tab/crypto", tags=["crypto-picks"])


async def _require_admin(request: Request) -> None:
    user = getattr(request.state, "current_user", None)
    if user is None:
        user = await get_current_user(request)
    if not is_admin_user(user):
        raise HTTPException(status_code=404, detail="Not found")


@router.get("", response_class=HTMLResponse)
async def crypto_picks_tab(request: Request):
    await _require_admin(request)
    context = {
        "request": request,
        "picks": [],
        "total": 0,
        "scanner_data_date": None,
    }

    try:
        async with get_connection() as conn:
            prices = await fetch_prices(conn, "crypto")
            if prices.empty:
                return templates.TemplateResponse(
                    request,
                    "components/crypto_picks_tab.html",
                    context,
                )

            wide = prices.pivot(index="date", columns="ticker", values="close")
            wide = wide.sort_index()
            data_date = str(max(wide.index))[:10]
            scanner_results = {}

            for scanner in ("momentum", "drawdown"):
                frame, active_snapshot = build_scanner_snapshot(wide, scanner)
                periods = await sync_scanner_periods(
                    conn,
                    "crypto",
                    scanner,
                    data_date,
                    active_snapshot,
                )
                records = (
                    frame.to_dict(orient="records")
                    if not frame.empty
                    else []
                )
                records = annotate_scanner_results(records, scanner, periods)
                scanner_results[scanner] = [
                    record
                    for record in records
                    if record.get("recommendation_class") == "long"
                    and is_scanner_signal_within_horizon(
                        scanner,
                        record.get("signal_age_days"),
                    )
                ]

        latest_prices = {
            ticker: series.dropna().iloc[-1]
            for ticker, series in wide.items()
            if not series.dropna().empty
        }
        picks = aggregate_crypto_long_picks(scanner_results, latest_prices)
        for pick in picks:
            ticker_series = wide[pick["ticker"]].dropna()
            progress = build_price_progress(
                ticker_series.items(),
                pick.get("signal_first_seen_date"),
            )
            pick["price_progress"] = progress
            pick["progress_change_pct"] = (
                progress[0]["change_pct"] if progress else None
            )
            pick["progress_change_display"] = (
                progress[0]["change_display"] if progress else "—"
            )
        return templates.TemplateResponse(
            request,
            "components/crypto_picks_tab.html",
            {
                **context,
                "picks": picks,
                "total": len(picks),
                "scanner_data_date": format_scanner_date(data_date),
            },
        )
    except HTTPException:
        raise
    except Exception as exc:
        return templates.TemplateResponse(
            request,
            "components/crypto_picks_tab.html",
            {**context, "error": str(exc) or "Раздел временно недоступен"},
        )
