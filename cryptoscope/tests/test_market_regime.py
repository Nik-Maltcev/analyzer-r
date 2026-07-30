import json

import aiosqlite
import numpy as np
import pandas as pd
import pytest

from app.core.market_regime import (
    CALCULATION_VERSION,
    REGIME_ORDER,
    build_market_regime_history,
    build_regime_trade_plan,
    expire_alpha_trade_journal,
    fetch_alpha_statistics,
    fetch_market_regime_report,
    sync_alpha_trade_journal,
)
from app.db.schema import (
    CREATE_ALPHA_TRADE_JOURNAL,
    CREATE_MARKET_REGIME_SNAPSHOTS,
    CREATE_SCANNER_SIGNAL_PERIODS,
)


def _wide_prices(
    btc_returns: np.ndarray,
    *,
    tickers: int = 8,
    dispersion: float = 0.001,
) -> pd.DataFrame:
    dates = pd.date_range("2025-01-01", periods=len(btc_returns), freq="D")
    btc = 100.0 * np.exp(np.cumsum(btc_returns))
    data = {"BTC/USD": btc}
    for index in range(tickers - 1):
        phase = np.sin(np.arange(len(dates)) / (5.0 + index))
        asset_returns = btc_returns * (0.85 + index * 0.03)
        asset_returns = asset_returns + phase * dispersion
        data[f"ASSET{index}/USD"] = (10 + index) * np.exp(
            np.cumsum(asset_returns)
        )
    return pd.DataFrame(data, index=dates)


def test_probabilities_are_normalized_and_finite():
    returns = np.full(120, 0.004)
    history = build_market_regime_history(_wide_prices(returns))

    assert history
    probabilities = history[-1]["probabilities"]
    assert set(probabilities) == set(REGIME_ORDER)
    assert abs(sum(probabilities.values()) - 1.0) < 1e-5
    assert all(np.isfinite(value) for value in probabilities.values())
    assert history[-1]["dominant_regime"] == "trend"
    assert history[-1]["trend_direction"] == "up"


def test_sharp_correlated_selloff_enables_protective_risk():
    returns = np.concatenate([
        np.full(90, 0.001),
        np.full(8, -0.045),
    ])
    history = build_market_regime_history(
        _wide_prices(returns, dispersion=0.0001)
    )

    latest = history[-1]
    assert latest["dominant_regime"] == "panic"
    assert latest["risk_state"] == "panic"
    assert latest["risk_multiplier"] <= 0.15


def test_future_prices_do_not_rewrite_past_snapshot():
    base = _wide_prices(np.full(110, 0.002))
    baseline = build_market_regime_history(base, sessions=90)
    comparison_date = baseline[-6]["data_date"]
    expected = next(
        item for item in baseline if item["data_date"] == comparison_date
    )

    changed = base.copy()
    changed.iloc[-5:, :] *= np.array(
        [0.90, 0.82, 0.87, 0.84, 0.89]
    )[:, None]
    recalculated = build_market_regime_history(changed, sessions=90)
    actual = next(
        item for item in recalculated if item["data_date"] == comparison_date
    )

    assert actual["probabilities"] == expected["probabilities"]
    assert actual["metrics"] == expected["metrics"]


def _latest_for_plan(
    regime: str,
    *,
    trend_direction: str = "mixed",
    risk_multiplier: float = 1.0,
    momentum_status: str = "limited",
) -> dict:
    return {
        "dominant_regime": regime,
        "trend_direction": trend_direction,
        "risk_state": "panic" if regime == "panic" else "normal",
        "risk_multiplier": risk_multiplier,
        "strategies": [
            {
                "key": "momentum",
                "status": momentum_status,
                "weight": 0.3,
            },
            {
                "key": "drawdown",
                "status": "limited",
                "weight": 0.2,
            },
        ],
    }


def _period(
    ticker: str,
    *,
    scanner: str = "momentum",
    direction: str = "long",
    confidence: str = "Высокая",
    age: int = 1,
) -> dict:
    return {
        "scanner": scanner,
        "ticker_a": ticker,
        "direction": direction,
        "confidence": confidence,
        "first_seen_date": "2026-07-28",
        "last_seen_date": "2026-07-28",
        "observation_count": age,
        "current_price": 123.45,
    }


def _trade_candidate(
    ticker: str,
    direction: str,
    price: float,
) -> dict:
    return {
        "ticker": ticker,
        "direction": direction,
        "scanner_label": "Momentum",
        "confidence": "Высокая",
        "first_seen_date": "2026-07-28",
        "age_days": 1,
        "current_price": price,
    }


def test_range_trade_plan_returns_specific_long_and_short_candidates():
    report = build_regime_trade_plan(
        _latest_for_plan("range"),
        [
            _period("ETH/USD", scanner="drawdown", direction="long"),
            _period("SOL/USD", direction="short", confidence="Средняя"),
            _period("DOGE/USD", confidence="Низкая"),
        ],
    )

    assert [item["ticker"] for item in report["candidates"]] == [
        "ETH/USD",
    ]
    assert report["candidates"][0]["action_label"] == "Купить"
    assert report["candidates"][0]["planned_close_date"] == "2026-08-06"
    assert report["rejected_count"] == 2


def test_trade_plan_does_not_hide_valid_candidates_after_fifth_item():
    report = build_regime_trade_plan(
        _latest_for_plan("range"),
        [
            _period(f"ASSET{index}/USD", direction="short")
            for index in range(7)
        ],
    )

    assert report["count"] == 7


def test_trade_plan_closes_signal_when_horizon_is_reached():
    active = build_regime_trade_plan(
        _latest_for_plan("range"),
        [_period("ETH/USD", age=4)],
    )
    expired = build_regime_trade_plan(
        _latest_for_plan("range"),
        [_period("ETH/USD", age=5)],
    )

    assert active["count"] == 1
    assert active["candidates"][0]["planned_close_date"] == "2026-07-29"
    assert expired["count"] == 0
    assert expired["rejected_count"] == 1


def test_trend_trade_plan_rejects_signals_against_btc_direction():
    report = build_regime_trade_plan(
        _latest_for_plan("trend", trend_direction="up"),
        [
            _period("ETH/USD", direction="long"),
            _period("SOL/USD", direction="short"),
        ],
    )

    assert [item["ticker"] for item in report["candidates"]] == ["ETH/USD"]
    assert report["rejected_count"] == 1


def test_trade_plan_removes_conflicting_direction_for_same_coin():
    report = build_regime_trade_plan(
        _latest_for_plan("range"),
        [
            _period("ETH/USD", direction="long"),
            _period("ETH/USD", direction="short"),
        ],
    )

    assert report["candidates"] == []
    assert report["conflict_count"] == 1
    assert "не прошли фильтр" in report["empty_reason"]


def test_panic_trade_plan_keeps_only_high_confidence_short():
    report = build_regime_trade_plan(
        _latest_for_plan(
            "panic",
            trend_direction="down",
            risk_multiplier=0.15,
            momentum_status="off",
        ),
        [
            _period("ETH/USD", direction="long"),
            _period("SOL/USD", direction="short", confidence="Средняя"),
            _period("BTC/USD", direction="short"),
        ],
    )

    assert [item["ticker"] for item in report["candidates"]] == ["BTC/USD"]
    assert report["position_size_label"] == "не более 15% обычного размера"


@pytest.mark.asyncio
async def test_report_loads_fresh_trade_plan_from_persisted_scanner_periods():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute(CREATE_MARKET_REGIME_SNAPSHOTS)
        await conn.execute(CREATE_SCANNER_SIGNAL_PERIODS)
        await conn.execute(
            """
            CREATE TABLE prices (
                market TEXT,
                ticker TEXT,
                date TEXT,
                close REAL
            )
            """
        )
        await conn.execute(
            """
            INSERT INTO prices (market, ticker, date, close)
            VALUES ('crypto', 'ETH/USD', '2026-07-28', 3200.0)
            """
        )
        await conn.execute(
            """
            INSERT INTO scanner_signal_periods (
                market, scanner, signal_key, ticker_a, ticker_b,
                direction, confidence, first_seen_date, last_seen_date,
                observation_count, status
            ) VALUES (
                'crypto', 'drawdown', 'ETH/USD', 'ETH/USD', '',
                'long', 'Высокая', '2026-07-27', '2026-07-28',
                2, 'active'
            )
            """
        )
        metrics = {
            "btc_price": 100000,
            "btc_return_7": 1.0,
            "btc_return_20": 1.5,
            "btc_sma_spread": 0.4,
            "btc_volatility": 35.0,
            "volatility_percentile": 50.0,
            "drawdown_60": -4.0,
            "breadth_20": 45.0,
            "breadth_change_5": 2.0,
            "average_correlation": 0.4,
            "dispersion_7": 6.0,
            "universe_size": 87,
        }
        strategies = _latest_for_plan("range")["strategies"]
        await conn.execute(
            """
            INSERT INTO market_regime_snapshots (
                calculation_version, market, data_date, dominant_regime,
                trend_direction, risk_state, risk_multiplier, confidence,
                probabilities_json, metrics_json, strategies_json,
                warnings_json
            ) VALUES (?, 'crypto', '2026-07-28', 'range', 'mixed',
                      'normal', 1.0, 0.2, ?, ?, ?, '[]')
            """,
            (
                CALCULATION_VERSION,
                json.dumps({
                    "trend": 0.2,
                    "range": 0.5,
                    "panic": 0.1,
                    "recovery": 0.2,
                }),
                json.dumps(metrics),
                json.dumps(strategies, ensure_ascii=False),
            ),
        )
        await conn.commit()

        report = await fetch_market_regime_report(conn)
        expected_daily_lag_report = await fetch_market_regime_report(
            conn,
            evaluation_date="2026-07-29",
        )
        expired_report = await fetch_market_regime_report(
            conn,
            evaluation_date="2026-08-05",
        )

    assert report["trade_plan"]["count"] == 1
    assert report["trade_plan"]["candidates"][0]["ticker"] == "ETH/USD"
    assert report["trade_plan"]["candidates"][0]["current_price_label"] == "$3 200.00"
    assert expected_daily_lag_report["is_stale"] is False
    assert expected_daily_lag_report["stale_days"] == 1
    assert expired_report["trade_plan"]["count"] == 0
    assert expired_report["trade_plan"]["expired_count"] == 1
    assert expired_report["is_stale"] is True
    assert expired_report["stale_days"] == 8


@pytest.mark.asyncio
async def test_alpha_journal_freezes_entries_and_closed_results():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute(
            """
            CREATE TABLE prices (
                market TEXT,
                ticker TEXT,
                date TEXT,
                close REAL
            )
            """
        )
        await conn.executemany(
            """
            INSERT INTO prices (market, ticker, date, close)
            VALUES ('crypto', ?, ?, ?)
            """,
            [
                ("ETH/USD", "2026-07-28", 100.0),
                ("SOL/USD", "2026-07-28", 200.0),
                ("ETH/USD", "2026-07-29", 110.0),
                ("SOL/USD", "2026-07-29", 180.0),
                ("ETH/USD", "2026-07-30", 105.0),
                ("SOL/USD", "2026-07-30", 190.0),
            ],
        )
        await conn.commit()

        first_plan = {
            "candidates": [
                _trade_candidate("ETH/USD", "long", 100.0),
                _trade_candidate("SOL/USD", "short", 200.0),
            ]
        }
        await sync_alpha_trade_journal(
            conn,
            {"data_date": "2026-07-28", "dominant_regime": "range"},
            first_plan,
        )
        second_plan = {
            "candidates": [
                _trade_candidate("ETH/USD", "long", 110.0),
                _trade_candidate("SOL/USD", "short", 180.0),
            ]
        }
        await sync_alpha_trade_journal(
            conn,
            {"data_date": "2026-07-29", "dominant_regime": "trend"},
            second_plan,
        )

        cursor = await conn.execute(
            """
            SELECT ticker, entry_price, last_price
            FROM alpha_trade_journal
            ORDER BY ticker
            """
        )
        active = [dict(row) for row in await cursor.fetchall()]
        assert active == [
            {"ticker": "ETH/USD", "entry_price": 100.0, "last_price": 110.0},
            {"ticker": "SOL/USD", "entry_price": 200.0, "last_price": 180.0},
        ]

        active_stats = await fetch_alpha_statistics(conn)
        assert active_stats["summary"]["active"] == 2
        assert active_stats["summary"]["active_cash"] == 20.0
        assert active_stats["as_of_date"] == "2026-07-29"
        assert active_stats["closed_today"] == []
        assert [
            (row["ticker"], row["result_pct"], row["cash_result"])
            for row in active_stats["active_trades"]
        ] == [
            ("ETH/USD", 10.0, 10.0),
            ("SOL/USD", 10.0, 10.0),
        ]
        live_stats = await fetch_alpha_statistics(
            conn,
            live_prices={
                "ETH/USD": 120.0,
                "SOL/USD": 160.0,
            },
            live_price_source_label="live MEXC · 29.07 12:00",
        )
        assert live_stats["summary"]["active_cash"] == 40.0
        assert live_stats["has_live_prices"] is True
        assert {
            row["current_price_source_label"]
            for row in live_stats["active_trades"]
        } == {"live MEXC · 29.07 12:00"}

        same_snapshot = await sync_alpha_trade_journal(
            conn,
            {"data_date": "2026-07-29", "dominant_regime": "range"},
            {"candidates": []},
        )
        assert same_snapshot["closed"] == 0
        assert same_snapshot["active"] == 2
        cursor = await conn.execute(
            """
            SELECT ticker, status, last_price
            FROM alpha_trade_journal
            ORDER BY ticker
            """
        )
        persisted = [dict(row) for row in await cursor.fetchall()]
        assert persisted == [
            {"ticker": "ETH/USD", "status": "active", "last_price": 110.0},
            {"ticker": "SOL/USD", "status": "active", "last_price": 180.0},
        ]

        await sync_alpha_trade_journal(
            conn,
            {"data_date": "2026-07-30", "dominant_regime": "range"},
            {"candidates": []},
        )
        closed_stats = await fetch_alpha_statistics(conn)
        assert closed_stats["summary"]["closed"] == 2
        assert closed_stats["summary"]["winners"] == 2
        assert closed_stats["summary"]["realized_cash"] == 10.0
        assert closed_stats["summary"]["active_cash"] == 0.0
        assert closed_stats["summary"]["total_cash"] == 10.0
        assert closed_stats["as_of_date_label"] == "30.07.2026"
        assert closed_stats["active_trades"] == []
        assert len(closed_stats["closed_today"]) == 2
        assert len(closed_stats["history"]) == 2
        assert {
            row["exit_reason_label"]
            for row in closed_stats["closed_today"]
        } == {"Сигнал исчез или не прошёл фильтр режима"}
        eth_history = next(
            row
            for row in closed_stats["history"]
            if row["ticker"] == "ETH/USD"
        )
        assert eth_history["opened_on_label"] == "28.07.2026"
        assert eth_history["closed_on_label"] == "30.07.2026"
        assert eth_history["entry_price_label"] == "$100"
        assert eth_history["current_price_label"] == "$105"
        assert eth_history["result_pct"] == 5.0
        assert eth_history["cash_result"] == 5.0

        await conn.execute(
            """
            UPDATE prices
            SET close = 1.0
            WHERE date = '2026-07-30'
            """
        )
        await conn.commit()
        unchanged = await fetch_alpha_statistics(conn)
        assert unchanged["summary"]["realized_cash"] == 10.0


@pytest.mark.asyncio
async def test_alpha_statistics_include_only_high_confidence_trades():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute(CREATE_ALPHA_TRADE_JOURNAL)
        await conn.executemany(
            """
            INSERT INTO alpha_trade_journal (
                calculation_version, ticker, direction, scanner, confidence,
                regime, opened_on, entry_price, signal_age_at_entry,
                last_seen_on, last_price, stake, status
            ) VALUES (?, ?, 'long', 'Momentum', ?, 'range', '2026-07-28',
                      100.0, 1, '2026-07-28', 110.0, 100.0, 'active')
            """,
            [
                (CALCULATION_VERSION, "ETH/USD", "Высокая"),
                (CALCULATION_VERSION, "SOL/USD", "Средняя"),
            ],
        )
        await conn.execute(
            """
            INSERT INTO alpha_trade_journal (
                calculation_version, ticker, direction, scanner, confidence,
                regime, opened_on, entry_price, signal_age_at_entry,
                last_seen_on, last_price, closed_on, exit_price, exit_reason,
                return_pct, cash_result, stake, status
            ) VALUES (?, 'STX/USD', 'short', 'Momentum', 'Высокая', 'range',
                      '2026-07-28', 0.1326, 1, '2026-07-28', 0.1326,
                      '2026-07-28', 0.1326, 'signal_or_regime_filter',
                      0.0, 0.0, 100.0, 'closed')
            """,
            (CALCULATION_VERSION,),
        )
        await conn.commit()

        statistics = await fetch_alpha_statistics(conn)

    assert statistics["summary"]["opened"] == 1
    assert statistics["summary"]["active"] == 1
    assert statistics["summary"]["active_cash"] == 10.0
    assert [row["ticker"] for row in statistics["active_trades"]] == [
        "ETH/USD"
    ]
    assert statistics["history"] == []


@pytest.mark.asyncio
async def test_alpha_calendar_expiry_uses_observed_live_price():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute(CREATE_ALPHA_TRADE_JOURNAL)
        await conn.execute(
            """
            INSERT INTO alpha_trade_journal (
                calculation_version, ticker, direction, scanner, confidence,
                regime, opened_on, entry_price, signal_age_at_entry,
                last_seen_on, last_price, stake, status
            ) VALUES (?, 'WLD/USD', 'short', 'Momentum', 'Высокая', 'range',
                      '2026-07-28', 100.0, 4, '2026-07-28', 100.0,
                      100.0, 'active')
            """,
            (CALCULATION_VERSION,),
        )
        await conn.commit()

        result = await expire_alpha_trade_journal(
            conn,
            as_of_date="2026-07-30",
            live_prices={"WLD/USD": 80.0},
        )
        cursor = await conn.execute(
            """
            SELECT status, closed_on, exit_price, exit_reason,
                   return_pct, cash_result
            FROM alpha_trade_journal
            WHERE ticker = 'WLD/USD'
            """
        )
        closed = dict(await cursor.fetchone())

    assert result["closed"] == 1
    assert result["skipped"] == []
    assert closed == {
        "status": "closed",
        "closed_on": "2026-07-30",
        "exit_price": 80.0,
        "exit_reason": "horizon_reached",
        "return_pct": 20.0,
        "cash_result": 20.0,
    }
