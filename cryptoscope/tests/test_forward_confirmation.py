"""Тесты крипто-панели Forward Confirmation."""

from __future__ import annotations

import sqlite3

import pytest

from app.core.forward_confirmation import (
    ALPHA_CALCULATION_VERSION,
    SHORT_TERM_VERSION,
    _build_report,
    _credibility,
)
from app.db.schema import (
    CREATE_ALPHA_TRADE_JOURNAL,
    CREATE_CRYPTO_STRATEGY_TRADES,
    CREATE_SHORT_TERM_FORWARD_TRADES,
)


def _create_all(conn: sqlite3.Connection) -> None:
    conn.execute(CREATE_SHORT_TERM_FORWARD_TRADES)
    conn.execute(CREATE_ALPHA_TRADE_JOURNAL)
    conn.execute(CREATE_CRYPTO_STRATEGY_TRADES)


def _memory() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def _insert_short_term(conn: sqlite3.Connection, *, wins: int, losses: int) -> None:
    """Закрытые сделки Short-Term: сначала победы (long), затем убытки (short)."""
    signal_time = 0
    for _ in range(wins):
        signal_time += 1
        conn.execute(
            """
            INSERT INTO short_term_forward_trades (
                calculation_version, strategy, ticker, direction, signal_time,
                signal_price, score, confidence, timeframe_minutes, hold_minutes,
                stop_pct, target_pct, status, exit_time, cash_result, net_return_pct
            ) VALUES (?, 'momentum', 'BTC/USD', 'long', ?, 100.0, 2.0, 'high',
                      60, 240, 0.0, 0.0, 'closed', ?, 1.0, 1.0)
            """,
            (SHORT_TERM_VERSION, signal_time, signal_time * 60_000),
        )
    for _ in range(losses):
        signal_time += 1
        conn.execute(
            """
            INSERT INTO short_term_forward_trades (
                calculation_version, strategy, ticker, direction, signal_time,
                signal_price, score, confidence, timeframe_minutes, hold_minutes,
                stop_pct, target_pct, status, exit_time, cash_result, net_return_pct
            ) VALUES (?, 'momentum', 'ETH/USD', 'short', ?, 100.0, 2.0, 'high',
                      60, 240, 0.0, 0.0, 'closed', ?, -0.5, -0.5)
            """,
            (SHORT_TERM_VERSION, signal_time, signal_time * 60_000),
        )


def _insert_alpha(conn: sqlite3.Connection, *, wins: int) -> None:
    for index in range(1, wins + 1):
        conn.execute(
            """
            INSERT INTO alpha_trade_journal (
                calculation_version, ticker, direction, scanner, confidence, regime,
                opened_on, entry_price, signal_age_at_entry, last_seen_on, last_price,
                closed_on, exit_price, exit_reason, return_pct, cash_result,
                status, episode_canonical
            ) VALUES (?, 'BTC/USD', 'long', 'momentum', 'Высокая', 'risk_on',
                      '2026-01-01', 100.0, 0, '2026-01-05', 110.0,
                      ?, 110.0, 'horizon', 2.0, 2.0, 'closed', 1)
            """,
            (ALPHA_CALCULATION_VERSION, f"2026-02-{index:02d}"),
        )


def _insert_momentum(conn: sqlite3.Connection, *, wins: int) -> None:
    for index in range(1, wins + 1):
        conn.execute(
            """
            INSERT INTO crypto_strategy_trades (
                period_id, market, scanner, signal_key, ticker, direction, confidence,
                opened_on, entry_price, entry_recorded_at, entry_source,
                closed_on, exit_price, exit_recorded_at, exit_reason,
                return_pct, cash_result, stake, strategy_version
            ) VALUES (?, 'crypto', 'momentum', ?, 'SOL/USD', 'long', 'Высокая',
                      '2026-01-01', 100.0, '2026-01-01', 'live',
                      ?, 103.0, ?, 'horizon', 3.0, 3.0, 100.0, 'momentum-long-v2-mexc')
            """,
            (index, f"momentum:SOL/USD:{index}", f"2026-03-{index:02d}",
             f"2026-03-{index:02d}"),
        )


# --- Математика достоверности (без БД) -------------------------------------

def test_credibility_empty():
    stats = _credibility([])
    assert stats["sample"] == 0
    assert stats["has_data"] is False
    assert stats["win_rate"] == 0.0
    assert stats["profit_factor"] == 0.0
    assert stats["net_cash"] == 0.0
    assert stats["max_drawdown"] == 0.0
    assert stats["verdict"] == "Данных пока мало"
    assert stats["verdict_tone"] == "neutral"


def test_credibility_preliminary_below_target():
    trades = [
        {"cash_result": 1.0, "net_return_pct": 1.0, "direction": "long"}
    ] * 12
    stats = _credibility(trades)
    assert stats["sample"] == 12
    assert stats["verdict"] == "Предварительный результат"
    assert stats["verdict_tone"] == "neutral"
    assert 0.0 <= stats["win_rate_low"] <= 100.0 <= stats["win_rate_high"]


def test_credibility_positive_verdict():
    trades = [
        {"cash_result": 1.0, "net_return_pct": 1.0, "direction": "long"}
    ] * 24 + [
        {"cash_result": -0.5, "net_return_pct": -0.5, "direction": "short"}
    ] * 6
    stats = _credibility(trades)
    assert stats["sample"] == 30
    assert stats["wins"] == 24 and stats["losses"] == 6
    assert stats["win_rate"] == pytest.approx(80.0)
    assert stats["win_rate_low"] <= 80.0 <= stats["win_rate_high"]
    assert stats["profit_factor"] == pytest.approx(8.0)
    assert stats["net_cash"] == pytest.approx(21.0)
    assert stats["max_drawdown"] == pytest.approx(3.0)
    assert stats["long_trades"] == 24 and stats["short_trades"] == 6
    assert stats["verdict"] == "Преимущество подтверждается"
    assert stats["verdict_tone"] == "positive"


def test_credibility_negative_verdict():
    trades = [
        {"cash_result": 0.1, "net_return_pct": 0.1, "direction": "long"}
    ] * 15 + [
        {"cash_result": -1.0, "net_return_pct": -1.0, "direction": "short"}
    ] * 15
    stats = _credibility(trades)
    assert stats["sample"] == 30
    assert stats["profit_factor"] < 1.2
    assert stats["verdict"] == "Преимущество не подтверждено"
    assert stats["verdict_tone"] == "negative"


# --- Интеграция с реальной схемой БД ---------------------------------------

def test_build_report_empty_db():
    conn = _memory()
    _create_all(conn)
    report = _build_report(conn)
    assert report["summary"] == {"total": 3, "with_data": 0, "confirmed": 0}
    for strategy in report["strategies"]:
        assert strategy["sample"] == 0
        assert strategy["error"] is None
        assert strategy["verdict_tone"] == "neutral"


def test_build_report_short_term_positive():
    conn = _memory()
    _create_all(conn)
    _insert_short_term(conn, wins=24, losses=6)
    report = _build_report(conn)
    by_key = {strategy["key"]: strategy for strategy in report["strategies"]}
    short_term = by_key["short_term"]
    assert short_term["sample"] == 30
    assert short_term["win_rate"] == pytest.approx(80.0)
    assert short_term["profit_factor"] == pytest.approx(8.0)
    assert short_term["net_cash"] == pytest.approx(21.0)
    assert short_term["max_drawdown"] == pytest.approx(3.0)
    assert short_term["verdict_tone"] == "positive"
    assert report["summary"]["with_data"] == 1
    assert report["summary"]["confirmed"] == 1


def test_build_report_alpha_and_momentum_loaders():
    conn = _memory()
    _create_all(conn)
    _insert_alpha(conn, wins=5)
    _insert_momentum(conn, wins=5)
    report = _build_report(conn)
    by_key = {strategy["key"]: strategy for strategy in report["strategies"]}

    alpha = by_key["alpha"]
    assert alpha["error"] is None
    assert alpha["sample"] == 5
    assert alpha["wins"] == 5
    assert alpha["net_cash"] == pytest.approx(10.0)
    assert alpha["verdict"] == "Данных пока мало"

    momentum = by_key["momentum"]
    assert momentum["error"] is None
    assert momentum["sample"] == 5
    assert momentum["net_cash"] == pytest.approx(15.0)

    # Крипто-фильтр: сделки другого рынка не попадают в momentum.
    conn.execute(
        """
        INSERT INTO crypto_strategy_trades (
            period_id, market, scanner, signal_key, ticker, direction,
            opened_on, entry_price, entry_recorded_at, entry_source,
            closed_on, exit_price, exit_recorded_at, exit_reason,
            return_pct, cash_result, stake, strategy_version
        ) VALUES (999, 'stocks', 'momentum', 'momentum:SPY:999', 'SPY', 'long',
                  '2026-01-01', 100.0, '2026-01-01', 'live',
                  '2026-03-09', 103.0, '2026-03-09', 'horizon',
                  3.0, 3.0, 100.0, 'momentum-long-v1')
        """
    )
    report2 = _build_report(conn)
    by_key2 = {strategy["key"]: strategy for strategy in report2["strategies"]}
    assert by_key2["momentum"]["sample"] == 5


def test_build_report_tolerates_missing_tables():
    conn = _memory()
    conn.execute(CREATE_SHORT_TERM_FORWARD_TRADES)  # только эта таблица
    _insert_short_term(conn, wins=24, losses=6)
    report = _build_report(conn)
    by_key = {strategy["key"]: strategy for strategy in report["strategies"]}
    assert by_key["short_term"]["sample"] == 30
    assert by_key["short_term"]["error"] is None
    assert by_key["alpha"]["error"] is not None
    assert by_key["alpha"]["sample"] == 0
    assert by_key["momentum"]["error"] is not None
    assert by_key["momentum"]["sample"] == 0
