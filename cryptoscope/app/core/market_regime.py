"""Transparent crypto market-regime classifier and strategy risk overlay."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas as pd

from app.db.schema import (
    CREATE_ALPHA_TRADE_JOURNAL,
    CREATE_MARKET_REGIME_SNAPSHOTS,
)

CALCULATION_VERSION = "crypto-regime-v1"
ALPHA_TRADE_STAKE = 100.0
REGIME_ORDER = ("trend", "range", "panic", "recovery")
MIN_HISTORY = 60
HISTORY_SESSIONS = 90
TRADE_PLAN_HORIZONS = {
    "momentum": 5,
    "drawdown": 10,
}
TRADE_PLAN_SCANNER_LABELS = {
    "momentum": "Momentum",
    "drawdown": "Просадка",
}
TRADE_PLAN_CONFIDENCE = {
    "Высокая": ("Высокая", 2),
    "high": ("Высокая", 2),
    "Средняя": ("Средняя", 1),
    "medium": ("Средняя", 1),
}
TRADE_PLAN_MIN_CONFIDENCE_RANK = 2

REGIME_LABELS = {
    "trend": "Тренд",
    "range": "Боковик",
    "panic": "Паника",
    "recovery": "Восстановление",
}
RISK_LABELS = {
    "normal": "Обычный риск",
    "elevated": "Сниженный риск",
    "panic": "Защитный режим",
    "recovery": "Осторожное восстановление",
}


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _clip(value: Any, lower: float = 0.0, upper: float = 1.0) -> float:
    return float(np.clip(_finite(value), lower, upper))


def _scale(value: Any, low: float, high: float) -> float:
    if high <= low:
        return 0.0
    return _clip((_finite(value) - low) / (high - low))


def _softmax(scores: dict[str, float], temperature: float = 0.72) -> dict[str, float]:
    values = np.array(
        [max(0.01, _finite(scores.get(name), 0.01)) for name in REGIME_ORDER],
        dtype=float,
    )
    logits = values / max(temperature, 0.1)
    logits -= logits.max()
    probabilities = np.exp(logits)
    probabilities /= probabilities.sum()
    return {
        name: round(float(probability), 6)
        for name, probability in zip(REGIME_ORDER, probabilities)
    }


def _rolling_percentile(series: pd.Series, window: int = 180) -> pd.Series:
    def percentile(values: np.ndarray) -> float:
        current = values[-1]
        finite = values[np.isfinite(values)]
        if not np.isfinite(current) or len(finite) < 20:
            return np.nan
        return float((finite <= current).mean() * 100)

    return series.rolling(window, min_periods=20).apply(percentile, raw=True)


@dataclass(frozen=True)
class RegimeInputs:
    prices: pd.DataFrame
    returns: pd.DataFrame
    btc: pd.Series
    btc_return_7: pd.Series
    btc_return_20: pd.Series
    btc_sma20: pd.Series
    btc_sma50: pd.Series
    btc_volatility: pd.Series
    volatility_percentile: pd.Series
    drawdown_60: pd.Series
    breadth_20: pd.Series
    breadth_change_5: pd.Series
    dispersion_7: pd.Series


def _build_inputs(wide: pd.DataFrame) -> RegimeInputs:
    prices = wide.copy().sort_index().astype(float)
    prices.index = pd.to_datetime(prices.index)
    prices = prices.loc[~prices.index.duplicated(keep="last")]
    prices = prices.replace([np.inf, -np.inf], np.nan)

    btc_ticker = next(
        (
            ticker
            for ticker in prices.columns
            if str(ticker).upper().split("/", 1)[0] == "BTC"
        ),
        None,
    )
    if btc_ticker is None:
        raise ValueError("BTC price history is required")

    returns = np.log(prices / prices.shift(1))
    btc = prices[btc_ticker]
    btc_return_7 = (btc / btc.shift(7) - 1) * 100
    btc_return_20 = (btc / btc.shift(20) - 1) * 100
    btc_sma20 = btc.rolling(20, min_periods=20).mean()
    btc_sma50 = btc.rolling(50, min_periods=50).mean()
    btc_volatility = (
        returns[btc_ticker].rolling(20, min_periods=15).std(ddof=0)
        * math.sqrt(365)
        * 100
    )
    volatility_percentile = _rolling_percentile(btc_volatility)
    drawdown_60 = (btc / btc.rolling(60, min_periods=30).max() - 1) * 100

    sma20 = prices.rolling(20, min_periods=20).mean()
    available = prices.notna() & sma20.notna()
    breadth_total = available.sum(axis=1).replace(0, np.nan)
    breadth_20 = ((prices > sma20) & available).sum(axis=1) / breadth_total * 100
    breadth_change_5 = breadth_20 - breadth_20.shift(5)
    returns_7 = (prices / prices.shift(7) - 1) * 100
    dispersion_7 = returns_7.std(axis=1, ddof=0)

    return RegimeInputs(
        prices=prices,
        returns=returns,
        btc=btc,
        btc_return_7=btc_return_7,
        btc_return_20=btc_return_20,
        btc_sma20=btc_sma20,
        btc_sma50=btc_sma50,
        btc_volatility=btc_volatility,
        volatility_percentile=volatility_percentile,
        drawdown_60=drawdown_60,
        breadth_20=breadth_20,
        breadth_change_5=breadth_change_5,
        dispersion_7=dispersion_7,
    )


def _average_correlation(
    returns: pd.DataFrame,
    position: int,
    window: int = 20,
) -> float:
    sample = returns.iloc[max(0, position - window + 1): position + 1]
    valid_columns = sample.count()[sample.count() >= max(10, window // 2)].index
    if len(valid_columns) < 3:
        return 0.0
    correlation = sample[valid_columns].corr().to_numpy(dtype=float)
    upper = correlation[np.triu_indices_from(correlation, k=1)]
    finite = upper[np.isfinite(upper)]
    return float(np.median(finite)) if len(finite) else 0.0


def _strategy_mix(
    probabilities: dict[str, float],
    trend_direction: str,
    risk_multiplier: float,
) -> list[dict[str, Any]]:
    trend = probabilities["trend"]
    range_probability = probabilities["range"]
    panic = probabilities["panic"]
    recovery = probabilities["recovery"]

    momentum_direction_factor = 1.0 if trend_direction == "up" else 0.30
    raw = {
        "momentum": (
            trend * momentum_direction_factor + recovery * 0.35
        ) * (1 - panic * 0.8),
        "pairs": (
            range_probability * 0.90 + trend * 0.10
        ) * (1 - panic * 0.75),
        "drawdown": (
            recovery * 0.85 + range_probability * 0.10
        ) * (1 - panic * 0.65),
        "reserve": panic * 1.30 + (1 - risk_multiplier) * 0.75,
    }
    denominator = sum(max(0.0, value) for value in raw.values()) or 1.0
    weights = {
        name: max(0.0, value) / denominator
        for name, value in raw.items()
    }

    definitions = (
        ("momentum", "Momentum", "Торговля по устойчивому импульсу"),
        ("pairs", "Парные сигналы", "Возврат отклонения к среднему"),
        ("drawdown", "Отскок после просадки", "Вход только после подтверждения"),
        ("reserve", "Резерв", "Капитал без новых входов"),
    )
    result = []
    for key, label, description in definitions:
        weight = weights[key]
        if key == "reserve":
            status = "reserve"
        elif weight >= 0.34 and risk_multiplier >= 0.5:
            status = "active"
        elif weight >= 0.16 and risk_multiplier >= 0.25:
            status = "limited"
        else:
            status = "off"
        result.append({
            "key": key,
            "label": label,
            "description": description,
            "weight": round(weight, 6),
            "weight_pct": round(weight * 100, 1),
            "status": status,
            "status_label": {
                "active": "Основная",
                "limited": "Ограниченно",
                "off": "Не использовать",
                "reserve": "Держать свободным",
            }[status],
        })
    return result


def _format_trade_plan_price(value: Any) -> str:
    price = _finite(value, float("nan"))
    if not math.isfinite(price) or price <= 0:
        return "—"
    if price >= 1000:
        return f"${price:,.2f}".replace(",", " ")
    if price >= 1:
        return f"${price:.4f}".rstrip("0").rstrip(".")
    if price >= 0.01:
        return f"${price:.6f}".rstrip("0").rstrip(".")
    return f"${price:.10f}".rstrip("0").rstrip(".")


def _trade_plan_permission(
    latest: dict[str, Any],
    scanner: str,
    direction: str,
    confidence_rank: int,
) -> tuple[bool, str]:
    regime = str(latest.get("dominant_regime") or "")
    trend_direction = str(latest.get("trend_direction") or "mixed")
    strategy = next(
        (
            item
            for item in latest.get("strategies", [])
            if item.get("key") == scanner
        ),
        {},
    )

    if regime == "panic":
        if scanner == "momentum" and direction == "short" and confidence_rank >= 2:
            return True, "Защитный режим допускает только подтверждённый SHORT"
        return False, "В защитном режиме новые направленные входы ограничены"

    if regime == "trend":
        if scanner == "momentum":
            if trend_direction == "up" and direction == "long":
                return True, "LONG совпадает с направлением BTC"
            if trend_direction == "down" and direction == "short":
                return True, "SHORT совпадает с направлением BTC"
            if trend_direction == "mixed" and confidence_rank >= 2:
                return True, "Смешанный тренд требует высокой уверенности"
            return False, "Сигнал направлен против текущего тренда BTC"
        if strategy.get("status") == "off":
            return False, "Текущий режим отключил этот тип сигнала"
        if scanner == "drawdown" and direction == "long" and confidence_rank >= 2:
            return True, "Отскок подтверждён, но уступает трендовым входам"
        return False, "Просадка без сильного подтверждения не допускается"

    if strategy.get("status") == "off":
        return False, "Текущий режим отключил этот тип сигнала"

    if regime == "recovery":
        if direction != "long":
            return False, "В восстановлении приоритет у подтверждённых LONG"
        if scanner == "drawdown":
            return True, "Отскок соответствует фазе восстановления"
        if scanner == "momentum" and confidence_rank >= 1:
            return True, "Импульс подтверждает восстановление"

    if regime == "range":
        if scanner == "drawdown" and direction == "long":
            return True, "Отскок соответствует возврату к среднему"
        if scanner == "momentum" and confidence_rank >= 1:
            return True, "Короткий импульс допустим с ограниченным риском"

    return False, "Сигнал не соответствует текущему режиму"


def build_regime_trade_plan(
    latest: dict[str, Any],
    periods: list[dict[str, Any]],
    limit: int | None = None,
) -> dict[str, Any]:
    """Turn fresh scanner periods into regime-compatible trade candidates."""
    prepared: list[dict[str, Any]] = []
    rejected = 0
    for period in periods:
        scanner = str(period.get("scanner") or "").lower()
        direction = str(period.get("direction") or "").lower()
        confidence_label, confidence_rank = TRADE_PLAN_CONFIDENCE.get(
            str(period.get("confidence") or "").strip(),
            ("Без уровня", 0),
        )
        horizon = TRADE_PLAN_HORIZONS.get(scanner)
        try:
            age_days = max(1, int(period.get("observation_count") or 1))
        except (TypeError, ValueError):
            age_days = 1
        if (
            not horizon
            or direction not in {"long", "short"}
            or confidence_rank < TRADE_PLAN_MIN_CONFIDENCE_RANK
            or age_days >= horizon
            or _finite(period.get("current_price"), 0.0) <= 0
        ):
            rejected += 1
            continue

        allowed, fit_reason = _trade_plan_permission(
            latest,
            scanner,
            direction,
            confidence_rank,
        )
        if not allowed:
            rejected += 1
            continue

        remaining_days = max(0, horizon - age_days)
        try:
            planned_close = date.fromisoformat(
                str(latest.get("data_date") or period.get("last_seen_date"))
            ) + timedelta(days=remaining_days)
            planned_close_date = planned_close.isoformat()
            planned_close_date_label = planned_close.strftime("%d.%m.%Y")
        except (TypeError, ValueError):
            planned_close_date = ""
            planned_close_date_label = "—"
        strategy = next(
            (
                item
                for item in latest.get("strategies", [])
                if item.get("key") == scanner
            ),
            {},
        )
        prepared.append({
            "ticker": str(period.get("ticker_a") or "").upper(),
            "symbol": str(period.get("ticker_a") or "").split("/", 1)[0].upper(),
            "direction": direction,
            "direction_label": "LONG" if direction == "long" else "SHORT",
            "action_label": "Купить" if direction == "long" else "Шорт",
            "scanner": scanner,
            "scanner_label": TRADE_PLAN_SCANNER_LABELS[scanner],
            "scanner_labels": [TRADE_PLAN_SCANNER_LABELS[scanner]],
            "confidence": confidence_label,
            "confidence_rank": confidence_rank,
            "age_days": age_days,
            "remaining_days": remaining_days,
            "planned_close_date": planned_close_date,
            "planned_close_date_label": planned_close_date_label,
            "review_label": (
                "пересмотреть сегодня"
                if remaining_days == 0
                else f"пересмотреть через ≈{remaining_days} дн."
            ),
            "first_seen_date": str(period.get("first_seen_date") or ""),
            "current_price": _finite(period.get("current_price"), 0.0),
            "current_price_label": _format_trade_plan_price(
                period.get("current_price")
            ),
            "fit_reason": fit_reason,
            "strategy_status": str(strategy.get("status") or "limited"),
            "strategy_weight": _finite(strategy.get("weight"), 0.0),
        })

    by_ticker: dict[str, list[dict[str, Any]]] = {}
    for candidate in prepared:
        by_ticker.setdefault(candidate["ticker"], []).append(candidate)

    candidates = []
    conflicts = 0
    for rows in by_ticker.values():
        directions = {row["direction"] for row in rows}
        if len(directions) > 1:
            conflicts += 1
            continue
        best = max(
            rows,
            key=lambda row: (
                row["confidence_rank"],
                row["strategy_status"] == "active",
                row["strategy_weight"],
                -row["age_days"],
            ),
        ).copy()
        best["scanner_labels"] = list(dict.fromkeys(
            row["scanner_label"] for row in rows
        ))
        if len(best["scanner_labels"]) > 1:
            best["fit_reason"] = (
                "Сигнал подтверждён Momentum и сканером просадки"
            )
        best["scanner_label"] = " + ".join(best["scanner_labels"])
        candidates.append(best)

    candidates.sort(
        key=lambda row: (
            row["confidence_rank"],
            row["strategy_status"] == "active",
            row["strategy_weight"],
            -row["age_days"],
        ),
        reverse=True,
    )
    if limit is not None:
        candidates = candidates[:max(1, int(limit))]

    if candidates:
        empty_reason = ""
    elif str(latest.get("risk_state") or "") == "panic":
        empty_reason = (
            "Защитный режим: свежих SHORT-сигналов высокой уверенности нет. "
            "Новые позиции сейчас не открывать."
        )
    elif periods:
        empty_reason = (
            "Свежие сигналы есть, но они не прошли фильтр режима, "
            "направления или уверенности."
        )
    else:
        empty_reason = (
            "На дату расчёта нет свежих Momentum или Drawdown-сигналов. "
            "Новых сделок сейчас нет."
        )

    risk_pct = round(_finite(latest.get("risk_multiplier"), 0.0) * 100)
    return {
        "candidates": candidates,
        "count": len(candidates),
        "source_count": len(periods),
        "rejected_count": rejected,
        "conflict_count": conflicts,
        "risk_pct": risk_pct,
        "position_size_label": (
            "обычный размер"
            if risk_pct >= 100
            else f"не более {risk_pct}% обычного размера"
        ),
        "empty_reason": empty_reason,
    }


async def _fetch_fresh_trade_plan_periods(
    conn,
    data_date: str,
) -> list[dict[str, Any]]:
    cursor = await conn.execute(
        """
        SELECT
            s.scanner,
            s.ticker_a,
            s.direction,
            s.confidence,
            s.first_seen_date,
            s.last_seen_date,
            s.observation_count,
            p.close AS current_price
        FROM scanner_signal_periods AS s
        LEFT JOIN prices AS p
          ON p.market = 'crypto'
         AND p.ticker = s.ticker_a
         AND p.date = s.last_seen_date
        WHERE s.market = 'crypto'
          AND s.scanner IN ('momentum', 'drawdown')
          AND s.status = 'active'
          AND s.last_seen_date = ?
        ORDER BY s.ticker_a, s.scanner
        """,
        (data_date,),
    )
    return [dict(row) for row in await cursor.fetchall()]


def _alpha_return_pct(
    entry_price: Any,
    current_price: Any,
    direction: str,
) -> float:
    entry = _finite(entry_price, 0.0)
    current = _finite(current_price, 0.0)
    if entry <= 0 or current <= 0:
        return 0.0
    raw_return = (current / entry - 1.0) * 100.0
    return round(raw_return if direction == "long" else -raw_return, 6)


def _alpha_scanner_horizon(scanner: Any) -> int | None:
    normalized = str(scanner or "").strip().lower()
    horizons = []
    if "momentum" in normalized:
        horizons.append(TRADE_PLAN_HORIZONS["momentum"])
    if "drawdown" in normalized or "просад" in normalized:
        horizons.append(TRADE_PLAN_HORIZONS["drawdown"])
    return min(horizons) if horizons else None


def _alpha_planned_close_date(row: dict[str, Any]) -> date | None:
    try:
        opened_on = date.fromisoformat(str(row.get("opened_on") or "")[:10])
    except ValueError:
        return None
    horizon = _alpha_scanner_horizon(row.get("scanner"))
    if horizon is None:
        return None
    age_at_entry = max(1, int(row.get("signal_age_at_entry") or 1))
    remaining_days = max(0, horizon - age_at_entry)
    return opened_on + timedelta(days=remaining_days)


async def expire_alpha_trade_journal(
    conn,
    *,
    as_of_date: str,
    live_prices: dict[str, Any],
) -> dict[str, Any]:
    """Close calendar-expired Alpha positions at an observed live price."""
    await ensure_market_regime_schema(conn)
    evaluation_date = date.fromisoformat(str(as_of_date)[:10])
    cursor = await conn.execute(
        """
        SELECT *
        FROM alpha_trade_journal
        WHERE calculation_version = ?
          AND status = 'active'
        ORDER BY id
        """,
        (CALCULATION_VERSION,),
    )
    active_rows = [dict(row) for row in await cursor.fetchall()]
    normalized_prices = {
        str(ticker).upper(): _finite(price, 0.0)
        for ticker, price in (live_prices or {}).items()
    }

    closed = 0
    skipped: list[str] = []
    for row in active_rows:
        planned_close = _alpha_planned_close_date(row)
        if planned_close is None or planned_close > evaluation_date:
            continue
        ticker = str(row.get("ticker") or "").upper()
        exit_price = normalized_prices.get(ticker, 0.0)
        if exit_price <= 0:
            skipped.append(ticker)
            continue
        return_pct = _alpha_return_pct(
            row.get("entry_price"),
            exit_price,
            str(row.get("direction") or ""),
        )
        cash_result = round(
            _finite(row.get("stake"), ALPHA_TRADE_STAKE)
            * return_pct
            / 100.0,
            6,
        )
        await conn.execute(
            """
            UPDATE alpha_trade_journal
            SET status = 'closed',
                last_seen_on = ?,
                last_price = ?,
                closed_on = ?,
                exit_price = ?,
                exit_reason = 'horizon_reached',
                return_pct = ?,
                cash_result = ?,
                updated_at = datetime('now')
            WHERE id = ?
              AND status = 'active'
            """,
            (
                evaluation_date.isoformat(),
                exit_price,
                evaluation_date.isoformat(),
                exit_price,
                return_pct,
                cash_result,
                row["id"],
            ),
        )
        closed += 1

    await conn.commit()
    return {
        "closed": closed,
        "skipped": skipped,
        "as_of_date": evaluation_date.isoformat(),
    }


async def sync_alpha_trade_journal(
    conn,
    latest: dict[str, Any],
    trade_plan: dict[str, Any],
) -> dict[str, Any]:
    """Advance the immutable Alpha recommendation journal by one data date."""
    await ensure_market_regime_schema(conn)
    data_date = str(latest.get("data_date") or "")
    if not data_date:
        raise RuntimeError("Alpha journal requires a regime data date")

    candidates = {
        str(item.get("ticker") or "").upper(): item
        for item in trade_plan.get("candidates", [])
        if str(item.get("ticker") or "").strip()
    }
    cursor = await conn.execute(
        """
        SELECT *
        FROM alpha_trade_journal
        WHERE calculation_version = ?
          AND status = 'active'
        ORDER BY id
        """,
        (CALCULATION_VERSION,),
    )
    active_rows = [dict(row) for row in await cursor.fetchall()]
    active_by_ticker = {row["ticker"]: row for row in active_rows}

    price_tickers = sorted(set(active_by_ticker) | set(candidates))
    prices: dict[str, float] = {}
    if price_tickers:
        placeholders = ",".join("?" for _ in price_tickers)
        cursor = await conn.execute(
            f"""
            SELECT ticker, close
            FROM prices
            WHERE market = 'crypto'
              AND date = ?
              AND ticker IN ({placeholders})
            """,
            (data_date, *price_tickers),
        )
        prices = {
            str(row["ticker"]).upper(): _finite(row["close"], 0.0)
            for row in await cursor.fetchall()
        }

    opened = 0
    closed = 0
    updated = 0
    deferred_tickers: set[str] = set()
    for ticker, active in active_by_ticker.items():
        candidate = candidates.get(ticker)
        same_direction = (
            candidate
            and candidate.get("direction") == active["direction"]
        )
        current_price = _finite(
            candidate.get("current_price") if candidate else prices.get(ticker),
            0.0,
        )
        if same_direction:
            if current_price <= 0:
                raise RuntimeError(
                    f"Missing current Alpha price for active {ticker}"
                )
            await conn.execute(
                """
                UPDATE alpha_trade_journal
                SET scanner = ?,
                    confidence = ?,
                    regime = ?,
                    last_seen_on = ?,
                    last_price = ?,
                    updated_at = datetime('now')
                WHERE id = ?
                """,
                (
                    str(candidate.get("scanner_label") or ""),
                    str(candidate.get("confidence") or ""),
                    str(latest.get("dominant_regime") or ""),
                    data_date,
                    current_price,
                    active["id"],
                ),
            )
            updated += 1
            continue

        # A repeated run for the same completed market snapshot must not close
        # and reopen a forward position at the exact same price. Wait until a
        # newer daily candle can provide a genuine exit observation.
        if data_date <= str(active.get("last_seen_on") or ""):
            deferred_tickers.add(ticker)
            continue

        if current_price <= 0:
            raise RuntimeError(
                f"Cannot close Alpha trade without a price for {ticker}"
            )
        return_pct = _alpha_return_pct(
            active["entry_price"],
            current_price,
            active["direction"],
        )
        cash_result = round(
            _finite(active["stake"], ALPHA_TRADE_STAKE)
            * return_pct
            / 100.0,
            6,
        )
        exit_reason = (
            "direction_changed"
            if candidate
            else "signal_or_regime_filter"
        )
        await conn.execute(
            """
            UPDATE alpha_trade_journal
            SET status = 'closed',
                last_seen_on = ?,
                last_price = ?,
                closed_on = ?,
                exit_price = ?,
                exit_reason = ?,
                return_pct = ?,
                cash_result = ?,
                updated_at = datetime('now')
            WHERE id = ?
            """,
            (
                data_date,
                current_price,
                data_date,
                current_price,
                exit_reason,
                return_pct,
                cash_result,
                active["id"],
            ),
        )
        closed += 1

    for ticker, candidate in candidates.items():
        if ticker in deferred_tickers:
            continue
        active = active_by_ticker.get(ticker)
        if active and candidate.get("direction") == active["direction"]:
            continue
        entry_price = _finite(candidate.get("current_price"), 0.0)
        if entry_price <= 0:
            raise RuntimeError(f"Cannot open Alpha trade without a price for {ticker}")
        await conn.execute(
            """
            INSERT INTO alpha_trade_journal (
                calculation_version,
                ticker,
                direction,
                scanner,
                confidence,
                regime,
                opened_on,
                entry_price,
                signal_first_seen_date,
                signal_age_at_entry,
                last_seen_on,
                last_price,
                stake,
                status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
            """,
            (
                CALCULATION_VERSION,
                ticker,
                str(candidate.get("direction") or ""),
                str(candidate.get("scanner_label") or ""),
                str(candidate.get("confidence") or ""),
                str(latest.get("dominant_regime") or ""),
                data_date,
                entry_price,
                str(candidate.get("first_seen_date") or ""),
                max(1, int(candidate.get("age_days") or 1)),
                data_date,
                entry_price,
                ALPHA_TRADE_STAKE,
            ),
        )
        opened += 1

    await conn.commit()
    return {
        "opened": opened,
        "closed": closed,
        "updated": updated,
        "active": len(set(candidates) | deferred_tickers),
        "data_date": data_date,
    }


def _alpha_stats_row(
    key: str,
    label: str,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    active = [row for row in rows if row["status"] == "active"]
    closed = [row for row in rows if row["status"] == "closed"]
    winners = [
        row for row in closed
        if _finite(row.get("return_pct"), 0.0) > 0
    ]
    realized = sum(_finite(row.get("cash_result"), 0.0) for row in closed)
    current = sum(
        _finite(row.get("stake"), ALPHA_TRADE_STAKE)
        * _alpha_return_pct(
            row["entry_price"],
            row["last_price"],
            row["direction"],
        )
        / 100.0
        for row in active
    )
    return {
        "key": key,
        "label": label,
        "active": len(active),
        "closed": len(closed),
        "winners": len(winners),
        "win_rate": round(len(winners) / len(closed) * 100, 1) if closed else 0.0,
        "realized_cash": round(realized, 2),
        "active_cash": round(current, 2),
        "total_cash": round(realized + current, 2),
    }


def _format_alpha_date(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "—"
    try:
        return date.fromisoformat(raw[:10]).strftime("%d.%m.%Y")
    except ValueError:
        return raw


def _alpha_exit_reason_label(value: Any) -> str:
    return {
        "direction_changed": "Направление сигнала изменилось",
        "signal_or_regime_filter": "Сигнал исчез или не прошёл фильтр режима",
        "manual": "Закрыто вручную",
        "horizon_reached": "Расчётный срок сигнала завершён",
    }.get(str(value or ""), "Сигнал завершён по правилам Alpha")


def _alpha_trade_row(row: dict[str, Any]) -> dict[str, Any]:
    is_active = row.get("status") == "active"
    current_price = (
        _finite(row.get("last_price"), 0.0)
        if is_active
        else _finite(row.get("exit_price"), 0.0)
    )
    result_pct = (
        _alpha_return_pct(
            row.get("entry_price"),
            current_price,
            str(row.get("direction") or ""),
        )
        if is_active
        else _finite(row.get("return_pct"), 0.0)
    )
    stake = _finite(row.get("stake"), ALPHA_TRADE_STAKE)
    cash_result = (
        round(stake * result_pct / 100.0, 2)
        if is_active
        else round(_finite(row.get("cash_result"), 0.0), 2)
    )
    direction = str(row.get("direction") or "")
    ticker = str(row.get("ticker") or "")
    return {
        "id": row.get("id"),
        "ticker": ticker,
        "symbol": ticker.split("/")[0],
        "direction": direction,
        "direction_label": "LONG" if direction == "long" else "SHORT",
        "action_label": "Купить" if direction == "long" else "Шорт",
        "scanner": str(row.get("scanner") or "—"),
        "confidence": str(row.get("confidence") or "—"),
        "regime": str(row.get("regime") or "—"),
        "opened_on": row.get("opened_on"),
        "opened_on_label": _format_alpha_date(row.get("opened_on")),
        "entry_price": _finite(row.get("entry_price"), 0.0),
        "entry_price_label": _format_trade_plan_price(row.get("entry_price")),
        "last_seen_on": row.get("last_seen_on"),
        "last_seen_on_label": _format_alpha_date(row.get("last_seen_on")),
        "current_price": current_price,
        "current_price_label": _format_trade_plan_price(current_price),
        "current_price_source_label": str(
            row.get("_current_price_source_label")
            or f"расчёт {_format_alpha_date(row.get('last_seen_on'))}"
        ),
        "closed_on": row.get("closed_on"),
        "closed_on_label": _format_alpha_date(row.get("closed_on")),
        "result_pct": round(result_pct, 2),
        "cash_result": cash_result,
        "exit_reason": str(row.get("exit_reason") or ""),
        "exit_reason_label": _alpha_exit_reason_label(row.get("exit_reason")),
        "status": str(row.get("status") or ""),
        "stake": round(stake, 2),
    }


async def fetch_alpha_statistics(
    conn,
    as_of_date: str | None = None,
    live_prices: dict[str, Any] | None = None,
    live_price_source_label: str | None = None,
) -> dict[str, Any]:
    await ensure_market_regime_schema(conn)
    cursor = await conn.execute(
        """
        SELECT *
        FROM alpha_trade_journal
        WHERE calculation_version = ?
          AND confidence IN ('Высокая', 'high')
          AND NOT (
              status = 'closed'
              AND closed_on = opened_on
              AND exit_reason = 'signal_or_regime_filter'
              AND ABS(COALESCE(exit_price, 0) - entry_price) < 0.000000000001
          )
        ORDER BY opened_on, id
        """,
        (CALCULATION_VERSION,),
    )
    rows = [dict(row) for row in await cursor.fetchall()]
    display_rows = [row.copy() for row in rows]
    normalized_live_prices = {
        str(ticker).upper(): _finite(price, 0.0)
        for ticker, price in (live_prices or {}).items()
    }
    for row in display_rows:
        if row["status"] != "active":
            continue
        live_price = normalized_live_prices.get(
            str(row.get("ticker") or "").upper(),
            0.0,
        )
        if live_price <= 0:
            continue
        row["last_price"] = live_price
        row["_current_price_source_label"] = (
            live_price_source_label or "live MEXC"
        )

    summary = _alpha_stats_row("all", "Все идеи", display_rows)
    summary["opened"] = len(rows)
    summary["started_on"] = rows[0]["opened_on"] if rows else None
    summary["stake"] = ALPHA_TRADE_STAKE

    direction_rows = [
        _alpha_stats_row(
            direction,
            "LONG" if direction == "long" else "SHORT",
            [row for row in display_rows if row["direction"] == direction],
        )
        for direction in ("long", "short")
        if any(row["direction"] == direction for row in display_rows)
    ]
    scanners = sorted({
        str(row["scanner"])
        for row in display_rows
        if row["scanner"]
    })
    scanner_rows = [
        _alpha_stats_row(
            scanner.lower().replace(" ", "-"),
            scanner,
            [row for row in display_rows if row["scanner"] == scanner],
        )
        for scanner in scanners
    ]
    effective_date = str(as_of_date or "").strip()
    if not effective_date:
        effective_date = max(
            (str(row.get("last_seen_on") or "") for row in rows),
            default="",
        )
    active_trades = [
        _alpha_trade_row(row)
        for row in display_rows
        if row["status"] == "active"
    ]
    closed_rows = sorted(
        (row for row in rows if row["status"] == "closed"),
        key=lambda row: (
            str(row.get("closed_on") or ""),
            int(row.get("id") or 0),
        ),
        reverse=True,
    )
    history = [_alpha_trade_row(row) for row in closed_rows]
    closed_today = [
        row for row in history
        if str(row.get("closed_on") or "") == effective_date
    ]
    return {
        "is_ready": bool(rows),
        "summary": summary,
        "directions": direction_rows,
        "scanners": scanner_rows,
        "as_of_date": effective_date or None,
        "as_of_date_label": _format_alpha_date(effective_date),
        "active_trades": active_trades,
        "closed_today": closed_today,
        "history": history,
        "has_live_prices": any(
            "_current_price_source_label" in row
            for row in display_rows
        ),
    }


def _classify_day(inputs: RegimeInputs, position: int) -> dict[str, Any] | None:
    if position < MIN_HISTORY - 1:
        return None
    date_value = inputs.prices.index[position]
    btc_price = _finite(inputs.btc.iloc[position], float("nan"))
    sma20 = _finite(inputs.btc_sma20.iloc[position], float("nan"))
    sma50 = _finite(inputs.btc_sma50.iloc[position], float("nan"))
    if not all(math.isfinite(value) and value > 0 for value in (btc_price, sma20, sma50)):
        return None

    return_7 = _finite(inputs.btc_return_7.iloc[position])
    return_20 = _finite(inputs.btc_return_20.iloc[position])
    sma_spread = (sma20 / sma50 - 1) * 100
    volatility = _finite(inputs.btc_volatility.iloc[position])
    volatility_percentile = _finite(
        inputs.volatility_percentile.iloc[position],
        50.0,
    )
    drawdown = _finite(inputs.drawdown_60.iloc[position])
    breadth = _finite(inputs.breadth_20.iloc[position], 50.0)
    breadth_change = _finite(inputs.breadth_change_5.iloc[position])
    dispersion = _finite(inputs.dispersion_7.iloc[position])
    average_correlation = _average_correlation(
        inputs.returns,
        position,
    )

    direction_alignment = _scale(abs(breadth - 50), 8, 38)
    directional_strength = (
        0.45 * _scale(abs(sma_spread), 0.8, 7.0)
        + 0.35 * _scale(abs(return_20), 2.5, 20.0)
        + 0.20 * direction_alignment
    )
    negative_context = max(
        _scale(-return_7, 1.5, 12.0),
        _scale(-return_20, 3.0, 25.0),
        _scale(-drawdown, 5.0, 25.0),
    )
    panic_score = (
        0.22 * _scale(-return_7, 1.0, 12.0)
        + 0.22 * _scale(-return_20, 2.0, 25.0)
        + 0.18 * _scale(-drawdown, 4.0, 25.0)
        + 0.16 * _scale(volatility_percentile, 60.0, 95.0)
        + 0.12 * _scale(average_correlation, 0.45, 0.85)
        + 0.10 * _scale(45.0 - breadth, 5.0, 35.0)
    ) * (0.25 + 0.75 * negative_context)

    recovery_context = _scale(-drawdown, 4.0, 20.0)
    recovery_score = (
        0.25 * recovery_context
        + 0.25 * _scale(return_7, 0.5, 10.0)
        + 0.20 * _scale(breadth_change, 2.0, 25.0)
        + 0.15 * (1.0 if btc_price > sma20 else 0.0)
        + 0.15 * (1 - _scale(volatility_percentile, 65.0, 95.0))
    ) * (0.20 + 0.80 * recovery_context)

    range_score = (
        0.38 * (1 - _scale(abs(sma_spread), 0.8, 5.0))
        + 0.27 * (1 - _scale(abs(return_20), 2.5, 15.0))
        + 0.20 * (1 - _scale(volatility_percentile, 55.0, 92.0))
        + 0.15 * (1 - direction_alignment)
    )
    trend_score = directional_strength * (1 - 0.75 * panic_score)

    scores = {
        "trend": 0.08 + trend_score,
        "range": 0.08 + range_score,
        "panic": 0.08 + panic_score,
        "recovery": 0.08 + recovery_score,
    }
    probabilities = _softmax(scores)
    dominant = max(probabilities, key=probabilities.get)
    sorted_probabilities = sorted(probabilities.values(), reverse=True)
    confidence = sorted_probabilities[0] - sorted_probabilities[1]
    trend_direction = (
        "up"
        if sma_spread >= 0 and return_20 >= 0
        else "down"
        if sma_spread <= 0 and return_20 <= 0
        else "mixed"
    )

    if probabilities["panic"] >= 0.42 or panic_score >= 0.62:
        risk_state, risk_multiplier = "panic", 0.15
    elif dominant == "recovery":
        risk_state, risk_multiplier = "recovery", 0.50
    elif (
        probabilities["panic"] >= 0.24
        or volatility_percentile >= 75
        or average_correlation >= 0.72
    ):
        risk_state, risk_multiplier = "elevated", 0.60
    else:
        risk_state, risk_multiplier = "normal", 1.0

    warnings = []
    if volatility_percentile >= 80:
        warnings.append("Волатильность находится в верхнем историческом диапазоне")
    if average_correlation >= 0.72:
        warnings.append("Корреляция монет выросла: диверсификация работает слабее")
    if breadth <= 30:
        warnings.append("Рост поддерживает менее трети отслеживаемого рынка")
    if drawdown <= -15:
        warnings.append("BTC остаётся в глубокой просадке от 60-дневного максимума")
    if dispersion >= 15:
        warnings.append("Разброс доходностей монет повышен")

    metrics = {
        "btc_price": round(btc_price, 8),
        "btc_return_7": round(return_7, 4),
        "btc_return_20": round(return_20, 4),
        "btc_sma_spread": round(sma_spread, 4),
        "btc_volatility": round(volatility, 4),
        "volatility_percentile": round(volatility_percentile, 2),
        "drawdown_60": round(drawdown, 4),
        "breadth_20": round(breadth, 2),
        "breadth_change_5": round(breadth_change, 2),
        "average_correlation": round(average_correlation, 4),
        "dispersion_7": round(dispersion, 4),
        "universe_size": int(inputs.prices.iloc[position].notna().sum()),
    }
    strategies = _strategy_mix(
        probabilities,
        trend_direction,
        risk_multiplier,
    )
    return {
        "calculation_version": CALCULATION_VERSION,
        "market": "crypto",
        "data_date": date_value.strftime("%Y-%m-%d"),
        "dominant_regime": dominant,
        "trend_direction": trend_direction,
        "risk_state": risk_state,
        "risk_multiplier": risk_multiplier,
        "confidence": round(confidence, 6),
        "probabilities": probabilities,
        "metrics": metrics,
        "strategies": strategies,
        "warnings": warnings,
    }


def build_market_regime_history(
    wide: pd.DataFrame,
    sessions: int = HISTORY_SESSIONS,
) -> list[dict[str, Any]]:
    """Classify historical sessions using only data available on each date."""
    if wide.empty or len(wide) < MIN_HISTORY:
        return []
    inputs = _build_inputs(wide)
    start = max(MIN_HISTORY - 1, len(inputs.prices) - max(1, sessions))
    history = []
    for position in range(start, len(inputs.prices)):
        snapshot = _classify_day(inputs, position)
        if snapshot:
            history.append(snapshot)
    return history


async def ensure_market_regime_schema(conn) -> None:
    await conn.execute(CREATE_MARKET_REGIME_SNAPSHOTS)
    await conn.execute(CREATE_ALPHA_TRADE_JOURNAL)
    await conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_market_regime_latest
        ON market_regime_snapshots(
            calculation_version,
            market,
            data_date DESC
        )
        """
    )
    await conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_alpha_trade_active
        ON alpha_trade_journal(calculation_version, ticker)
        WHERE status = 'active'
        """
    )
    await conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_alpha_trade_history
        ON alpha_trade_journal(
            calculation_version,
            status,
            closed_on,
            opened_on
        )
        """
    )
    await conn.commit()


async def sync_market_regime_snapshots(
    db_path: str | None = None,
    sessions: int = HISTORY_SESSIONS,
) -> dict[str, Any]:
    """Backfill immutable point-in-time crypto regime snapshots."""
    from app.db.database import get_connection

    async with get_connection(db_path) as conn:
        await ensure_market_regime_schema(conn)
        cursor = await conn.execute(
            """
            SELECT ticker, date, close
            FROM prices
            WHERE market = 'crypto'
            ORDER BY date, ticker
            """
        )
        rows = await cursor.fetchall()
        if not rows:
            raise RuntimeError("Crypto prices are empty")
        frame = pd.DataFrame([dict(row) for row in rows])
        wide = frame.pivot(index="date", columns="ticker", values="close")
        history = build_market_regime_history(wide, sessions=sessions)
        if not history:
            raise RuntimeError("Not enough crypto history for regime analysis")

        inserted = 0
        for snapshot in history:
            cursor = await conn.execute(
                """
                INSERT OR IGNORE INTO market_regime_snapshots (
                    calculation_version,
                    market,
                    data_date,
                    dominant_regime,
                    trend_direction,
                    risk_state,
                    risk_multiplier,
                    confidence,
                    probabilities_json,
                    metrics_json,
                    strategies_json,
                    warnings_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot["calculation_version"],
                    snapshot["market"],
                    snapshot["data_date"],
                    snapshot["dominant_regime"],
                    snapshot["trend_direction"],
                    snapshot["risk_state"],
                    snapshot["risk_multiplier"],
                    snapshot["confidence"],
                    json.dumps(snapshot["probabilities"], ensure_ascii=False),
                    json.dumps(snapshot["metrics"], ensure_ascii=False),
                    json.dumps(snapshot["strategies"], ensure_ascii=False),
                    json.dumps(snapshot["warnings"], ensure_ascii=False),
                ),
            )
            inserted += max(0, cursor.rowcount)
        await conn.commit()
        latest = history[-1]
        periods = await _fetch_fresh_trade_plan_periods(
            conn,
            latest["data_date"],
        )
        trade_plan = build_regime_trade_plan(latest, periods)
        journal_result = await sync_alpha_trade_journal(
            conn,
            latest,
            trade_plan,
        )
        return {
            "status": "ok",
            "version": CALCULATION_VERSION,
            "snapshots": len(history),
            "inserted": inserted,
            "latest_data_date": latest["data_date"],
            "dominant_regime": latest["dominant_regime"],
            "journal": journal_result,
        }


def _display_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    probabilities = snapshot["probabilities"]
    metrics = snapshot["metrics"]
    snapshot["regime_label"] = REGIME_LABELS.get(
        snapshot["dominant_regime"],
        snapshot["dominant_regime"],
    )
    snapshot["risk_label"] = RISK_LABELS.get(
        snapshot["risk_state"],
        snapshot["risk_state"],
    )
    snapshot["risk_pct"] = round(snapshot["risk_multiplier"] * 100)
    snapshot["confidence_pct"] = round(snapshot["confidence"] * 100)
    snapshot["probability_rows"] = [
        {
            "key": key,
            "label": REGIME_LABELS[key],
            "value": round(probabilities[key] * 100, 1),
        }
        for key in REGIME_ORDER
    ]
    snapshot["metric_rows"] = [
        {
            "label": "BTC за 20 дней",
            "value": f"{metrics['btc_return_20']:+.2f}%",
            "tone": "positive" if metrics["btc_return_20"] >= 0 else "negative",
            "detail": "направление базового рынка",
        },
        {
            "label": "Ширина рынка",
            "value": f"{metrics['breadth_20']:.0f}%",
            "tone": "positive" if metrics["breadth_20"] >= 50 else "negative",
            "detail": "монет выше своей SMA20",
        },
        {
            "label": "Волатильность BTC",
            "value": f"{metrics['btc_volatility']:.1f}%",
            "tone": "negative" if metrics["volatility_percentile"] >= 75 else "neutral",
            "detail": f"{metrics['volatility_percentile']:.0f}-й перцентиль",
        },
        {
            "label": "Корреляция рынка",
            "value": f"{metrics['average_correlation']:.2f}",
            "tone": "negative" if metrics["average_correlation"] >= 0.72 else "neutral",
            "detail": "медиана за 20 дней",
        },
        {
            "label": "Просадка BTC",
            "value": f"{metrics['drawdown_60']:.2f}%",
            "tone": "negative" if metrics["drawdown_60"] <= -10 else "neutral",
            "detail": "от максимума за 60 дней",
        },
        {
            "label": "Разброс монет",
            "value": f"{metrics['dispersion_7']:.2f}%",
            "tone": "negative" if metrics["dispersion_7"] >= 15 else "neutral",
            "detail": "доходности за 7 дней",
        },
    ]
    return snapshot


async def fetch_market_regime_report(
    conn,
    history_limit: int = 30,
    live_prices: dict[str, Any] | None = None,
    live_price_source_label: str | None = None,
    evaluation_date: str | None = None,
) -> dict[str, Any]:
    await ensure_market_regime_schema(conn)
    cursor = await conn.execute(
        """
        SELECT *
        FROM market_regime_snapshots
        WHERE calculation_version = ?
          AND market = 'crypto'
        ORDER BY data_date DESC
        LIMIT ?
        """,
        (CALCULATION_VERSION, max(1, int(history_limit))),
    )
    rows = await cursor.fetchall()
    snapshots = []
    for row in rows:
        item = dict(row)
        item["probabilities"] = json.loads(item.pop("probabilities_json"))
        item["metrics"] = json.loads(item.pop("metrics_json"))
        item["strategies"] = json.loads(item.pop("strategies_json"))
        item["warnings"] = json.loads(item.pop("warnings_json"))
        snapshots.append(_display_snapshot(item))
    snapshots.reverse()
    latest = snapshots[-1] if snapshots else None
    trade_plan = {
        "candidates": [],
        "count": 0,
        "source_count": 0,
        "rejected_count": 0,
        "conflict_count": 0,
        "risk_pct": 0,
        "position_size_label": "новые позиции не открывать",
        "empty_reason": "Режим рынка ещё не рассчитан.",
    }
    if latest:
        periods = await _fetch_fresh_trade_plan_periods(
            conn,
            latest["data_date"],
        )
        trade_plan = build_regime_trade_plan(latest, periods)
    evaluated_on = date.fromisoformat(
        str(evaluation_date or (latest or {}).get("data_date") or date.today())[:10]
    )
    expired_candidates = []
    fresh_candidates = []
    for candidate in trade_plan.get("candidates", []):
        raw_close_date = str(candidate.get("planned_close_date") or "")
        try:
            planned_close = date.fromisoformat(raw_close_date[:10])
        except ValueError:
            fresh_candidates.append(candidate)
            continue
        if planned_close <= evaluated_on:
            expired_candidates.append(candidate)
        else:
            fresh_candidates.append(candidate)
    if expired_candidates:
        trade_plan["candidates"] = fresh_candidates
        trade_plan["count"] = len(fresh_candidates)
        trade_plan["expired_count"] = len(expired_candidates)
        if not fresh_candidates:
            trade_plan["empty_reason"] = (
                "Просроченные рекомендации скрыты. Нужен свежий расчёт рынка."
            )
    else:
        trade_plan["expired_count"] = 0

    latest_date = (
        date.fromisoformat(str(latest["data_date"])[:10])
        if latest
        else None
    )
    stale_days = (
        max(0, (evaluated_on - latest_date).days)
        if latest_date is not None
        else 0
    )
    statistics = await fetch_alpha_statistics(
        conn,
        latest["data_date"] if latest else None,
        live_prices=live_prices,
        live_price_source_label=live_price_source_label,
    )

    history = [
        {
            "data_date": item["data_date"],
            "dominant_regime": item["dominant_regime"],
            "regime_label": item["regime_label"],
            "risk_multiplier": item["risk_multiplier"],
            "probabilities": item["probabilities"],
        }
        for item in snapshots
    ]
    return {
        "calculation_version": CALCULATION_VERSION,
        "latest": latest,
        "trade_plan": trade_plan,
        "statistics": statistics,
        "history": history,
        "history_json": json.dumps(history, ensure_ascii=False),
        "is_ready": latest is not None,
        "evaluation_date": evaluated_on.isoformat(),
        # Completed UTC daily candles normally trail the Moscow calendar by
        # one day. Only a larger gap means the Alpha snapshot is stale.
        "is_stale": stale_days > 1,
        "stale_days": stale_days,
    }
