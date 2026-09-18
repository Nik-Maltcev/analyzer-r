"""Transparent long-horizon strategy research without look-ahead execution."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timedelta
from statistics import NormalDist
from typing import Any

import numpy as np
import pandas as pd

RESEARCH_VERSION = "long-term-fixed-rules-v2-30"
BOLLINGER_LIVE_VERSION = "bollinger-recovery-monthly-v1"
DEPOSIT_RESEARCH_VERSION = "long-term-investment-weekly-v1"
BOLLINGER_STRATEGY_KEY = "bollinger_recovery"
BOLLINGER_HORIZON_DAYS = 365
BOLLINGER_TARGET_RETURN = 0.15
BOLLINGER_ROUND_TRIP_COST = 0.004


@dataclass(frozen=True)
class LongTermStrategy:
    key: str
    label: str
    family: str


@dataclass(frozen=True)
class LongTermDepositPlan:
    key: str
    label: str
    horizon_days: int
    target_return: float


DEPOSIT_PLANS = (
    LongTermDepositPlan("3m-10", "3 месяца · минимум +10%", 90, 0.10),
    LongTermDepositPlan("3m-15", "3 месяца · минимум +15%", 90, 0.15),
    LongTermDepositPlan("6m-10", "6 месяцев · минимум +10%", 180, 0.10),
    LongTermDepositPlan("6m-15", "6 месяцев · минимум +15%", 180, 0.15),
    LongTermDepositPlan("12m-10", "12 месяцев · минимум +10%", 365, 0.10),
    LongTermDepositPlan("12m-15", "12 месяцев · минимум +15%", 365, 0.15),
    LongTermDepositPlan("12m-20", "12 месяцев · минимум +20%", 365, 0.20),
)


STRATEGIES = (
    LongTermStrategy("buy_hold", "Buy & Hold Benchmark", "benchmark"),
    LongTermStrategy("trend_price_sma100", "Price above SMA 100", "trend"),
    LongTermStrategy("trend_price_sma200", "Price above SMA 200", "trend"),
    LongTermStrategy("trend", "SMA 50/200 + Momentum", "trend"),
    LongTermStrategy("trend_sma100_200", "SMA 100/200", "trend"),
    LongTermStrategy("trend_ema50_200", "EMA 50/200", "trend"),
    LongTermStrategy("trend_ema100_200", "EMA 100/200", "trend"),
    LongTermStrategy("trend_triple_ma", "Triple Moving Average", "trend"),
    LongTermStrategy("trend_breakout_100", "100-Day Range Breakout", "trend"),
    LongTermStrategy("trend_breakout_200", "200-Day Range Breakout", "trend"),
    LongTermStrategy("momentum_90", "90-Day Absolute Momentum", "momentum"),
    LongTermStrategy("momentum_180", "180-Day Absolute Momentum", "momentum"),
    LongTermStrategy("momentum_270", "270-Day Absolute Momentum", "momentum"),
    LongTermStrategy("momentum_365", "365-Day Absolute Momentum", "momentum"),
    LongTermStrategy("momentum_dual_90_180", "Dual Momentum 90/180", "momentum"),
    LongTermStrategy("momentum_dual_180_365", "Dual Momentum 180/365", "momentum"),
    LongTermStrategy("momentum_rank_90", "Top Relative Momentum 90", "momentum"),
    LongTermStrategy("momentum", "Top Relative Momentum 180", "momentum"),
    LongTermStrategy("momentum_rank_365", "Top Relative Momentum 365", "momentum"),
    LongTermStrategy("momentum_composite", "Composite Momentum", "momentum"),
    LongTermStrategy("drawdown_recovery_20", "20% Drawdown Recovery", "reversal"),
    LongTermStrategy("drawdown_recovery", "25% Drawdown Recovery", "reversal"),
    LongTermStrategy("drawdown_recovery_30", "30% Drawdown Recovery", "reversal"),
    LongTermStrategy("drawdown_recovery_40", "40% Drawdown Recovery", "reversal"),
    LongTermStrategy("rsi14_recovery", "RSI 14 Recovery", "reversal"),
    LongTermStrategy("rsi28_recovery", "RSI 28 Recovery", "reversal"),
    LongTermStrategy("bollinger_recovery", "Bollinger Recovery", "reversal"),
    LongTermStrategy("low_vol_trend", "Low-Volatility Trend", "risk"),
    LongTermStrategy("vol_contraction_trend", "Volatility Contraction Trend", "risk"),
    LongTermStrategy("breadth_trend", "Broad-Market Trend", "risk"),
)


def wilson_interval(successes: int, observations: int, z: float = 1.96) -> tuple[float, float]:
    """Return a Wilson score confidence interval for a binomial hit rate."""
    if observations <= 0:
        return 0.0, 0.0
    probability = successes / observations
    denominator = 1 + z**2 / observations
    centre = probability + z**2 / (2 * observations)
    margin = z * math.sqrt(
        probability * (1 - probability) / observations
        + z**2 / (4 * observations**2)
    )
    return (
        max(0.0, (centre - margin) / denominator),
        min(1.0, (centre + margin) / denominator),
    )


def _prepare_features(prices: pd.DataFrame) -> pd.DataFrame:
    required = {"ticker", "date", "close"}
    missing = required - set(prices.columns)
    if missing:
        raise ValueError(f"Missing long-term price columns: {sorted(missing)}")

    frame = prices.loc[:, ["ticker", "date", "close"]].copy()
    frame["date"] = pd.to_datetime(frame["date"], utc=True).dt.tz_localize(None)
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame = frame.dropna().sort_values(["ticker", "date"])
    frame = frame.drop_duplicates(["ticker", "date"], keep="last")
    grouped = frame.groupby("ticker", group_keys=False)["close"]
    for days in (30, 90, 180, 270, 365):
        frame[f"return_{days}"] = grouped.pct_change(days)
    for days in (20, 50, 100, 200):
        frame[f"sma_{days}"] = grouped.transform(
            lambda values, window=days: values.rolling(window).mean()
        )
    for days in (50, 100, 200):
        frame[f"ema_{days}"] = grouped.transform(
            lambda values, span=days: values.ewm(span=span, adjust=False).mean()
        )
    frame["high_365"] = grouped.transform(lambda values: values.rolling(365).max())
    frame["prior_high_100"] = grouped.transform(
        lambda values: values.shift(1).rolling(100).max()
    )
    frame["prior_high_200"] = grouped.transform(
        lambda values: values.shift(1).rolling(200).max()
    )
    frame["drawdown_365"] = frame["close"] / frame["high_365"] - 1
    frame["recent_worst_drawdown"] = frame.groupby("ticker", group_keys=False)[
        "drawdown_365"
    ].transform(lambda values: values.rolling(90).min())
    log_return = grouped.transform(lambda values: np.log(values / values.shift(1)))
    frame["volatility_30"] = log_return.groupby(frame["ticker"]).transform(
        lambda values: values.rolling(30).std(ddof=0) * math.sqrt(365)
    )
    frame["volatility_90"] = log_return.groupby(frame["ticker"]).transform(
        lambda values: values.rolling(90).std(ddof=0) * math.sqrt(365)
    )
    frame["volatility_rank"] = frame.groupby("date")["volatility_90"].rank(pct=True)
    for days in (90, 180, 365):
        frame[f"momentum_rank_{days}"] = frame.groupby("date")[
            f"return_{days}"
        ].rank(pct=True)

    delta = grouped.diff()
    gains = delta.clip(lower=0)
    losses = -delta.clip(upper=0)
    for days in (14, 28):
        average_gain = gains.groupby(frame["ticker"]).transform(
            lambda values, window=days: values.rolling(window).mean()
        )
        average_loss = losses.groupby(frame["ticker"]).transform(
            lambda values, window=days: values.rolling(window).mean()
        )
        strength = average_gain / average_loss.replace(0, np.nan)
        frame[f"rsi_{days}"] = 100 - 100 / (1 + strength)
        frame[f"rsi_{days}_low_30"] = frame.groupby("ticker")[f"rsi_{days}"].transform(
            lambda values: values.rolling(30).min()
        )

    rolling_sd_50 = grouped.transform(lambda values: values.rolling(50).std(ddof=0))
    frame["bollinger_z_50"] = (frame["close"] - frame["sma_50"]) / rolling_sd_50
    frame["bollinger_z_50_low_30"] = frame.groupby("ticker")[
        "bollinger_z_50"
    ].transform(lambda values: values.rolling(30).min())
    breadth = frame.groupby("date").apply(
        lambda rows: float((rows["close"] > rows["sma_200"]).mean()),
        include_groups=False,
    )
    frame["breadth_200"] = frame["date"].map(breadth)

    frame["signal_buy_hold"] = True
    frame["signal_trend_price_sma100"] = frame["close"] > frame["sma_100"]
    frame["signal_trend_price_sma200"] = frame["close"] > frame["sma_200"]
    frame["signal_trend"] = (
        (frame["close"] > frame["sma_200"])
        & (frame["sma_50"] > frame["sma_200"])
        & (frame["return_90"] > 0.05)
        & (frame["return_180"] > 0.10)
    )
    frame["signal_trend_sma100_200"] = (
        (frame["close"] > frame["sma_200"])
        & (frame["sma_100"] > frame["sma_200"])
    )
    frame["signal_trend_ema50_200"] = (
        (frame["close"] > frame["ema_200"])
        & (frame["ema_50"] > frame["ema_200"])
    )
    frame["signal_trend_ema100_200"] = (
        (frame["close"] > frame["ema_200"])
        & (frame["ema_100"] > frame["ema_200"])
    )
    frame["signal_trend_triple_ma"] = (
        (frame["close"] > frame["sma_20"])
        & (frame["sma_20"] > frame["sma_50"])
        & (frame["sma_50"] > frame["sma_200"])
    )
    frame["signal_trend_breakout_100"] = frame["close"] >= frame["prior_high_100"]
    frame["signal_trend_breakout_200"] = frame["close"] >= frame["prior_high_200"]

    for days in (90, 180, 270, 365):
        frame[f"signal_momentum_{days}"] = frame[f"return_{days}"] > 0
    frame["signal_momentum_dual_90_180"] = (
        (frame["return_90"] > 0) & (frame["return_180"] > 0)
    )
    frame["signal_momentum_dual_180_365"] = (
        (frame["return_180"] > 0) & (frame["return_365"] > 0)
    )
    frame["signal_momentum_rank_90"] = (
        (frame["momentum_rank_90"] >= 0.80) & (frame["return_90"] > 0)
    )
    frame["signal_momentum"] = (
        (frame["momentum_rank_180"] >= 0.80)
        & (frame["return_90"] > 0)
        & (frame["close"] > frame["sma_200"])
    )
    frame["signal_momentum_rank_365"] = (
        (frame["momentum_rank_365"] >= 0.80) & (frame["return_365"] > 0)
    )
    composite_momentum = (
        frame["momentum_rank_90"]
        + frame["momentum_rank_180"]
        + frame["momentum_rank_365"]
    ) / 3
    frame["signal_momentum_composite"] = (
        (composite_momentum >= 0.75)
        & (frame["return_90"] > 0)
        & (frame["return_180"] > 0)
    )

    recovery_base = (frame["return_30"] > 0.05) & (frame["close"] > frame["sma_50"])
    frame["signal_drawdown_recovery_20"] = (
        (frame["recent_worst_drawdown"] <= -0.20) & recovery_base
    )
    frame["signal_drawdown_recovery"] = (
        (frame["recent_worst_drawdown"] <= -0.25)
        & (frame["return_30"] >= 0.10)
        & (frame["close"] > frame["sma_50"])
    )
    frame["signal_drawdown_recovery_30"] = (
        (frame["recent_worst_drawdown"] <= -0.30) & recovery_base
    )
    frame["signal_drawdown_recovery_40"] = (
        (frame["recent_worst_drawdown"] <= -0.40) & recovery_base
    )
    frame["signal_rsi14_recovery"] = (
        (frame["rsi_14_low_30"] <= 30)
        & (frame["rsi_14"] >= 40)
        & (frame["return_30"] > 0)
    )
    frame["signal_rsi28_recovery"] = (
        (frame["rsi_28_low_30"] <= 35)
        & (frame["rsi_28"] >= 45)
        & (frame["return_30"] > 0)
    )
    frame["signal_bollinger_recovery"] = (
        (frame["bollinger_z_50_low_30"] <= -2)
        & (frame["bollinger_z_50"] >= -0.5)
        & (frame["return_30"] > 0)
    )

    base_trend = frame["close"] > frame["sma_200"]
    frame["signal_low_vol_trend"] = base_trend & (frame["volatility_rank"] <= 0.50)
    frame["signal_vol_contraction_trend"] = (
        base_trend & (frame["volatility_30"] <= frame["volatility_90"] * 0.80)
    )
    frame["signal_breadth_trend"] = base_trend & (frame["breadth_200"] >= 0.60)

    common_ready = frame["return_365"].notna() & frame["sma_200"].notna()
    for strategy in STRATEGIES:
        column = f"signal_{strategy.key}"
        frame[column] = frame[column].fillna(False) & common_ready
    return frame


def _month_end_rows(frame: pd.DataFrame) -> pd.DataFrame:
    keyed = frame.copy()
    keyed["month"] = keyed["date"].dt.to_period("M")
    positions = keyed.groupby(["ticker", "month"])["date"].idxmax()
    return keyed.loc[positions].sort_values(["ticker", "date"])


def _strategy_events(
    features: pd.DataFrame,
    strategy: LongTermStrategy,
    *,
    horizon_days: int,
    target_return: float,
    round_trip_cost: float,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    monthly = _month_end_rows(features)
    signal_column = f"signal_{strategy.key}"

    for ticker, history in features.groupby("ticker"):
        history = history.sort_values("date").reset_index(drop=True)
        dates = history["date"].to_numpy(dtype="datetime64[ns]")
        ticker_monthly = monthly.loc[monthly["ticker"] == ticker]
        next_allowed_entry: pd.Timestamp | None = None
        for signal in ticker_monthly.itertuples(index=False):
            if not bool(getattr(signal, signal_column)):
                continue
            signal_date = pd.Timestamp(signal.date)
            signal_position = int(np.searchsorted(dates, np.datetime64(signal_date), side="left"))
            entry_position = signal_position + 1
            if entry_position >= len(history):
                continue
            entry = history.iloc[entry_position]
            entry_date = pd.Timestamp(entry["date"])
            if next_allowed_entry is not None and entry_date < next_allowed_entry:
                continue
            desired_exit = entry_date + timedelta(days=horizon_days)
            exit_position = int(
                np.searchsorted(dates, np.datetime64(desired_exit), side="left")
            )
            if exit_position >= len(history):
                continue
            exit_row = history.iloc[exit_position]
            entry_price = float(entry["close"])
            exit_price = float(exit_row["close"])
            net_return = exit_price / entry_price - 1 - round_trip_cost
            path = history.iloc[entry_position: exit_position + 1]["close"].astype(float)
            drawdowns = path / path.cummax() - 1
            events.append({
                "strategy": strategy.key,
                "strategy_label": strategy.label,
                "ticker": str(ticker),
                "signal_date": signal_date.date().isoformat(),
                "entry_date": entry_date.date().isoformat(),
                "exit_date": pd.Timestamp(exit_row["date"]).date().isoformat(),
                "entry_price": entry_price,
                "exit_price": exit_price,
                "net_return": float(net_return),
                "net_return_pct": float(net_return * 100),
                "success": bool(net_return >= target_return),
                "max_drawdown_pct": float(drawdowns.min() * 100),
                "return_90_at_signal": float(signal.return_90),
                "return_180_at_signal": float(signal.return_180),
                "volatility_90_at_signal": float(signal.volatility_90),
            })
            # One asset cannot contribute overlapping one-year observations.
            next_allowed_entry = desired_exit
    return events


def _summarize(
    events: pd.DataFrame,
    strategy: LongTermStrategy,
    *,
    probability_threshold: float,
    minimum_independent_events: int,
    familywise_z: float,
    horizon_days: int,
    target_return: float,
) -> dict[str, Any]:
    subset = events.loc[events["strategy"] == strategy.key] if not events.empty else events
    asset_observations = int(len(subset))
    asset_successes = int(subset["success"].sum()) if asset_observations else 0
    episodes: list[dict[str, Any]] = []
    if asset_observations:
        current: list[float] = []
        episode_start: pd.Timestamp | None = None
        episode_end: pd.Timestamp | None = None
        for row in subset.sort_values("entry_date").itertuples(index=False):
            entry_date = pd.Timestamp(row.entry_date)
            if episode_end is None or entry_date >= episode_end:
                if current and episode_start is not None:
                    portfolio_return = float(np.mean(current))
                    episodes.append({
                        "start": episode_start,
                        "return": portfolio_return,
                        "success": portfolio_return >= target_return,
                    })
                episode_start = entry_date
                episode_end = entry_date + timedelta(days=horizon_days)
                current = []
            current.append(float(row.net_return))
        if current and episode_start is not None:
            portfolio_return = float(np.mean(current))
            episodes.append({
                "start": episode_start,
                "return": portfolio_return,
                "success": portfolio_return >= target_return,
            })

    observations = len(episodes)
    successes = sum(int(episode["success"]) for episode in episodes)
    hit_rate = successes / observations if observations else 0.0
    lower, upper = wilson_interval(successes, observations)
    familywise_lower, familywise_upper = wilson_interval(
        successes,
        observations,
        z=familywise_z,
    )
    qualified = (
        observations >= minimum_independent_events
        and hit_rate > probability_threshold
        and familywise_lower > probability_threshold
    )
    return {
        "strategy": strategy.key,
        "label": strategy.label,
        "family": strategy.family,
        "asset_events": asset_observations,
        "asset_successes": asset_successes,
        "asset_probability_pct": (
            round(asset_successes / asset_observations * 100, 2)
            if asset_observations
            else 0.0
        ),
        "independent_market_episodes": observations,
        "independent_events": observations,
        "successes": successes,
        "empirical_probability": hit_rate,
        "probability_pct": round(hit_rate * 100, 2),
        "confidence_95_low_pct": round(lower * 100, 2),
        "confidence_95_high_pct": round(upper * 100, 2),
        "familywise_confidence_low_pct": round(familywise_lower * 100, 2),
        "familywise_confidence_high_pct": round(familywise_upper * 100, 2),
        "median_net_return_pct": (
            round(float(subset["net_return_pct"].median()), 2)
            if asset_observations
            else None
        ),
        "quartile_25_net_return_pct": (
            round(float(subset["net_return_pct"].quantile(0.25)), 2)
            if asset_observations
            else None
        ),
        "worst_net_return_pct": (
            round(float(subset["net_return_pct"].min()), 2)
            if asset_observations
            else None
        ),
        "worst_drawdown_pct": (
            round(float(subset["max_drawdown_pct"].min()), 2)
            if asset_observations
            else None
        ),
        "qualifies": qualified,
    }


def evaluate_long_term_strategies(
    prices: pd.DataFrame,
    *,
    horizon_days: int = 365,
    target_return: float = 0.15,
    probability_threshold: float = 0.80,
    minimum_independent_events: int = 20,
    round_trip_cost: float = 0.004,
    familywise_test_count: int | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Evaluate fixed strategies using next-day entries and non-overlapping episodes."""
    features = _prepare_features(prices)
    all_events = []
    for strategy in STRATEGIES:
        all_events.extend(
            _strategy_events(
                features,
                strategy,
                horizon_days=horizon_days,
                target_return=target_return,
                round_trip_cost=round_trip_cost,
            )
        )
    events = pd.DataFrame(all_events)
    # Bonferroni-adjust the interval because the research evaluates many fixed
    # rules at once. This prevents a lucky strategy from passing by chance.
    tested_hypotheses = familywise_test_count or len(STRATEGIES)
    familywise_z = NormalDist().inv_cdf(1 - 0.05 / (2 * tested_hypotheses))
    summaries = [
        _summarize(
            events,
            strategy,
            probability_threshold=probability_threshold,
            minimum_independent_events=minimum_independent_events,
            familywise_z=familywise_z,
            horizon_days=horizon_days,
            target_return=target_return,
        )
        for strategy in STRATEGIES
    ]
    report = {
        "research_version": RESEARCH_VERSION,
        "method": "monthly_signals_next_daily_close_non_overlapping_episodes",
        "horizon_days": horizon_days,
        "target_return_pct": target_return * 100,
        "round_trip_cost_pct": round_trip_cost * 100,
        "required_probability_pct": probability_threshold * 100,
        "minimum_independent_events": minimum_independent_events,
        "strategy_count": len(STRATEGIES),
        "familywise_test_count": tested_hypotheses,
        "multiple_testing_control": "Bonferroni-adjusted Wilson interval, family-wise 95%",
        "strategies": summaries,
        "qualified_strategies": [row["strategy"] for row in summaries if row["qualifies"]],
        "interpretation": (
            "Asset events in overlapping market windows are correlated and are combined into "
            "equal-weight portfolio episodes. Empirical hit rates are a feasibility screen, "
            "not calibrated live forecasts. Qualification uses the Bonferroni-adjusted "
            "family-wise confidence lower bound."
        ),
    }
    return events, report


def _weekly_rows(
    frame: pd.DataFrame,
    *,
    completed_through: pd.Timestamp | None = None,
) -> pd.DataFrame:
    """Return the last available daily row of each completed Friday week."""
    keyed = frame.copy()
    keyed["week"] = keyed["date"].dt.to_period("W-FRI")
    keyed["week_end"] = keyed["week"].dt.end_time.dt.normalize()
    if completed_through is not None:
        keyed = keyed.loc[
            keyed["week_end"] <= pd.Timestamp(completed_through).normalize()
        ]
    if keyed.empty:
        return keyed
    positions = keyed.groupby(["ticker", "week"])["date"].idxmax()
    return keyed.loc[positions].sort_values(["ticker", "date"])


def _strategy_events_from_rows(
    features: pd.DataFrame,
    signal_rows: pd.DataFrame,
    strategy: LongTermStrategy,
    *,
    horizon_days: int,
    target_return: float,
    round_trip_cost: float,
) -> list[dict[str, Any]]:
    """Evaluate a fixed rule on supplied periodic rows without overlapping a coin."""
    events: list[dict[str, Any]] = []
    signal_column = f"signal_{strategy.key}"
    for ticker, history in features.groupby("ticker"):
        history = history.sort_values("date").reset_index(drop=True)
        dates = history["date"].to_numpy(dtype="datetime64[ns]")
        ticker_signals = signal_rows.loc[signal_rows["ticker"] == ticker]
        next_allowed_entry: pd.Timestamp | None = None
        for signal in ticker_signals.itertuples(index=False):
            if not bool(getattr(signal, signal_column)):
                continue
            signal_date = pd.Timestamp(signal.date)
            signal_position = int(
                np.searchsorted(dates, np.datetime64(signal_date), side="left")
            )
            entry_position = signal_position + 1
            if entry_position >= len(history):
                continue
            entry = history.iloc[entry_position]
            entry_date = pd.Timestamp(entry["date"])
            if next_allowed_entry is not None and entry_date < next_allowed_entry:
                continue
            desired_exit = entry_date + timedelta(days=horizon_days)
            exit_position = int(
                np.searchsorted(dates, np.datetime64(desired_exit), side="left")
            )
            if exit_position >= len(history):
                continue
            exit_row = history.iloc[exit_position]
            entry_price = float(entry["close"])
            exit_price = float(exit_row["close"])
            net_return = exit_price / entry_price - 1 - round_trip_cost
            path = history.iloc[entry_position: exit_position + 1]["close"].astype(float)
            drawdowns = path / path.cummax() - 1
            events.append({
                "strategy": strategy.key,
                "strategy_label": strategy.label,
                "ticker": str(ticker),
                "signal_date": signal_date.date().isoformat(),
                "entry_date": entry_date.date().isoformat(),
                "exit_date": pd.Timestamp(exit_row["date"]).date().isoformat(),
                "entry_price": entry_price,
                "exit_price": exit_price,
                "net_return": float(net_return),
                "net_return_pct": float(net_return * 100),
                "success": bool(net_return >= target_return),
                "max_drawdown_pct": float(drawdowns.min() * 100),
            })
            next_allowed_entry = desired_exit
    return events


def evaluate_long_term_deposit_strategies(
    prices: pd.DataFrame,
    *,
    probability_threshold: float = 0.80,
    minimum_independent_events: int = 20,
    round_trip_cost: float = BOLLINGER_ROUND_TRIP_COST,
) -> dict[str, Any]:
    """Research buy-now-and-hold plans from fixed weekly observations."""
    features = _prepare_features(prices)
    weekly = _weekly_rows(features)
    familywise_tests = len(STRATEGIES) * len(DEPOSIT_PLANS)
    familywise_z = NormalDist().inv_cdf(1 - 0.05 / (2 * familywise_tests))
    events_by_horizon: dict[int, pd.DataFrame] = {}

    for horizon_days in sorted({plan.horizon_days for plan in DEPOSIT_PLANS}):
        rows: list[dict[str, Any]] = []
        for strategy in STRATEGIES:
            rows.extend(
                _strategy_events_from_rows(
                    features,
                    weekly,
                    strategy,
                    horizon_days=horizon_days,
                    target_return=0,
                    round_trip_cost=round_trip_cost,
                )
            )
        events_by_horizon[horizon_days] = pd.DataFrame(rows)

    plans: list[dict[str, Any]] = []
    for plan in DEPOSIT_PLANS:
        events = events_by_horizon[plan.horizon_days].copy()
        events["success"] = events["net_return"] >= plan.target_return
        summaries = [
            _summarize(
                events,
                strategy,
                probability_threshold=probability_threshold,
                minimum_independent_events=minimum_independent_events,
                familywise_z=familywise_z,
                horizon_days=plan.horizon_days,
                target_return=plan.target_return,
            )
            for strategy in STRATEGIES
        ]
        plans.append({
            "key": plan.key,
            "label": plan.label,
            "horizon_days": plan.horizon_days,
            "target_return_pct": plan.target_return * 100,
            "strategies": summaries,
            "qualified_strategies": [
                row["strategy"] for row in summaries if row["qualifies"]
            ],
        })

    return {
        "research_version": DEPOSIT_RESEARCH_VERSION,
        "method": "completed_weekly_signals_next_daily_close_non_overlapping",
        "strategy_count": len(STRATEGIES),
        "plan_count": len(DEPOSIT_PLANS),
        "combination_count": familywise_tests,
        "required_probability_pct": probability_threshold * 100,
        "minimum_independent_events": minimum_independent_events,
        "round_trip_cost_pct": round_trip_cost * 100,
        "multiple_testing_control": (
            "Bonferroni-adjusted Wilson interval across all strategies and plans"
        ),
        "plans": plans,
    }


def build_long_term_investment_report(
    prices: pd.DataFrame,
    research_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build today's deposit-style buy-and-hold decision from fixed research."""
    features = _prepare_features(prices)
    if features.empty:
        return {
            "is_ready": False,
            "error": "Нет дневных данных для Long Term Investment",
            "plans": [],
        }
    payload = research_report or {}
    research = payload.get("deposit_research", payload)
    research_plans = {
        row.get("key"): row for row in research.get("plans", [])
    }
    if not research_plans:
        return {
            "is_ready": False,
            "error": "Не рассчитана недельная модель входа по текущей цене",
            "plans": [],
        }

    data_date = pd.Timestamp(features["date"].max())
    weekly = _weekly_rows(features, completed_through=data_date)
    if weekly.empty:
        return {
            "is_ready": False,
            "error": "Нет завершённой недели для Long Term Investment",
            "plans": [],
        }
    evaluation_date = pd.Timestamp(weekly["date"].max())
    latest = weekly.sort_values("date").groupby("ticker").tail(1)
    strategy_by_key = {strategy.key: strategy for strategy in STRATEGIES}
    plans: list[dict[str, Any]] = []

    for fixed_plan in DEPOSIT_PLANS:
        researched = research_plans.get(fixed_plan.key, {})
        summaries = {
            row.get("strategy"): row
            for row in researched.get("strategies", [])
        }
        candidates: list[dict[str, Any]] = []
        for row in latest.itertuples(index=False):
            active_setups = []
            for strategy_key, summary in summaries.items():
                strategy = strategy_by_key.get(strategy_key)
                if strategy is None or strategy.family == "benchmark":
                    continue
                if not bool(getattr(row, f"signal_{strategy_key}")):
                    continue
                if int(summary.get("asset_events") or 0) < 20:
                    continue
                active_setups.append(summary)
            if not active_setups:
                continue
            best = max(
                active_setups,
                key=lambda item: (
                    float(item.get("asset_probability_pct") or 0),
                    int(item.get("asset_events") or 0),
                ),
            )
            current_price = float(row.close)
            target_price = current_price * (
                1 + fixed_plan.target_return + BOLLINGER_ROUND_TRIP_COST
            )
            median_return = float(best.get("median_net_return_pct") or 0)
            downside_return = float(best.get("quartile_25_net_return_pct") or 0)
            qualified = bool(best.get("qualifies", False))
            candidates.append({
                "ticker": str(row.ticker),
                "symbol": str(row.ticker).split("/", 1)[0],
                "current_price": current_price,
                "current_price_label": _price_label(current_price),
                "target_price": target_price,
                "target_price_label": _price_label(target_price),
                "strategy": best.get("strategy"),
                "strategy_label": best.get("label"),
                "active_strategy_count": len(active_setups),
                "historical_probability_pct": best.get("asset_probability_pct", 0),
                "historical_events": best.get("asset_events", 0),
                "independent_episodes": best.get("independent_market_episodes", 0),
                "confidence_low_pct": best.get("familywise_confidence_low_pct", 0),
                "median_return_pct": median_return,
                "downside_return_pct": downside_return,
                "target_multiplier": 1 + fixed_plan.target_return,
                "median_multiplier": max(0, 1 + median_return / 100),
                "downside_multiplier": max(0, 1 + downside_return / 100),
                "decision": "buy" if qualified else "wait",
                "decision_label": "Купить сейчас" if qualified else "Подождать",
            })
        candidates.sort(
            key=lambda item: (
                item["decision"] != "buy",
                -float(item["historical_probability_pct"]),
                -int(item["historical_events"]),
                item["ticker"],
            )
        )
        buy_candidates = [row for row in candidates if row["decision"] == "buy"]
        plans.append({
            "key": fixed_plan.key,
            "label": fixed_plan.label,
            "horizon_days": fixed_plan.horizon_days,
            "horizon_months": 3 if fixed_plan.horizon_days == 90 else (
                6 if fixed_plan.horizon_days == 180 else 12
            ),
            "target_return_pct": fixed_plan.target_return * 100,
            "maturity_date": (
                data_date + timedelta(days=fixed_plan.horizon_days)
            ).date().isoformat(),
            "decision": "buy" if buy_candidates else "wait",
            "decision_label": "Есть вход" if buy_candidates else "Подождать",
            "qualified_count": len(buy_candidates),
            "best_candidate": candidates[0] if candidates else None,
            "candidates": candidates[:5],
        })

    return {
        "is_ready": True,
        "product_version": DEPOSIT_RESEARCH_VERSION,
        "data_date": data_date.date().isoformat(),
        "evaluation_date": evaluation_date.date().isoformat(),
        "valid_until": (evaluation_date + timedelta(days=7)).date().isoformat(),
        "amount_default": 1000,
        "required_probability_pct": research.get("required_probability_pct", 80),
        "round_trip_cost_pct": research.get("round_trip_cost_pct", 0.4),
        "default_plan_key": "12m-15",
        "qualified_count": sum(plan["qualified_count"] for plan in plans),
        "plans": plans,
        "status_label": (
            "Есть подходящий вход"
            if any(plan["qualified_count"] for plan in plans)
            else "Сейчас лучше подождать"
        ),
    }


def _price_label(value: float) -> str:
    if value >= 1000:
        return f"${value:,.2f}".replace(",", " ")
    if value >= 1:
        return f"${value:.4f}".rstrip("0").rstrip(".")
    return f"${value:.8f}".rstrip("0").rstrip(".")


def _completed_month_end(data_date: pd.Timestamp) -> pd.Timestamp:
    current_month_end = data_date + pd.offsets.MonthEnd(0)
    if data_date.normalize() < current_month_end.normalize():
        return data_date - pd.offsets.MonthEnd(1)
    return current_month_end


def build_bollinger_recovery_report(
    prices: pd.DataFrame,
    research_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the stable monthly Bollinger Recovery live monitoring report."""
    features = _prepare_features(prices)
    if features.empty:
        return {
            "is_ready": False,
            "error": "Нет дневных данных для Long Term Deals",
            "active": [],
        }

    data_date = pd.Timestamp(features["date"].max())
    evaluation_date = _completed_month_end(data_date)
    monthly = _month_end_rows(features)
    monthly = monthly.loc[monthly["date"] <= evaluation_date]
    active: list[dict[str, Any]] = []
    scan: list[dict[str, Any]] = []

    for ticker, history in features.groupby("ticker"):
        history = history.sort_values("date").reset_index(drop=True)
        dates = history["date"].to_numpy(dtype="datetime64[ns]")
        ticker_monthly = monthly.loc[monthly["ticker"] == ticker]
        latest_signal_row = ticker_monthly.iloc[-1] if not ticker_monthly.empty else None
        if latest_signal_row is not None:
            scan.append({
                "ticker": str(ticker),
                "signal": bool(latest_signal_row["signal_bollinger_recovery"]),
                "bollinger_z": round(float(latest_signal_row["bollinger_z_50"]), 2),
                "recent_low_z": round(
                    float(latest_signal_row["bollinger_z_50_low_30"]),
                    2,
                ),
                "return_30_pct": round(float(latest_signal_row["return_30"] * 100), 2),
                "evaluated_on": pd.Timestamp(latest_signal_row["date"]).date().isoformat(),
            })

        next_allowed_entry: pd.Timestamp | None = None
        for signal in ticker_monthly.itertuples(index=False):
            if not bool(signal.signal_bollinger_recovery):
                continue
            signal_date = pd.Timestamp(signal.date)
            signal_position = int(
                np.searchsorted(dates, np.datetime64(signal_date), side="left")
            )
            entry_position = signal_position + 1
            if entry_position >= len(history):
                continue
            entry = history.iloc[entry_position]
            entry_date = pd.Timestamp(entry["date"])
            if next_allowed_entry is not None and entry_date < next_allowed_entry:
                continue
            planned_exit = entry_date + timedelta(days=BOLLINGER_HORIZON_DAYS)
            next_allowed_entry = planned_exit
            if planned_exit <= data_date:
                continue
            latest = history.iloc[-1]
            entry_price = float(entry["close"])
            current_price = float(latest["close"])
            current_return = (
                current_price / entry_price - 1 - BOLLINGER_ROUND_TRIP_COST
            )
            target_price = entry_price * (
                1 + BOLLINGER_TARGET_RETURN + BOLLINGER_ROUND_TRIP_COST
            )
            active.append({
                "ticker": str(ticker),
                "symbol": str(ticker).split("/", 1)[0],
                "signal_date": signal_date.date().isoformat(),
                "entry_date": entry_date.date().isoformat(),
                "entry_price": entry_price,
                "entry_price_label": _price_label(entry_price),
                "current_price": current_price,
                "current_price_label": _price_label(current_price),
                "target_price": target_price,
                "target_price_label": _price_label(target_price),
                "current_return_pct": round(current_return * 100, 2),
                "planned_exit": planned_exit.date().isoformat(),
                "days_held": max(0, (data_date - entry_date).days),
                "days_remaining": max(0, (planned_exit - data_date).days),
            })

    strategy_history = {}
    if research_report:
        research = research_report.get("research", research_report)
        strategy_history = next(
            (
                row
                for row in research.get("strategies", [])
                if row.get("strategy") == BOLLINGER_STRATEGY_KEY
            ),
            {},
        )

    active.sort(key=lambda row: row["ticker"])
    scan.sort(key=lambda row: (not row["signal"], row["ticker"]))
    next_evaluation = evaluation_date + pd.offsets.MonthEnd(1)
    return {
        "is_ready": True,
        "strategy_version": BOLLINGER_LIVE_VERSION,
        "research_version": RESEARCH_VERSION,
        "strategy_label": "Bollinger Recovery",
        "data_date": data_date.date().isoformat(),
        "evaluation_date": evaluation_date.date().isoformat(),
        "next_evaluation_date": next_evaluation.date().isoformat(),
        "horizon_days": BOLLINGER_HORIZON_DAYS,
        "target_return_pct": BOLLINGER_TARGET_RETURN * 100,
        "round_trip_cost_pct": BOLLINGER_ROUND_TRIP_COST * 100,
        "active": active,
        "scan": scan,
        "historical": strategy_history,
        "is_validated": bool(strategy_history.get("qualifies", False)),
        "status_label": (
            "Подтверждена"
            if strategy_history.get("qualifies", False)
            else "Экспериментальное наблюдение"
        ),
        "rules": [
            "За последние 30 дней Bollinger Z опускался до −2σ или ниже",
            "К концу месяца Bollinger Z восстановился до −0,5σ или выше",
            "Доходность последних 30 дней стала положительной",
            "Вход — на закрытии следующей дневной свечи",
            "Плановый выход — через 365 дней",
        ],
    }
