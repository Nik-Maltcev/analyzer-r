#!/usr/bin/env python3
"""Read-only reconciliation of every persisted performance calculation."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import math
import os
import sqlite3
import sys
from typing import Any

sys.path.insert(
    0,
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
)

from app.core.calculator import (  # noqa: E402
    CALCULATION_VERSION,
    calc_pair_performance,
    calc_single_performance,
    infer_position_kind,
)
from app.core.crypto_picks import CRYPTO_PICKS_TRACKING_START  # noqa: E402
from app.core.crypto_v2 import STRATEGY_VERSION as CRYPTO_V2_VERSION  # noqa: E402


MONEY_TOLERANCE = 0.011
PERCENT_TOLERANCE = 0.00011
PRICE_TOLERANCE = 1e-10
PRICE_RELATIVE_TOLERANCE = 1e-8


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _close(a: Any, b: Any, tolerance: float) -> bool:
    return _finite(a) and _finite(b) and abs(float(a) - float(b)) <= tolerance


def _price_close(a: Any, b: Any) -> bool:
    if not (_finite(a) and _finite(b)):
        return False
    left = float(a)
    right = float(b)
    return math.isclose(
        left,
        right,
        rel_tol=PRICE_RELATIVE_TOLERANCE,
        abs_tol=PRICE_TOLERANCE,
    )


def _parse_date(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            return datetime.strptime(text[:10], "%Y-%m-%d")
        except ValueError:
            return None


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row[1])
        for row in conn.execute(f'PRAGMA table_info("{table}")')
    }


def _error(
    report: dict[str, Any],
    *,
    journal: str,
    row_id: Any,
    field: str,
    stored: Any,
    expected: Any,
) -> None:
    report["errors"].append(
        {
            "journal": journal,
            "row_id": row_id,
            "field": field,
            "stored": stored,
            "expected": expected,
        }
    )


def _check_value(
    report: dict[str, Any],
    *,
    journal: str,
    row_id: Any,
    field: str,
    stored: Any,
    expected: Any,
    tolerance: float,
) -> None:
    if not _close(stored, expected, tolerance):
        _error(
            report,
            journal=journal,
            row_id=row_id,
            field=field,
            stored=stored,
            expected=expected,
        )


def _check_date_order(
    report: dict[str, Any],
    *,
    journal: str,
    row_id: Any,
    start_field: str,
    start: Any,
    end_field: str,
    end: Any,
) -> None:
    start_date = _parse_date(start)
    end_date = _parse_date(end)
    if start_date is None or end_date is None:
        _error(
            report,
            journal=journal,
            row_id=row_id,
            field=f"{start_field}/{end_field}",
            stored=[start, end],
            expected="valid ISO dates",
        )
        return
    try:
        is_ordered = end_date >= start_date
    except TypeError:
        is_ordered = str(end) >= str(start)
    if not is_ordered:
        _error(
            report,
            journal=journal,
            row_id=row_id,
            field=f"{start_field}/{end_field}",
            stored=[start, end],
            expected=f"{end_field} >= {start_field}",
        )


def _check_mexc_daily_price(
    conn: sqlite3.Connection,
    report: dict[str, Any],
    *,
    journal: str,
    row_id: Any,
    ticker: Any,
    date: Any,
    field: str,
    stored: Any,
) -> None:
    if "crypto_price_versions" not in _tables(conn):
        return
    source = conn.execute(
        """
        SELECT close
        FROM crypto_price_versions
        WHERE provider = 'mexc' AND ticker = ? AND date = ?
        """,
        (str(ticker), str(date)[:10]),
    ).fetchone()
    if source is None:
        report["warnings"].append(
            f"{journal}:{row_id}: no MEXC source price for "
            f"{ticker} on {str(date)[:10]}"
        )
        return
    report["checked_source_prices"] += 1
    if not _price_close(stored, source[0]):
        _error(
            report,
            journal=journal,
            row_id=row_id,
            field=field,
            stored=stored,
            expected=source[0],
        )


def _audit_favorites(
    conn: sqlite3.Connection,
    report: dict[str, Any],
) -> None:
    table = "favorites"
    required = {
        "id",
        "status",
        "position_kind",
        "ticker_b",
        "signal_type",
        "price_a_entry",
        "price_b_entry",
        "entry_time",
        "exit_time",
        "exit_price_a",
        "exit_price_b",
        "hedge_ratio_entry",
        "capital_at_entry",
        "leverage_at_entry",
        "taker_fee_pct_at_entry",
        "funding_rate_pct_at_entry",
        "calculation_version",
        "exit_hold_days",
        "exit_spread_move_pp",
        "exit_unlevered_return_pct",
        "exit_gross_pnl",
        "exit_gross_return_pct",
        "exit_pair_move_pct",
        "exit_total_cost",
        "exit_net_pnl",
        "exit_net_return_pct",
    }
    missing = required - _columns(conn, table)
    if missing:
        report["warnings"].append(
            f"{table}: missing audit columns: {', '.join(sorted(missing))}"
        )
        return

    rows = conn.execute(
        """
        SELECT *
        FROM favorites
        WHERE status = 'closed'
        ORDER BY id
        """
    ).fetchall()
    for row in rows:
        report["checked"]["favorites"] += 1
        _check_date_order(
            report,
            journal=table,
            row_id=row["id"],
            start_field="entry_time",
            start=row["entry_time"],
            end_field="exit_time",
            end=row["exit_time"],
        )
        version = str(row["calculation_version"] or "")
        if version != CALCULATION_VERSION:
            report["warnings"].append(
                f"favorites:{row['id']}: legacy calculation {version or 'unknown'}"
            )
            continue

        kind = infer_position_kind(row["position_kind"], row["ticker_b"])
        common = {
            "signal_type": str(row["signal_type"] or ""),
            "entry_price": row["price_a_entry"],
            "price_now": row["exit_price_a"],
            "capital": row["capital_at_entry"],
            "leverage": row["leverage_at_entry"],
            "taker_fee_pct": row["taker_fee_pct_at_entry"],
            "funding_rate_8h_pct": row["funding_rate_pct_at_entry"],
            "hold_days": row["exit_hold_days"],
        }
        if kind == "single":
            calculated = calc_single_performance(**common)
        else:
            calculated = calc_pair_performance(
                signal_type=common["signal_type"],
                entry_a=common["entry_price"],
                entry_b=row["price_b_entry"],
                price_a_now=common["price_now"],
                price_b_now=row["exit_price_b"],
                capital=common["capital"],
                leverage=common["leverage"],
                taker_fee_pct=common["taker_fee_pct"],
                funding_rate_8h_pct=common["funding_rate_8h_pct"],
                hold_days=common["hold_days"],
                hedge_ratio=(
                    1.0
                    if row["hedge_ratio_entry"] is None
                    else row["hedge_ratio_entry"]
                ),
            )
        if not calculated.get("complete"):
            _error(
                report,
                journal=table,
                row_id=row["id"],
                field="prices",
                stored="incomplete",
                expected="positive finite entry and exit prices",
            )
            continue

        checks = (
            ("exit_spread_move_pp", "spread_move_pp", PERCENT_TOLERANCE),
            (
                "exit_unlevered_return_pct",
                "unlevered_return_pct",
                PERCENT_TOLERANCE,
            ),
            ("exit_pair_move_pct", "pair_move_pct", PERCENT_TOLERANCE),
            ("exit_gross_pnl", "gross_pnl", MONEY_TOLERANCE),
            (
                "exit_gross_return_pct",
                "gross_return_pct",
                PERCENT_TOLERANCE,
            ),
            ("exit_total_cost", "total_cost", MONEY_TOLERANCE),
            ("exit_net_pnl", "net_pnl", MONEY_TOLERANCE),
            (
                "exit_net_return_pct",
                "net_return_pct",
                PERCENT_TOLERANCE,
            ),
        )
        for stored_field, calculated_field, tolerance in checks:
            _check_value(
                report,
                journal=table,
                row_id=row["id"],
                field=stored_field,
                stored=row[stored_field],
                expected=calculated[calculated_field],
                tolerance=tolerance,
            )


def _audit_simple_trades(
    conn: sqlite3.Connection,
    report: dict[str, Any],
    *,
    table: str,
    id_field: str,
    entry_field: str,
    exit_field: str,
    stake_field: str,
    status_field: str | None = None,
    entry_date_field: str | None = None,
    exit_date_field: str | None = None,
    verify_mexc_prices: bool = False,
) -> None:
    where = (
        f"WHERE {status_field} = 'closed'"
        if status_field
        else ""
    )
    rows = conn.execute(
        f'SELECT * FROM "{table}" {where} ORDER BY "{id_field}"'
    ).fetchall()
    for row in rows:
        report["checked"][table] += 1
        row_id = row[id_field]
        entry = row[entry_field]
        exit_price = row[exit_field]
        stake = row[stake_field]
        if entry_date_field and exit_date_field:
            _check_date_order(
                report,
                journal=table,
                row_id=row_id,
                start_field=entry_date_field,
                start=row[entry_date_field],
                end_field=exit_date_field,
                end=row[exit_date_field],
            )
        if not all(_finite(value) and float(value) > 0 for value in (entry, exit_price, stake)):
            _error(
                report,
                journal=table,
                row_id=row_id,
                field="prices_or_stake",
                stored=[entry, exit_price, stake],
                expected="positive finite values",
            )
            continue
        expected_return = (float(exit_price) / float(entry) - 1) * 100
        expected_cash = float(stake) * expected_return / 100
        _check_value(
            report,
            journal=table,
            row_id=row_id,
            field="return_pct",
            stored=row["return_pct"],
            expected=expected_return,
            tolerance=PERCENT_TOLERANCE,
        )
        _check_value(
            report,
            journal=table,
            row_id=row_id,
            field="cash_result",
            stored=row["cash_result"],
            expected=expected_cash,
            tolerance=MONEY_TOLERANCE,
        )
        if verify_mexc_prices and entry_date_field and exit_date_field:
            _check_mexc_daily_price(
                conn,
                report,
                journal=table,
                row_id=row_id,
                ticker=row["ticker"],
                date=row[entry_date_field],
                field=entry_field,
                stored=entry,
            )
            if (
                table != "crypto_strategy_trades"
                or str(row["exit_reason"] or "") != "manual"
            ):
                _check_mexc_daily_price(
                    conn,
                    report,
                    journal=table,
                    row_id=row_id,
                    ticker=row["ticker"],
                    date=row[exit_date_field],
                    field=exit_field,
                    stored=exit_price,
                )


def _audit_crypto_scanner_periods(
    conn: sqlite3.Connection,
    report: dict[str, Any],
) -> None:
    table = "scanner_signal_periods"
    required = {
        "id",
        "market",
        "scanner",
        "ticker_a",
        "direction",
        "first_seen_date",
        "last_seen_date",
        "observation_count",
        "strategy_admitted_date",
        "strategy_entry_price",
        "strategy_entry_recorded_at",
        "strategy_entry_source",
        "strategy_exit_date",
        "strategy_exit_price",
        "strategy_exit_reason",
        "strategy_return_pct",
        "strategy_cash_result",
        "strategy_stake",
        "strategy_version",
    }
    missing = required - _columns(conn, table)
    if missing:
        report["warnings"].append(
            f"{table}: missing audit columns: {', '.join(sorted(missing))}"
        )
        return

    rows = conn.execute(
        """
        SELECT *
        FROM scanner_signal_periods
        WHERE market = 'crypto'
          AND scanner = 'momentum'
          AND direction = 'long'
          AND strategy_admitted_date IS NOT NULL
        ORDER BY id
        """
    ).fetchall()
    journal_available = "crypto_strategy_trades" in _tables(conn)
    tracking_start = _parse_date(CRYPTO_PICKS_TRACKING_START)

    for row in rows:
        report["checked"]["scanner_signal_periods"] += 1
        row_id = row["id"]
        _check_date_order(
            report,
            journal=table,
            row_id=row_id,
            start_field="first_seen_date",
            start=row["first_seen_date"],
            end_field="last_seen_date",
            end=row["last_seen_date"],
        )
        _check_date_order(
            report,
            journal=table,
            row_id=row_id,
            start_field="first_seen_date",
            start=row["first_seen_date"],
            end_field="strategy_admitted_date",
            end=row["strategy_admitted_date"],
        )
        _check_date_order(
            report,
            journal=table,
            row_id=row_id,
            start_field="strategy_admitted_date",
            start=row["strategy_admitted_date"],
            end_field="last_seen_date",
            end=row["last_seen_date"],
        )
        if int(row["observation_count"] or 0) <= 0:
            _error(
                report,
                journal=table,
                row_id=row_id,
                field="observation_count",
                stored=row["observation_count"],
                expected="positive integer",
            )

        entry = row["strategy_entry_price"]
        stake = row["strategy_stake"]
        if not all(
            _finite(value) and float(value) > 0
            for value in (entry, stake)
        ):
            _error(
                report,
                journal=table,
                row_id=row_id,
                field="entry_price_or_stake",
                stored=[entry, stake],
                expected="positive finite values",
            )
            continue
        _check_mexc_daily_price(
            conn,
            report,
            journal=table,
            row_id=row_id,
            ticker=row["ticker_a"],
            date=row["strategy_admitted_date"],
            field="strategy_entry_price",
            stored=entry,
        )

        admitted = _parse_date(row["strategy_admitted_date"])
        recorded = _parse_date(row["strategy_entry_recorded_at"])
        is_retrospective = bool(
            (tracking_start and admitted and admitted < tracking_start)
            or (
                admitted
                and recorded
                and (recorded.date() - admitted.date()).days > 1
            )
        )
        mode = "retrospective" if is_retrospective else "forward"
        report["crypto_period_modes"][mode] += 1
        if is_retrospective:
            report["warnings"].append(
                f"{table}:{row_id}: retrospectively reconstructed entry; "
                "exclude it from forward-performance claims"
            )

        exit_values = {
            "strategy_exit_date": row["strategy_exit_date"],
            "strategy_exit_price": row["strategy_exit_price"],
            "strategy_exit_reason": row["strategy_exit_reason"],
            "strategy_return_pct": row["strategy_return_pct"],
            "strategy_cash_result": row["strategy_cash_result"],
        }
        has_any_exit = any(value not in (None, "") for value in exit_values.values())
        if not has_any_exit:
            if journal_available:
                journal = conn.execute(
                    "SELECT period_id FROM crypto_strategy_trades WHERE period_id = ?",
                    (row_id,),
                ).fetchone()
                if journal is not None:
                    _error(
                        report,
                        journal=table,
                        row_id=row_id,
                        field="journal_state",
                        stored="active period with completed journal row",
                        expected="no journal row before exit",
                    )
            continue

        missing_exit = [
            field for field, value in exit_values.items()
            if value in (None, "")
        ]
        if missing_exit:
            _error(
                report,
                journal=table,
                row_id=row_id,
                field="exit_snapshot",
                stored=missing_exit,
                expected="all exit fields frozen together",
            )
            continue
        _check_date_order(
            report,
            journal=table,
            row_id=row_id,
            start_field="strategy_admitted_date",
            start=row["strategy_admitted_date"],
            end_field="strategy_exit_date",
            end=row["strategy_exit_date"],
        )
        exit_price = row["strategy_exit_price"]
        if not _finite(exit_price) or float(exit_price) <= 0:
            _error(
                report,
                journal=table,
                row_id=row_id,
                field="strategy_exit_price",
                stored=exit_price,
                expected="positive finite value",
            )
            continue
        expected_return = (float(exit_price) / float(entry) - 1) * 100
        expected_cash = float(stake) * expected_return / 100
        _check_value(
            report,
            journal=table,
            row_id=row_id,
            field="strategy_return_pct",
            stored=row["strategy_return_pct"],
            expected=expected_return,
            tolerance=PERCENT_TOLERANCE,
        )
        _check_value(
            report,
            journal=table,
            row_id=row_id,
            field="strategy_cash_result",
            stored=row["strategy_cash_result"],
            expected=expected_cash,
            tolerance=MONEY_TOLERANCE,
        )
        if str(row["strategy_exit_reason"] or "") != "manual":
            _check_mexc_daily_price(
                conn,
                report,
                journal=table,
                row_id=row_id,
                ticker=row["ticker_a"],
                date=row["strategy_exit_date"],
                field="strategy_exit_price",
                stored=exit_price,
            )

        if not journal_available:
            _error(
                report,
                journal=table,
                row_id=row_id,
                field="journal",
                stored="table absent",
                expected="matching immutable crypto_strategy_trades row",
            )
            continue
        journal = conn.execute(
            "SELECT * FROM crypto_strategy_trades WHERE period_id = ?",
            (row_id,),
        ).fetchone()
        if journal is None:
            _error(
                report,
                journal=table,
                row_id=row_id,
                field="journal",
                stored=None,
                expected="matching immutable crypto_strategy_trades row",
            )
            continue
        numeric_pairs = (
            ("strategy_entry_price", "entry_price", PRICE_TOLERANCE),
            ("strategy_exit_price", "exit_price", PRICE_TOLERANCE),
            ("strategy_return_pct", "return_pct", PERCENT_TOLERANCE),
            ("strategy_cash_result", "cash_result", MONEY_TOLERANCE),
            ("strategy_stake", "stake", MONEY_TOLERANCE),
        )
        for period_field, journal_field, tolerance in numeric_pairs:
            _check_value(
                report,
                journal=table,
                row_id=row_id,
                field=f"journal.{journal_field}",
                stored=journal[journal_field],
                expected=row[period_field],
                tolerance=tolerance,
            )
        text_pairs = (
            ("strategy_admitted_date", "opened_on"),
            ("strategy_exit_date", "closed_on"),
            ("strategy_exit_reason", "exit_reason"),
            ("strategy_version", "strategy_version"),
        )
        for period_field, journal_field in text_pairs:
            if str(journal[journal_field] or "") != str(row[period_field] or ""):
                _error(
                    report,
                    journal=table,
                    row_id=row_id,
                    field=f"journal.{journal_field}",
                    stored=journal[journal_field],
                    expected=row[period_field],
                )


def _audit_momentum_runs(
    conn: sqlite3.Connection,
    report: dict[str, Any],
) -> None:
    allocations = conn.execute(
        """
        SELECT *
        FROM momentum_portfolio_allocations
        WHERE exit_date IS NOT NULL
        ORDER BY run_date, ticker
        """
    ).fetchall()
    for row in allocations:
        report["checked"]["momentum_portfolio_allocations"] += 1
        row_id = f"{row['run_date']}:{row['ticker']}"
        entry = row["entry_price"]
        exit_price = row["exit_price"]
        allocation = row["allocation"]
        _check_date_order(
            report,
            journal="momentum_portfolio_allocations",
            row_id=row_id,
            start_field="run_date",
            start=row["run_date"],
            end_field="exit_date",
            end=row["exit_date"],
        )
        if not all(
            _finite(value) and float(value) > 0
            for value in (entry, exit_price, allocation)
        ):
            _error(
                report,
                journal="momentum_portfolio_allocations",
                row_id=row_id,
                field="prices_or_allocation",
                stored=[entry, exit_price, allocation],
                expected="positive finite values",
            )
            continue
        entry = float(entry)
        exit_price = float(exit_price)
        allocation = float(allocation)
        expected_return = (exit_price / entry - 1) * 100
        expected_cash = allocation * expected_return / 100
        _check_value(
            report,
            journal="momentum_portfolio_allocations",
            row_id=row_id,
            field="return_pct",
            stored=row["return_pct"],
            expected=expected_return,
            tolerance=PERCENT_TOLERANCE,
        )
        _check_value(
            report,
            journal="momentum_portfolio_allocations",
            row_id=row_id,
            field="cash_result",
            stored=row["cash_result"],
            expected=expected_cash,
            tolerance=MONEY_TOLERANCE,
        )
        expected_units = allocation / entry
        _check_value(
            report,
            journal="momentum_portfolio_allocations",
            row_id=row_id,
            field="units",
            stored=row["units"],
            expected=expected_units,
            tolerance=0.000001,
        )
        _check_mexc_daily_price(
            conn,
            report,
            journal="momentum_portfolio_allocations",
            row_id=row_id,
            ticker=row["ticker"],
            date=row["run_date"],
            field="entry_price",
            stored=entry,
        )
        _check_mexc_daily_price(
            conn,
            report,
            journal="momentum_portfolio_allocations",
            row_id=row_id,
            ticker=row["ticker"],
            date=row["exit_date"],
            field="exit_price",
            stored=exit_price,
        )

    runs = conn.execute(
        """
        SELECT *
        FROM momentum_portfolio_runs
        WHERE finalized_on IS NOT NULL
        ORDER BY run_date
        """
    ).fetchall()
    for row in runs:
        report["checked"]["momentum_portfolio_runs"] += 1
        capital = row["capital"]
        if not _finite(capital) or float(capital) <= 0:
            _error(
                report,
                journal="momentum_portfolio_runs",
                row_id=row["run_date"],
                field="capital",
                stored=capital,
                expected="positive finite value",
            )
            continue
        cash = conn.execute(
            """
            SELECT COALESCE(SUM(cash_result), 0)
            FROM momentum_portfolio_allocations
            WHERE run_date = ?
            """,
            (row["run_date"],),
        ).fetchone()[0]
        expected_return = float(cash) / float(capital) * 100
        aggregates = conn.execute(
            """
            SELECT COUNT(*), COALESCE(SUM(allocation), 0)
            FROM momentum_portfolio_allocations
            WHERE run_date = ?
            """,
            (row["run_date"],),
        ).fetchone()
        allocation_count = int(aggregates[0])
        allocated = float(aggregates[1])
        expected_reserve = float(capital) - allocated
        _check_value(
            report,
            journal="momentum_portfolio_runs",
            row_id=row["run_date"],
            field="allocated",
            stored=row["allocated"],
            expected=allocated,
            tolerance=MONEY_TOLERANCE,
        )
        _check_value(
            report,
            journal="momentum_portfolio_runs",
            row_id=row["run_date"],
            field="reserve",
            stored=row["reserve"],
            expected=expected_reserve,
            tolerance=MONEY_TOLERANCE,
        )
        if int(row["selected_total"]) != allocation_count:
            _error(
                report,
                journal="momentum_portfolio_runs",
                row_id=row["run_date"],
                field="selected_total",
                stored=row["selected_total"],
                expected=allocation_count,
            )
        _check_value(
            report,
            journal="momentum_portfolio_runs",
            row_id=row["run_date"],
            field="cash_result",
            stored=row["cash_result"],
            expected=cash,
            tolerance=MONEY_TOLERANCE,
        )
        _check_value(
            report,
            journal="momentum_portfolio_runs",
            row_id=row["run_date"],
            field="return_pct",
            stored=row["return_pct"],
            expected=expected_return,
            tolerance=PERCENT_TOLERANCE,
        )


def audit_database(
    db_path: str,
    *,
    require_data: bool = False,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "db_path": os.path.abspath(db_path),
        "checked": {
            "favorites": 0,
            "scanner_signal_periods": 0,
            "crypto_strategy_trades": 0,
            "crypto_v2_trades": 0,
            "momentum_portfolio_allocations": 0,
            "momentum_portfolio_runs": 0,
        },
        "checked_source_prices": 0,
        "crypto_period_modes": {
            "forward": 0,
            "retrospective": 0,
        },
        "errors": [],
        "warnings": [],
    }
    if not os.path.exists(db_path):
        report["errors"].append(
            {
                "journal": "database",
                "row_id": None,
                "field": "path",
                "stored": db_path,
                "expected": "existing SQLite database",
            }
        )
        report["ok"] = False
        return report

    uri = f"file:{os.path.abspath(db_path).replace(os.sep, '/')}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        tables = _tables(conn)

        if "favorites" in tables:
            _audit_favorites(conn, report)
        else:
            report["warnings"].append("favorites: table is absent")

        if "scanner_signal_periods" in tables:
            _audit_crypto_scanner_periods(conn, report)
        else:
            report["warnings"].append(
                "scanner_signal_periods: table is absent"
            )

        if "crypto_strategy_trades" in tables:
            _audit_simple_trades(
                conn,
                report,
                table="crypto_strategy_trades",
                id_field="period_id",
                entry_field="entry_price",
                exit_field="exit_price",
                stake_field="stake",
                entry_date_field="opened_on",
                exit_date_field="closed_on",
                verify_mexc_prices=True,
            )
        else:
            report["warnings"].append(
                "crypto_strategy_trades: table is absent"
            )

        if "crypto_v2_trades" in tables:
            _audit_simple_trades(
                conn,
                report,
                table="crypto_v2_trades",
                id_field="id",
                entry_field="entry_price",
                exit_field="exit_price",
                stake_field="allocation",
                status_field="status",
                entry_date_field="entry_date",
                exit_date_field="exit_date",
                verify_mexc_prices=True,
            )
            legacy_versions = conn.execute(
                """
                SELECT DISTINCT strategy_version
                FROM crypto_v2_trades
                WHERE strategy_version <> ?
                """,
                (CRYPTO_V2_VERSION,),
            ).fetchall()
            for version_row in legacy_versions:
                report["warnings"].append(
                    "crypto_v2_trades: legacy strategy version "
                    f"{version_row[0]!r}; current is {CRYPTO_V2_VERSION!r}"
                )
        else:
            report["warnings"].append("crypto_v2_trades: table is absent")

        momentum_tables = {
            "momentum_portfolio_runs",
            "momentum_portfolio_allocations",
        }
        if momentum_tables <= tables:
            _audit_momentum_runs(conn, report)
        else:
            report["warnings"].append(
                "momentum portfolio: journal tables are absent"
            )

    report["ok"] = not report["errors"]
    report["checked_total"] = sum(report["checked"].values())
    if require_data and report["checked_total"] <= 0:
        report["errors"].append(
            {
                "journal": "database",
                "row_id": None,
                "field": "checked_total",
                "stored": report["checked_total"],
                "expected": "at least one persisted calculation row",
            }
        )
        report["ok"] = False
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        default=os.environ.get("DB_PATH", "/data/market.db"),
        help="Path to market.db (opened read-only)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON",
    )
    parser.add_argument(
        "--require-data",
        action="store_true",
        help="Fail when no persisted calculation rows were checked",
    )
    args = parser.parse_args()
    report = audit_database(args.db, require_data=args.require_data)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"Database: {report['db_path']}")
        print(f"Checked rows: {report.get('checked_total', 0)}")
        print(f"Errors: {len(report['errors'])}")
        for item in report["errors"]:
            print(
                "ERROR "
                f"{item['journal']}:{item['row_id']}:{item['field']} "
                f"stored={item['stored']!r} expected={item['expected']!r}"
            )
        for warning in report["warnings"]:
            print(f"WARNING {warning}")
        print("AUDIT OK" if report["ok"] else "AUDIT FAILED")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
