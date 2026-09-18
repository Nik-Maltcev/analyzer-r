"""Past-only research for metals, uranium and natural-gas strategies.

Oil and gasoline are intentionally excluded.  Every signal uses only information
known on its entry date.  Results are also translated into nominal and real RUB.
"""

from __future__ import annotations

import json
from pathlib import Path
from statistics import NormalDist

import numpy as np
import pandas as pd

from analyze_country_etf_rub_real import (
    CAPITAL_GAINS_TAX_RATE,
    DEPOSIT_RATE,
    STARTING_RUB,
    cpi_for_dates,
    load_cbr_usd_rub,
    load_rosstat_month_start_cpi,
    values_asof,
    wilson_lower,
)


HORIZONS = tuple(range(1, 13))
SINGLE_LEG_COST_PCT = 0.20
PAIR_COST_PCT = 0.40
MIN_EVENTS = 20
REQUIRED_RATE = 0.80

ASSETS = {
    "GLD": "Gold",
    "SLV": "Silver",
    "PPLT": "Platinum",
    "PALL": "Palladium",
    "CPER": "Copper",
    "URA": "Uranium miners",
    "UNG": "Natural gas",
    "GLTR": "Precious-metals basket",
    "DBB": "Base metals",
    "LIT": "Lithium and battery chain",
    "REMX": "Rare earth and strategic metals",
    "NLR": "Nuclear-energy chain",
    "TAN": "Solar energy",
    "ICLN": "Clean energy",
    "DBA": "Agricultural commodities",
    "WOOD": "Timber and forestry",
    "SLX": "Steel producers",
    "PHO": "Water infrastructure",
}

PAIR_DEFINITIONS = {
    "GLD/SLV": ("GLD", "SLV", "Gold / silver ratio"),
    "PPLT/PALL": ("PPLT", "PALL", "Platinum / palladium ratio"),
    "GDX/GLD": ("GDX", "GLD", "Gold miners / gold"),
    "SIL/SLV": ("SIL", "SLV", "Silver miners / silver"),
    "COPX/CPER": ("COPX", "CPER", "Copper miners / copper"),
    "NLR/URA": ("NLR", "URA", "Nuclear chain / uranium miners"),
    "TAN/ICLN": ("TAN", "ICLN", "Solar / clean-energy basket"),
}

CFTC_TO_TICKER = {
    "GOLD - COMMODITY EXCHANGE INC.": "GLD",
    "SILVER - COMMODITY EXCHANGE INC.": "SLV",
    "PLATINUM - NEW YORK MERCANTILE EXCHANGE": "PPLT",
    "PALLADIUM - NEW YORK MERCANTILE EXCHANGE": "PALL",
    "COPPER-GRADE #1 - COMMODITY EXCHANGE INC.": "CPER",
    "COPPER- #1 - COMMODITY EXCHANGE INC.": "CPER",
    "NATURAL GAS - NEW YORK MERCANTILE EXCHANGE": "UNG",
    "NAT GAS NYME - NEW YORK MERCANTILE EXCHANGE": "UNG",
}


def _month_ends(frame: pd.DataFrame) -> pd.DataFrame:
    return (
        frame.set_index("date")
        .resample("ME")
        .last()
        .dropna(subset=["close"])
        .reset_index()
    )


def _exit_row(frame: pd.DataFrame, entry_date: pd.Timestamp, months: int) -> pd.Series | None:
    target = entry_date + pd.DateOffset(months=months)
    positions = frame["date"].searchsorted(target, side="left")
    if positions >= len(frame):
        return None
    return frame.iloc[int(positions)]


def _long_events(prices: pd.DataFrame) -> list[dict]:
    events: list[dict] = []
    for ticker, label in ASSETS.items():
        daily = prices.loc[prices["ticker"] == ticker, ["date", "close"]].copy()
        daily = daily.sort_values("date").drop_duplicates("date")
        daily["ma200"] = daily["close"].rolling(200).mean()
        daily["ret20"] = daily["close"].pct_change(20)
        daily["ret5"] = daily["close"].pct_change(5)
        daily["ret210"] = daily["close"].pct_change(210)
        daily["high126_prev"] = daily["close"].rolling(126).max().shift(1)
        monthly = _month_ends(daily)
        rules = {
            "buy_hold": pd.Series(True, index=monthly.index),
            "trend_10m": (monthly["close"] > monthly["ma200"]) & (monthly["ret210"] > 0),
            "dip_in_uptrend": (monthly["ret210"] > 0) & (monthly["ret20"] <= -0.05),
            "oversold_recovery": (monthly["ret20"] <= -0.08) & (monthly["ret5"] > 0),
            "six_month_breakout": monthly["close"] >= monthly["high126_prev"],
        }
        for strategy, mask in rules.items():
            candidates = monthly.loc[mask.fillna(False)]
            for horizon in HORIZONS:
                unavailable_until = pd.Timestamp.min
                for row in candidates.itertuples(index=False):
                    if row.date <= unavailable_until:
                        continue
                    exit_row = _exit_row(daily, row.date, horizon)
                    if exit_row is None:
                        continue
                    gross = (float(exit_row["close"]) / float(row.close) - 1.0) * 100.0
                    events.append({
                        "family": "directional",
                        "instrument": ticker,
                        "asset": label,
                        "strategy": strategy,
                        "horizon_months": horizon,
                        "direction": "long",
                        "entry_date": row.date,
                        "exit_date": exit_row["date"],
                        "gross_return_pct": gross,
                        "net_return_pct": gross - SINGLE_LEG_COST_PCT,
                    })
                    unavailable_until = exit_row["date"]
    return events


def _rotation_events(prices: pd.DataFrame) -> list[dict]:
    pivot = prices.loc[prices["ticker"].isin(ASSETS), ["date", "ticker", "close"]].pivot(
        index="date", columns="ticker", values="close"
    ).sort_index()
    month = pivot.resample("ME").last().dropna(how="all")
    momentum = month.pct_change(10, fill_method=None)
    events: list[dict] = []
    for horizon in HORIZONS:
        unavailable_until = pd.Timestamp.min
        for date, scores in momentum.iterrows():
            if date <= unavailable_until:
                continue
            selected = scores[scores > 0].nlargest(2).index.tolist()
            if len(selected) != 2:
                continue
            entry_pos = pivot.index.searchsorted(date, side="right") - 1
            exit_pos = pivot.index.searchsorted(date + pd.DateOffset(months=horizon), side="left")
            if entry_pos < 0 or exit_pos >= len(pivot):
                continue
            entry = pivot.iloc[entry_pos][selected]
            exit_ = pivot.iloc[exit_pos][selected]
            if entry.isna().any() or exit_.isna().any():
                continue
            gross = float(((exit_ / entry) - 1.0).mean() * 100.0)
            events.append({
                "family": "rotation",
                "instrument": "+".join(selected),
                "asset": "Top-2 resource rotation",
                "strategy": "top2_10m_momentum",
                "horizon_months": horizon,
                "direction": "long",
                "entry_date": pivot.index[entry_pos],
                "exit_date": pivot.index[exit_pos],
                "gross_return_pct": gross,
                "net_return_pct": gross - SINGLE_LEG_COST_PCT,
            })
            unavailable_until = pivot.index[exit_pos]
    return events


def _pair_events(prices: pd.DataFrame) -> list[dict]:
    pivot = prices[["date", "ticker", "close"]].pivot(
        index="date", columns="ticker", values="close"
    ).sort_index()
    events: list[dict] = []
    for pair, (ticker_a, ticker_b, label) in PAIR_DEFINITIONS.items():
        frame = pivot[[ticker_a, ticker_b]].dropna().copy()
        log_ratio = np.log(frame[ticker_a] / frame[ticker_b])
        mean = log_ratio.rolling(756, min_periods=504).mean().shift(1)
        sd = log_ratio.rolling(756, min_periods=504).std(ddof=0).shift(1)
        zscore = (log_ratio - mean) / sd
        previous = zscore.shift(1)
        entries = ((zscore <= -2) & (previous > -2)) | ((zscore >= 2) & (previous < 2))
        candidate_dates = frame.index[entries.fillna(False)]
        for horizon in HORIZONS:
            unavailable_until = pd.Timestamp.min
            for entry_date in candidate_dates:
                if entry_date <= unavailable_until:
                    continue
                direction = "long_ratio" if zscore.loc[entry_date] < 0 else "short_ratio"
                deadline = entry_date + pd.DateOffset(months=horizon)
                future = zscore.loc[(zscore.index > entry_date) & (zscore.index <= deadline)]
                if direction == "long_ratio":
                    reverted = future[future >= 0]
                else:
                    reverted = future[future <= 0]
                exit_date = reverted.index[0] if len(reverted) else frame.index[
                    min(frame.index.searchsorted(deadline, side="left"), len(frame) - 1)
                ]
                if exit_date <= entry_date or exit_date >= frame.index[-1]:
                    continue
                ret_a = frame.loc[exit_date, ticker_a] / frame.loc[entry_date, ticker_a] - 1.0
                ret_b = frame.loc[exit_date, ticker_b] / frame.loc[entry_date, ticker_b] - 1.0
                gross = (ret_a - ret_b) / 2.0 if direction == "long_ratio" else (ret_b - ret_a) / 2.0
                events.append({
                    "family": "relative_value",
                    "instrument": pair,
                    "asset": label,
                    "strategy": "rolling_3y_ratio_reversion",
                    "horizon_months": horizon,
                    "direction": direction,
                    "entry_date": entry_date,
                    "exit_date": exit_date,
                    "gross_return_pct": float(gross * 100.0),
                    "net_return_pct": float(gross * 100.0 - PAIR_COST_PCT),
                })
                unavailable_until = exit_date
    return events


def _rolling_percentile(series: pd.Series, window: int = 156) -> pd.Series:
    return series.rolling(window, min_periods=104).apply(
        lambda x: float(np.mean(x[:-1] <= x[-1])) if len(x) > 1 else np.nan,
        raw=True,
    )


def _cftc_events(prices: pd.DataFrame, path: Path) -> list[dict]:
    raw = pd.read_csv(path)
    raw["date"] = pd.to_datetime(raw["report_date_as_yyyy_mm_dd"])
    raw["ticker"] = raw["market_and_exchange_names"].map(CFTC_TO_TICKER)
    numeric = [
        "open_interest_all", "prod_merc_positions_long", "prod_merc_positions_short",
        "m_money_positions_long_all", "m_money_positions_short_all",
    ]
    raw[numeric] = raw[numeric].apply(pd.to_numeric, errors="coerce")
    raw = raw.sort_values(["ticker", "date"]).drop_duplicates(["ticker", "date"], keep="last")
    events: list[dict] = []
    for ticker, cot in raw.dropna(subset=["ticker"]).groupby("ticker"):
        daily = prices.loc[prices["ticker"] == ticker, ["date", "close"]].sort_values("date")
        if daily.empty:
            continue
        cot = cot.sort_values("date").copy()
        cot["managed_net"] = (
            cot["m_money_positions_long_all"] - cot["m_money_positions_short_all"]
        ) / cot["open_interest_all"]
        cot["producer_net"] = (
            cot["prod_merc_positions_long"] - cot["prod_merc_positions_short"]
        ) / cot["open_interest_all"]
        cot["managed_pctile"] = _rolling_percentile(cot["managed_net"])
        cot["producer_pctile"] = _rolling_percentile(cot["producer_net"])
        cot = pd.merge_asof(cot, daily, on="date", direction="backward")
        cot["price_4w"] = cot["close"].pct_change(4)
        rules = {
            "cftc_managed_capitulation_turn": (cot["managed_pctile"] <= 0.10) & (cot["price_4w"] > 0),
            "cftc_producer_accumulation": (cot["producer_pctile"] >= 0.90) & (cot["price_4w"] > 0),
        }
        for strategy, mask in rules.items():
            candidates = cot.loc[mask.fillna(False)]
            for horizon in HORIZONS:
                unavailable_until = pd.Timestamp.min
                for row in candidates.itertuples(index=False):
                    if row.date <= unavailable_until or pd.isna(row.close):
                        continue
                    exit_row = _exit_row(daily, row.date, horizon)
                    if exit_row is None:
                        continue
                    gross = (float(exit_row["close"]) / float(row.close) - 1.0) * 100.0
                    events.append({
                        "family": "positioning",
                        "instrument": ticker,
                        "asset": ASSETS[ticker],
                        "strategy": strategy,
                        "horizon_months": horizon,
                        "direction": "long",
                        "entry_date": row.date,
                        "exit_date": exit_row["date"],
                        "gross_return_pct": gross,
                        "net_return_pct": gross - SINGLE_LEG_COST_PCT,
                    })
                    unavailable_until = exit_row["date"]
    return events


def _storage_events(prices: pd.DataFrame, paths: list[Path]) -> list[dict]:
    records: list[dict] = []
    for path in paths:
        records.extend(json.loads(path.read_text(encoding="utf-8"))["response"]["data"])
    storage = pd.DataFrame(records)
    storage = storage.loc[(storage["duoarea"] == "R48") & (storage["process"] == "SWO")].copy()
    storage["date"] = pd.to_datetime(storage["period"])
    storage["value"] = pd.to_numeric(storage["value"], errors="coerce")
    storage = storage.sort_values("date").drop_duplicates("date")
    storage["week"] = storage["date"].dt.isocalendar().week.astype(int).clip(upper=52)
    storage["season_mean"] = storage.groupby("week")["value"].transform(
        lambda x: x.shift(1).rolling(5, min_periods=3).mean()
    )
    storage["season_sd"] = storage.groupby("week")["value"].transform(
        lambda x: x.shift(1).rolling(5, min_periods=3).std(ddof=0)
    )
    storage["inventory_z"] = (storage["value"] - storage["season_mean"]) / storage["season_sd"]
    daily = prices.loc[prices["ticker"] == "UNG", ["date", "close"]].sort_values("date")
    storage = pd.merge_asof(storage, daily, on="date", direction="backward")
    storage["price_8w"] = storage["close"].pct_change(8)
    rules = {
        "storage_shortage_with_trend": (storage["inventory_z"] <= -1) & (storage["price_8w"] > 0),
        "storage_glut_with_downtrend": (storage["inventory_z"] >= 1) & (storage["price_8w"] < 0),
    }
    events: list[dict] = []
    for strategy, mask in rules.items():
        direction = "long" if "shortage" in strategy else "short"
        candidates = storage.loc[mask.fillna(False)]
        for horizon in HORIZONS:
            unavailable_until = pd.Timestamp.min
            for row in candidates.itertuples(index=False):
                if row.date <= unavailable_until or pd.isna(row.close):
                    continue
                exit_row = _exit_row(daily, row.date, horizon)
                if exit_row is None:
                    continue
                raw_return = float(exit_row["close"]) / float(row.close) - 1.0
                gross = raw_return * 100.0 if direction == "long" else -raw_return * 100.0
                events.append({
                    "family": "fundamental",
                    "instrument": "UNG",
                    "asset": "Natural gas storage",
                    "strategy": strategy,
                    "horizon_months": horizon,
                    "direction": direction,
                    "entry_date": row.date,
                    "exit_date": exit_row["date"],
                    "gross_return_pct": gross,
                    "net_return_pct": gross - SINGLE_LEG_COST_PCT,
                })
                unavailable_until = exit_row["date"]
    return events


def _enrich_rub(events: pd.DataFrame, fx: pd.Series, cpi: pd.Series) -> pd.DataFrame:
    frame = events.copy()
    frame["entry_date"] = pd.to_datetime(frame["entry_date"])
    frame["exit_date"] = pd.to_datetime(frame["exit_date"])
    frame["entry_usd_rub"] = values_asof(fx, frame["entry_date"])
    frame["exit_usd_rub"] = values_asof(fx, frame["exit_date"])
    frame["entry_cpi"] = cpi_for_dates(cpi, frame["entry_date"])
    frame["exit_cpi"] = cpi_for_dates(cpi, frame["exit_date"])
    frame = frame.dropna(subset=["entry_usd_rub", "exit_usd_rub", "entry_cpi", "exit_cpi"])
    frame["holding_days"] = (frame["exit_date"] - frame["entry_date"]).dt.days
    frame["usd_factor"] = (1 + frame["net_return_pct"] / 100).clip(lower=0)
    frame["fx_factor"] = frame["exit_usd_rub"] / frame["entry_usd_rub"]
    frame["inflation_factor"] = frame["exit_cpi"] / frame["entry_cpi"]
    frame["rub_factor"] = frame["usd_factor"] * frame["fx_factor"]
    gain = (frame["rub_factor"] - 1).clip(lower=0)
    frame["rub_factor_after_tax"] = frame["rub_factor"] - CAPITAL_GAINS_TAX_RATE * gain
    frame["deposit_factor"] = 1 + DEPOSIT_RATE * frame["holding_days"] / 365
    frame["rub_return_pct"] = (frame["rub_factor"] - 1) * 100
    frame["real_return_pct"] = (frame["rub_factor"] / frame["inflation_factor"] - 1) * 100
    frame["real_return_after_tax_pct"] = (
        frame["rub_factor_after_tax"] / frame["inflation_factor"] - 1
    ) * 100
    frame["deposit_real_return_pct"] = (frame["deposit_factor"] / frame["inflation_factor"] - 1) * 100
    frame["final_rub"] = STARTING_RUB * frame["rub_factor"]
    frame["success_usd_10"] = frame["net_return_pct"] >= 10
    frame["success_rub_10"] = frame["rub_return_pct"] >= 10
    frame["success_real_10"] = frame["real_return_pct"] >= 10
    frame["beats_deposit"] = frame["rub_factor"] > frame["deposit_factor"]
    return frame


def _summary(events: pd.DataFrame) -> pd.DataFrame:
    keys = ["family", "instrument", "asset", "strategy", "horizon_months"]
    rows = []
    for key, group in events.groupby(keys, sort=False):
        row = dict(zip(keys, key))
        row.update({
            "events": len(group),
            "usd_plus_10_count": int(group["success_usd_10"].sum()),
            "usd_plus_10_pct": group["success_usd_10"].mean() * 100,
            "rub_plus_10_pct": group["success_rub_10"].mean() * 100,
            "real_plus_10_pct": group["success_real_10"].mean() * 100,
            "beats_deposit_pct": group["beats_deposit"].mean() * 100,
            "positive_usd_pct": (group["net_return_pct"] > 0).mean() * 100,
            "median_usd_return_pct": group["net_return_pct"].median(),
            "median_rub_return_pct": group["rub_return_pct"].median(),
            "median_real_return_pct": group["real_return_pct"].median(),
            "mean_usd_return_pct": group["net_return_pct"].mean(),
            "worst_usd_return_pct": group["net_return_pct"].min(),
            "best_usd_return_pct": group["net_return_pct"].max(),
            "median_final_rub": group["final_rub"].median(),
            "first_entry": group["entry_date"].min().date().isoformat(),
            "last_exit": group["exit_date"].max().date().isoformat(),
        })
        rows.append(row)
    result = pd.DataFrame(rows)
    z95 = 1.96
    family_z = NormalDist().inv_cdf(1 - 0.05 / (2 * max(len(result), 1)))
    result["usd_plus_10_wilson_low_pct"] = result.apply(
        lambda r: wilson_lower(int(r.usd_plus_10_count), int(r.events), z95) * 100, axis=1
    )
    result["usd_plus_10_familywise_low_pct"] = result.apply(
        lambda r: wilson_lower(int(r.usd_plus_10_count), int(r.events), family_z) * 100, axis=1
    )
    result["formally_over_80"] = (
        (result["events"] >= MIN_EVENTS)
        & (result["usd_plus_10_pct"] >= REQUIRED_RATE * 100)
        & (result["usd_plus_10_familywise_low_pct"] >= REQUIRED_RATE * 100)
    )
    numeric = result.select_dtypes(include="number").columns
    result[numeric] = result[numeric].round(2)
    return result.sort_values(
        ["usd_plus_10_pct", "median_usd_return_pct", "events"], ascending=[False, False, False]
    )


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    output = root / "data" / "resource_arbitrage"
    prices = pd.read_csv(root / "data" / "resource_prices.csv")
    prices["date"] = pd.to_datetime(prices["date"])

    event_rows = _long_events(prices)
    event_rows += _rotation_events(prices)
    event_rows += _pair_events(prices)
    event_rows += _cftc_events(prices, output / "cftc_disaggregated.csv")
    event_rows += _storage_events(prices, [
        output / "eia_gas_storage_0.json", output / "eia_gas_storage_5000.json"
    ])
    events = pd.DataFrame(event_rows)

    rub_dir = root / "data" / "rub_real_return"
    fx = load_cbr_usd_rub(rub_dir / "cbr_usd_rub.xml")
    cpi = load_rosstat_month_start_cpi(rub_dir / "rosstat_cpi_monthly.xlsx")
    events = _enrich_rub(events, fx, cpi)
    summary = _summary(events)

    events.to_csv(output / "resource_strategy_events.csv", index=False)
    summary.to_csv(output / "resource_strategy_summary.csv", index=False)
    eligible = summary.loc[summary["events"] >= MIN_EVENTS]
    report = {
        "method": {
            "oil_and_gasoline": "excluded",
            "signal_timing": "past-only fixed rules; entries and exits do not overlap within a test",
            "horizons_months": list(HORIZONS),
            "target": "net USD return of at least 10%; RUB, real-RUB and 10% deposit comparisons separate",
            "costs_pct": {"single_leg_round_trip": SINGLE_LEG_COST_PCT, "pair_round_trip": PAIR_COST_PCT},
            "price_source": "Yahoo Finance adjusted daily prices",
            "positioning_source": "CFTC Disaggregated Futures Only",
            "gas_storage_source": "EIA Lower 48 weekly working gas",
            "fx_source": "Bank of Russia official USD/RUB",
            "inflation_source": "Rosstat monthly CPI",
            "tax": "Primary columns assume user's zero-tax scenario; 13% sensitivity retained in event file",
            "warning": "UNG and continuous commodity proxies contain roll effects; historical success is not a forecast.",
        },
        "coverage": {
            "events": int(len(events)),
            "tests": int(len(summary)),
            "eligible_tests_20_events": int(len(eligible)),
            "formally_over_80_count": int(summary["formally_over_80"].sum()),
            "first_entry": events["entry_date"].min().date().isoformat(),
            "last_exit": events["exit_date"].max().date().isoformat(),
        },
        "top_eligible": eligible.head(25).replace({np.nan: None}).to_dict(orient="records"),
    }
    (output / "resource_strategy_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report["coverage"], ensure_ascii=False))
    print(eligible[[
        "family", "instrument", "strategy", "horizon_months", "events",
        "usd_plus_10_pct", "positive_usd_pct", "median_usd_return_pct",
        "real_plus_10_pct", "beats_deposit_pct", "usd_plus_10_familywise_low_pct",
    ]].head(30).to_string(index=False))


if __name__ == "__main__":
    main()
