"""Admin-only crypto buy ideas assembled from daily scanners."""

from __future__ import annotations

import csv
import io
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response

from app.access import is_admin_user
from app.auth import get_current_user
from app.core.crypto_picks import (
    CRYPTO_PICKS_TRACKING_START,
    aggregate_crypto_long_picks,
    build_completed_crypto_history,
    build_price_progress,
    build_crypto_signal_export,
    select_crypto_sell_actions,
)
from app.core.scanner_history import (
    auto_close_crypto_positions,
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


async def _refresh_crypto_prices_for_today(conn, prices) -> dict:
    tickers = sorted({
        str(ticker)
        for ticker in prices["ticker"].dropna().unique()
        if ticker
    })
    result = await refresh_crypto_live_prices(tickers, ttl_seconds=0)
    live_prices = result.get("prices") or {}
    if not live_prices:
        raise RuntimeError("Binance не вернул актуальные котировки")

    current_date = datetime.now(
        ZoneInfo("Europe/Moscow")
    ).date().isoformat()
    await conn.executemany(
        """
        INSERT INTO prices (ticker, date, close, volume, market)
        VALUES (?, ?, ?, NULL, 'crypto')
        ON CONFLICT(ticker, date) DO UPDATE SET
            close = excluded.close,
            market = 'crypto'
        """,
        [
            (ticker, current_date, float(price))
            for ticker, price in live_prices.items()
            if price and float(price) > 0
        ],
    )
    await conn.commit()
    return {
        **result,
        "updated": len(live_prices),
        "data_date": current_date,
    }


@router.post("/close", response_class=HTMLResponse)
async def close_crypto_pick(
    request: Request,
    ticker: str = Form(...),
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
        ticker_prices = ticker_prices.sort_values("date")
        close_date = str(ticker_prices.iloc[-1]["date"])[:10]
        close_price = float(ticker_prices.iloc[-1]["close"])
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
    return await crypto_picks_tab(request, refresh=False)


@router.get("/export.csv")
async def export_crypto_picks_csv(request: Request):
    await _require_admin(request)
    async with get_connection() as conn:
        await ensure_scanner_history_schema(conn)
        prices = await fetch_prices(conn, "crypto")
        if prices.empty:
            raise HTTPException(status_code=404, detail="No crypto data")
        cursor = await conn.execute(
            """
            SELECT *
            FROM scanner_signal_periods
            WHERE market = 'crypto'
              AND scanner IN ('momentum', 'drawdown')
              AND direction = 'long'
            ORDER BY first_seen_date DESC, id DESC
            """
        )
        periods = [dict(row) for row in await cursor.fetchall()]

    wide = prices.pivot(index="date", columns="ticker", values="close")
    wide = wide.sort_index()
    data_date = str(max(wide.index))[:10]
    prices_by_ticker = {
        ticker: list(series.dropna().items())
        for ticker, series in wide.items()
        if not series.dropna().empty
    }
    report = build_crypto_signal_export(periods, prices_by_ticker, data_date)
    summary = report["summary"]

    output = io.StringIO(newline="")
    writer = csv.writer(output, delimiter=";", lineterminator="\r\n")
    writer.writerow(["MEANX: позиции раздела Крипта"])
    writer.writerow(["Данные на", data_date])
    writer.writerow(["История раздела с", CRYPTO_PICKS_TRACKING_START])
    writer.writerow([
        "Методика",
        "$100 на каждую монету; пересекающиеся сигналы сканеров объединены; "
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

    filename = f"meanx-crypto-positions-{data_date}.csv"
    return Response(
        content="\ufeff" + output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("", response_class=HTMLResponse)
async def crypto_picks_tab(
    request: Request,
    refresh: bool = Query(False),
):
    await _require_admin(request)
    context = {
        "request": request,
        "picks": [],
        "total": 0,
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
            if prices.empty:
                return templates.TemplateResponse(
                    request,
                    "components/crypto_picks_tab.html",
                    context,
                )

            if refresh:
                try:
                    refresh_result = await _refresh_crypto_prices_for_today(
                        conn,
                        prices,
                    )
                    updated_at = refresh_result.get("updated_at")
                    context["refresh_result"] = {
                        "updated": refresh_result["updated"],
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
                    prices = await fetch_prices(conn, "crypto")
                except Exception as exc:
                    print(f"Crypto picks refresh failed: {exc}")
                    context["refresh_error"] = (
                        "Не удалось обновить котировки Binance. "
                        "Показан последний сохранённый расчёт."
                    )

            wide = prices.pivot(index="date", columns="ticker", values="close")
            wide = wide.sort_index()
            data_date = str(max(wide.index))[:10]
            context["auto_close_result"] = await auto_close_crypto_positions(
                conn,
                wide,
                data_date,
            )
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
                    and not record.get("signal_suppressed")
                    and is_scanner_signal_within_horizon(
                        scanner,
                        record.get("signal_age_days"),
                    )
                ]

            cursor = await conn.execute(
                """
                SELECT *
                FROM scanner_signal_periods
                WHERE market = 'crypto'
                  AND scanner IN ('momentum', 'drawdown')
                  AND direction = 'long'
                ORDER BY first_seen_date DESC, id DESC
                """
            )
            completed_periods = [
                dict(row) for row in await cursor.fetchall()
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
        prices_by_ticker = {
            ticker: list(series.dropna().items())
            for ticker, series in wide.items()
            if not series.dropna().empty
        }
        history = build_completed_crypto_history(
            completed_periods,
            prices_by_ticker,
        )
        weekly_summary = build_crypto_signal_export(
            completed_periods,
            prices_by_ticker,
            data_date,
        )["weekly_summary"]
        history_profitable = sum(
            1 for item in history if item["is_profitable"]
        )
        history_cash_result = round(
            sum(float(item["cash_result"]) for item in history),
            2,
        )
        sell_actions = select_crypto_sell_actions(history, data_date)
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
