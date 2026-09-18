"""Единая крипто-панель подтверждения стратегий (forward test).

Сводит уже собранные закрытые forward/paper-сделки крипто-стратегий в одну
честную таблицу. По каждой стратегии считается: размер выборки, доля прибыльных
сделок с 95% доверительным интервалом (Wilson), profit factor, суммарный
результат в долларах (каждая учебная сделка — $100), максимальная просадка и
вердикт простым языком.

Только рынок криптовалюты. Журналы не переписываются задним числом: закрытая
сделка фиксируется один раз, поэтому это out-of-sample проверка, а не бэктест.

Пороги и формулировки вердикта совпадают с app/core/reversal_lab.py
(``_forward_credibility``), а доверительный интервал переиспользует проверенную
``app/core/long_term.py::wilson_interval``.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import numpy as np

from app.core.long_term import wilson_interval
from app.core.market_regime import CALCULATION_VERSION as ALPHA_CALCULATION_VERSION
from app.core.short_term_lab import CALCULATION_VERSION as SHORT_TERM_VERSION

STAKE_USD = 100.0
SAMPLE_TARGET = 30
SAMPLE_MIN = 10
PROFIT_FACTOR_TARGET = 1.2


def _credibility(trades: list[dict]) -> dict:
    """Uniform honest statistics over a list of closed forward trades.

    Each trade is a dict with ``cash_result`` ($ at a $100 stake),
    ``net_return_pct`` (return after costs) and ``direction``.
    """
    count = len(trades)
    wins = [trade for trade in trades if float(trade["cash_result"]) > 0]
    losses = [trade for trade in trades if float(trade["cash_result"]) < 0]
    win_rate = len(wins) / count if count else 0.0
    win_rate_low, win_rate_high = wilson_interval(len(wins), count)

    gross_wins = sum(float(trade["cash_result"]) for trade in wins)
    gross_losses = abs(sum(float(trade["cash_result"]) for trade in losses))
    profit_factor = (
        gross_wins / gross_losses if gross_losses else (999.0 if wins else 0.0)
    )
    net_cash = sum(float(trade["cash_result"]) for trade in trades)

    equity = np.cumsum([float(trade["cash_result"]) for trade in trades])
    if len(equity):
        peaks = np.maximum.accumulate(np.insert(equity, 0, 0.0))[1:]
        max_drawdown = abs(float((equity - peaks).min()))
    else:
        max_drawdown = 0.0

    average_net_pct = (
        sum(float(trade["net_return_pct"]) for trade in trades) / count if count else 0.0
    )

    if count < SAMPLE_MIN:
        verdict = "Данных пока мало"
        verdict_tone = "neutral"
        verdict_detail = "Не делайте вывод о стратегии по нескольким сделкам."
    elif count < SAMPLE_TARGET:
        verdict = "Предварительный результат"
        verdict_tone = "neutral"
        verdict_detail = (
            f"Для устойчивого вывода нужно минимум {SAMPLE_TARGET} закрытых сделок."
        )
    elif profit_factor >= PROFIT_FACTOR_TARGET and average_net_pct > 0:
        verdict = "Преимущество подтверждается"
        verdict_tone = "positive"
        verdict_detail = "После расходов результат положительный; наблюдение продолжается."
    else:
        verdict = "Преимущество не подтверждено"
        verdict_tone = "negative"
        verdict_detail = "Forward-тест не показывает устойчивой прибыли после расходов."

    return {
        "has_data": count > 0,
        "sample": count,
        "sample_target": SAMPLE_TARGET,
        "sample_progress": min(count / SAMPLE_TARGET * 100, 100.0) if count else 0.0,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": win_rate * 100,
        "win_rate_low": win_rate_low * 100,
        "win_rate_high": win_rate_high * 100,
        "profit_factor": profit_factor,
        "net_cash": net_cash,
        "average_net_pct": average_net_pct,
        "max_drawdown": max_drawdown,
        "long_trades": sum(trade["direction"] == "long" for trade in trades),
        "short_trades": sum(trade["direction"] == "short" for trade in trades),
        "verdict": verdict,
        "verdict_tone": verdict_tone,
        "verdict_detail": verdict_detail,
    }


def _short_term_trades(conn: sqlite3.Connection) -> list[dict]:
    """Крипто Short-Term Lab: живой журнал, обновляется каждые 15 минут."""
    rows = conn.execute(
        """
        SELECT direction, cash_result, net_return_pct
        FROM short_term_forward_trades
        WHERE calculation_version = ? AND status = 'closed'
        ORDER BY exit_time, id
        """,
        (SHORT_TERM_VERSION,),
    ).fetchall()
    return [
        {
            "direction": row["direction"],
            "cash_result": float(row["cash_result"] or 0.0),
            "net_return_pct": float(row["net_return_pct"] or 0.0),
        }
        for row in rows
    ]


def _alpha_trades(conn: sqlite3.Connection) -> list[dict]:
    """Market Regime Alpha (крипта): ежедневный журнал alpha_trade_journal."""
    rows = conn.execute(
        """
        SELECT direction, cash_result, return_pct
        FROM alpha_trade_journal
        WHERE calculation_version = ?
              AND episode_canonical = 1
              AND closed_on IS NOT NULL
        ORDER BY closed_on, id
        """,
        (ALPHA_CALCULATION_VERSION,),
    ).fetchall()
    return [
        {
            "direction": row["direction"],
            "cash_result": float(row["cash_result"] or 0.0),
            "net_return_pct": float(row["return_pct"] or 0.0),
        }
        for row in rows
    ]


def _momentum_trades(conn: sqlite3.Connection) -> list[dict]:
    """Crypto Picks Momentum: неизменяемый журнал закрытых сделок."""
    rows = conn.execute(
        """
        SELECT direction, cash_result, return_pct
        FROM crypto_strategy_trades
        WHERE market = 'crypto' AND closed_on IS NOT NULL
        ORDER BY closed_on, period_id
        """,
    ).fetchall()
    return [
        {
            "direction": row["direction"],
            "cash_result": float(row["cash_result"] or 0.0),
            "net_return_pct": float(row["return_pct"] or 0.0),
        }
        for row in rows
    ]


_SOURCES = (
    {
        "key": "short_term",
        "label": "Short-Term (крипта)",
        "description": "Короткие внутридневные сделки. Журнал обновляется каждые 15 минут.",
        "loader": _short_term_trades,
    },
    {
        "key": "alpha",
        "label": "Market Regime Alpha (крипта)",
        "description": "Сделки с учётом режима крипто-рынка. Журнал обновляется ежедневно.",
        "loader": _alpha_trades,
    },
    {
        "key": "momentum",
        "label": "Crypto Picks Momentum",
        "description": "Momentum-позиции по крипто-сканеру. Ежедневный журнал.",
        "loader": _momentum_trades,
    },
)


def _build_report(conn: sqlite3.Connection) -> dict:
    conn.row_factory = sqlite3.Row
    strategies: list[dict] = []
    for source in _SOURCES:
        try:
            trades = source["loader"](conn)
            stats = _credibility(trades)
            error = None
        except Exception as exc:  # одна стратегия не должна ронять всю панель
            stats = _credibility([])
            error = repr(exc)
        strategies.append(
            {
                "key": source["key"],
                "label": source["label"],
                "description": source["description"],
                "error": error,
                **stats,
            }
        )

    return {
        "generated_at": datetime.now(UTC).strftime("%d.%m.%Y %H:%M UTC"),
        "stake": STAKE_USD,
        "strategies": strategies,
        "summary": {
            "total": len(strategies),
            "with_data": sum(strategy["sample"] > 0 for strategy in strategies),
            "confirmed": sum(
                strategy["verdict_tone"] == "positive" for strategy in strategies
            ),
        },
    }


def build_forward_confirmation_report(db_path: str) -> dict:
    """Открыть БД и собрать панель подтверждения по крипто-стратегиям."""
    conn = sqlite3.connect(db_path, timeout=60)
    try:
        return _build_report(conn)
    finally:
        conn.close()
