import sqlite3

from app.core.calculator import CALCULATION_VERSION, calc_pair_performance
from app.db.schema import (
    CREATE_CRYPTO_STRATEGY_TRADES,
    CREATE_CRYPTO_PRICE_VERSIONS,
    CREATE_CRYPTO_V2_TRADES,
    CREATE_FAVORITES,
    CREATE_MOMENTUM_PORTFOLIO_ALLOCATIONS,
    CREATE_MOMENTUM_PORTFOLIO_RUNS,
    CREATE_SCANNER_SIGNAL_PERIODS,
)
from scripts.audit_calculations import audit_database


def _create_auditable_db(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.execute(CREATE_FAVORITES)
    conn.execute(CREATE_CRYPTO_STRATEGY_TRADES)
    conn.execute(CREATE_CRYPTO_PRICE_VERSIONS)
    conn.execute(CREATE_CRYPTO_V2_TRADES)
    conn.execute(CREATE_MOMENTUM_PORTFOLIO_RUNS)
    conn.execute(CREATE_MOMENTUM_PORTFOLIO_ALLOCATIONS)
    conn.execute(CREATE_SCANNER_SIGNAL_PERIODS)

    result = calc_pair_performance(
        signal_type="long_a",
        entry_a=100.0,
        entry_b=200.0,
        price_a_now=110.0,
        price_b_now=190.0,
        capital=1000.0,
        leverage=1.0,
        taker_fee_pct=0.02,
        funding_rate_8h_pct=0.0,
        hold_days=1.5,
        hedge_ratio=1.0,
    )
    conn.execute(
        """
        INSERT INTO favorites (
            pair, market, position_kind, source, ticker_a, ticker_b,
            signal_type, hedge_ratio_entry, price_a_entry, price_b_entry,
            entry_time, exit_time, exit_price_a, exit_price_b, status,
            capital_at_entry, leverage_at_entry,
            taker_fee_pct_at_entry, funding_rate_pct_at_entry,
            calculation_version, exit_hold_days, exit_spread_move_pp,
            exit_unlevered_return_pct, exit_pair_move_pct, exit_gross_pnl,
            exit_gross_return_pct, exit_total_cost, exit_net_pnl,
            exit_net_return_pct
        )
        VALUES (
            'A_B', 'crypto', 'pair', 'signal', 'A', 'B',
            'long_a', 1, 100, 200,
            '2026-07-20 00:00:00', '2026-07-21 12:00:00', 110, 190,
            'closed', 1000, 1, 0.02, 0, ?, 1.5, ?, ?, ?, ?, ?, ?, ?, ?
        )
        """,
        (
            CALCULATION_VERSION,
            result["spread_move_pp"],
            result["unlevered_return_pct"],
            result["pair_move_pct"],
            result["gross_pnl"],
            result["gross_return_pct"],
            result["total_cost"],
            result["net_pnl"],
            result["net_return_pct"],
        ),
    )
    conn.execute(
        """
        INSERT INTO crypto_price_versions (
            provider, ticker, date, close, market
        )
        VALUES
            ('mexc', 'TEST/USD', '2026-07-20', 100, 'crypto'),
            ('mexc', 'TEST/USD', '2026-07-21', 110, 'crypto'),
            ('mexc', 'V2/USD', '2026-07-20', 50, 'crypto'),
            ('mexc', 'V2/USD', '2026-07-21', 55, 'crypto'),
            ('mexc', 'M3/USD', '2026-07-20', 100, 'crypto'),
            ('mexc', 'M3/USD', '2026-07-21', 110, 'crypto')
        """
    )
    conn.execute(
        """
        INSERT INTO scanner_signal_periods (
            id, market, scanner, signal_key, ticker_a, direction,
            confidence, strategy_admitted_date, strategy_confidence,
            strategy_entry_price, strategy_entry_recorded_at,
            strategy_entry_source, strategy_exit_date, strategy_exit_price,
            strategy_exit_recorded_at, strategy_exit_reason,
            strategy_return_pct, strategy_cash_result, strategy_stake,
            strategy_version, first_seen_date, last_seen_date,
            observation_count, status, ended_date
        )
        VALUES (
            7, 'crypto', 'momentum', 'TEST', 'TEST/USD', 'long',
            'high', '2026-07-20', 'high', 100,
            '2026-07-20 00:00:00', 'mexc_daily_close',
            '2026-07-21', 110, '2026-07-21 00:00:00', 'horizon',
            10, 10, 100, 'test-v1', '2026-07-20', '2026-07-21',
            2, 'closed', '2026-07-21'
        )
        """
    )
    conn.execute(
        """
        INSERT INTO crypto_strategy_trades (
            period_id, market, scanner, signal_key, ticker, direction,
            confidence, opened_on, entry_price, entry_recorded_at,
            entry_source, closed_on, exit_price, exit_recorded_at,
            exit_reason, return_pct, cash_result, stake, strategy_version
        )
        VALUES (
            7, 'crypto', 'momentum', 'TEST', 'TEST/USD', 'long',
            'high', '2026-07-20', 100, '2026-07-20 00:00:00',
            'mexc_daily_close', '2026-07-21', 110,
            '2026-07-21 00:00:00', 'horizon', 10, 10, 100, 'test-v1'
        )
        """
    )
    conn.execute(
        """
        INSERT INTO crypto_v2_trades (
            strategy_version, mode, ticker, confidence, entry_date,
            entry_price, entry_score, entry_regime, exposure_factor,
            allocation, weight, volatility_pct, exit_date, exit_price,
            exit_reason, return_pct, cash_result, status,
            last_evaluated_date
        )
        VALUES (
            'test-v2', 'forward', 'V2/USD', 'high', '2026-07-20',
            50, 12, 'risk_on', 1, 200, 1, 3, '2026-07-21', 55,
            'signal_ended', 10, 20, 'closed', '2026-07-21'
        )
        """
    )
    conn.execute(
        """
        INSERT INTO momentum_portfolio_runs (
            run_date, strategy_version, status, entries_allowed, capital,
            allocated, reserve, candidates_total, selected_total,
            btc_above_sma50, btc_distance_pct, breadth_positive,
            breadth_total, breadth_pct, finalized_on, cash_result, return_pct
        )
        VALUES (
            '2026-07-20', 'test-m3', 'invested', 1, 300,
            300, 0, 3, 1, 1, 2, 8, 10, 80,
            '2026-07-21', 30, 10
        )
        """
    )
    conn.execute(
        """
        INSERT INTO momentum_portfolio_allocations (
            run_date, ticker, rank, confidence, momentum_score,
            volatility_pct, weight, allocation, units, entry_price,
            exit_date, exit_price, return_pct, cash_result
        )
        VALUES (
            '2026-07-20', 'M3/USD', 1, 'high', 15,
            3, 1, 300, 3, 100,
            '2026-07-21', 110, 10, 30
        )
        """
    )
    conn.commit()
    conn.close()


def test_calculation_auditor_accepts_reconciled_rows(tmp_path):
    db_path = tmp_path / "market.db"
    _create_auditable_db(str(db_path))

    report = audit_database(str(db_path))

    assert report["ok"] is True
    assert report["checked"]["favorites"] == 1
    assert report["checked"]["scanner_signal_periods"] == 1
    assert report["checked"]["crypto_strategy_trades"] == 1
    assert report["checked"]["crypto_v2_trades"] == 1
    assert report["checked"]["momentum_portfolio_allocations"] == 1
    assert report["checked"]["momentum_portfolio_runs"] == 1
    assert report["errors"] == []


def test_calculation_auditor_rejects_period_journal_divergence(tmp_path):
    db_path = tmp_path / "market.db"
    _create_auditable_db(str(db_path))
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        UPDATE scanner_signal_periods
        SET strategy_cash_result = 9
        WHERE id = 7
        """
    )
    conn.commit()
    conn.close()

    report = audit_database(str(db_path))

    assert report["ok"] is False
    assert any(
        item["journal"] == "scanner_signal_periods"
        and item["field"] in {
            "strategy_cash_result",
            "journal.cash_result",
        }
        for item in report["errors"]
    )


def test_calculation_auditor_can_require_persisted_rows(tmp_path):
    db_path = tmp_path / "empty.db"
    sqlite3.connect(db_path).close()

    report = audit_database(str(db_path), require_data=True)

    assert report["ok"] is False
    assert report["checked_total"] == 0


def test_calculation_auditor_rejects_corrupted_cash_result(tmp_path):
    db_path = tmp_path / "market.db"
    _create_auditable_db(str(db_path))
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE crypto_strategy_trades SET cash_result = 999 WHERE period_id = 7"
    )
    conn.commit()
    conn.close()

    report = audit_database(str(db_path))

    assert report["ok"] is False
    assert any(
        item["journal"] == "crypto_strategy_trades"
        and item["field"] == "cash_result"
        for item in report["errors"]
    )


def test_calculation_auditor_rejects_corrupted_portfolio_total(tmp_path):
    db_path = tmp_path / "market.db"
    _create_auditable_db(str(db_path))
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        UPDATE momentum_portfolio_runs
        SET cash_result = 999
        WHERE run_date = '2026-07-20'
        """
    )
    conn.commit()
    conn.close()

    report = audit_database(str(db_path))

    assert report["ok"] is False
    assert any(
        item["journal"] == "momentum_portfolio_runs"
        and item["field"] == "cash_result"
        for item in report["errors"]
    )


def test_calculation_auditor_rejects_price_not_in_source_archive(tmp_path):
    db_path = tmp_path / "market.db"
    _create_auditable_db(str(db_path))
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        UPDATE crypto_strategy_trades
        SET entry_price = 101, return_pct = 8.9108910891,
            cash_result = 8.9108910891
        WHERE period_id = 7
        """
    )
    conn.commit()
    conn.close()

    report = audit_database(str(db_path))

    assert report["ok"] is False
    assert any(
        item["journal"] == "crypto_strategy_trades"
        and item["field"] == "entry_price"
        and item["expected"] == 100
        for item in report["errors"]
    )


def test_calculation_auditor_rejects_exit_before_entry(tmp_path):
    db_path = tmp_path / "market.db"
    _create_auditable_db(str(db_path))
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        UPDATE crypto_v2_trades
        SET exit_date = '2026-07-19'
        WHERE ticker = 'V2/USD'
        """
    )
    conn.commit()
    conn.close()

    report = audit_database(str(db_path))

    assert report["ok"] is False
    assert any(
        item["journal"] == "crypto_v2_trades"
        and item["field"] == "entry_date/exit_date"
        for item in report["errors"]
    )
