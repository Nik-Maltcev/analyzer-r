from app.core.crypto_picks import (
    aggregate_crypto_long_picks,
    apply_crypto_confidence_admission,
    build_completed_crypto_history,
    build_crypto_signal_export,
    build_crypto_window_summary,
    build_price_progress,
    filter_crypto_rows_by_confidence,
    is_excluded_crypto_confidence,
    select_crypto_sell_actions,
)


def test_crypto_window_summary_uses_positions_opened_in_last_seven_days():
    summary = build_crypto_window_summary(
        [
            {
                "status": "completed",
                "start_date": "2026-07-17",
                "result_date": "2026-07-18",
                "stake": 100,
                "return_pct": 10,
                "cash_result": 10,
                "confidence": "Высокая",
            },
            {
                "status": "completed",
                "start_date": "2026-07-10",
                "result_date": "2026-07-16",
                "stake": 100,
                "return_pct": 20,
                "cash_result": 20,
                "confidence": "Средняя",
            },
            {
                "status": "active",
                "start_date": "2026-07-19",
                "result_date": "2026-07-23",
                "stake": 100,
                "return_pct": -5,
                "cash_result": -5,
                "confidence": "Высокая",
            },
            {
                "status": "closed_early",
                "start_date": "2026-07-20",
                "result_date": "2026-07-22",
                "stake": 100,
                "return_pct": -10,
                "cash_result": -10,
                "confidence": "Средняя",
            },
            {
                "status": "completed",
                "start_date": "2026-07-21",
                "result_date": "2026-07-23",
                "stake": 100,
                "return_pct": 0,
                "cash_result": 0,
                "confidence": "Низкая",
            },
        ],
        "2026-07-23",
    )

    assert summary["start_date"] == "2026-07-17"
    assert summary["end_date"] == "2026-07-23"
    assert summary["positions_total"] == 4
    assert summary["positions_active"] == 1
    assert summary["positions_completed"] == 3
    assert summary["positions_profitable"] == 1
    assert summary["positions_unprofitable"] == 1
    assert summary["positions_flat"] == 1
    assert summary["total_invested"] == 400
    assert summary["total_result"] == -5
    assert summary["realized_result"] == 0
    assert summary["unrealized_result"] == -5
    assert summary["realized_result_display"] == "$0.00"
    assert summary["unrealized_result_display"] == "-$5.00"
    assert summary["portfolio_return_pct"] == -1.25
    assert len(summary["completed_history"]) == 3
    assert sum(
        item["cash_result"] for item in summary["completed_history"]
    ) == summary["realized_result"]
    assert {
        item["close_reason"] for item in summary["completed_history"]
    } == {None, "signal_ended"}
    by_confidence = {
        item["label"]: item
        for item in summary["confidence_breakdown"]
    }
    assert by_confidence["Высокая"]["positions_total"] == 2
    assert by_confidence["Высокая"]["positions_completed"] == 1
    assert by_confidence["Высокая"]["positions_profitable"] == 1
    assert by_confidence["Высокая"]["positions_active"] == 1
    assert by_confidence["Высокая"]["win_rate"] == 100
    assert by_confidence["Высокая"]["realized_result"] == 10
    assert by_confidence["Средняя"]["positions_total"] == 1
    assert by_confidence["Средняя"]["positions_profitable"] == 0
    assert by_confidence["Средняя"]["win_rate"] == 0
    assert by_confidence["Средняя"]["realized_result"] == -10
    assert by_confidence["Низкая"]["positions_total"] == 1
    assert by_confidence["Низкая"]["positions_flat"] == 1
    assert by_confidence["Низкая"]["win_rate"] == 0
    assert "Без уровня" not in by_confidence


def test_crypto_window_summary_breaks_down_results_by_scanner():
    summary = build_crypto_window_summary(
        [
            {
                "status": "completed",
                "start_date": "2026-07-20",
                "result_date": "2026-07-22",
                "stake": 100,
                "return_pct": -6,
                "cash_result": -6,
                "scanners": ["Momentum"],
            },
            {
                "status": "completed",
                "start_date": "2026-07-21",
                "result_date": "2026-07-23",
                "stake": 100,
                "return_pct": 2,
                "cash_result": 2,
                "scanner_labels": "Drawdown",
            },
            {
                "status": "completed",
                "start_date": "2026-07-19",
                "result_date": "2026-07-22",
                "stake": 100,
                "return_pct": 10,
                "cash_result": 10,
                "scanners": ["Drawdown", "Momentum"],
            },
            {
                "status": "active",
                "ticker": "ACT/USD",
                "start_date": "2026-07-22",
                "result_date": "2026-07-24",
                "stake": 100,
                "start_price": 100,
                "quantity": 1,
                "return_pct": 3,
                "cash_result": 3,
                "scanner_label": "Momentum",
            },
            {
                "status": "completed",
                "start_date": "2026-07-20",
                "result_date": "2026-07-21",
                "stake": 100,
                "return_pct": 1,
                "cash_result": 1,
            },
        ],
        "2026-07-23",
        prices_by_ticker={
            "ACT/USD": [
                ("2026-07-22", 100),
                ("2026-07-23", 103),
            ],
        },
    )

    by_scanner = {
        item["label"]: item for item in summary["scanner_breakdown"]
    }
    assert [item["key"] for item in summary["scanner_breakdown"]] == [
        "momentum",
    ]
    momentum = by_scanner["Momentum"]
    assert momentum["positions_total"] == 3
    assert momentum["positions_completed"] == 2
    assert momentum["positions_active"] == 1
    assert momentum["positions_profitable"] == 1
    assert momentum["win_rate"] == 50
    assert momentum["realized_result"] == 4
    assert momentum["result_display"] == "+$4.00"
    assert "Drawdown" not in by_scanner


def test_crypto_window_summary_scanner_breakdown_handles_empty_input():
    summary = build_crypto_window_summary([], "2026-07-23")

    assert [
        (item["label"], item["positions_total"], item["result_display"])
        for item in summary["scanner_breakdown"]
    ] == [("Momentum", 0, "$0.00")]


def test_filter_crypto_rows_by_confidence_supports_multi_select():
    rows = [
        {"confidence": "Высокая", "cash_result": 1},
        {"confidence": "Средняя", "cash_result": 2},
        {"confidence": "Низкая", "cash_result": 3},
        {"confidence": "unknown", "cash_result": 4},
        {"cash_result": 5},
    ]

    assert [
        row["cash_result"]
        for row in filter_crypto_rows_by_confidence(rows, ["high", "medium"])
    ] == [1, 2]
    assert [
        row["cash_result"]
        for row in filter_crypto_rows_by_confidence(rows, ["low"])
    ] == [3]
    assert len(filter_crypto_rows_by_confidence(rows, [])) == 5
    assert len(filter_crypto_rows_by_confidence(rows, ["bogus"])) == 5


def test_crypto_window_summary_reconciles_with_confidence_filter():
    rows = [
        {
            "status": "completed",
            "start_date": "2026-07-20",
            "result_date": "2026-07-22",
            "stake": 100,
            "return_pct": 10,
            "cash_result": 10,
            "confidence": "Высокая",
        },
        {
            "status": "completed",
            "start_date": "2026-07-20",
            "result_date": "2026-07-22",
            "stake": 100,
            "return_pct": -6,
            "cash_result": -6,
            "confidence": "Низкая",
        },
    ]

    summary = build_crypto_window_summary(
        filter_crypto_rows_by_confidence(rows, ["high"]),
        "2026-07-23",
    )

    assert summary["positions_total"] == 1
    assert summary["realized_result"] == 10
    by_confidence = {
        item["label"]: item for item in summary["confidence_breakdown"]
    }
    assert by_confidence["Высокая"]["positions_total"] == 1
    assert "Низкая" not in by_confidence


def test_is_excluded_crypto_confidence_admits_only_medium_and_high():
    assert is_excluded_crypto_confidence("Низкая")
    assert is_excluded_crypto_confidence(None)
    assert is_excluded_crypto_confidence("")
    assert is_excluded_crypto_confidence("unknown")
    assert is_excluded_crypto_confidence("Без уровня")
    assert not is_excluded_crypto_confidence("Средняя")
    assert not is_excluded_crypto_confidence("Высокая")


def test_confidence_admission_is_frozen_when_position_enters_strategy():
    periods = [
        {
            "ticker_a": "IMPROVED/USD",
            "status": "active",
            "confidence": "Низкая",
            "strategy_admitted_date": "2026-07-26",
            "strategy_confidence": "Высокая",
        },
        {
            "ticker_a": "WEAKENED/USD",
            "status": "active",
            "confidence": "Высокая",
            "strategy_admitted_date": "2026-07-24",
            "strategy_confidence": "Высокая",
        },
        {
            "ticker_a": "NOT_ADMITTED/USD",
            "status": "active",
            "confidence": "Низкая",
            "strategy_admitted_date": None,
            "strategy_confidence": None,
        },
        {
            "ticker_a": "DONE_LOW/USD",
            "status": "closed",
            "confidence": "Низкая",
            "strategy_admitted_date": None,
            "strategy_confidence": None,
        },
        {
            "ticker_a": "DONE_HIGH/USD",
            "status": "completed",
            "confidence": "Высокая",
            "strategy_admitted_date": "2026-07-20",
            "strategy_confidence": "Средняя",
        },
    ]

    admitted = apply_crypto_confidence_admission(periods)
    by_ticker = {row["ticker_a"]: row for row in admitted}

    assert set(by_ticker) == {
        "IMPROVED/USD",
        "WEAKENED/USD",
        "DONE_HIGH/USD",
    }
    assert by_ticker["IMPROVED/USD"]["confidence"] == "Высокая"
    assert by_ticker["WEAKENED/USD"]["confidence"] == "Высокая"
    assert by_ticker["DONE_HIGH/USD"]["confidence"] == "Средняя"


def test_crypto_position_uses_admission_price_without_extending_raw_horizon():
    report = build_crypto_signal_export(
        [
            {
                "id": 90,
                "scanner": "momentum",
                "ticker_a": "TEST/USD",
                "direction": "long",
                "confidence": "Низкая",
                "strategy_admitted_date": "2026-07-22",
                "strategy_confidence": "Высокая",
                "first_seen_date": "2026-07-20",
                "last_seen_date": "2026-07-24",
                "observation_count": 5,
                "status": "active",
            },
        ],
        {
            "TEST/USD": [
                ("2026-07-20", 100),
                ("2026-07-21", 90),
                ("2026-07-22", 80),
                ("2026-07-23", 88),
                ("2026-07-24", 96),
            ],
        },
        "2026-07-24",
        tracking_start_date="2026-07-20",
    )

    position = report["rows"][0]
    assert position["start_date"] == "2026-07-22"
    assert position["start_price"] == 80
    assert position["result_date"] == "2026-07-24"
    assert position["result_price"] == 96
    assert position["return_pct"] == 20
    assert position["cash_result"] == 20
    assert position["confidence"] == "Высокая"


def test_confidence_breakdown_skips_empty_low_group():
    summary = build_crypto_window_summary(
        [
            {
                "status": "completed",
                "start_date": "2026-07-20",
                "result_date": "2026-07-22",
                "stake": 100,
                "return_pct": 4,
                "cash_result": 4,
                "confidence": "Средняя",
            },
        ],
        "2026-07-23",
    )

    assert [
        item["label"] for item in summary["confidence_breakdown"]
    ] == ["Средняя", "Высокая"]


def test_crypto_window_summary_supports_longer_windows():
    rows = [
        {
            "status": "completed",
            "ticker": "BTC/USD",
            "start_date": "2026-07-12",
            "result_date": "2026-07-16",
            "stake": 100,
            "start_price": 100,
            "quantity": 1,
            "return_pct": 5,
            "cash_result": 5,
            "confidence": "Высокая",
        },
        {
            "status": "active",
            "ticker": "ETH/USD",
            "start_date": "2026-07-22",
            "result_date": "2026-07-24",
            "stake": 100,
            "start_price": 100,
            "quantity": 1,
            "return_pct": -2,
            "cash_result": -2,
            "confidence": "Средняя",
        },
    ]
    prices = {
        "BTC/USD": [
            ("2026-07-12", 100),
            ("2026-07-13", 102),
            ("2026-07-14", 103),
            ("2026-07-15", 104),
            ("2026-07-16", 105),
        ],
        "ETH/USD": [
            ("2026-07-22", 100),
            ("2026-07-23", 99),
            ("2026-07-24", 98),
        ],
    }

    seven_days = build_crypto_window_summary(
        rows,
        "2026-07-24",
        days=7,
        prices_by_ticker=prices,
    )
    fourteen_days = build_crypto_window_summary(
        rows,
        "2026-07-24",
        days=14,
        prices_by_ticker=prices,
    )
    thirty_days = build_crypto_window_summary(
        rows,
        "2026-07-24",
        days=30,
        prices_by_ticker=prices,
    )

    assert seven_days["positions_total"] == 1
    assert fourteen_days["positions_total"] == 2
    assert thirty_days["positions_total"] == 2
    assert fourteen_days["total_result"] == 3
    assert seven_days["result_timeline"][-1]["result"] == -2
    assert fourteen_days["result_timeline"][-1]["result"] == 3


def test_crypto_window_summary_clamps_to_tracking_start():
    summary = build_crypto_window_summary(
        [
            {
                "status": "active",
                "ticker": "ETH/USD",
                "start_date": "2026-07-20",
                "result_date": "2026-07-24",
                "stake": 100,
                "start_price": 100,
                "quantity": 1,
                "return_pct": 4,
                "cash_result": 4,
                "confidence": "Высокая",
            },
        ],
        "2026-07-24",
        days=14,
        prices_by_ticker={
            "ETH/USD": [
                ("2026-07-20", 100),
                ("2026-07-24", 104),
            ],
        },
        tracking_start_date="2026-07-20",
    )

    assert summary["days"] == 14
    assert summary["available_days"] == 5
    assert summary["is_partial_window"] is True
    assert summary["start_date"] == "2026-07-20"
    assert summary["start_date_display"] == "20.07"
    assert len(summary["result_timeline"]) == 5


def test_crypto_picks_merge_scanners_and_use_shortest_horizon():
    picks = aggregate_crypto_long_picks(
        {
            "momentum": [
                {
                    "ticker": "ETH/USD",
                    "recommendation_class": "long",
                    "confidence": "Средняя",
                    "signal_age_days": 1,
                    "signal_remaining_days": 4,
                    "signal_first_seen_date": "20.07.2026",
                    "signal_within_horizon": True,
                }
            ],
            "drawdown": [
                {
                    "ticker": "ETH/USD",
                    "recommendation_class": "long",
                    "confidence": "Высокая",
                    "signal_age_days": 3,
                    "signal_remaining_days": 7,
                    "signal_first_seen_date": "18.07.2026",
                    "signal_within_horizon": True,
                },
                {
                    "ticker": "BTC/USD",
                    "recommendation_class": "wait",
                    "signal_age_days": 1,
                    "signal_remaining_days": 9,
                },
            ],
        },
        {"ETH/USD": 3456.78, "BTC/USD": 120000},
    )

    assert len(picks) == 1
    assert picks[0]["symbol"] == "ETH"
    assert picks[0]["scanner_count"] == 2
    assert picks[0]["confidence"] == "Высокая"
    assert picks[0]["signal_age_days"] == 3
    assert picks[0]["signal_remaining_days"] == 4
    assert picks[0]["signal_first_seen_date"] == "18.07.2026"
    assert picks[0]["action_text"] == "планово продать примерно через 4 дня"


def test_crypto_picks_ignore_expired_and_non_directional_scanners():
    picks = aggregate_crypto_long_picks(
        {
            "momentum": [
                {
                    "ticker": "SOL/USD",
                    "recommendation_class": "long",
                    "signal_within_horizon": False,
                }
            ],
            "corrbreak": [
                {
                    "ticker": "XRP/USD",
                    "recommendation_class": "long",
                    "signal_within_horizon": True,
                }
            ],
        },
        {},
    )

    assert picks == []


def test_price_progress_starts_on_signal_date_and_tracks_daily_change():
    progress = build_price_progress(
        [
            ("2026-07-18", 70),
            ("2026-07-19", 80),
            ("2026-07-20", 100),
        ],
        "19.07.2026",
    )

    assert [day["date_display"] for day in progress] == ["20.07", "19.07"]
    assert progress[0]["price_display"] == "$100"
    assert progress[0]["change_display"] == "+25.00%"
    assert progress[0]["day_change_display"] == "+25.00%"
    assert progress[0]["is_latest"] is True
    assert progress[1]["price_display"] == "$80"
    assert progress[1]["day_change_display"] == "+0.00%"
    assert progress[1]["is_start"] is True


def test_completed_crypto_history_uses_fixed_scanner_horizon():
    history = build_completed_crypto_history(
        [
            {
                "id": 1,
                "scanner": "momentum",
                "ticker_a": "LTC/USD",
                "direction": "long",
                "first_seen_date": "2026-07-01",
                "last_seen_date": "2026-07-08",
                "observation_count": 8,
            },
            {
                "id": 2,
                "scanner": "drawdown",
                "ticker_a": "ETH/USD",
                "direction": "long",
                "first_seen_date": "2026-07-01",
                "last_seen_date": "2026-07-08",
                "observation_count": 8,
            },
        ],
        {
            "LTC/USD": [
                (f"2026-07-{day:02d}", price)
                for day, price in enumerate(
                    [100, 101, 102, 103, 110, 90, 80, 70],
                    start=1,
                )
            ],
            "ETH/USD": [
                (f"2026-07-{day:02d}", 100 + day)
                for day in range(1, 9)
            ],
        },
    )

    assert len(history) == 1
    assert history[0]["symbol"] == "LTC"
    assert history[0]["horizon_days"] == 5
    assert history[0]["end_date_display"] == "05.07.2026"
    assert history[0]["start_price_display"] == "$100"
    assert history[0]["end_price_display"] == "$110"
    assert history[0]["return_display"] == "+10.00%"
    assert history[0]["cash_result"] == 10.0
    assert history[0]["cash_result_display"] == "+$10.00"
    assert history[0]["is_profitable"] is True


def test_completed_crypto_history_keeps_negative_results():
    history = build_completed_crypto_history(
        [
            {
                "scanner": "momentum",
                "ticker_a": "SOL/USD",
                "direction": "long",
                "first_seen_date": "2026-07-01",
                "last_seen_date": "2026-07-05",
                "observation_count": 5,
            }
        ],
        {
            "SOL/USD": [
                ("2026-07-01", 100),
                ("2026-07-02", 98),
                ("2026-07-03", 95),
                ("2026-07-04", 93),
                ("2026-07-05", 90),
            ]
        },
    )

    assert history[0]["return_display"] == "-10.00%"
    assert history[0]["cash_result"] == -10.0
    assert history[0]["cash_result_display"] == "-$10.00"
    assert history[0]["result_label"] == "В минусе"
    assert history[0]["is_profitable"] is False


def test_sell_actions_only_include_signals_completed_today():
    history = [
        {"ticker": "LTC/USD", "end_date": "2026-07-21"},
        {"ticker": "ETH/USD", "end_date": "2026-07-20"},
    ]

    actions = select_crypto_sell_actions(history, "21.07.2026")

    assert [item["ticker"] for item in actions] == ["LTC/USD"]


def test_sell_actions_include_auto_close_but_not_manual_close():
    history = [
        {
            "ticker": "XMR/USD",
            "end_date": "2026-07-24",
            "close_reason": "manual",
        },
        {
            "ticker": "LTC/USD",
            "end_date": "2026-07-24",
            "close_reason": "auto_30_daily",
        },
    ]

    actions = select_crypto_sell_actions(history, "2026-07-24")

    assert [item["ticker"] for item in actions] == ["LTC/USD"]


def test_crypto_signal_export_keeps_manual_close_price_fixed():
    report = build_crypto_signal_export(
        [
            {
                "id": 20,
                "scanner": "momentum",
                "ticker_a": "XMR/USD",
                "direction": "long",
                "first_seen_date": "2026-07-20",
                "last_seen_date": "2026-07-22",
                "observation_count": 3,
                "status": "suppressed",
                "ended_date": "2026-07-22",
                "close_reason": "manual",
                "closed_price": 125,
                "confidence": "Высокая",
            }
        ],
        {
            "XMR/USD": [
                ("2026-07-20", 100),
                ("2026-07-21", 110),
                ("2026-07-22", 125),
                ("2026-07-23", 200),
            ],
        },
        "2026-07-23",
        tracking_start_date="2026-07-20",
    )

    assert report["summary"]["positions_total"] == 1
    position = report["rows"][0]
    assert position["status"] == "closed_manual"
    assert position["result_date"] == "2026-07-22"
    assert position["result_price"] == 125
    assert position["return_pct"] == 25
    assert position["cash_result"] == 25
    assert report["summary"]["realized_result"] == 25
    assert report["summary"]["unrealized_result"] == 0


def test_crypto_signal_export_includes_completed_closed_and_active_results():
    report = build_crypto_signal_export(
        [
            {
                "id": 1,
                "scanner": "momentum",
                "ticker_a": "LTC/USD",
                "direction": "long",
                "first_seen_date": "2026-07-01",
                "last_seen_date": "2026-07-05",
                "observation_count": 5,
                "status": "closed",
                "ended_date": "2026-07-06",
            },
            {
                "id": 2,
                "scanner": "drawdown",
                "ticker_a": "ETH/USD",
                "direction": "long",
                "first_seen_date": "2026-07-01",
                "last_seen_date": "2026-07-03",
                "observation_count": 3,
                "status": "closed",
                "ended_date": "2026-07-04",
            },
            {
                "id": 3,
                "scanner": "momentum",
                "ticker_a": "SOL/USD",
                "direction": "long",
                "first_seen_date": "2026-07-03",
                "last_seen_date": "2026-07-05",
                "observation_count": 3,
                "status": "active",
            },
        ],
        {
            "LTC/USD": [
                ("2026-07-01", 100),
                ("2026-07-02", 102),
                ("2026-07-03", 104),
                ("2026-07-04", 106),
                ("2026-07-05", 110),
                ("2026-07-06", 90),
            ],
            "ETH/USD": [
                ("2026-07-01", 100),
                ("2026-07-02", 95),
                ("2026-07-03", 92),
                ("2026-07-04", 90),
                ("2026-07-05", 80),
            ],
            "SOL/USD": [
                ("2026-07-03", 50),
                ("2026-07-04", 55),
                ("2026-07-05", 60),
            ],
        },
        "2026-07-05",
        tracking_start_date="2026-07-01",
    )

    rows = {item["ticker"]: item for item in report["rows"]}
    assert rows["LTC/USD"]["status"] == "completed"
    assert rows["LTC/USD"]["result_date"] == "2026-07-05"
    assert rows["LTC/USD"]["return_pct"] == 10.0
    assert rows["ETH/USD"]["status"] == "closed_early"
    assert rows["ETH/USD"]["result_date"] == "2026-07-04"
    assert rows["ETH/USD"]["return_pct"] == -10.0
    assert rows["SOL/USD"]["status"] == "active"
    assert rows["SOL/USD"]["return_pct"] == 20.0
    assert report["summary"]["positions_total"] == 3
    assert report["summary"]["total_invested"] == 300.0
    assert report["summary"]["total_result"] == 20.0
    assert report["summary"]["portfolio_return_pct"] == 6.6667
    weekly = report["weekly_summary"]
    assert weekly["positions_completed"] == 2
    assert len(weekly["completed_history"]) == 2
    assert weekly["realized_result"] == 0
    assert sum(
        item["cash_result"] for item in weekly["completed_history"]
    ) == weekly["realized_result"]
    assert {
        item["close_reason"] for item in weekly["completed_history"]
    } == {None, "signal_ended"}


def test_crypto_signal_export_excludes_old_history_without_rewriting_entry():
    report = build_crypto_signal_export(
        [
            {
                "id": 1,
                "scanner": "momentum",
                "ticker_a": "OLD/USD",
                "direction": "long",
                "first_seen_date": "2026-07-01",
                "last_seen_date": "2026-07-05",
                "observation_count": 5,
                "status": "closed",
                "ended_date": "2026-07-06",
            },
            {
                "id": 2,
                "scanner": "momentum",
                "ticker_a": "LIVE/USD",
                "direction": "long",
                "first_seen_date": "2026-07-18",
                "last_seen_date": "2026-07-22",
                "observation_count": 5,
                "status": "active",
            },
        ],
        {
            "OLD/USD": [
                (f"2026-07-{day:02d}", 100 + day)
                for day in range(1, 7)
            ],
            "LIVE/USD": [
                ("2026-07-18", 80),
                ("2026-07-19", 90),
                ("2026-07-20", 100),
                ("2026-07-21", 105),
                ("2026-07-22", 110),
            ],
        },
        "2026-07-22",
        tracking_start_date="2026-07-20",
    )

    assert [item["ticker"] for item in report["rows"]] == ["LIVE/USD"]
    assert report["rows"][0]["start_date"] == "2026-07-18"
    assert report["rows"][0]["tracked_from_date"] == "2026-07-20"
    assert report["rows"][0]["result_date"] == "2026-07-22"
    assert report["rows"][0]["return_pct"] == 37.5
    assert report["weekly_summary"]["positions_total"] == 1


def test_crypto_signal_export_merges_overlapping_scanners_into_one_position():
    report = build_crypto_signal_export(
        [
            {
                "id": 10,
                "scanner": "momentum",
                "ticker_a": "XMR/USD",
                "direction": "long",
                "first_seen_date": "2026-07-20",
                "last_seen_date": "2026-07-24",
                "observation_count": 5,
                "status": "closed",
                "ended_date": "2026-07-25",
                "confidence": "Средняя",
            },
            {
                "id": 11,
                "scanner": "drawdown",
                "ticker_a": "XMR/USD",
                "direction": "long",
                "first_seen_date": "2026-07-22",
                "last_seen_date": "2026-07-25",
                "observation_count": 4,
                "status": "closed",
                "ended_date": "2026-07-26",
                "confidence": "Высокая",
            },
        ],
        {
            "XMR/USD": [
                ("2026-07-20", 100),
                ("2026-07-21", 102),
                ("2026-07-22", 104),
                ("2026-07-23", 106),
                ("2026-07-24", 108),
                ("2026-07-25", 110),
                ("2026-07-26", 120),
            ],
        },
        "2026-07-26",
        tracking_start_date="2026-07-20",
    )

    assert report["summary"]["positions_total"] == 1
    position = report["rows"][0]
    assert position["ticker"] == "XMR/USD"
    assert position["scanner_labels"] == "Drawdown + Momentum"
    assert position["start_date"] == "2026-07-20"
    assert position["result_date"] == "2026-07-24"
    assert position["stake"] == 100.0
    assert position["quantity"] == 1.0
    assert position["position_value"] == 108.0
    assert position["cash_result"] == 8.0
    assert position["return_pct"] == 8.0
    assert position["confidence"] == "Средняя"


def test_crypto_signal_export_keeps_completed_trade_before_repeat_signal():
    report = build_crypto_signal_export(
        [
            {
                "id": 30,
                "scanner": "momentum",
                "ticker_a": "BAL/USD",
                "direction": "long",
                "first_seen_date": "2026-07-19",
                "last_seen_date": "2026-07-23",
                "observation_count": 5,
                "status": "closed",
                "ended_date": "2026-07-24",
            },
            {
                "id": 31,
                "scanner": "momentum",
                "ticker_a": "BAL/USD",
                "direction": "long",
                "first_seen_date": "2026-07-23",
                "last_seen_date": "2026-07-24",
                "observation_count": 2,
                "status": "active",
            },
        ],
        {
            "BAL/USD": [
                ("2026-07-19", 100),
                ("2026-07-20", 102),
                ("2026-07-21", 105),
                ("2026-07-22", 110),
                ("2026-07-23", 118),
                ("2026-07-24", 120),
            ],
        },
        "2026-07-24",
        tracking_start_date="2026-07-19",
    )

    assert report["summary"]["positions_total"] == 2
    completed = next(
        item for item in report["rows"] if item["status"] == "completed"
    )
    active = next(
        item for item in report["rows"] if item["status"] == "active"
    )
    assert completed["ticker"] == "BAL/USD"
    assert completed["start_date"] == "2026-07-19"
    assert completed["result_date"] == "2026-07-23"
    assert completed["cash_result"] == 18.0
    assert active["start_date"] == "2026-07-23"
    assert active["result_date"] == "2026-07-24"
    completed_history = report["weekly_summary"]["completed_history"]
    assert len(completed_history) == 1
    assert completed_history[0]["cash_result"] == 18.0


def test_active_confirmation_does_not_reopen_completed_position():
    report = build_crypto_signal_export(
        [
            {
                "id": 40,
                "scanner": "momentum",
                "ticker_a": "BAL/USD",
                "direction": "long",
                "first_seen_date": "2026-07-19",
                "last_seen_date": "2026-07-23",
                "observation_count": 5,
                "status": "active",
            },
            {
                "id": 41,
                "scanner": "drawdown",
                "ticker_a": "BAL/USD",
                "direction": "long",
                "first_seen_date": "2026-07-21",
                "last_seen_date": "2026-07-24",
                "observation_count": 4,
                "status": "active",
            },
        ],
        {
            "BAL/USD": [
                ("2026-07-19", 100),
                ("2026-07-20", 102),
                ("2026-07-21", 105),
                ("2026-07-22", 110),
                ("2026-07-23", 118),
                ("2026-07-24", 120),
            ],
        },
        "2026-07-24",
        tracking_start_date="2026-07-19",
    )

    assert report["summary"]["positions_total"] == 1
    position = report["rows"][0]
    assert position["status"] == "completed"
    assert position["result_date"] == "2026-07-23"
    assert position["cash_result"] == 18.0
    assert report["summary"]["realized_result"] == 18.0
    assert report["summary"]["unrealized_result"] == 0.0


def test_early_drawdown_end_does_not_cut_completed_momentum_trade():
    report = build_crypto_signal_export(
        [
            {
                "id": 50,
                "scanner": "momentum",
                "ticker_a": "BAL/USD",
                "direction": "long",
                "first_seen_date": "2026-07-19",
                "last_seen_date": "2026-07-23",
                "observation_count": 5,
                "status": "active",
            },
            {
                "id": 51,
                "scanner": "drawdown",
                "ticker_a": "BAL/USD",
                "direction": "long",
                "first_seen_date": "2026-07-20",
                "last_seen_date": "2026-07-21",
                "observation_count": 2,
                "status": "closed",
                "ended_date": "2026-07-22",
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

    assert report["summary"]["positions_total"] == 1
    position = report["rows"][0]
    assert position["status"] == "completed"
    assert position["start_date"] == "2026-07-19"
    assert position["tracked_from_date"] == "2026-07-20"
    assert position["result_date"] == "2026-07-23"
    assert position["start_price"] == 0.1054
    assert position["result_price"] == 0.1253
    assert position["return_pct"] == 18.8805
    assert position["cash_result"] == 18.88
    assert report["weekly_summary"]["positions_total"] == 1
