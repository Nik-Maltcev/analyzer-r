from decimal import Decimal
import time

from app.core.crypto_picks import (
    build_crypto_signal_export,
    build_crypto_window_summary,
)
from app.data import mexc_market


def _reference_return(start_price, result_price):
    start = Decimal(str(start_price))
    result = Decimal(str(result_price))
    return (result / start - Decimal("1")) * Decimal("100")


def test_report_matches_independent_decimal_reference():
    report = build_crypto_signal_export(
        [
            {
                "id": 1,
                "scanner": "momentum",
                "ticker_a": "GAIN/USD",
                "direction": "long",
                "first_seen_date": "2026-07-20",
                "last_seen_date": "2026-07-24",
                "observation_count": 5,
                "status": "active",
            },
            {
                "id": 2,
                "scanner": "drawdown",
                "ticker_a": "LOSS/USD",
                "direction": "long",
                "first_seen_date": "2026-07-20",
                "last_seen_date": "2026-07-22",
                "ended_date": "2026-07-23",
                "observation_count": 3,
                "status": "closed",
            },
        ],
        {
            "GAIN/USD": [
                ("2026-07-20", 0.099),
                ("2026-07-21", 0.1019),
                ("2026-07-22", 0.1597),
                ("2026-07-23", 0.1253),
                ("2026-07-24", 0.12),
            ],
            "LOSS/USD": [
                ("2026-07-20", 10),
                ("2026-07-21", 9.5),
                ("2026-07-22", 9),
                ("2026-07-23", 8),
            ],
        },
        "2026-07-24",
        tracking_start_date="2026-07-20",
    )

    by_ticker = {row["ticker"]: row for row in report["rows"]}
    gain_reference = _reference_return(0.099, 0.12)
    loss_reference = _reference_return(10, 8)

    assert Decimal(str(by_ticker["GAIN/USD"]["return_pct"])).quantize(
        Decimal("0.0001")
    ) == gain_reference.quantize(Decimal("0.0001"))
    assert Decimal(str(by_ticker["LOSS/USD"]["return_pct"])).quantize(
        Decimal("0.0001")
    ) == loss_reference.quantize(Decimal("0.0001"))
    assert report["summary"]["total_result"] == round(
        float(gain_reference + loss_reference),
        2,
    )


def test_bootstrap_position_keeps_real_entry_in_every_output():
    report = build_crypto_signal_export(
        [
            {
                "id": 10,
                "scanner": "momentum",
                "ticker_a": "BAL/USD",
                "direction": "long",
                "first_seen_date": "2026-07-19",
                "last_seen_date": "2026-07-23",
                "observation_count": 5,
                "status": "active",
            },
        ],
        {
            "BAL/USD": [
                ("2026-07-19", 0.1054),
                ("2026-07-20", 0.099),
                ("2026-07-21", 0.1019),
                ("2026-07-22", 0.1597),
                ("2026-07-23", 0.1253),
            ],
        },
        "2026-07-23",
        tracking_start_date="2026-07-20",
    )

    row = report["rows"][0]
    history = report["weekly_summary"]["completed_history"][0]
    reference = float(_reference_return(0.1054, 0.1253))

    assert row["start_date"] == "2026-07-19"
    assert row["tracked_from_date"] == "2026-07-20"
    assert row["return_pct"] == round(reference, 4)
    assert history["start_price"] == row["start_price"]
    assert history["end_price"] == row["result_price"]
    assert history["return_pct"] == round(row["return_pct"], 2)
    assert history["cash_result"] == row["cash_result"]


def test_all_windows_reconcile_with_their_visible_history():
    rows = [
        {
            "position_id": "A",
            "status": "completed",
            "ticker": "A/USD",
            "start_date": "2026-07-10",
            "tracked_from_date": "2026-07-10",
            "result_date": "2026-07-14",
            "start_price": 100,
            "result_price": 110,
            "stake": 100,
            "quantity": 1,
            "return_pct": 10,
            "cash_result": 10,
            "confidence": "unknown",
            "symbol": "A",
            "scanner_labels": "Momentum",
            "held_days": 5,
        },
        {
            "position_id": "B",
            "status": "completed",
            "ticker": "B/USD",
            "start_date": "2026-07-18",
            "tracked_from_date": "2026-07-18",
            "result_date": "2026-07-22",
            "start_price": 100,
            "result_price": 95,
            "stake": 100,
            "quantity": 1,
            "return_pct": -5,
            "cash_result": -5,
            "confidence": "unknown",
            "symbol": "B",
            "scanner_labels": "Momentum",
            "held_days": 5,
        },
        {
            "position_id": "C",
            "status": "active",
            "ticker": "C/USD",
            "start_date": "2026-07-23",
            "tracked_from_date": "2026-07-23",
            "result_date": "2026-07-24",
            "start_price": 100,
            "result_price": 102,
            "stake": 100,
            "quantity": 1,
            "return_pct": 2,
            "cash_result": 2,
            "confidence": "unknown",
            "symbol": "C",
            "scanner_labels": "Drawdown",
            "held_days": 2,
        },
    ]

    for days in (7, 14, 30):
        summary = build_crypto_window_summary(
            rows,
            "2026-07-24",
            days=days,
        )
        history_cash = round(
            sum(row["cash_result"] for row in summary["completed_history"]),
            2,
        )
        assert history_cash == summary["realized_result"]
        assert round(
            summary["realized_result"] + summary["unrealized_result"],
            2,
        ) == summary["total_result"]
        assert summary["result_timeline"][-1]["result"] == summary["total_result"]


def test_live_marks_change_only_active_positions():
    report = build_crypto_signal_export(
        [
            {
                "id": 20,
                "scanner": "momentum",
                "ticker_a": "FIXED/USD",
                "direction": "long",
                "first_seen_date": "2026-07-20",
                "last_seen_date": "2026-07-24",
                "observation_count": 5,
                "status": "active",
            },
            {
                "id": 21,
                "scanner": "drawdown",
                "ticker_a": "LIVE/USD",
                "direction": "long",
                "first_seen_date": "2026-07-23",
                "last_seen_date": "2026-07-24",
                "observation_count": 2,
                "status": "active",
            },
        ],
        {
            "FIXED/USD": [
                ("2026-07-20", 100),
                ("2026-07-21", 101),
                ("2026-07-22", 102),
                ("2026-07-23", 103),
                ("2026-07-24", 110),
            ],
            "LIVE/USD": [
                ("2026-07-23", 100),
                ("2026-07-24", 90),
            ],
        },
        "2026-07-24",
        tracking_start_date="2026-07-20",
        active_marks={
            "FIXED/USD": 150,
            "LIVE/USD": 95,
        },
        active_mark_date="2026-07-24",
    )

    by_ticker = {row["ticker"]: row for row in report["rows"]}
    assert by_ticker["FIXED/USD"]["status"] == "completed"
    assert by_ticker["FIXED/USD"]["result_price"] == 110
    assert by_ticker["FIXED/USD"]["return_pct"] == 10
    assert by_ticker["LIVE/USD"]["status"] == "active"
    assert by_ticker["LIVE/USD"]["result_price"] == 95
    assert by_ticker["LIVE/USD"]["return_pct"] == -5


def test_mexc_snapshot_rejects_stale_quotes():
    original_map = mexc_market.TICKER_MAP
    original_prices = mexc_market.live_prices
    original_updates = mexc_market._last_update
    now = time.time()
    try:
        mexc_market.TICKER_MAP = {
            "FRESH/USD": ["FRESHUSDT"],
            "STALE/USD": ["STALEUSDT"],
        }
        mexc_market.live_prices = {
            "FRESHUSDT": 105.0,
            "STALEUSDT": 90.0,
        }
        mexc_market._last_update = {
            "FRESHUSDT": now,
            "STALEUSDT": now - 120,
        }

        prices, updated_at = mexc_market.get_crypto_live_snapshot(
            ["FRESH/USD", "STALE/USD"],
            updated_since=now - 5,
        )

        assert prices == {"FRESH/USD": 105.0}
        assert updated_at is not None
    finally:
        mexc_market.TICKER_MAP = original_map
        mexc_market.live_prices = original_prices
        mexc_market._last_update = original_updates
