from app.core.crypto_picks import (
    aggregate_crypto_long_picks,
    build_completed_crypto_history,
    build_crypto_signal_export,
    build_price_progress,
    select_crypto_sell_actions,
)


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
    assert report["summary"]["signals_total"] == 3
    assert report["summary"]["total_invested"] == 300.0
    assert report["summary"]["total_result"] == 20.0
    assert report["summary"]["portfolio_return_pct"] == 6.6667
