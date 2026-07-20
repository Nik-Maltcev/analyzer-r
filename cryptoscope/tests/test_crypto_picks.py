from app.core.crypto_picks import (
    aggregate_crypto_long_picks,
    build_price_progress,
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
    assert progress[0]["is_latest"] is True
    assert progress[1]["price_display"] == "$80"
    assert progress[1]["is_start"] is True
