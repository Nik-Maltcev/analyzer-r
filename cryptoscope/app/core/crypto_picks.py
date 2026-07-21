"""Admin-facing aggregation of actionable crypto scanner ideas."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Iterable


SCANNER_LABELS = {
    "momentum": "Momentum",
    "drawdown": "Drawdown",
}

CRYPTO_PICK_HORIZONS = {
    "momentum": 5,
    "drawdown": 10,
}

CONFIDENCE_RANK = {
    "Низкая": 1,
    "Средняя": 2,
    "Высокая": 3,
}


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


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
        if (
            horizon is None
            or str(period.get("direction") or "") != "long"
            or observations < horizon
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
        if len(dated_prices) < horizon:
            continue

        start_date, start_price = dated_prices[0]
        end_date, end_price = dated_prices[horizon - 1]
        return_pct = (end_price / start_price - 1) * 100
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
    ]


def build_crypto_signal_export(
    periods: Iterable[dict[str, Any]],
    prices_by_ticker: dict[str, Iterable[tuple[Any, Any]]],
    data_date: Any,
    stake_per_signal: float = 100.0,
) -> dict[str, Any]:
    """Build an auditable journal and equal-stake result for every LONG signal."""
    target_date = _date_key(data_date)
    latest_date = target_date[1] if target_date[0] == 0 else "9999-12-31"
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

    rows: list[dict[str, Any]] = []
    for period in periods:
        scanner = str(period.get("scanner") or "")
        horizon = CRYPTO_PICK_HORIZONS.get(scanner)
        if horizon is None or str(period.get("direction") or "") != "long":
            continue

        ticker = str(period.get("ticker_a") or "").strip()
        start_key = _date_key(period.get("first_seen_date"))
        if not ticker or start_key[0] != 0:
            continue

        dated_prices = [
            (price_date, price)
            for price_date, price in price_cache.get(ticker, [])
            if price_date >= start_key[1]
        ]
        if not dated_prices:
            continue

        observations = _safe_int(period.get("observation_count"), 0)
        raw_status = str(period.get("status") or "active")
        if observations >= horizon and len(dated_prices) >= horizon:
            result_date, result_price = dated_prices[horizon - 1]
            status = "completed"
            status_label = "Завершён по сроку"
            result_type = "реализованный"
            held_days = horizon
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
            held_days = len(available)
        else:
            result_date, result_price = dated_prices[-1]
            status = "active"
            status_label = "Активен"
            result_type = "текущий"
            held_days = len(dated_prices)

        start_date, start_price = dated_prices[0]
        return_pct = (result_price / start_price - 1) * 100
        cash_result = stake_per_signal * return_pct / 100
        rows.append({
            "period_id": period.get("id"),
            "ticker": ticker,
            "symbol": _ticker_symbol(ticker),
            "scanner": scanner,
            "scanner_label": SCANNER_LABELS[scanner],
            "status": status,
            "status_label": status_label,
            "result_type": result_type,
            "horizon_days": horizon,
            "held_days": held_days,
            "start_date": start_date,
            "result_date": result_date,
            "start_price": start_price,
            "result_price": result_price,
            "return_pct": round(return_pct, 4),
            "cash_result": round(cash_result, 4),
            "is_profitable": return_pct > 0,
        })

    rows.sort(
        key=lambda item: (
            item["start_date"],
            item["symbol"],
            item["scanner"],
            item.get("period_id") or 0,
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
        "signals_total": len(rows),
        "signals_active": sum(1 for item in rows if item["status"] == "active"),
        "signals_profitable": sum(1 for item in rows if item["is_profitable"]),
        "stake_per_signal": stake_per_signal,
        "total_invested": round(total_invested, 2),
        "portfolio_value": round(total_invested + total_result, 2),
        "total_result": round(total_result, 2),
        "realized_result": round(realized_result, 2),
        "unrealized_result": round(unrealized_result, 2),
        "portfolio_return_pct": round(
            total_result / total_invested * 100,
            4,
        ) if total_invested else 0.0,
    }
    return {"rows": rows, "summary": summary}


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
