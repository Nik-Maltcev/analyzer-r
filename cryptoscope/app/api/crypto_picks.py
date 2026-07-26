"""Admin-only crypto buy ideas assembled from daily scanners."""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response

from app.access import is_admin_user
from app.auth import get_current_user
from app.core.crypto_picks import (
    ACTIVE_CRYPTO_SCANNERS,
    CRYPTO_PICKS_TRACKING_START,
    aggregate_crypto_long_picks,
    apply_crypto_confidence_admission,
    build_crypto_window_summary,
    build_price_progress,
    build_crypto_signal_export,
    filter_crypto_rows_by_confidence,
    is_excluded_crypto_confidence,
    select_crypto_sell_actions,
)
from app.core.scanner_history import (
    annotate_scanner_results,
    build_scanner_snapshot,
    close_crypto_ticker_periods,
    ensure_scanner_history_schema,
    format_scanner_date,
    is_scanner_signal_within_horizon,
    sync_scanner_periods,
)
from app.data.binance_ws import refresh_crypto_live_prices
from app.db.database import fetch_prices, get_connection
from app.ui.templates import templates

router = APIRouter(prefix="/tab/crypto", tags=["crypto-picks"])
RESULT_WINDOW_OPTIONS = (7, 14, 30)
CONFIDENCE_FILTER_OPTIONS = (
    ("high", "Высокая"),
    ("medium", "Средняя"),
)


def _normalize_result_window(value: int) -> int:
    return value if value in RESULT_WINDOW_OPTIONS else 7


def _parse_confidence_filter(value: str | None) -> list[str]:
    """Keep known confidence keys in canonical (display) order."""
    requested = set(str(value or "").split(","))
    return [
        key
        for key, _label in CONFIDENCE_FILTER_OPTIONS
        if key in requested
    ]


async def _require_admin(request: Request) -> None:
    user = getattr(request.state, "current_user", None)
    if user is None:
        user = await get_current_user(request)
    if not is_admin_user(user):
        raise HTTPException(status_code=404, detail="Not found")


def _csv_number(value: object, digits: int = 4) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    return f"{number:.{digits}f}".rstrip("0").rstrip(".")


async def _refresh_crypto_prices_for_today(tickers: Iterable[str]) -> dict:
    """Fetch live marks without mutating the daily scanner price series."""
    tickers = sorted({
        str(ticker)
        for ticker in tickers
        if ticker
    })
    result = await refresh_crypto_live_prices(tickers, ttl_seconds=0)
    live_prices = result.get("prices") or {}
    if not live_prices:
        raise RuntimeError("Binance не вернул актуальные котировки")

    current_date = datetime.now(
        ZoneInfo("Europe/Moscow")
    ).date().isoformat()
    return {
        **result,
        "updated": len(live_prices),
        "data_date": current_date,
    }


def _overlay_live_prices(
    prices_by_ticker: dict[str, list[tuple[object, object]]],
    live_prices: dict[str, float],
    live_date: str,
) -> dict[str, list[tuple[object, object]]]:
    """Mark active positions to market without changing scanner observations."""
    overlaid = {
        ticker: list(rows)
        for ticker, rows in prices_by_ticker.items()
    }
    for ticker, live_price in live_prices.items():
        try:
            normalized_price = float(live_price)
        except (TypeError, ValueError):
            continue
        if normalized_price <= 0:
            continue
        rows = [
            (raw_date, raw_price)
            for raw_date, raw_price in overlaid.get(ticker, [])
            if str(raw_date)[:10] != live_date
        ]
        rows.append((live_date, normalized_price))
        overlaid[ticker] = rows
    return overlaid


@router.post("/close", response_class=HTMLResponse)
async def close_crypto_pick(
    request: Request,
    ticker: str = Form(...),
    window_days: int = Form(7),
    confidence: str = Form(""),
):
    await _require_admin(request)
    normalized_ticker = str(ticker or "").strip().upper()
    if not normalized_ticker or len(normalized_ticker) > 40:
        raise HTTPException(status_code=400, detail="Invalid ticker")

    async with get_connection() as conn:
        prices = await fetch_prices(conn, "crypto")
        if prices.empty:
            raise HTTPException(status_code=409, detail="No crypto data")
        ticker_prices = prices[prices["ticker"] == normalized_ticker]
        if ticker_prices.empty:
            raise HTTPException(status_code=404, detail="Ticker not found")
        live_result = await refresh_crypto_live_prices(
            [normalized_ticker],
            ttl_seconds=0,
        )
        live_prices = live_result.get("prices") or {}
        close_price = live_prices.get(normalized_ticker)
        if close_price is None or float(close_price) <= 0:
            raise HTTPException(
                status_code=503,
                detail="Current Binance price is unavailable",
            )
        close_date = datetime.now(
            ZoneInfo("Europe/Moscow")
        ).date().isoformat()
        close_price = float(close_price)
        affected = await close_crypto_ticker_periods(
            conn,
            normalized_ticker,
            close_date,
            close_price,
            "manual",
        )

    request.state.crypto_close_result = {
        "ticker": normalized_ticker,
        "affected": affected,
        "close_price": close_price,
        "close_price_display": f"${close_price:.8f}".rstrip("0").rstrip("."),
    }
    return await crypto_picks_tab(
        request,
        refresh=False,
        window_days=_normalize_result_window(window_days),
        confidence=confidence,
    )


@router.get("/export.csv")
async def export_crypto_picks_csv(request: Request):
    await _require_admin(request)
    async with get_connection() as conn:
        await ensure_scanner_history_schema(conn)
        prices = await fetch_prices(conn, "crypto")
        if prices.empty:
            raise HTTPException(status_code=404, detail="No crypto data")
        scanner_placeholders = ", ".join("?" for _ in ACTIVE_CRYPTO_SCANNERS)
        cursor = await conn.execute(
            f"""
            SELECT *
            FROM scanner_signal_periods
            WHERE market = 'crypto'
              AND scanner IN ({scanner_placeholders})
              AND direction = 'long'
            ORDER BY first_seen_date DESC, id DESC
            """,
            tuple(ACTIVE_CRYPTO_SCANNERS),
        )
        periods = [dict(row) for row in await cursor.fetchall()]

    wide = prices.pivot(index="date", columns="ticker", values="close")
    wide = wide.sort_index()
    data_date = str(max(wide.index))[:10]

    # Same admission rule as the tab: active by today's confidence.
    current_confidence = {}
    for scanner in ACTIVE_CRYPTO_SCANNERS:
        snap_frame, _active = build_scanner_snapshot(wide, scanner)
        if snap_frame.empty:
            continue
        for record in snap_frame.to_dict(orient="records"):
            if record.get("recommendation_class") == "long":
                current_confidence[str(record.get("ticker"))] = (
                    record.get("confidence")
                )
    periods = apply_crypto_confidence_admission(periods, current_confidence)
    prices_by_ticker = {
        ticker: list(series.dropna().items())
        for ticker, series in wide.items()
        if not series.dropna().empty
    }
    report_date = data_date
    active_marks = {}
    report = build_crypto_signal_export(
        periods,
        prices_by_ticker,
        report_date,
    )
    active_tickers = [
        item["ticker"]
        for item in report["rows"]
        if item["status"] == "active"
    ]
    try:
        if active_tickers:
            live_result = await _refresh_crypto_prices_for_today(
                active_tickers,
            )
            report_date = max(
                report_date,
                str(live_result.get("data_date") or report_date),
            )
            active_marks = live_result.get("prices") or {}
    except Exception as exc:
        print(f"Crypto CSV live price refresh failed: {exc}")
    if active_marks:
        report = build_crypto_signal_export(
            periods,
            prices_by_ticker,
            report_date,
            active_marks=active_marks,
            active_mark_date=report_date,
        )
    summary = report["summary"]

    output = io.StringIO(newline="")
    writer = csv.writer(output, delimiter=";", lineterminator="\r\n")
    writer.writerow(["MEANX: позиции раздела Крипта"])
    writer.writerow(["Данные на", report_date])
    writer.writerow(["История раздела с", CRYPTO_PICKS_TRACKING_START])
    writer.writerow([
        "Методика",
        "$100 на каждую монету; цена начала и сигналы: дневные данные "
        "Twelve Data; активная цена: Binance, а при недоступности пары "
        "последний дневной снимок; пересекающиеся сигналы сканеров объединены; "
        "без комиссий, проскальзывания и налогов",
    ])
    writer.writerow(["Всего позиций", summary["positions_total"]])
    writer.writerow(["Активных позиций", summary["positions_active"]])
    writer.writerow(["Позиций в плюсе", summary["positions_profitable"]])
    writer.writerow(["Условно вложено, USD", _csv_number(summary["total_invested"], 2)])
    writer.writerow(["Текущая стоимость, USD", _csv_number(summary["portfolio_value"], 2)])
    writer.writerow(["Общий результат, USD", _csv_number(summary["total_result"], 2)])
    writer.writerow(["Общий результат, %", _csv_number(summary["portfolio_return_pct"], 4)])
    writer.writerow(["Реализованный результат, USD", _csv_number(summary["realized_result"], 2)])
    writer.writerow(["Текущий результат активных, USD", _csv_number(summary["unrealized_result"], 2)])
    writer.writerow([])
    writer.writerow([
        "ID позиции",
        "ID сигналов",
        "Монета",
        "Пара",
        "Сканеры",
        "Уверенность на входе",
        "Статус",
        "Дата покупки",
        "Дата продажи/расчёта",
        "Дней в позиции",
        "Вложено, USD",
        "Цена покупки, USD",
        "Цена продажи/сейчас, USD",
        "Количество монет",
        "Стоимость позиции, USD",
        "Результат, USD",
        "Результат, %",
        "Тип результата",
    ])
    for item in report["rows"]:
        writer.writerow([
            item["position_id"],
            item["period_ids"],
            item["symbol"],
            item["ticker"],
            item["scanner_labels"],
            item["confidence"],
            item["status_label"],
            item["start_date"],
            item["result_date"],
            item["held_days"],
            _csv_number(item["stake"], 2),
            _csv_number(item["start_price"], 8),
            _csv_number(item["result_price"], 8),
            _csv_number(item["quantity"], 8),
            _csv_number(item["position_value"], 4),
            _csv_number(item["cash_result"], 4),
            _csv_number(item["return_pct"], 4),
            item["result_type"],
        ])

    filename = f"meanx-crypto-positions-{report_date}.csv"
    return Response(
        content="\ufeff" + output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("", response_class=HTMLResponse)
async def crypto_picks_tab(
    request: Request,
    refresh: bool = Query(False),
    window_days: int = Query(7),
    confidence: str = Query(""),
):
    await _require_admin(request)
    window_days = _normalize_result_window(window_days)
    confidence_filter = _parse_confidence_filter(confidence)
    context = {
        "request": request,
        "picks": [],
        "total": 0,
        "active_scanner_count": len(ACTIVE_CRYPTO_SCANNERS),
        "confidence_filter": confidence_filter,
        "confidence_options": CONFIDENCE_FILTER_OPTIONS,
        "scanner_data_date": None,
        "history": [],
        "history_total": 0,
        "history_profitable": 0,
        "history_win_rate": None,
        "history_cash_result": 0.0,
        "history_cash_result_display": "$0.00",
        "sell_actions": [],
        "sell_total": 0,
        "weekly_summary": None,
        "result_window_days": window_days,
        "result_window_options": RESULT_WINDOW_OPTIONS,
        "refresh_result": None,
        "refresh_error": None,
        "close_result": getattr(
            request.state,
            "crypto_close_result",
            None,
        ),
        "auto_close_result": [],
    }

    try:
        async with get_connection() as conn:
            prices = await fetch_prices(conn, "crypto")
            live_result = None
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
            records_by_scanner = {}

            for scanner in ACTIVE_CRYPTO_SCANNERS:
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
                records_by_scanner[scanner] = records
                scanner_results[scanner] = [
                    record
                    for record in records
                    if record.get("recommendation_class") == "long"
                    and not record.get("signal_suppressed")
                    and not is_excluded_crypto_confidence(
                        record.get("confidence")
                    )
                    and is_scanner_signal_within_horizon(
                        scanner,
                        record.get("signal_age_days"),
                    )
                ]

            scanner_placeholders = ", ".join(
                "?" for _ in ACTIVE_CRYPTO_SCANNERS
            )
            cursor = await conn.execute(
                f"""
                SELECT *
                FROM scanner_signal_periods
                WHERE market = 'crypto'
                  AND scanner IN ({scanner_placeholders})
                  AND direction = 'long'
                ORDER BY first_seen_date DESC, id DESC
                """,
                tuple(ACTIVE_CRYPTO_SCANNERS),
            )
            period_rows = [dict(row) for row in await cursor.fetchall()]

        # Active positions are admitted by today's scanner confidence (frozen
        # entry confidence still decides completed ones).
        current_confidence = {}
        for records in records_by_scanner.values():
            for record in records:
                if record.get("recommendation_class") == "long":
                    current_confidence[str(record.get("ticker"))] = (
                        record.get("confidence")
                    )
        completed_periods = apply_crypto_confidence_admission(
            period_rows,
            current_confidence,
        )

        daily_prices_by_ticker = {
            ticker: list(series.dropna().items())
            for ticker, series in wide.items()
            if not series.dropna().empty
        }
        prices_by_ticker = daily_prices_by_ticker
        report_date = data_date
        active_marks = {}
        if context["close_result"]:
            report_date = max(
                report_date,
                datetime.now(
                    ZoneInfo("Europe/Moscow")
                ).date().isoformat(),
            )
        report = build_crypto_signal_export(
            completed_periods,
            daily_prices_by_ticker,
            report_date,
        )
        if refresh:
            try:
                active_tickers = [
                    item["ticker"]
                    for item in report["rows"]
                    if item["status"] == "active"
                ]
                if active_tickers:
                    live_result = await _refresh_crypto_prices_for_today(
                        active_tickers,
                    )
                else:
                    live_result = {
                        "prices": {},
                        "updated": 0,
                        "updated_at": datetime.now(
                            ZoneInfo("Europe/Moscow")
                        ),
                        "data_date": report_date,
                    }
                updated_at = live_result.get("updated_at")
                context["refresh_result"] = {
                    "updated": live_result["updated"],
                    "updated_at": (
                        updated_at.astimezone(
                            ZoneInfo("Europe/Moscow")
                        ).strftime("%d.%m.%Y %H:%M")
                        if updated_at
                        else datetime.now(
                            ZoneInfo("Europe/Moscow")
                        ).strftime("%d.%m.%Y %H:%M")
                    ),
                }
            except Exception as exc:
                print(f"Crypto picks refresh failed: {exc}")
                context["refresh_error"] = (
                    "Не удалось обновить котировки Binance. "
                    "Показан последний сохранённый расчёт."
                )
        if live_result:
            report_date = max(
                report_date,
                str(live_result.get("data_date") or report_date),
            )
            prices_by_ticker = _overlay_live_prices(
                prices_by_ticker,
                live_result.get("prices") or {},
                report_date,
            )
            active_marks = live_result.get("prices") or {}
        if active_marks:
            report = build_crypto_signal_export(
                completed_periods,
                daily_prices_by_ticker,
                report_date,
                active_marks=active_marks,
                active_mark_date=report_date,
            )
        active_positions = {
            item["ticker"]: item
            for item in report["rows"]
            if item["status"] == "active"
        }
        latest_prices = {
            ticker: rows[-1][1]
            for ticker, rows in prices_by_ticker.items()
            if rows
        }
        picks = [
            pick
            for pick in aggregate_crypto_long_picks(
                scanner_results,
                latest_prices,
            )
            if pick["ticker"] in active_positions
        ]
        for pick in picks:
            position = active_positions[pick["ticker"]]
            pick["signal_first_seen_date"] = datetime.strptime(
                position["start_date"],
                "%Y-%m-%d",
            ).strftime("%d.%m.%Y")
            progress = build_price_progress(
                prices_by_ticker.get(pick["ticker"], []),
                position["start_date"],
            )
            pick["price_progress"] = progress
            pick["progress_change_pct"] = (
                progress[0]["change_pct"] if progress else None
            )
            pick["progress_change_display"] = (
                progress[0]["change_display"] if progress else "—"
            )
        weekly_summary = build_crypto_window_summary(
            filter_crypto_rows_by_confidence(
                report["rows"],
                confidence_filter,
            ),
            report_date,
            days=window_days,
            prices_by_ticker=prices_by_ticker,
            tracking_start_date=CRYPTO_PICKS_TRACKING_START,
        )
        history = weekly_summary["completed_history"]
        history_profitable = weekly_summary["positions_profitable"]
        history_cash_result = weekly_summary["realized_result"]
        sell_actions = select_crypto_sell_actions(history, report_date)
        return templates.TemplateResponse(
            request,
            "components/crypto_picks_tab.html",
            {
                **context,
                "picks": picks,
                "total": len(picks),
                "scanner_data_date": format_scanner_date(data_date),
                "history": history,
                "history_total": len(history),
                "history_profitable": history_profitable,
                "history_win_rate": (
                    round(history_profitable / len(history) * 100)
                    if history
                    else None
                ),
                "history_cash_result": history_cash_result,
                "history_cash_result_display": (
                    f"+${history_cash_result:.2f}"
                    if history_cash_result > 0
                    else f"-${abs(history_cash_result):.2f}"
                    if history_cash_result < 0
                    else "$0.00"
                ),
                "sell_actions": sell_actions,
                "sell_total": len(sell_actions),
                "weekly_summary": weekly_summary,
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
