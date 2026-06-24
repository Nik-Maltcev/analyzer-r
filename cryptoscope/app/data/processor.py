"""Data processing utilities."""

import numpy as np
import pandas as pd
from typing import Dict, Any, List, Optional


def pivot_to_wide(df: pd.DataFrame, ticker_col: str = "ticker",
                  date_col: str = "date", value_col: str = "close") -> pd.DataFrame:
    """Pivot long-format price data to wide matrix [dates × tickers]."""
    wide = df.pivot(index=date_col, columns=ticker_col, values=value_col)
    wide.index = pd.to_datetime(wide.index)
    wide = wide.sort_index()
    return wide


def compute_log_returns(df: pd.DataFrame) -> pd.DataFrame:
    """Compute log returns from price DataFrame."""
    return np.log(df / df.shift(1))


def get_latest_prices(price_wide: pd.DataFrame) -> Dict[str, float]:
    """Get latest price for each ticker."""
    latest_date = price_wide.index.max()
    row = price_wide.loc[latest_date]
    return row.dropna().to_dict()


def normalize_ticker(ticker: str) -> str:
    """Normalize ticker name: '/' to '.' for DataFrame column compatibility."""
    return ticker.replace("/", ".")


def denormalize_ticker(ticker: str) -> str:
    """Reverse normalization: '.' back to '/'."""
    parts = ticker.split(".")
    if len(parts) == 2:
        return f"{parts[0]}/{parts[1]}"
    return ticker
