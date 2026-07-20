"""Admin-facing aggregation of actionable crypto scanner ideas."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Iterable


SCANNER_LABELS = {
    "momentum": "Momentum",
    "drawdown": "Drawdown",
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
    for iso_date, price in normalized:
        change_pct = (price / start_price - 1) * 100
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
            "is_start": iso_date == normalized[0][0],
            "is_latest": iso_date == latest_date,
        })
    return list(reversed(progress))


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
