import pandas as pd

from app.api.ui_routes import (
    _annotate_leg_clusters,
    _make_forecast_trades,
    _make_signal_cards,
    _make_watchlist,
)


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
        "backtest_trades": 0,
        "backtest_validated": 0,
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


def test_forecast_does_not_invent_metrics_without_validated_backtest():
    trade = _make_forecast_trades(
        _pair(is_coint=1, is_coint_stable=1, halflife=13)
    )[0]

    assert trade["win_rate"] is None
    assert trade["avg_pnl_pct"] is None
    assert trade["n_similar"] == 0


def test_forecast_uses_stored_out_of_sample_metrics():
    trade = _make_forecast_trades(
        _pair(
            is_coint=1,
            is_coint_stable=1,
            halflife=13,
            backtest_trades=8,
            backtest_validated=1,
            backtest_win_rate=62.5,
            backtest_avg_pnl_pct=1.2,
            backtest_avg_hold_days=4.5,
        )
    )[0]

    assert trade["win_rate"] == 62.5
    assert trade["avg_pnl_pct"] == 1.2
    assert trade["avg_hold_days"] == 4.5


def _wait_card(**overrides):
    overrides.setdefault("signal_type", "wait")
    overrides.setdefault("signal", "Ждать")
    overrides.setdefault("strength", "Нет")
    return _make_signal_cards(_pair(**overrides))[0]


def test_watchlist_includes_blocked_near_signal_pair():
    card = _wait_card(z_now=-2.59, z_forecast=-2.59)

    watchlist = _make_watchlist([card])

    assert len(watchlist) == 1
    assert watchlist[0]["signal_type"] == "wait"
    assert watchlist[0]["z_now"] == -2.59


def test_watchlist_includes_forming_pair_below_threshold():
    card = _wait_card(z_now=1.7, z_forecast=None)

    assert _make_watchlist([card]) == [card]


def test_watchlist_excludes_calm_pairs():
    card = _wait_card(z_now=0.4, z_forecast=0.2)
    no_z = _wait_card(z_now=None, z_forecast=None)

    assert _make_watchlist([card, no_z]) == []


def test_watchlist_excludes_actionable_signals():
    card = _make_signal_cards(
        _pair(is_coint=1, is_coint_stable=1, halflife=13)
    )[0]

    assert card["signal_type"] == "long_a"
    assert _make_watchlist([card]) == []


def test_watchlist_sorted_by_extreme_z():
    cards = [
        _wait_card(ticker_a="AAA", ticker_b="BBB", z_now=1.6, z_forecast=None),
        _wait_card(ticker_a="CCC", ticker_b="DDD", z_now=-2.2, z_forecast=None),
        _wait_card(ticker_a="EEE", ticker_b="FFF", z_now=1.9, z_forecast=None),
    ]

    watchlist = _make_watchlist(cards)

    assert [w["ticker_a"] for w in watchlist] == ["CCC", "EEE", "AAA"]


def test_watchlist_respects_limit():
    cards = [
        _wait_card(ticker_a=f"T{i}", ticker_b="USD", z_now=1.5 + i * 0.1, z_forecast=None)
        for i in range(8)
    ]

    assert len(_make_watchlist(cards)) == 6


def test_leg_clusters_flag_shared_tickers():
    first = _wait_card(ticker_a="BTC/USD", ticker_b="ETH/USD")
    second = _wait_card(ticker_a="BTC/USD", ticker_b="SOL/USD")
    third = _wait_card(ticker_a="ADA/USD", ticker_b="XRP/USD")

    hotspots = _annotate_leg_clusters([first, second, third])

    assert hotspots == {"BTC/USD": 2}
    assert first["shared_legs"] == ["BTC/USD"]
    assert second["shared_legs"] == ["BTC/USD"]
    assert third["shared_legs"] == []


def test_leg_clusters_empty_without_overlap():
    first = _wait_card(ticker_a="BTC/USD", ticker_b="ETH/USD")
    second = _wait_card(ticker_a="ADA/USD", ticker_b="XRP/USD")

    hotspots = _annotate_leg_clusters([first, second])

    assert hotspots == {}
    assert first["shared_legs"] == []
    assert second["shared_legs"] == []
