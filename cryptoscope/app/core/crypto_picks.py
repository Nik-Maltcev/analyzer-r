"""Admin-facing aggregation of actionable crypto scanner ideas."""

from __future__ import annotations

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
    if price >= 1000:
        return f"${price:,.2f}".replace(",", " ")
    if price >= 1:
        return f"${price:.3f}".rstrip("0").rstrip(".")
    if price >= 0.01:
        return f"${price:.5f}".rstrip("0").rstrip(".")
    return f"${price:.8f}".rstrip("0").rstrip(".")


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
