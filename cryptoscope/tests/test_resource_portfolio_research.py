from __future__ import annotations

import numpy as np
import pandas as pd

from scripts.build_resource_portfolio_research import _cap_weights, _return_series
from app.api.long_term import _resource_risk_decision


def test_capped_weights_sum_to_one_and_respect_cap() -> None:
    weights = _cap_weights(pd.Series([10.0, 4.0, 3.0, 2.0, 1.0]), cap=0.30)
    assert np.isclose(weights.sum(), 1.0)
    assert weights.max() <= 0.3000001


def test_new_weight_is_applied_only_next_month() -> None:
    dates = pd.to_datetime(["2024-01-31", "2024-02-29", "2024-03-31"])
    monthly = pd.DataFrame({"A": [100.0, 110.0, 121.0]}, index=dates)
    weights = {"test": pd.DataFrame({"A": [0.0, 1.0, 1.0]}, index=dates)}
    returns, _, _ = _return_series(monthly, weights)
    assert np.isclose(returns.loc[dates[1], "test"], 0.0)
    assert np.isclose(returns.loc[dates[2], "test"], 0.099)


def test_plain_risk_decisions_use_the_agreed_thresholds() -> None:
    assert _resource_risk_decision(70, 55, -25, 20)[0] == "consider"
    assert _resource_risk_decision(72.12, 41.82, -28.51, 13) == (
        "limited", "Только небольшой суммой", 25_000
    )
    assert _resource_risk_decision(90, 90, -50, 30)[0] == "wait"
