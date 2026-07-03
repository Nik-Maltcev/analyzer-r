import pandas as pd

from app.api.ui_routes import _make_forecast_trades, _make_signal_cards


def _pair(**overrides):
    row = {
        "ticker_a": "DOT/USD",
        "ticker_b": "SOL/USD",
        "corr": 0.69,
        "is_coint": 0,
        "is_coint_stable": 0,
        "coint_stability": 0,
        "halflife": None,
        "score": 0.69,
        "z_now": -2.59,
        "z_forecast": -2.59,
        "signal": "Лонг DOT/USD / Шорт SOL/USD",
        "signal_type": "long_a",
        "strength": "Прогнозный",
        "signal_eligible": 1,
        "signal_started_at": "2026-06-24 10:00:00",
        "computed_at": "2026-06-24 10:00:00",
        "market_regime": "normal",
    }
    row.update(overrides)
    return pd.DataFrame([row])


def test_unvalidated_persisted_signal_is_rendered_as_observation():
    card = _make_signal_cards(_pair())[0]

    assert card["signal_type"] == "wait"
    assert card["signal_eligible"] is False
    assert card["strength"] == "Наблюдение"
    assert card["signal_expected_end_date"] is None


def test_unvalidated_persisted_signal_is_not_a_forecast_trade():
    assert _make_forecast_trades(_pair()) == []


def test_validated_signal_keeps_complete_timing():
    card = _make_signal_cards(
        _pair(
            is_coint=1,
            is_coint_stable=1,
            coint_stability=100,
            halflife=13,
        )
    )[0]

    assert card["signal_type"] == "long_a"
    assert card["signal_eligible"] is True
    assert card["halflife"] == 13
    assert card["signal_expected_end_date"] == "07.07.2026"
