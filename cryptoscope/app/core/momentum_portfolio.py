"""Forward-test journal for the admin Momentum risk portfolio."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

import aiosqlite
import pandas as pd

from app.core.crypto_picks import (
    build_admin_momentum_portfolio,
    is_excluded_crypto_confidence,
)
from app.core.scanner_history import (
    annotate_scanner_results,
    build_scanner_snapshot,
    fetch_active_scanner_periods,
    is_scanner_signal_within_horizon,
)
from app.db.schema import (
    CREATE_MOMENTUM_PORTFOLIO_ALLOCATIONS,
    CREATE_MOMENTUM_PORTFOLIO_INDICES,
    CREATE_MOMENTUM_PORTFOLIO_RUNS,
)

STRATEGY_VERSION = "momentum-risk-daily-v1"
MODEL_CAPITAL = 300.0


def _date_label(value: Any) -> str:
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").strftime(
            "%d.%m.%Y"
        )
    except (TypeError, ValueError):
        return str(value or "—")


def _money(value: Any, signed: bool = False) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    return f"{number:+.2f}" if signed else f"${number:,.2f}"


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


def _signed_money(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    if number > 0:
        return f"+${number:.2f}"
    if number < 0:
        return f"-${abs(number):.2f}"
    return "$0.00"


async def ensure_momentum_portfolio_schema(conn) -> None:
    await conn.execute(CREATE_MOMENTUM_PORTFOLIO_RUNS)
    await conn.execute(CREATE_MOMENTUM_PORTFOLIO_ALLOCATIONS)
    for statement in CREATE_MOMENTUM_PORTFOLIO_INDICES:
        await conn.execute(statement)
    await conn.commit()


def _daily_histories(
    wide: pd.DataFrame,
) -> dict[str, list[tuple[str, float]]]:
    return {
        str(ticker): [
            (str(raw_date)[:10], float(raw_price))
            for raw_date, raw_price in series.dropna().items()
        ]
        for ticker, series in wide.items()
        if not series.dropna().empty
    }


async def _momentum_candidates(
    conn,
    wide: pd.DataFrame,
) -> list[dict[str, Any]]:
    frame, _active = build_scanner_snapshot(wide, "momentum")
    if frame.empty:
        return []
    periods = await fetch_active_scanner_periods(
        conn,
        "crypto",
        "momentum",
    )
    records = annotate_scanner_results(
        frame.to_dict(orient="records"),
        "momentum",
        periods,
    )
    return [
        record
        for record in records
        if record.get("recommendation_class") == "long"
        and not record.get("signal_suppressed")
        and not is_excluded_crypto_confidence(record.get("confidence"))
        and is_scanner_signal_within_horizon(
            "momentum",
            record.get("signal_age_days"),
        )
    ]


async def _finalize_previous_runs(
    conn,
    histories: dict[str, list[tuple[str, float]]],
    data_date: str,
) -> int:
    cursor = await conn.execute(
        """
        SELECT run_date, capital
        FROM momentum_portfolio_runs
        WHERE finalized_on IS NULL AND run_date < ?
        ORDER BY run_date
        """,
        (data_date,),
    )
    runs = [dict(row) for row in await cursor.fetchall()]
    finalized = 0

    for run in runs:
        run_date = str(run["run_date"])
        cursor = await conn.execute(
            """
            SELECT *
            FROM momentum_portfolio_allocations
            WHERE run_date = ?
            ORDER BY rank
            """,
            (run_date,),
        )
        allocations = [dict(row) for row in await cursor.fetchall()]
        exits: list[dict[str, Any]] = []
        for allocation in allocations:
            invested = float(allocation["allocation"])
            entry_price = float(allocation["entry_price"])
            if invested <= 0:
                exits.append({
                    "ticker": allocation["ticker"],
                    "exit_date": data_date,
                    "exit_price": entry_price,
                    "return_pct": 0.0,
                    "cash_result": 0.0,
                })
                continue
            future = [
                (date_value, close)
                for date_value, close in histories.get(
                    str(allocation["ticker"]),
                    [],
                )
                if run_date < date_value <= data_date
            ]
            if not future:
                exits = []
                break
            exit_date, exit_price = future[0]
            return_pct = (
                (exit_price / entry_price - 1) * 100
                if entry_price > 0
                else 0.0
            )
            exits.append({
                "ticker": allocation["ticker"],
                "exit_date": exit_date,
                "exit_price": exit_price,
                "return_pct": return_pct,
                "cash_result": invested * return_pct / 100,
            })

        if allocations and not exits:
            continue

        finalized_on = (
            max(item["exit_date"] for item in exits)
            if exits
            else data_date
        )
        cash_result = sum(item["cash_result"] for item in exits)
        capital = float(run["capital"])
        return_pct = cash_result / capital * 100 if capital else 0.0
        for item in exits:
            await conn.execute(
                """
                UPDATE momentum_portfolio_allocations
                SET exit_date = ?, exit_price = ?,
                    return_pct = ?, cash_result = ?
                WHERE run_date = ? AND ticker = ?
                  AND exit_date IS NULL
                """,
                (
                    item["exit_date"],
                    item["exit_price"],
                    item["return_pct"],
                    item["cash_result"],
                    run_date,
                    item["ticker"],
                ),
            )
        await conn.execute(
            """
            UPDATE momentum_portfolio_runs
            SET finalized_on = ?, cash_result = ?, return_pct = ?,
                finalized_at = datetime('now')
            WHERE run_date = ? AND finalized_on IS NULL
            """,
            (finalized_on, cash_result, return_pct, run_date),
        )
        finalized += 1
    return finalized


async def sync_momentum_portfolio_journal(db_path: str) -> dict[str, Any]:
    """Finalize yesterday and freeze today's portfolio exactly once."""
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        await ensure_momentum_portfolio_schema(conn)
        cursor = await conn.execute(
            """
            SELECT MAX(date) AS data_date
            FROM prices
            WHERE market = 'crypto'
            """
        )
        latest_row = await cursor.fetchone()
        data_date = str(latest_row["data_date"] or "")[:10]
        if not data_date:
            return {"status": "no_crypto_prices", "created": False}

        cursor = await conn.execute(
            """
            SELECT
                EXISTS(
                    SELECT 1
                    FROM momentum_portfolio_runs
                    WHERE run_date = ?
                ) AS has_current,
                EXISTS(
                    SELECT 1
                    FROM momentum_portfolio_runs
                    WHERE finalized_on IS NULL AND run_date < ?
                ) AS has_pending
            """,
            (data_date, data_date),
        )
        journal_state = await cursor.fetchone()
        if journal_state["has_current"] and not journal_state["has_pending"]:
            return {
                "status": "already_recorded",
                "created": False,
                "run_date": data_date,
                "finalized": 0,
            }

        cursor = await conn.execute(
            """
            SELECT ticker, date, close
            FROM prices
            WHERE market = 'crypto'
            ORDER BY ticker, date
            """
        )
        price_rows = await cursor.fetchall()
        if not price_rows:
            return {"status": "no_crypto_prices", "created": False}

        prices = pd.DataFrame([dict(row) for row in price_rows])
        wide = prices.pivot(
            index="date",
            columns="ticker",
            values="close",
        ).sort_index()
        histories = _daily_histories(wide)
        finalized = await _finalize_previous_runs(
            conn,
            histories,
            data_date,
        )

        cursor = await conn.execute(
            """
            SELECT 1
            FROM momentum_portfolio_runs
            WHERE run_date = ?
            """,
            (data_date,),
        )
        if await cursor.fetchone():
            await conn.commit()
            return {
                "status": "already_recorded",
                "created": False,
                "run_date": data_date,
                "finalized": finalized,
            }

        candidates = await _momentum_candidates(conn, wide)
        current_prices = {
            ticker: values[-1][1]
            for ticker, values in histories.items()
            if values
        }
        model = build_admin_momentum_portfolio(
            candidates,
            histories,
            latest_prices=current_prices,
            capital=MODEL_CAPITAL,
        )
        insert_cursor = await conn.execute(
            """
            INSERT OR IGNORE INTO momentum_portfolio_runs (
                run_date, strategy_version, status, entries_allowed,
                capital, allocated, reserve, candidates_total,
                selected_total, btc_above_sma50, btc_distance_pct,
                breadth_positive, breadth_total, breadth_pct
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data_date,
                STRATEGY_VERSION,
                model["status"],
                int(model["entries_allowed"]),
                model["capital"],
                model["allocated"],
                model["reserve"],
                model["candidates_total"],
                model["selected_total"],
                (
                    None
                    if model["btc_above_sma50"] is None
                    else int(model["btc_above_sma50"])
                ),
                model["btc_distance_pct"],
                model["breadth_positive"],
                model["breadth_total"],
                model["breadth_ratio"],
            ),
        )
        if insert_cursor.rowcount == 0:
            await conn.commit()
            return {
                "status": "already_recorded",
                "created": False,
                "run_date": data_date,
                "finalized": finalized,
            }
        for allocation in model["allocations"]:
            await conn.execute(
                """
                INSERT INTO momentum_portfolio_allocations (
                    run_date, ticker, rank, confidence,
                    momentum_score, volatility_pct, weight,
                    allocation, units, entry_price
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    data_date,
                    allocation["ticker"],
                    allocation["rank"],
                    allocation["confidence"],
                    allocation["momentum_score"],
                    allocation["volatility_pct"],
                    allocation["weight"],
                    allocation["target_allocation"],
                    allocation["units"],
                    allocation["current_price"],
                ),
            )
        await conn.commit()
        return {
            "status": "created",
            "created": True,
            "run_date": data_date,
            "finalized": finalized,
            "selected": model["selected_total"],
            "entries_allowed": model["entries_allowed"],
        }


async def fetch_momentum_portfolio_report(conn) -> dict[str, Any]:
    await ensure_momentum_portfolio_schema(conn)
    cursor = await conn.execute(
        """
        SELECT *
        FROM momentum_portfolio_runs
        ORDER BY run_date DESC
        """
    )
    runs = [dict(row) for row in await cursor.fetchall()]
    cursor = await conn.execute(
        """
        SELECT *
        FROM momentum_portfolio_allocations
        ORDER BY run_date DESC, rank
        """
    )
    allocation_rows = [dict(row) for row in await cursor.fetchall()]
    allocations_by_date: dict[str, list[dict[str, Any]]] = {}
    for row in allocation_rows:
        row["symbol"] = str(row["ticker"]).split("/", 1)[0]
        row["entry_price_display"] = _price(row["entry_price"])
        row["allocation_display"] = _money(row["allocation"])
        row["target_allocation_display"] = row["allocation_display"]
        row["weight_display"] = f"{float(row['weight']) * 100:.1f}%"
        row["volatility_display"] = f"{float(row['volatility_pct']):.2f}%"
        row["momentum_score_display"] = (
            f"{float(row['momentum_score']):+.2f}"
        )
        row["return_display"] = (
            f"{float(row['return_pct']):+.2f}%"
            if row.get("return_pct") is not None
            else "—"
        )
        row["cash_result_display"] = (
            f"${float(row['cash_result']):+.2f}"
            if row.get("cash_result") is not None
            else "—"
        )
        allocations_by_date.setdefault(str(row["run_date"]), []).append(row)

    completed = [run for run in runs if run.get("finalized_on")]
    cumulative_cash = sum(float(run["cash_result"] or 0) for run in completed)
    compounded = math.prod(
        1 + float(run["return_pct"] or 0) / 100
        for run in reversed(completed)
    ) - 1 if completed else 0.0
    positive = sum(float(run["cash_result"] or 0) > 0 for run in completed)
    negative = sum(float(run["cash_result"] or 0) < 0 for run in completed)

    for run in runs:
        run_date = str(run["run_date"])
        run["run_date_display"] = _date_label(run_date)
        run["finalized_on_display"] = _date_label(run.get("finalized_on"))
        run["allocations"] = allocations_by_date.get(run_date, [])
        run["entries_allowed"] = bool(run["entries_allowed"])
        run["capital_display"] = _money(run["capital"])
        run["allocated_display"] = _money(run["allocated"])
        run["reserve_display"] = _money(run["reserve"])
        run["cash_result_display"] = (
            f"${float(run['cash_result']):+.2f}"
            if run.get("cash_result") is not None
            else "—"
        )
        run["return_display"] = (
            f"{float(run['return_pct']):+.2f}%"
            if run.get("return_pct") is not None
            else "—"
        )
        run["btc_distance_display"] = (
            f"{float(run['btc_distance_pct']):+.2f}%"
            if run.get("btc_distance_pct") is not None
            else "—"
        )
        run["breadth_display"] = (
            f"{float(run['breadth_pct']):.1f}%"
            if run.get("breadth_pct") is not None
            else "—"
        )
        run["status_label"] = {
            "risk_on": "Покупки разрешены",
            "paused": "Капитал в USDT",
            "unavailable": "Недостаточно данных",
        }.get(str(run["status"]), str(run["status"]))
        run["status_detail"] = {
            "risk_on": "Оба условия рыночного фильтра выполнены.",
            "paused": (
                "Кандидаты сохранены для проверки, но модельный капитал "
                "остаётся в USDT."
            ),
            "unavailable": (
                "Новые покупки отключены до полного расчёта фильтра."
            ),
        }.get(str(run["status"]), "")

    current = next(
        (run for run in runs if not run.get("finalized_on")),
        None,
    )
    return {
        "current": current,
        "runs": runs[:30],
        "completed_total": len(completed),
        "positive_total": positive,
        "negative_total": negative,
        "flat_total": len(completed) - positive - negative,
        "win_rate": (
            positive / len(completed) * 100 if completed else 0.0
        ),
        "win_rate_display": (
            f"{positive / len(completed) * 100:.1f}%"
            if completed
            else "—"
        ),
        "cumulative_cash": cumulative_cash,
        "cumulative_cash_display": f"${cumulative_cash:+.2f}",
        "compounded_return_pct": compounded * 100,
        "compounded_return_display": f"{compounded * 100:+.2f}%",
        "strategy_version": STRATEGY_VERSION,
    }


def apply_momentum_live_prices(
    report: dict[str, Any],
    latest_prices: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Mark the open daily portfolio without changing its journal rows."""
    latest_prices = latest_prices or {}
    current = report.get("current")
    active_cash = 0.0
    active_positions = 0

    if current and not current.get("finalized_on"):
        for item in current.get("allocations") or []:
            invested = float(item.get("allocation") or 0)
            entry_price = float(item.get("entry_price") or 0)
            mark_price = latest_prices.get(str(item.get("ticker") or ""))
            try:
                mark_price = float(mark_price)
            except (TypeError, ValueError):
                mark_price = entry_price
            if not math.isfinite(mark_price) or mark_price <= 0:
                mark_price = entry_price

            return_pct = (
                (mark_price / entry_price - 1) * 100
                if invested > 0 and entry_price > 0
                else 0.0
            )
            cash_result = invested * return_pct / 100
            item["current_price"] = mark_price
            item["current_price_display"] = _price(mark_price)
            item["current_return_pct"] = return_pct
            item["current_return_display"] = f"{return_pct:+.2f}%"
            item["current_cash_result"] = cash_result
            item["current_cash_result_display"] = _signed_money(cash_result)
            if invested > 0:
                active_positions += 1
                active_cash += cash_result

    active_capital = float(current.get("capital") or 0) if current else 0.0
    active_return = (
        active_cash / active_capital * 100
        if active_capital > 0
        else 0.0
    )
    realized_cash = float(report.get("cumulative_cash") or 0)
    total_cash = realized_cash + active_cash
    completed_factor = 1 + float(
        report.get("compounded_return_pct") or 0
    ) / 100
    total_return = (
        completed_factor * (1 + active_return / 100) - 1
    ) * 100

    report.update({
        "active_positions_total": active_positions,
        "active_cash": round(active_cash, 2),
        "active_cash_display": _signed_money(active_cash),
        "active_return_pct": round(active_return, 4),
        "active_return_display": f"{active_return:+.2f}%",
        "total_cash": round(total_cash, 2),
        "total_cash_display": _signed_money(total_cash),
        "total_return_pct": round(total_return, 4),
        "total_return_display": f"{total_return:+.2f}%",
    })
    return report
