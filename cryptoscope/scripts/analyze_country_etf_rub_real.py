"""Compare country ETF events with a 10% RUB deposit in real RUB terms.

ETF prices come from the existing Yahoo Finance adjusted-price research.
USD/RUB is read from the Bank of Russia XML history and monthly CPI from the
Rosstat workbook.  Inflation is approximated at month granularity.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path
from statistics import NormalDist

import numpy as np
import pandas as pd


STARTING_RUB = 100_000.0
DEPOSIT_RATE = 0.10
CAPITAL_GAINS_TAX_RATE = 0.13
MINIMUM_EVENTS = 20
REQUIRED_PROBABILITY = 0.80


def wilson_lower(successes: int, observations: int, z: float) -> float:
    if observations == 0:
        return 0.0
    probability = successes / observations
    denominator = 1.0 + z * z / observations
    center = probability + z * z / (2.0 * observations)
    margin = z * np.sqrt(
        probability * (1.0 - probability) / observations
        + z * z / (4.0 * observations * observations)
    )
    return float((center - margin) / denominator)


def load_cbr_usd_rub(path: Path) -> pd.Series:
    root = ET.fromstring(path.read_bytes())
    rows: list[tuple[pd.Timestamp, float]] = []
    for record in root.findall("Record"):
        date = pd.to_datetime(record.attrib["Date"], dayfirst=True)
        nominal = float(record.findtext("Nominal", "1").replace(" ", "").replace(",", "."))
        value = float(record.findtext("Value", "").replace(" ", "").replace(",", "."))
        rate = value / nominal
        # Before the 1998 redenomination, CBR values are stated in old rubles.
        if date < pd.Timestamp("1998-01-01"):
            rate /= 1_000.0
        rows.append((date, rate))
    frame = pd.DataFrame(rows, columns=["date", "usd_rub"]).drop_duplicates("date")
    return frame.sort_values("date").set_index("date")["usd_rub"]


def load_rosstat_month_start_cpi(path: Path) -> pd.Series:
    raw = pd.read_excel(path, sheet_name="01", header=None)
    year_columns: list[tuple[int, int]] = []
    for column in range(1, raw.shape[1]):
        value = pd.to_numeric(raw.iloc[3, column], errors="coerce")
        if pd.notna(value) and 1991 <= int(value) <= 2100:
            year_columns.append((column, int(value)))

    monthly_factors: dict[pd.Timestamp, float] = {}
    for column, year in year_columns:
        for month in range(1, 13):
            value = pd.to_numeric(raw.iloc[4 + month, column], errors="coerce")
            if pd.notna(value):
                monthly_factors[pd.Timestamp(year=year, month=month, day=1)] = float(value) / 100.0

    level = 1.0
    month_start_levels: dict[pd.Timestamp, float] = {}
    for month, factor in sorted(monthly_factors.items()):
        month_start_levels[month] = level
        level *= factor
    return pd.Series(month_start_levels, name="cpi_level").sort_index()


def values_asof(series: pd.Series, dates: pd.Series) -> np.ndarray:
    index = pd.DatetimeIndex(pd.to_datetime(dates))
    positions = series.index.searchsorted(index, side="right") - 1
    result = np.full(len(index), np.nan, dtype=float)
    valid = positions >= 0
    result[valid] = series.iloc[positions[valid]].to_numpy(dtype=float)
    return result


def cpi_for_dates(cpi: pd.Series, dates: pd.Series) -> np.ndarray:
    months = pd.DatetimeIndex(pd.to_datetime(dates)).to_period("M").to_timestamp()
    return cpi.reindex(months).to_numpy(dtype=float)


def enrich_events(events: pd.DataFrame, fx: pd.Series, cpi: pd.Series) -> pd.DataFrame:
    frame = events.copy()
    for column in ("entry_date", "exit_date"):
        frame[column] = pd.to_datetime(frame[column])

    frame["entry_usd_rub"] = values_asof(fx, frame["entry_date"])
    frame["exit_usd_rub"] = values_asof(fx, frame["exit_date"])
    frame["entry_cpi"] = cpi_for_dates(cpi, frame["entry_date"])
    frame["exit_cpi"] = cpi_for_dates(cpi, frame["exit_date"])
    frame = frame.dropna(subset=["entry_usd_rub", "exit_usd_rub", "entry_cpi", "exit_cpi"])

    frame["holding_days"] = (frame["exit_date"] - frame["entry_date"]).dt.days
    frame["usd_factor"] = 1.0 + frame["net_return_pct"] / 100.0
    frame["fx_factor"] = frame["exit_usd_rub"] / frame["entry_usd_rub"]
    frame["inflation_factor"] = frame["exit_cpi"] / frame["entry_cpi"]
    frame["rub_factor_no_tax"] = frame["usd_factor"] * frame["fx_factor"]
    rub_gain = (frame["rub_factor_no_tax"] - 1.0).clip(lower=0.0)
    frame["rub_factor_after_tax"] = frame["rub_factor_no_tax"] - CAPITAL_GAINS_TAX_RATE * rub_gain
    frame["deposit_factor"] = 1.0 + DEPOSIT_RATE * frame["holding_days"] / 365.0

    frame["rub_return_no_tax_pct"] = (frame["rub_factor_no_tax"] - 1.0) * 100.0
    frame["rub_return_after_tax_pct"] = (frame["rub_factor_after_tax"] - 1.0) * 100.0
    frame["real_return_no_tax_pct"] = (
        frame["rub_factor_no_tax"] / frame["inflation_factor"] - 1.0
    ) * 100.0
    frame["real_return_after_tax_pct"] = (
        frame["rub_factor_after_tax"] / frame["inflation_factor"] - 1.0
    ) * 100.0
    frame["deposit_real_return_pct"] = (
        frame["deposit_factor"] / frame["inflation_factor"] - 1.0
    ) * 100.0
    frame["final_rub_no_tax"] = STARTING_RUB * frame["rub_factor_no_tax"]
    frame["final_rub_after_tax"] = STARTING_RUB * frame["rub_factor_after_tax"]
    frame["deposit_final_rub"] = STARTING_RUB * frame["deposit_factor"]
    frame["beats_deposit_no_tax"] = frame["rub_factor_no_tax"] > frame["deposit_factor"]
    frame["beats_deposit_after_tax"] = frame["rub_factor_after_tax"] > frame["deposit_factor"]
    frame["real_plus_10_no_tax"] = frame["real_return_no_tax_pct"] >= 10.0
    frame["real_plus_10_after_tax"] = frame["real_return_after_tax_pct"] >= 10.0
    return frame


def summarize_group(group: pd.DataFrame) -> pd.Series:
    count = len(group)
    return pd.Series({
        "events": count,
        "beats_deposit_no_tax_count": int(group["beats_deposit_no_tax"].sum()),
        "beats_deposit_no_tax_pct": round(group["beats_deposit_no_tax"].mean() * 100, 2),
        "beats_deposit_after_tax_count": int(group["beats_deposit_after_tax"].sum()),
        "beats_deposit_after_tax_pct": round(group["beats_deposit_after_tax"].mean() * 100, 2),
        "real_plus_10_no_tax_count": int(group["real_plus_10_no_tax"].sum()),
        "real_plus_10_no_tax_pct": round(group["real_plus_10_no_tax"].mean() * 100, 2),
        "real_plus_10_after_tax_count": int(group["real_plus_10_after_tax"].sum()),
        "real_plus_10_after_tax_pct": round(group["real_plus_10_after_tax"].mean() * 100, 2),
        "median_usd_return_pct": round(group["net_return_pct"].median(), 2),
        "median_rub_return_no_tax_pct": round(group["rub_return_no_tax_pct"].median(), 2),
        "median_rub_return_after_tax_pct": round(group["rub_return_after_tax_pct"].median(), 2),
        "median_real_return_no_tax_pct": round(group["real_return_no_tax_pct"].median(), 2),
        "median_real_return_after_tax_pct": round(group["real_return_after_tax_pct"].median(), 2),
        "median_deposit_real_return_pct": round(group["deposit_real_return_pct"].median(), 2),
        "median_final_rub_no_tax": round(group["final_rub_no_tax"].median(), 0),
        "median_final_rub_after_tax": round(group["final_rub_after_tax"].median(), 0),
        "median_deposit_final_rub": round(group["deposit_final_rub"].median(), 0),
        "worst_final_rub_no_tax": round(group["final_rub_no_tax"].min(), 0),
        "worst_final_rub_after_tax": round(group["final_rub_after_tax"].min(), 0),
    })


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    data_dir = root / "data" / "rub_real_return"
    events = pd.read_csv(root / "data" / "country_etf_long_term" / "country_etf_events.csv")
    labels = pd.read_csv(root / "data" / "country_etf_long_term" / "country_etf_summary.csv")
    label_map = labels[["strategy", "strategy_label"]].drop_duplicates("strategy")

    fx = load_cbr_usd_rub(data_dir / "cbr_usd_rub.xml")
    cpi = load_rosstat_month_start_cpi(data_dir / "rosstat_cpi_monthly.xlsx")
    enriched = enrich_events(events, fx, cpi)

    keys = ["ticker", "country", "horizon_months", "strategy"]
    summary = enriched.groupby(keys, sort=False).apply(summarize_group, include_groups=False).reset_index()
    summary = summary.merge(label_map, on="strategy", how="left")
    familywise_z = NormalDist().inv_cdf(1.0 - 0.05 / (2.0 * len(summary)))
    summary["beats_deposit_no_tax_wilson_low_pct"] = summary.apply(
        lambda row: round(
            wilson_lower(int(row["beats_deposit_no_tax_count"]), int(row["events"]), 1.96) * 100,
            2,
        ),
        axis=1,
    )
    summary["beats_deposit_no_tax_familywise_low_pct"] = summary.apply(
        lambda row: round(
            wilson_lower(
                int(row["beats_deposit_no_tax_count"]), int(row["events"]), familywise_z
            ) * 100,
            2,
        ),
        axis=1,
    )
    summary["formally_qualified"] = (
        (summary["events"] >= MINIMUM_EVENTS)
        & (summary["beats_deposit_no_tax_pct"] > REQUIRED_PROBABILITY * 100)
        & (summary["beats_deposit_no_tax_familywise_low_pct"] > REQUIRED_PROBABILITY * 100)
    )
    eligible = summary.loc[summary["events"] >= MINIMUM_EVENTS].copy()

    best_rows = []
    for country, group in eligible.groupby("country"):
        best = group.sort_values(
            ["beats_deposit_no_tax_pct", "median_real_return_no_tax_pct", "events"],
            ascending=[False, False, False],
        ).iloc[0]
        best_rows.append(best)
    best = pd.DataFrame(best_rows).sort_values("beats_deposit_no_tax_pct", ascending=False)

    buy_hold_12m = eligible.loc[
        (eligible["strategy"] == "buy_hold") & (eligible["horizon_months"] == 12)
    ].sort_values("beats_deposit_no_tax_pct", ascending=False)

    enriched.to_csv(data_dir / "country_etf_rub_real_events.csv", index=False)
    summary.to_csv(data_dir / "country_etf_rub_real_summary.csv", index=False)
    best.to_csv(data_dir / "country_etf_rub_real_best_by_country.csv", index=False)
    buy_hold_12m.to_csv(data_dir / "country_etf_rub_real_buy_hold_12m.csv", index=False)

    report = {
        "method": {
            "starting_rub": STARTING_RUB,
            "deposit_annual_rate_pct": DEPOSIT_RATE * 100,
            "capital_gains_tax_scenario_pct": CAPITAL_GAINS_TAX_RATE * 100,
            "minimum_events": MINIMUM_EVENTS,
            "required_probability_pct": REQUIRED_PROBABILITY * 100,
            "etf_prices": "Yahoo Finance auto-adjusted USD prices; 0.1% round-trip cost already included",
            "fx": "Bank of Russia official USD/RUB; latest prior published rate for each trade date",
            "inflation": "Rosstat monthly CPI, approximated from month-start price levels",
            "tax_note": "Simplified 13% tax on positive RUB capital gain; zero-tax scenario is also shown",
        },
        "coverage": {
            "event_count": int(len(enriched)),
            "first_entry": enriched["entry_date"].min().date().isoformat(),
            "last_exit": enriched["exit_date"].max().date().isoformat(),
            "country_count": int(enriched["country"].nunique()),
            "combination_count": int(len(summary)),
            "formally_qualified_count": int(summary["formally_qualified"].sum()),
        },
        "best_by_country": best.replace({np.nan: None}).to_dict(orient="records"),
        "buy_hold_12m": buy_hold_12m.replace({np.nan: None}).to_dict(orient="records"),
    }
    (data_dir / "country_etf_rub_real_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(json.dumps(report["coverage"], ensure_ascii=False))
    print("\nBest per country (zero-tax comparison):")
    print(best[[
        "country", "ticker", "horizon_months", "strategy", "events",
        "beats_deposit_no_tax_pct", "beats_deposit_after_tax_pct",
        "real_plus_10_no_tax_pct", "median_final_rub_no_tax",
        "median_deposit_final_rub", "worst_final_rub_no_tax",
    ]].head(20).to_string(index=False))
    print("\nPlain buy-and-hold, 12 months:")
    print(buy_hold_12m[[
        "country", "ticker", "events", "beats_deposit_no_tax_pct",
        "beats_deposit_after_tax_pct", "median_rub_return_no_tax_pct",
        "median_real_return_no_tax_pct", "median_deposit_real_return_pct",
        "median_final_rub_no_tax", "worst_final_rub_no_tax",
    ]].to_string(index=False))


if __name__ == "__main__":
    main()
