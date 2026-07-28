"""Engle-Granger cointegration, Z-score, AR(1) forecast engine."""

from typing import Optional

import numpy as np
from statsmodels.tsa.stattools import coint


def _empty_cointegration_result() -> dict:
    return {
        "halflife": None,
        "t_stat": None,
        "p_value": None,
        "critical_5pct": None,
        "is_coint": False,
        "hedge_ratio": None,
        "ar_phi": None,
    }


def engle_granger(pa: np.ndarray, pb: np.ndarray, min_obs: int = 60) -> dict:
    """
    Engle-Granger two-step cointegration test.
    
    Args:
        pa: log prices of asset A
        pb: log prices of asset B
        min_obs: minimum overlapping valid observations
        
    Returns:
        dict with halflife, t_stat, is_coint, hedge_ratio
    """
    try:
        pa = np.asarray(pa, dtype=float)
        pb = np.asarray(pb, dtype=float)
        ok = np.isfinite(pa) & np.isfinite(pb) & (pa > 0) & (pb > 0)
        la = np.log(pa[ok])
        lb = np.log(pb[ok])

        if len(la) < min_obs:
            return _empty_cointegration_result()

        X = np.column_stack([np.ones(len(lb)), lb])
        beta = np.linalg.lstsq(X, la, rcond=None)[0]
        hedge_ratio = float(beta[1])
        resid = la - (beta[0] + beta[1] * lb)

        maxlag = max(0, min(5, len(la) // 50))
        t_stat, p_value, critical_values = coint(
            la,
            lb,
            trend="c",
            maxlag=maxlag,
            autolag="aic" if maxlag else None,
        )
        critical_5pct = float(critical_values[1])
        is_coint = bool(
            np.isfinite(t_stat)
            and np.isfinite(p_value)
            and float(p_value) <= 0.05
            and float(t_stat) <= critical_5pct
        )

        y = resid[1:]
        x = resid[:-1]
        ar_fit = np.linalg.lstsq(
            np.column_stack([np.ones(len(x)), x]),
            y,
            rcond=None,
        )[0]
        ar_phi = float(ar_fit[1])
        halflife = None
        # The usual AR(1) half-life describes monotonic exponential decay and
        # is only defined for 0 < phi < 1. A negative phi is stationary but
        # oscillates around the mean, so abs(phi) would invent a misleading
        # positive holding horizon.
        if np.isfinite(ar_phi) and 0 < ar_phi < 1:
            halflife = -np.log(2) / np.log(ar_phi)

        return {
            "halflife": (
                max(1, round(halflife))
                if halflife is not None and halflife > 0
                else None
            ),
            "t_stat": float(t_stat),
            "p_value": float(p_value),
            "critical_5pct": critical_5pct,
            "is_coint": is_coint,
            "hedge_ratio": hedge_ratio,
            "ar_phi": ar_phi,
        }
    # statsmodels can raise several data-dependent exception classes (for
    # example on constant or near-collinear series). One bad pair must not
    # abort the analysis of its entire market.
    except Exception:
        return _empty_cointegration_result()


def compute_zscore(pa: np.ndarray, pb: np.ndarray, hedge_ratio: Optional[float] = None, min_obs: int = 30) -> dict:
    """
    Compute Z-score series for a pair spread.
    
    Args:
        pa: log prices of asset A
        pb: log prices of asset B
        hedge_ratio: hedge ratio from cointegration (default 1.0)
        min_obs: minimum valid observations
        
    Returns:
        dict with zscores (np.array), z_now, sd, mean
    """
    try:
        pa = np.asarray(pa, dtype=float)
        pb = np.asarray(pb, dtype=float)
        hr = (
            float(hedge_ratio)
            if hedge_ratio is not None and np.isfinite(float(hedge_ratio))
            else 1.0
        )

        ok = np.isfinite(pa) & np.isfinite(pb) & (pa > 0) & (pb > 0)
        la = np.log(pa[ok])
        lb = np.log(pb[ok])
        
        if len(la) < min_obs:
            return {"zscores": None, "z_now": None, "sd": None, "mean": None, "n": len(la)}
        
        spread = la - hr * lb
        mn = float(np.mean(spread))
        sd = float(np.std(spread, ddof=0))
        
        if sd <= 0 or np.isnan(sd):
            return {"zscores": None, "z_now": None, "sd": sd, "mean": mn, "n": len(la)}
        
        zscores = (spread - mn) / sd
        z_now = float(zscores[-1])
        
        return {"zscores": zscores, "z_now": z_now, "sd": sd, "mean": mn, "n": len(la)}
    except Exception:
        return {"zscores": None, "z_now": None, "sd": None, "mean": None, "n": 0}


def fit_fixed_zscore_model(
    pa: np.ndarray,
    pb: np.ndarray,
    z_at_entry: Optional[float] = None,
    price_a_entry: Optional[float] = None,
    price_b_entry: Optional[float] = None,
    hedge_ratio: Optional[float] = None,
    min_obs: int = 30,
) -> dict:
    """Fit and anchor a Z-score model for the lifetime of a favorite."""
    try:
        pa = np.asarray(pa, dtype=float)
        pb = np.asarray(pb, dtype=float)
        ok = np.isfinite(pa) & np.isfinite(pb) & (pa > 0) & (pb > 0)
        pa = pa[ok]
        pb = pb[ok]
        if len(pa) < min_obs:
            return {}

        hr = hedge_ratio
        if hr is None or not np.isfinite(float(hr)):
            hr = engle_granger(pa, pb, min_obs=min_obs).get("hedge_ratio")
        if hr is None or not np.isfinite(float(hr)):
            return {}
        hr = float(hr)

        zres = compute_zscore(pa, pb, hr, min_obs=min_obs)
        spread_sd = zres.get("sd")
        spread_mean = zres.get("mean")
        if (
            spread_sd is None
            or spread_mean is None
            or not np.isfinite(float(spread_sd))
            or float(spread_sd) <= 0
        ):
            return {}

        spread_sd = float(spread_sd)
        spread_mean = float(spread_mean)
        entry_z = (
            float(z_at_entry)
            if z_at_entry is not None and np.isfinite(float(z_at_entry))
            else None
        )
        entry_prices_valid = (
            price_a_entry is not None
            and price_b_entry is not None
            and np.isfinite(float(price_a_entry))
            and np.isfinite(float(price_b_entry))
            and float(price_a_entry) > 0
            and float(price_b_entry) > 0
        )

        if entry_prices_valid:
            entry_spread = (
                np.log(float(price_a_entry))
                - hr * np.log(float(price_b_entry))
            )
            if entry_z is None:
                entry_z = (entry_spread - spread_mean) / spread_sd
            else:
                # Keep the signal's entry Z while freezing future model changes.
                spread_mean = entry_spread - entry_z * spread_sd
        elif entry_z is None:
            entry_z = zres.get("z_now")

        return {
            "hedge_ratio_entry": hr,
            "spread_mean_entry": spread_mean,
            "spread_sd_entry": spread_sd,
            "z_at_entry": float(entry_z) if entry_z is not None else None,
        }
    except (TypeError, ValueError, FloatingPointError):
        return {}


def compute_fixed_zscore(
    price_a: float,
    price_b: float,
    hedge_ratio: float,
    spread_mean: float,
    spread_sd: float,
) -> Optional[float]:
    """Calculate current Z using model parameters frozen at entry."""
    try:
        values = [
            float(price_a),
            float(price_b),
            float(hedge_ratio),
            float(spread_mean),
            float(spread_sd),
        ]
        if not all(np.isfinite(value) for value in values):
            return None
        pa, pb, hr, mean, sd = values
        if pa <= 0 or pb <= 0 or sd <= 0:
            return None
        return float((np.log(pa) - hr * np.log(pb) - mean) / sd)
    except (TypeError, ValueError, FloatingPointError):
        return None


def forecast_zscore(zscores: np.ndarray, min_obs: int = 20) -> dict:
    """
    AR(1) one-step-ahead forecast of Z-score.
    
    Args:
        zscores: Z-score time series
        min_obs: minimum valid observations
        
    Returns:
        dict with z_forecast, phi, intercept, resid_sd
    """
    try:
        zscores = np.asarray(zscores, dtype=float)
        zc = zscores[np.isfinite(zscores)]
        if len(zc) < min_obs:
            fallback = float(zc[-1]) if len(zc) > 0 else None
            return {
                "z_forecast": fallback,
                "phi": None,
                "intercept": None,
                "resid_sd": None,
            }
        
        y = zc[1:]        # z_{t+1}
        x = zc[:-1]        # z_t
        
        Xmat = np.column_stack([np.ones(len(x)), x])
        coefs = np.linalg.lstsq(Xmat, y, rcond=None)[0]
        intercept, phi = float(coefs[0]), float(coefs[1])
        
        fitted = intercept + phi * x
        resid_sd = float(np.std(y - fitted, ddof=0))

        # An explosive/non-stationary AR(1) fit is not a valid mean-reversion
        # forecast and must not be allowed to create an actionable signal.
        z_forecast = (
            intercept + phi * float(zc[-1])
            if np.isfinite(phi) and abs(phi) < 1
            else None
        )

        return {
            "z_forecast": (
                float(z_forecast)
                if z_forecast is not None and np.isfinite(z_forecast)
                else None
            ),
            "phi": phi,
            "intercept": intercept,
            "resid_sd": resid_sd,
        }
    except Exception:
        return {
            "z_forecast": None,
            "phi": None,
            "intercept": None,
            "resid_sd": None,
        }
