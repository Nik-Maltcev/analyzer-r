from app.core.crypto_picks import (
    aggregate_crypto_long_picks,
    build_completed_crypto_history,
    build_crypto_signal_export,
    build_crypto_window_summary,
    build_price_progress,
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
    assert summary["portfolio_return_pct"] == -1.25
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


def test_crypto_signal_export_excludes_pre_section_history_and_clamps_entry():
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
    assert report["rows"][0]["start_date"] == "2026-07-20"
    assert report["rows"][0]["result_date"] == "2026-07-22"
    assert report["rows"][0]["return_pct"] == 10.0


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
    assert position["result_date"] == "2026-07-26"
    assert position["stake"] == 100.0
    assert position["quantity"] == 1.0
    assert position["position_value"] == 120.0
    assert position["cash_result"] == 20.0
    assert position["return_pct"] == 20.0
    assert position["confidence"] == "Средняя"
