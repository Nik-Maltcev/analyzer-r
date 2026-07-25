"""Admin-facing aggregation of actionable crypto scanner ideas."""

from __future__ import annotations

import math
from bisect import bisect_right
from datetime import datetime, timedelta
from typing import Any, Iterable


SCANNER_LABELS = {
    "momentum": "Momentum",
    "drawdown": "Drawdown",
}

CRYPTO_PICK_HORIZONS = {
    "momentum": 5,
    "drawdown": 10,
}

CRYPTO_PICKS_TRACKING_START = "2026-07-20"

# Scanners feeding the crypto tab (picks, summary, history, CSV export).
ACTIVE_CRYPTO_SCANNERS = ("momentum", "drawdown")

CONFIDENCE_RANK = {
    "Низкая": 1,
    "Средняя": 2,
    "Высокая": 3,
}

CONFIDENCE_LEVELS = ("Низкая", "Средняя", "Высокая")
UNKNOWN_CONFIDENCE = "Без уровня"

CONFIDENCE_KEY = {
    "Низкая": "low",
    "Средняя": "medium",
    "Высокая": "high",
    UNKNOWN_CONFIDENCE: "unknown",
}


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _normalize_confidence(value: Any) -> str:
    confidence = str(value or "").strip()
    return confidence if confidence in CONFIDENCE_RANK else UNKNOWN_CONFIDENCE


def filter_crypto_rows_by_confidence(
    rows: Iterable[dict[str, Any]],
    selected_keys: Iterable[str],
) -> list[dict[str, Any]]:
    """Keep rows matching the UI confidence filter (empty selection = all)."""
    wanted = {
        label
        for label in CONFIDENCE_LEVELS
        if CONFIDENCE_KEY[label] in set(selected_keys)
    }
    if not wanted:
        return list(rows)
    return [
        row
        for row in rows
        if _normalize_confidence(row.get("confidence")) in wanted
    ]


def is_excluded_crypto_confidence(value: Any) -> bool:
    """Low-confidence signals are not admitted to the crypto tab."""
    return _normalize_confidence(value) == "Низкая"


def _ticker_symbol(ticker: str) -> str:
    return str(ticker).split("/", 1)[0].strip().upper()


def _date_key(value: Any) -> tuple[int, str]:
    text = str(value or "").strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return (0, datetime.strptime(text[:10], fmt).strftime("%Y-%m-%d"))
        except ValueError:
            continue
    return (1, text)


def _days_word(days: int) -> str:
    days = abs(int(days))
    if days % 10 == 1 and days % 100 != 11:
        return "день"
    if days % 10 in {2, 3, 4} and days % 100 not in {12, 13, 14}:
        return "дня"
    return "дней"


def _sell_text(remaining_days: int) -> str:
    if remaining_days <= 0:
        return "пересмотреть позицию и решить о продаже сегодня"
    return (
        f"планово продать примерно через {remaining_days} "
        f"{_days_word(remaining_days)}"
    )


def _format_price(value: Any) -> str:
    try:
        price = float(value)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(price) or price <= 0:
        return "—"
    if price >= 1000:
        return f"${price:,.2f}".replace(",", " ")
    if price >= 1:
        return f"${price:.3f}".rstrip("0").rstrip(".")
    if price >= 0.01:
        return f"${price:.5f}".rstrip("0").rstrip(".")
    return f"${price:.8f}".rstrip("0").rstrip(".")


def build_price_progress(
    dated_prices: Iterable[tuple[Any, Any]],
    first_seen_date: Any,
) -> list[dict[str, Any]]:
    """Return newest-first daily prices measured from signal discovery."""
    start_key = _date_key(first_seen_date)
    if start_key[0] != 0:
        return []

    normalized: list[tuple[str, float]] = []
    for raw_date, raw_price in dated_prices:
        date_key = _date_key(raw_date)
        try:
            price = float(raw_price)
        except (TypeError, ValueError):
            continue
        if (
            date_key[0] != 0
            or date_key[1] < start_key[1]
            or not math.isfinite(price)
            or price <= 0
        ):
            continue
        normalized.append((date_key[1], price))

    normalized.sort(key=lambda item: item[0])
    if not normalized:
        return []

    start_price = normalized[0][1]
    latest_date = normalized[-1][0]
    progress = []
    previous_price: float | None = None
    for iso_date, price in normalized:
        change_pct = (price / start_price - 1) * 100
        day_change_pct = (
            (price / previous_price - 1) * 100
            if previous_price is not None
            else 0.0
        )
        progress.append({
            "date": iso_date,
            "date_display": datetime.strptime(
                iso_date,
                "%Y-%m-%d",
            ).strftime("%d.%m"),
            "price": price,
            "price_display": _format_price(price),
            "change_pct": round(change_pct, 2),
            "change_display": f"{change_pct:+.2f}%",
            "day_change_pct": round(day_change_pct, 2),
            "day_change_display": f"{day_change_pct:+.2f}%",
            "is_start": iso_date == normalized[0][0],
            "is_latest": iso_date == latest_date,
        })
        previous_price = price
    return list(reversed(progress))


def build_completed_crypto_history(
    periods: Iterable[dict[str, Any]],
    prices_by_ticker: dict[str, Iterable[tuple[Any, Any]]],
    stake_per_signal: float = 100.0,
) -> list[dict[str, Any]]:
    """Build completed LONG outcomes at each scanner's fixed horizon."""
    history: list[dict[str, Any]] = []
    price_cache: dict[str, list[tuple[str, float]]] = {}

    for ticker, rows in prices_by_ticker.items():
        normalized: list[tuple[str, float]] = []
        for raw_date, raw_price in rows:
            date_key = _date_key(raw_date)
            try:
                price = float(raw_price)
            except (TypeError, ValueError):
                continue
            if (
                date_key[0] == 0
                and math.isfinite(price)
                and price > 0
            ):
                normalized.append((date_key[1], price))
        price_cache[ticker] = sorted(normalized, key=lambda item: item[0])

    for period in periods:
        scanner = str(period.get("scanner") or "")
        horizon = CRYPTO_PICK_HORIZONS.get(scanner)
        observations = _safe_int(period.get("observation_count"), 0)
        close_reason = str(period.get("close_reason") or "")
        forced_close = close_reason in {"manual", "auto_30_daily"}
        if (
            horizon is None
            or str(period.get("direction") or "") != "long"
            or (observations < horizon and not forced_close)
        ):
            continue

        ticker = str(period.get("ticker_a") or "").strip()
        start_key = _date_key(period.get("first_seen_date"))
        last_key = _date_key(period.get("last_seen_date"))
        if not ticker or start_key[0] != 0 or last_key[0] != 0:
            continue

        dated_prices = [
            (price_date, price)
            for price_date, price in price_cache.get(ticker, [])
            if start_key[1] <= price_date <= last_key[1]
        ]
        if not dated_prices or (len(dated_prices) < horizon and not forced_close):
            continue

        start_date, start_price = dated_prices[0]
        if forced_close:
            close_key = _date_key(
                period.get("ended_date") or period.get("last_seen_date")
            )
            end_date = close_key[1] if close_key[0] == 0 else dated_prices[-1][0]
            end_price = _safe_float(period.get("closed_price"))
            if end_price is None or end_price <= 0:
                available = [
                    (price_date, price)
                    for price_date, price in price_cache.get(ticker, [])
                    if price_date <= end_date
                ]
                if not available:
                    continue
                end_date, end_price = available[-1]
        else:
            end_date, end_price = dated_prices[horizon - 1]
        return_pct = (end_price / start_price - 1) * 100
        cash_result = round(stake_per_signal * return_pct / 100, 2)
        if return_pct > 0:
            result_label = "В плюсе"
        elif return_pct < 0:
            result_label = "В минусе"
        else:
            result_label = "Без изменений"

        history.append({
            "period_id": period.get("id"),
            "ticker": ticker,
            "symbol": _ticker_symbol(ticker),
            "scanner": scanner,
            "scanner_label": SCANNER_LABELS[scanner],
            "horizon_days": horizon,
            "close_reason": close_reason or None,
            "start_date": start_date,
            "start_date_display": datetime.strptime(
                start_date,
                "%Y-%m-%d",
            ).strftime("%d.%m.%Y"),
            "end_date": end_date,
            "end_date_display": datetime.strptime(
                end_date,
                "%Y-%m-%d",
            ).strftime("%d.%m.%Y"),
            "start_price": start_price,
            "start_price_display": _format_price(start_price),
            "end_price": end_price,
            "end_price_display": _format_price(end_price),
            "return_pct": round(return_pct, 2),
            "return_display": f"{return_pct:+.2f}%",
            "stake": stake_per_signal,
            "cash_result": cash_result,
            "cash_result_display": (
                f"+${cash_result:.2f}"
                if cash_result > 0
                else f"-${abs(cash_result):.2f}"
                if cash_result < 0
                else "$0.00"
            ),
            "is_profitable": return_pct > 0,
            "result_label": result_label,
        })

    return sorted(
        history,
        key=lambda item: (
            item["end_date"],
            item["start_date"],
            item["symbol"],
            item["scanner"],
        ),
        reverse=True,
    )


def select_crypto_sell_actions(
    history: Iterable[dict[str, Any]],
    data_date: Any,
) -> list[dict[str, Any]]:
    """Return completed positions that reached their horizon today."""
    target = _date_key(data_date)
    if target[0] != 0:
        return []
    return [
        item
        for item in history
        if _date_key(item.get("end_date")) == target
        and item.get("close_reason") != "manual"
    ]


def build_crypto_signal_export(
    periods: Iterable[dict[str, Any]],
    prices_by_ticker: dict[str, Iterable[tuple[Any, Any]]],
    data_date: Any,
    stake_per_signal: float = 100.0,
    tracking_start_date: Any = CRYPTO_PICKS_TRACKING_START,
    active_marks: dict[str, Any] | None = None,
    active_mark_date: Any = None,
) -> dict[str, Any]:
    """Build coin positions from LONG scanner periods with equal USD stakes."""
    target_date = _date_key(data_date)
    latest_date = target_date[1] if target_date[0] == 0 else "9999-12-31"
    tracking_start = _date_key(tracking_start_date)
    tracking_date = (
        tracking_start[1] if tracking_start[0] == 0 else "0000-01-01"
    )
    price_cache: dict[str, list[tuple[str, float]]] = {}

    for ticker, price_rows in prices_by_ticker.items():
        normalized: list[tuple[str, float]] = []
        for raw_date, raw_price in price_rows:
            date_key = _date_key(raw_date)
            try:
                price = float(raw_price)
            except (TypeError, ValueError):
                continue
            if (
                date_key[0] == 0
                and date_key[1] <= latest_date
                and math.isfinite(price)
                and price > 0
            ):
                normalized.append((date_key[1], price))
        price_cache[ticker] = sorted(normalized, key=lambda item: item[0])

    signal_rows: list[dict[str, Any]] = []
    for period in periods:
        scanner = str(period.get("scanner") or "")
        horizon = CRYPTO_PICK_HORIZONS.get(scanner)
        if horizon is None or str(period.get("direction") or "") != "long":
            continue

        ticker = str(period.get("ticker_a") or "").strip()
        start_key = _date_key(period.get("first_seen_date"))
        if not ticker or start_key[0] != 0:
            continue

        original_prices = [
            (price_date, price)
            for price_date, price in price_cache.get(ticker, [])
            if price_date >= start_key[1]
        ]
        if not original_prices:
            continue

        observations = _safe_int(period.get("observation_count"), 0)
        raw_status = str(period.get("status") or "active")
        close_reason = str(period.get("close_reason") or "")
        forced_close = close_reason in {"manual", "auto_30_daily"}
        horizon_result = (
            original_prices[horizon - 1]
            if observations >= horizon and len(original_prices) >= horizon
            else None
        )
        last_seen_key = _date_key(period.get("last_seen_date"))
        last_visible_date = (
            last_seen_key[1] if last_seen_key[0] == 0 else start_key[1]
        )
        ended_key = _date_key(period.get("ended_date"))
        if forced_close and ended_key[0] == 0:
            last_visible_date = ended_key[1]
        elif horizon_result is not None:
            last_visible_date = horizon_result[0]
        elif raw_status == "closed" and ended_key[0] == 0:
            last_visible_date = ended_key[1]
        if last_visible_date < tracking_date:
            continue

        # Tracking controls which positions belong to this product section. It
        # must never rewrite the entry of a signal that was already active.
        dated_prices = original_prices
        if not dated_prices:
            continue
        tracked_from_date = max(start_key[1], tracking_date)

        if forced_close:
            close_key = _date_key(
                period.get("ended_date") or period.get("last_seen_date")
            )
            close_date = close_key[1] if close_key[0] == 0 else latest_date
            available = [
                (price_date, price)
                for price_date, price in dated_prices
                if price_date <= close_date
            ]
            result_price = _safe_float(period.get("closed_price"))
            if result_price is None or result_price <= 0:
                if not available:
                    continue
                result_date, result_price = available[-1]
            else:
                result_date = close_date
            status = (
                "closed_manual"
                if close_reason == "manual"
                else "closed_auto"
            )
            status_label = (
                "Закрыта вручную"
                if close_reason == "manual"
                else "Автозакрытие: +30% за день"
            )
            result_type = "реализованный"
        elif horizon_result is not None:
            result_date, result_price = horizon_result
            if result_date < dated_prices[0][0]:
                continue
            status = "completed"
            status_label = "Завершён по сроку"
            result_type = "реализованный"
        elif raw_status == "closed":
            close_key = _date_key(
                period.get("ended_date") or period.get("last_seen_date")
            )
            close_date = close_key[1] if close_key[0] == 0 else latest_date
            available = [
                (price_date, price)
                for price_date, price in dated_prices
                if price_date <= close_date
            ]
            if not available:
                continue
            result_date, result_price = available[-1]
            status = "closed_early"
            status_label = "Закрыт раньше срока"
            result_type = "реализованный"
        else:
            result_date, result_price = dated_prices[-1]
            status = "active"
            status_label = "Активен"
            result_type = "текущий"

        start_date, start_price = dated_prices[0]
        held_days = sum(
            1 for price_date, _ in dated_prices if price_date <= result_date
        )
        return_pct = (result_price / start_price - 1) * 100
        cash_result = stake_per_signal * return_pct / 100
        signal_rows.append({
            "period_id": period.get("id"),
            "ticker": ticker,
            "symbol": _ticker_symbol(ticker),
            "scanner": scanner,
            "scanner_label": SCANNER_LABELS[scanner],
            "confidence": _normalize_confidence(period.get("confidence")),
            "status": status,
            "status_label": status_label,
            "result_type": result_type,
            "horizon_days": horizon,
            "held_days": held_days,
            "start_date": start_date,
            "tracked_from_date": tracked_from_date,
            "result_date": result_date,
            "start_price": start_price,
            "result_price": result_price,
            "return_pct": round(return_pct, 4),
            "cash_result": round(cash_result, 4),
            "is_profitable": return_pct > 0,
            "close_reason": close_reason or None,
        })

    rows = _merge_crypto_signal_positions(signal_rows, stake_per_signal)
    _mark_active_crypto_positions(
        rows,
        active_marks or {},
        active_mark_date,
    )
    rows.sort(
        key=lambda item: (
            item["start_date"],
            item["symbol"],
            item["position_id"],
        ),
        reverse=True,
    )
    total_invested = stake_per_signal * len(rows)
    total_result = sum(item["cash_result"] for item in rows)
    realized_result = sum(
        item["cash_result"]
        for item in rows
        if item["status"] != "active"
    )
    unrealized_result = total_result - realized_result
    summary = {
        "positions_total": len(rows),
        "positions_active": sum(1 for item in rows if item["status"] == "active"),
        "positions_profitable": sum(1 for item in rows if item["is_profitable"]),
        "stake_per_signal": stake_per_signal,
        "total_invested": round(total_invested, 2),
        "portfolio_value": round(total_invested + total_result, 2),
        "total_result": round(total_result, 2),
        "realized_result": round(realized_result, 2),
        "unrealized_result": round(unrealized_result, 2),
        "realized_result_display": (
            f"+${realized_result:.2f}"
            if realized_result > 0
            else f"-${abs(realized_result):.2f}"
            if realized_result < 0
            else "$0.00"
        ),
        "unrealized_result_display": (
            f"+${unrealized_result:.2f}"
            if unrealized_result > 0
            else f"-${abs(unrealized_result):.2f}"
            if unrealized_result < 0
            else "$0.00"
        ),
        "portfolio_return_pct": round(
            total_result / total_invested * 100,
            4,
        ) if total_invested else 0.0,
    }
    _validate_crypto_position_math(rows, summary)
    return {
        "rows": rows,
        "summary": summary,
        "weekly_summary": build_crypto_window_summary(
            rows,
            data_date,
            days=7,
            prices_by_ticker=prices_by_ticker,
            tracking_start_date=tracking_start_date,
        ),
    }


def _mark_active_crypto_positions(
    rows: Iterable[dict[str, Any]],
    active_marks: dict[str, Any],
    mark_date: Any,
) -> None:
    """Apply live marks only to open positions; closed outcomes stay fixed."""
    mark_key = _date_key(mark_date)
    if mark_key[0] != 0:
        return

    for row in rows:
        if row.get("status") != "active":
            continue
        mark_price = _safe_float(active_marks.get(str(row.get("ticker"))))
        start_price = _safe_float(row.get("start_price"))
        start_key = _date_key(row.get("start_date"))
        if (
            mark_price is None
            or mark_price <= 0
            or start_price is None
            or start_price <= 0
            or start_key[0] != 0
            or mark_key[1] < start_key[1]
        ):
            continue

        stake = float(row.get("stake") or 0)
        return_pct = (mark_price / start_price - 1) * 100
        cash_result = round(stake * return_pct / 100, 2)
        start_dt = datetime.strptime(start_key[1], "%Y-%m-%d")
        mark_dt = datetime.strptime(mark_key[1], "%Y-%m-%d")
        row.update({
            "held_days": (mark_dt - start_dt).days + 1,
            "result_date": mark_key[1],
            "result_price": mark_price,
            "position_value": round(stake + cash_result, 2),
            "return_pct": round(return_pct, 4),
            "cash_result": cash_result,
            "is_profitable": return_pct > 0,
        })


def _validate_crypto_position_math(
    rows: Iterable[dict[str, Any]],
    summary: dict[str, Any],
) -> None:
    """Fail closed if displayed position arithmetic becomes inconsistent."""
    positions = list(rows)
    for row in positions:
        start_price = float(row["start_price"])
        result_price = float(row["result_price"])
        stake = float(row["stake"])
        expected_return = (result_price / start_price - 1) * 100
        expected_cash = round(stake * expected_return / 100, 2)
        if not math.isclose(
            float(row["return_pct"]),
            expected_return,
            abs_tol=0.0001,
        ):
            raise ValueError(
                f"Crypto return mismatch for {row['position_id']}"
            )
        if not math.isclose(
            float(row["cash_result"]),
            expected_cash,
            abs_tol=0.005,
        ):
            raise ValueError(
                f"Crypto cash result mismatch for {row['position_id']}"
            )

    expected_total = round(
        sum(float(row["cash_result"]) for row in positions),
        2,
    )
    expected_realized = round(
        sum(
            float(row["cash_result"])
            for row in positions
            if row["status"] != "active"
        ),
        2,
    )
    expected_unrealized = round(expected_total - expected_realized, 2)
    expected_invested = round(
        sum(float(row["stake"]) for row in positions),
        2,
    )
    expected_portfolio_value = round(
        expected_invested + expected_total,
        2,
    )
    expected_return = round(
        expected_total / expected_invested * 100,
        4,
    ) if expected_invested else 0.0
    checks = {
        "total_result": expected_total,
        "realized_result": expected_realized,
        "unrealized_result": expected_unrealized,
        "total_invested": expected_invested,
        "portfolio_value": expected_portfolio_value,
        "portfolio_return_pct": expected_return,
    }
    for key, expected in checks.items():
        if not math.isclose(
            float(summary[key]),
            expected,
            abs_tol=0.005,
        ):
            raise ValueError(f"Crypto summary mismatch: {key}")


def build_crypto_window_summary(
    rows: Iterable[dict[str, Any]],
    data_date: Any,
    days: int = 7,
    prices_by_ticker: dict[str, Iterable[tuple[Any, Any]]] | None = None,
    tracking_start_date: Any = None,
) -> dict[str, Any]:
    """Summarize the cohort of positions opened in a trailing date window."""
    target_key = _date_key(data_date)
    window_days = max(1, _safe_int(days, 7))
    if target_key[0] != 0:
        return {
            "days": window_days,
            "available_days": 0,
            "is_partial_window": True,
            "tracking_start_date_display": "—",
            "positions_total": 0,
            "positions_active": 0,
            "positions_completed": 0,
            "positions_profitable": 0,
            "positions_unprofitable": 0,
            "positions_flat": 0,
            "total_invested": 0.0,
            "total_result": 0.0,
            "realized_result": 0.0,
            "unrealized_result": 0.0,
            "realized_result_display": "$0.00",
            "unrealized_result_display": "$0.00",
            "portfolio_return_pct": 0.0,
            "start_date": None,
            "end_date": None,
            "start_date_display": "—",
            "end_date_display": "—",
            "result_display": "$0.00",
            "return_display": "+0.00%",
            "result_timeline": [],
            "confidence_breakdown": _build_confidence_breakdown([]),
            "scanner_breakdown": _build_scanner_breakdown([]),
            "completed_history": [],
        }

    end_date = datetime.strptime(target_key[1], "%Y-%m-%d")
    start_date = end_date - timedelta(days=window_days - 1)
    tracking_key = _date_key(tracking_start_date)
    tracking_start = None
    if tracking_key[0] == 0:
        tracking_start = datetime.strptime(tracking_key[1], "%Y-%m-%d")
        if tracking_start > start_date:
            start_date = min(tracking_start, end_date)
    start_iso = start_date.strftime("%Y-%m-%d")
    end_iso = end_date.strftime("%Y-%m-%d")
    available_days = (end_date - start_date).days + 1
    relevant: list[dict[str, Any]] = []

    for row in rows:
        position_start = _date_key(
            row.get("tracked_from_date") or row.get("start_date")
        )
        position_end = _date_key(row.get("result_date"))
        if (
            position_start[0] == 0
            and position_end[0] == 0
            and position_start[1] >= start_iso
            and position_start[1] <= end_iso
        ):
            relevant.append(row)

    completed = [
        row for row in relevant if row.get("status") != "active"
    ]
    active = [
        row for row in relevant if row.get("status") == "active"
    ]
    total_invested = sum(float(row.get("stake") or 0) for row in relevant)
    total_result = sum(float(row.get("cash_result") or 0) for row in relevant)
    realized_result = sum(
        float(row.get("cash_result") or 0) for row in completed
    )
    unrealized_result = sum(
        float(row.get("cash_result") or 0) for row in active
    )

    summary = {
        "days": window_days,
        "available_days": available_days,
        "is_partial_window": available_days < window_days,
        "tracking_start_date_display": (
            tracking_start.strftime("%d.%m.%Y")
            if tracking_start is not None
            else start_date.strftime("%d.%m.%Y")
        ),
        "positions_total": len(relevant),
        "positions_active": len(active),
        "positions_completed": len(completed),
        "positions_profitable": sum(
            1 for row in completed if float(row.get("return_pct") or 0) > 0
        ),
        "positions_unprofitable": sum(
            1 for row in completed if float(row.get("return_pct") or 0) < 0
        ),
        "positions_flat": sum(
            1 for row in completed if float(row.get("return_pct") or 0) == 0
        ),
        "total_invested": round(total_invested, 2),
        "total_result": round(total_result, 2),
        "realized_result": round(realized_result, 2),
        "unrealized_result": round(unrealized_result, 2),
        "realized_result_display": (
            f"+${realized_result:.2f}"
            if realized_result > 0
            else f"-${abs(realized_result):.2f}"
            if realized_result < 0
            else "$0.00"
        ),
        "unrealized_result_display": (
            f"+${unrealized_result:.2f}"
            if unrealized_result > 0
            else f"-${abs(unrealized_result):.2f}"
            if unrealized_result < 0
            else "$0.00"
        ),
        "portfolio_return_pct": round(
            total_result / total_invested * 100,
            4,
        ) if total_invested else 0.0,
        "start_date": start_iso,
        "end_date": end_iso,
        "start_date_display": start_date.strftime("%d.%m"),
        "end_date_display": end_date.strftime("%d.%m"),
        "result_display": (
            f"+${total_result:.2f}"
            if total_result > 0
            else f"-${abs(total_result):.2f}"
            if total_result < 0
            else "$0.00"
        ),
        "return_display": (
            f"{total_result / total_invested * 100:+.2f}%"
            if total_invested
            else "+0.00%"
        ),
        "result_timeline": _build_portfolio_result_timeline(
            relevant,
            start_iso,
            end_iso,
            prices_by_ticker or {},
        ),
        "confidence_breakdown": _build_confidence_breakdown(relevant),
        "scanner_breakdown": _build_scanner_breakdown(relevant),
        "completed_history": _build_completed_position_history(completed),
    }
    _validate_crypto_window_math(summary)
    return summary


def _validate_crypto_window_math(summary: dict[str, Any]) -> None:
    """Keep every visible window total tied to the same position cohort."""
    if (
        int(summary["positions_active"])
        + int(summary["positions_completed"])
        != int(summary["positions_total"])
    ):
        raise ValueError("Crypto window position count mismatch")

    completed_outcomes = (
        int(summary["positions_profitable"])
        + int(summary["positions_unprofitable"])
        + int(summary["positions_flat"])
    )
    if completed_outcomes != int(summary["positions_completed"]):
        raise ValueError("Crypto window outcome count mismatch")

    expected_total = round(
        float(summary["realized_result"])
        + float(summary["unrealized_result"]),
        2,
    )
    if not math.isclose(
        float(summary["total_result"]),
        expected_total,
        abs_tol=0.005,
    ):
        raise ValueError("Crypto window result mismatch")

    history = list(summary.get("completed_history") or [])
    if len(history) != int(summary["positions_completed"]):
        raise ValueError("Crypto window history count mismatch")
    history_result = round(
        sum(float(item.get("cash_result") or 0) for item in history),
        2,
    )
    if not math.isclose(
        float(summary["realized_result"]),
        history_result,
        abs_tol=0.005,
    ):
        raise ValueError("Crypto window history result mismatch")

    confidence_total = sum(
        int(item.get("positions_total") or 0)
        for item in summary.get("confidence_breakdown") or []
    )
    if confidence_total != int(summary["positions_total"]):
        raise ValueError("Crypto window confidence count mismatch")

    timeline = list(summary.get("result_timeline") or [])
    if timeline and not math.isclose(
        float(timeline[-1]["result"]),
        float(summary["total_result"]),
        abs_tol=0.005,
    ):
        raise ValueError("Crypto window timeline result mismatch")


def _build_completed_position_history(
    rows: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Format the exact closed-position cohort used by the summary."""
    history: list[dict[str, Any]] = []
    close_reasons = {
        "closed_manual": "manual",
        "closed_auto": "auto_30_daily",
        "closed_early": "signal_ended",
    }

    for row in rows:
        status = str(row.get("status") or "")
        if status == "active":
            continue

        start_key = _date_key(row.get("start_date"))
        end_key = _date_key(row.get("result_date"))
        start_price = _safe_float(row.get("start_price"))
        end_price = _safe_float(row.get("result_price"))
        return_pct = float(row.get("return_pct") or 0)
        cash_result = float(row.get("cash_result") or 0)
        if (
            start_key[0] != 0
            or end_key[0] != 0
        ):
            continue

        if return_pct > 0:
            result_label = "В плюсе"
        elif return_pct < 0:
            result_label = "В минусе"
        else:
            result_label = "Без изменений"

        history.append({
            "position_id": row.get("position_id"),
            "ticker": str(row.get("ticker") or ""),
            "symbol": str(row.get("symbol") or ""),
            "scanner_label": str(
                row.get("scanner_labels")
                or row.get("scanner_label")
                or ""
            ),
            "horizon_days": _safe_int(row.get("held_days"), 0),
            "close_reason": close_reasons.get(status),
            "start_date": start_key[1],
            "start_date_display": datetime.strptime(
                start_key[1],
                "%Y-%m-%d",
            ).strftime("%d.%m.%Y"),
            "end_date": end_key[1],
            "end_date_display": datetime.strptime(
                end_key[1],
                "%Y-%m-%d",
            ).strftime("%d.%m.%Y"),
            "start_price": start_price,
            "start_price_display": _format_price(start_price),
            "end_price": end_price,
            "end_price_display": _format_price(end_price),
            "return_pct": round(return_pct, 2),
            "return_display": f"{return_pct:+.2f}%",
            "stake": float(row.get("stake") or 0),
            "cash_result": round(cash_result, 2),
            "cash_result_display": (
                f"+${cash_result:.2f}"
                if cash_result > 0
                else f"-${abs(cash_result):.2f}"
                if cash_result < 0
                else "$0.00"
            ),
            "is_profitable": return_pct > 0,
            "result_label": result_label,
        })

    return sorted(
        history,
        key=lambda item: (
            item["end_date"],
            item["start_date"],
            item["symbol"],
            str(item.get("position_id") or ""),
        ),
        reverse=True,
    )


def _build_portfolio_result_timeline(
    rows: Iterable[dict[str, Any]],
    start_date: str,
    end_date: str,
    prices_by_ticker: dict[str, Iterable[tuple[Any, Any]]],
) -> list[dict[str, Any]]:
    """Mark each window day with the cohort's actual or fixed USD result."""
    positions = list(rows)
    price_cache: dict[str, tuple[list[str], list[float]]] = {}
    for ticker, price_rows in prices_by_ticker.items():
        normalized: list[tuple[str, float]] = []
        for raw_date, raw_price in price_rows:
            date_key = _date_key(raw_date)
            price = _safe_float(raw_price)
            if (
                date_key[0] == 0
                and price is not None
                and price > 0
                and date_key[1] <= end_date
            ):
                normalized.append((date_key[1], price))
        normalized.sort(key=lambda item: item[0])
        price_cache[str(ticker)] = (
            [item[0] for item in normalized],
            [item[1] for item in normalized],
        )

    current = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")
    timeline: list[dict[str, Any]] = []
    while current <= end:
        date_iso = current.strftime("%Y-%m-%d")
        total_result = 0.0
        for row in positions:
            position_start = str(row.get("start_date") or "")
            position_end = str(row.get("result_date") or "")
            if not position_start or date_iso < position_start:
                continue

            if position_end and date_iso >= position_end:
                total_result += float(row.get("cash_result") or 0)
                continue

            ticker = str(row.get("ticker") or "")
            price_dates, price_values = price_cache.get(ticker, ([], []))
            price_index = bisect_right(price_dates, date_iso) - 1
            if price_index >= 0 and price_dates[price_index] >= position_start:
                stake = float(row.get("stake") or 0)
                quantity = _safe_float(row.get("quantity"))
                start_price = _safe_float(row.get("start_price"))
                if quantity is None and start_price:
                    quantity = stake / start_price
                if quantity is not None:
                    total_result += quantity * price_values[price_index] - stake
                    continue

        rounded_result = round(total_result, 2)
        timeline.append({
            "date": date_iso,
            "date_display": current.strftime("%d.%m"),
            "result": rounded_result,
            "result_display": (
                f"+${rounded_result:.2f}"
                if rounded_result > 0
                else f"-${abs(rounded_result):.2f}"
                if rounded_result < 0
                else "$0.00"
            ),
        })
        current += timedelta(days=1)

    return timeline


def _build_confidence_breakdown(
    rows: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    grouped = {
        level: []
        for level in (*CONFIDENCE_LEVELS, UNKNOWN_CONFIDENCE)
    }
    for row in rows:
        grouped[_normalize_confidence(row.get("confidence"))].append(row)

    result: list[dict[str, Any]] = []
    for confidence in (*CONFIDENCE_LEVELS, UNKNOWN_CONFIDENCE):
        items = grouped[confidence]
        if confidence in ("Низкая", UNKNOWN_CONFIDENCE) and not items:
            continue
        completed = [
            item for item in items if item.get("status") != "active"
        ]
        active = [
            item for item in items if item.get("status") == "active"
        ]
        profitable = sum(
            1
            for item in completed
            if float(item.get("return_pct") or 0) > 0
        )
        unprofitable = sum(
            1
            for item in completed
            if float(item.get("return_pct") or 0) < 0
        )
        flat = len(completed) - profitable - unprofitable
        realized_result = sum(
            float(item.get("cash_result") or 0)
            for item in completed
        )
        win_rate = (
            round(profitable / len(completed) * 100, 1)
            if completed
            else None
        )
        result.append({
            "key": CONFIDENCE_KEY[confidence],
            "label": confidence,
            "positions_total": len(items),
            "positions_active": len(active),
            "positions_completed": len(completed),
            "positions_profitable": profitable,
            "positions_unprofitable": unprofitable,
            "positions_flat": flat,
            "win_rate": win_rate,
            "win_rate_display": (
                f"{win_rate:.1f}".rstrip("0").rstrip(".")
                if win_rate is not None
                else "—"
            ),
            "realized_result": round(realized_result, 2),
            "result_display": (
                f"+${realized_result:.2f}"
                if realized_result > 0
                else f"-${abs(realized_result):.2f}"
                if realized_result < 0
                else "$0.00"
            ),
        })
    return result


def _row_scanner_labels(row: dict[str, Any]) -> list[str]:
    """Scanner names attached to a position (a coin can have both)."""
    scanners = row.get("scanners")
    if isinstance(scanners, (list, tuple)):
        return [str(item) for item in scanners if str(item).strip()]
    labels = str(row.get("scanner_labels") or row.get("scanner_label") or "")
    return [
        part.strip()
        for part in labels.split("+")
        if part.strip()
    ]


def _build_scanner_breakdown(
    rows: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Realized result per scanner; shared positions count for each side."""
    positions = list(rows)
    result: list[dict[str, Any]] = []
    for key in ACTIVE_CRYPTO_SCANNERS:
        label = SCANNER_LABELS[key]
        items = [
            row for row in positions if label in _row_scanner_labels(row)
        ]
        completed = [
            item for item in items if item.get("status") != "active"
        ]
        active = [
            item for item in items if item.get("status") == "active"
        ]
        profitable = sum(
            1
            for item in completed
            if float(item.get("return_pct") or 0) > 0
        )
        realized_result = sum(
            float(item.get("cash_result") or 0)
            for item in completed
        )
        win_rate = (
            round(profitable / len(completed) * 100, 1)
            if completed
            else None
        )
        result.append({
            "key": key,
            "label": label,
            "positions_total": len(items),
            "positions_active": len(active),
            "positions_completed": len(completed),
            "positions_profitable": profitable,
            "win_rate": win_rate,
            "win_rate_display": (
                f"{win_rate:.1f}".rstrip("0").rstrip(".")
                if win_rate is not None
                else "—"
            ),
            "realized_result": round(realized_result, 2),
            "result_display": (
                f"+${realized_result:.2f}"
                if realized_result > 0
                else f"-${abs(realized_result):.2f}"
                if realized_result < 0
                else "$0.00"
            ),
        })
    return result


def _merge_crypto_signal_positions(
    signal_rows: Iterable[dict[str, Any]],
    stake_per_position: float,
) -> list[dict[str, Any]]:
    """Merge overlapping scanner rows into one buy/sell position per coin."""
    status_priority = {
        "closed_manual": 0,
        "closed_auto": 1,
        "completed": 2,
        "closed_early": 3,
        "active": 4,
    }

    def select_end_row(
        episode: list[dict[str, Any]],
    ) -> dict[str, Any]:
        forced = [
            item
            for item in episode
            if item["status"] in {"closed_manual", "closed_auto"}
        ]
        if forced:
            return min(
                forced,
                key=lambda item: (
                    item["result_date"],
                    status_priority[item["status"]],
                    item["scanner"],
                ),
            )

        completed = [
            item for item in episode if item["status"] == "completed"
        ]
        if completed:
            return min(
                completed,
                key=lambda item: (
                    item["result_date"],
                    item["scanner"],
                ),
            )

        active = [
            item for item in episode if item["status"] == "active"
        ]
        if active:
            return max(
                active,
                key=lambda item: (
                    item["result_date"],
                    item["scanner"],
                ),
            )

        return max(
            episode,
            key=lambda item: (
                item["result_date"],
                item["scanner"],
            ),
        )

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in signal_rows:
        grouped.setdefault(str(row["ticker"]), []).append(row)

    positions: list[dict[str, Any]] = []
    for ticker, ticker_rows in grouped.items():
        ticker_rows.sort(
            key=lambda item: (
                item["start_date"],
                item["result_date"],
                item["scanner"],
            )
        )
        episodes: list[list[dict[str, Any]]] = []
        for row in ticker_rows:
            if not episodes:
                episodes.append([row])
                continue

            current_episode = episodes[-1]
            current_end = select_end_row(current_episode)
            close_boundary = (
                current_end["result_date"]
                if current_end["status"] != "active"
                else None
            )
            episode_end = max(
                item["result_date"] for item in current_episode
            )
            overlaps_open_position = (
                row["start_date"] < close_boundary
                if close_boundary is not None
                else row["start_date"] <= episode_end
            )
            if overlaps_open_position:
                episodes[-1].append(row)
            else:
                episodes.append([row])

        for sequence, episode in enumerate(episodes, start=1):
            start_row = min(
                episode,
                key=lambda item: (item["start_date"], item["scanner"]),
            )
            entry_rows = [
                item
                for item in episode
                if item["start_date"] == start_row["start_date"]
            ]
            entry_confidence = max(
                (
                    _normalize_confidence(item.get("confidence"))
                    for item in entry_rows
                ),
                key=lambda value: CONFIDENCE_RANK.get(value, 0),
                default=UNKNOWN_CONFIDENCE,
            )
            end_row = select_end_row(episode)
            start_price = float(start_row["start_price"])
            result_price = float(end_row["result_price"])
            return_pct = (result_price / start_price - 1) * 100
            cash_result = round(
                stake_per_position * return_pct / 100,
                2,
            )
            position_status = end_row["status"]
            is_active = position_status == "active"
            status_label = {
                "active": "Активна",
                "completed": "Продана",
                "closed_early": "Продана раньше срока",
                "closed_manual": "Закрыта вручную",
                "closed_auto": "Автозакрытие: +30% за день",
            }[position_status]
            start_dt = datetime.strptime(start_row["start_date"], "%Y-%m-%d")
            result_dt = datetime.strptime(end_row["result_date"], "%Y-%m-%d")
            scanner_names = sorted({item["scanner_label"] for item in episode})
            period_ids = sorted(
                str(item["period_id"])
                for item in episode
                if item.get("period_id") is not None
            )
            quantity = stake_per_position / start_price

            positions.append({
                "position_id": (
                    f"{start_row['symbol']}-{start_row['start_date']}-{sequence}"
                ),
                "period_ids": ",".join(period_ids),
                "ticker": ticker,
                "symbol": start_row["symbol"],
                "scanners": scanner_names,
                "scanner_labels": " + ".join(scanner_names),
                "source_signals": len(episode),
                "confidence": entry_confidence,
                "status": position_status,
                "status_label": status_label,
                "result_type": "текущий" if is_active else "реализованный",
                "held_days": (result_dt - start_dt).days + 1,
                "start_date": start_row["start_date"],
                "tracked_from_date": min(
                    item.get("tracked_from_date") or item["start_date"]
                    for item in episode
                ),
                "result_date": end_row["result_date"],
                "start_price": start_price,
                "result_price": result_price,
                "stake": stake_per_position,
                "quantity": quantity,
                "position_value": stake_per_position + cash_result,
                "return_pct": round(return_pct, 4),
                "cash_result": cash_result,
                "is_profitable": return_pct > 0,
            })

    return positions


def aggregate_crypto_long_picks(
    scanner_results: dict[str, Iterable[dict[str, Any]]],
    latest_prices: dict[str, Any],
) -> list[dict[str, Any]]:
    """Merge explicit LONG signals and keep the shortest review horizon."""
    grouped: dict[str, dict[str, Any]] = {}

    for scanner, records in scanner_results.items():
        if scanner not in SCANNER_LABELS:
            continue
        for record in records:
            if record.get("recommendation_class") != "long":
                continue
            if record.get("signal_within_horizon") is False:
                continue

            ticker = str(record.get("ticker") or "").strip()
            if not ticker:
                continue
            symbol = _ticker_symbol(ticker)
            age = max(1, _safe_int(record.get("signal_age_days"), 1))
            remaining = max(
                0,
                _safe_int(record.get("signal_remaining_days"), 0),
            )
            confidence = str(record.get("confidence") or "Низкая")
            rank = CONFIDENCE_RANK.get(confidence, 0)
            first_seen = str(record.get("signal_first_seen_date") or "")

            pick = grouped.setdefault(
                ticker,
                {
                    "ticker": ticker,
                    "symbol": symbol,
                    "signal_age_days": age,
                    "signal_remaining_days": remaining,
                    "signal_first_seen_date": first_seen,
                    "confidence": confidence,
                    "confidence_rank": rank,
                    "scanners": [],
                },
            )
            pick["signal_age_days"] = max(pick["signal_age_days"], age)
            pick["signal_remaining_days"] = min(
                pick["signal_remaining_days"],
                remaining,
            )
            if first_seen and (
                not pick["signal_first_seen_date"]
                or _date_key(first_seen) < _date_key(pick["signal_first_seen_date"])
            ):
                pick["signal_first_seen_date"] = first_seen
            if rank > pick["confidence_rank"]:
                pick["confidence"] = confidence
                pick["confidence_rank"] = rank
            if scanner not in pick["scanners"]:
                pick["scanners"].append(scanner)

    picks = []
    for ticker, pick in grouped.items():
        pick["scanner_labels"] = [
            SCANNER_LABELS[scanner] for scanner in pick["scanners"]
        ]
        pick["scanner_count"] = len(pick["scanners"])
        pick["current_price_display"] = _format_price(latest_prices.get(ticker))
        pick["action_text"] = _sell_text(pick["signal_remaining_days"])
        picks.append(pick)

    return sorted(
        picks,
        key=lambda item: (
            -item["scanner_count"],
            -item["confidence_rank"],
            item["signal_remaining_days"],
            item["symbol"],
        ),
    )
