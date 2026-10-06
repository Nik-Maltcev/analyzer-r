"""Live-сигналы: кросс-секционный моментум (long winners / short losers), крипта.

Реализует валидированный edge из ``research/momentum_lab.py`` (cmd_xs): ранжируем
крипту по доходности за ``lookback`` дней, лонгуем верхний квантиль, шортим нижний,
равные веса, дельта-нейтрально (половина капитала long / половина short).

Сигнал ДНЕВНОЙ (ребаланс ~раз в ``HOLD_DAYS``). Живые цены MEXC накладываются
поверх, чтобы ловить точку входа внутри дня; сам интрадей-тайминг — дискреционный
и НЕ является валидированным edge (см. дисклеймер в шаблоне).

Это НЕ time-series сканер ``momentum_scan`` (абсолютные пороги), а именно
кросс-секционный ранг относительно peers — тот, что пережил survivorship-check.
"""

from __future__ import annotations

import copy
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

DEFAULT_LOOKBACK = 14
ALLOWED_LOOKBACKS = (7, 14, 30)
QUANTILE = 0.2
MAX_LEGS = 8
MIN_LEGS = 2
MIN_UNIVERSE = 6
HOLD_DAYS = 7

_BASKET_CACHE: dict[str, dict[str, Any]] = {}
_CACHE_TTL = 900.0

STRATEGY_NOTE = {
    "backtest_period": "2024-09-29 → 2026-09-29 (~2 года, недельный ребаланс)",
    "universe_note": (
        "Binance/MEXC USDT-перпы из БД, топ по объёму, только крипта "
        "(tokenized-акции не входят в market='crypto')"
    ),
    "metrics": [
        ("Доля прибыльных недель", "56%"),
        ("Медиана недели", "+1.0%"),
        ("Средняя неделя (net)", "+1.6%"),
        ("Доход на капитал, 1x", "+36% / год"),
        ("Итог за 2 года, 1x", "×1.83"),
        ("Макс. просадка, 1x", "40%"),
    ],
    "caveats": [
        "Валидированный edge — ДНЕВНАЯ L/S корзина с удержанием ~7 дней. Быстрый вход «интрадей по алерту» edge НЕ добавляет: дневной сигнал не становится точнее от ловли момента внутри дня.",
        "Плечо убивает: тонкий средний плюс и толстый хвост (momentum-crash ~40% на 1x). 3x/5x обнуляют счёт ещё в бэктесте.",
        "Один режим: 2024–2026 был сильным альт-моментум рынком. На резких разворотах моментум крашится — вперёд может быть хуже.",
        "Комиссии учтены 0.04%/нога (taker); слиппедж шорт-ноги НЕ учтён. Реалистичный капитал от ~$1 000–2 000 (на $100 книжка не диверсифицируется).",
    ],
}


def _price(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    if number >= 100:
        return f"${number:,.2f}"
    if number >= 1:
        return f"${number:,.4f}".rstrip("0").rstrip(".")
    return f"${number:.8f}".rstrip("0").rstrip(".")


def _next_rebalance(as_of: str) -> str:
    try:
        base = datetime.strptime(as_of, "%Y-%m-%d")
    except (TypeError, ValueError):
        return "—"
    return (base + timedelta(days=HOLD_DAYS)).strftime("%Y-%m-%d")


def compute_cross_sectional_momentum(
    prices: pd.DataFrame,
    lookback: int = DEFAULT_LOOKBACK,
    quantile: float = QUANTILE,
    max_legs: int = MAX_LEGS,
) -> dict[str, Any]:
    """Rank crypto by trailing ``lookback``-day return; long top-q, short bottom-q.

    ``prices`` — DataFrame[ticker, date, close] (market='crypto'). Чистая функция,
    без сети и БД — тестируется синтетикой.
    """
    if prices is None or prices.empty:
        return {"status": "no_data"}

    wide = prices.pivot(index="date", columns="ticker", values="close").sort_index()
    if len(wide) < lookback + 1:
        return {"status": "insufficient_history", "rows": int(len(wide))}

    as_of = str(wide.index[-1])[:10]
    last = wide.iloc[-1]
    base = wide.iloc[-1 - lookback]

    returns: dict[str, float] = {}
    for ticker in wide.columns:
        older = base.get(ticker)
        newer = last.get(ticker)
        if pd.notna(older) and pd.notna(newer) and float(older) > 0 and float(newer) > 0:
            returns[str(ticker)] = (float(newer) / float(older) - 1) * 100.0

    if len(returns) < MIN_UNIVERSE:
        return {"status": "insufficient_universe", "universe": len(returns)}

    ranked = sorted(returns.items(), key=lambda kv: kv[1], reverse=True)
    universe = len(ranked)
    legs = max(MIN_LEGS, min(max_legs, round(quantile * universe)))
    if 2 * legs > universe:  # не хватает имён на дельта-нейтральную книгу
        legs = max(MIN_LEGS, universe // 2)

    long_names = ranked[:legs]
    short_names = ranked[universe - legs:]
    per_leg_weight = 0.5 / legs if legs else 0.0

    def _build(names: list[tuple[str, float]], side: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for rank, (ticker, momentum) in enumerate(names, start=1):
            close = float(last.get(ticker))
            out.append({
                "rank": rank,
                "ticker": ticker,
                "side": side,
                "momentum_pct": round(momentum, 2),
                "momentum_display": f"{momentum:+.1f}%",
                "weight": per_leg_weight,
                "weight_display": f"{per_leg_weight * 100:.1f}%",
                "last_close": close,
                "last_close_display": _price(close),
            })
        return out

    long_legs = _build(long_names, "long")
    short_legs = _build(short_names, "short")

    return {
        "status": "ok",
        "as_of": as_of,
        "lookback": lookback,
        "hold_days": HOLD_DAYS,
        "universe": universe,
        "legs_per_side": legs,
        "long_legs": long_legs,
        "short_legs": short_legs,
        "next_rebalance": _next_rebalance(as_of),
        "avg_long_momentum": round(
            sum(x["momentum_pct"] for x in long_legs) / len(long_legs), 2
        ) if long_legs else 0.0,
        "avg_short_momentum": round(
            sum(x["momentum_pct"] for x in short_legs) / len(short_legs), 2
        ) if short_legs else 0.0,
    }


def merge_live_prices(
    report: dict[str, Any],
    live_prices: dict[str, float],
) -> dict[str, Any]:
    """Наложить живые цены MEXC на корзину, не меняя сам ранг."""
    if report.get("status") != "ok":
        return report
    marked = 0
    for side in ("long_legs", "short_legs"):
        for leg in report.get(side, []):
            raw = live_prices.get(leg["ticker"])
            try:
                live = float(raw)
            except (TypeError, ValueError):
                live = None
            base = leg.get("last_close") or 0.0
            if live and live > 0:
                leg["live_price"] = live
                leg["live_price_display"] = _price(live)
                leg["intraday_pct"] = round((live / base - 1) * 100, 2) if base > 0 else 0.0
                leg["intraday_display"] = f"{leg['intraday_pct']:+.2f}%"
                marked += 1
            else:
                leg["live_price"] = base
                leg["live_price_display"] = leg.get("last_close_display", "—")
                leg["intraday_pct"] = 0.0
                leg["intraday_display"] = "—"
    report["live_marked_legs"] = marked
    report["marked_at"] = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    return report


async def _load_basket(lookback: int) -> dict[str, Any]:
    """Корзина из БД с кэшем (ранг дневной — нечего считать каждый поллинг)."""
    from app.db.database import fetch_prices, get_connection

    key = f"lb{lookback}"
    now = time.time()
    cached = _BASKET_CACHE.get(key)
    if cached and now - cached["ts"] < _CACHE_TTL:
        return cached["report"]

    async with get_connection() as conn:
        prices = await fetch_prices(conn, "crypto")
    report = compute_cross_sectional_momentum(prices, lookback)
    _BASKET_CACHE[key] = {"ts": now, "report": report}
    return report


async def build_live_momentum_report(lookback: int = DEFAULT_LOOKBACK) -> dict[str, Any]:
    if lookback not in ALLOWED_LOOKBACKS:
        lookback = DEFAULT_LOOKBACK

    report = copy.deepcopy(await _load_basket(lookback))

    if report.get("status") == "ok":
        from app.data.mexc_market import refresh_crypto_live_prices

        tickers = [
            leg["ticker"]
            for leg in report["long_legs"] + report["short_legs"]
        ]
        try:
            live = await refresh_crypto_live_prices(tickers, ttl_seconds=30)
            prices = live.get("prices") or {}
        except Exception:
            prices = {}
        report = merge_live_prices(report, prices)

    report["lookback"] = lookback
    report["lookback_options"] = list(ALLOWED_LOOKBACKS)
    report["strategy_note"] = STRATEGY_NOTE
    report["generated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return report
