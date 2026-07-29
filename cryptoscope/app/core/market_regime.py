"""Transparent crypto market-regime classifier and strategy risk overlay."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from app.db.schema import CREATE_MARKET_REGIME_SNAPSHOTS

CALCULATION_VERSION = "crypto-regime-v1"
REGIME_ORDER = ("trend", "range", "panic", "recovery")
MIN_HISTORY = 60
HISTORY_SESSIONS = 90

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
        return {
            "status": "ok",
            "version": CALCULATION_VERSION,
            "snapshots": len(history),
            "inserted": inserted,
            "latest_data_date": history[-1]["data_date"],
            "dominant_regime": history[-1]["dominant_regime"],
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


async def fetch_market_regime_report(conn, history_limit: int = 30) -> dict[str, Any]:
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
        "history": history,
        "history_json": json.dumps(history, ensure_ascii=False),
        "is_ready": latest is not None,
    }
