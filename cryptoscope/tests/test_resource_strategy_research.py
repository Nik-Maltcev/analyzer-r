from __future__ import annotations

import pandas as pd

from scripts.build_resource_strategy_research import ASSETS, HORIZONS, _exit_row


def test_resource_universe_excludes_oil_and_gasoline() -> None:
    text = " ".join([*ASSETS, *ASSETS.values()]).lower()
    assert "oil" not in text
    assert "gasoline" not in text


def test_every_month_from_one_through_twelve_is_tested() -> None:
    assert HORIZONS == tuple(range(1, 13))


def test_horizon_exit_is_never_before_target_date() -> None:
    frame = pd.DataFrame({
        "date": pd.to_datetime(["2024-01-31", "2024-02-29", "2024-03-01"]),
        "close": [100.0, 105.0, 106.0],
    })
    exit_row = _exit_row(frame, pd.Timestamp("2024-01-31"), 1)
    assert exit_row is not None
    assert exit_row["date"] == pd.Timestamp("2024-02-29")
