"""Position size and P&L calculator for MEXC Perpetual Futures."""

import math
from typing import Dict, Any, Optional


def calc_pair_performance(
    signal_type: str,
    entry_a: float,
    entry_b: float,
    price_a_now: float,
    price_b_now: float,
    capital: float = 1000.0,
    leverage: float = 1.0,
    taker_fee_pct: float = 0.02,
    funding_rate_8h_pct: float = 0.01,
    hold_days: float = 0.0,
) -> Dict[str, Any]:
    """Calculate equal-notional pair performance and estimated net P&L.

    ``capital`` is the total capital allocated to the pair. Gross exposure is
    split equally between both legs. Commission includes entry and estimated
    exit fills for both legs.
    """
    values = (entry_a, entry_b, price_a_now, price_b_now)
    complete = all(
        isinstance(value, (int, float))
        and math.isfinite(float(value))
        and float(value) > 0
        for value in values
    )
    if not complete:
        return {"complete": False}

    capital = max(float(capital), 0.0)
    leverage = max(float(leverage), 0.0)
    taker_fee_pct = max(float(taker_fee_pct), 0.0)
    funding_rate_8h_pct = max(float(funding_rate_8h_pct), 0.0)
    hold_days = max(float(hold_days), 0.0)

    price_return_a = (float(price_a_now) / float(entry_a) - 1) * 100
    price_return_b = (float(price_b_now) / float(entry_b) - 1) * 100

    if signal_type == "long_a":
        leg_a_side, leg_b_side = "Лонг", "Шорт"
        leg_a_pnl, leg_b_pnl = price_return_a, -price_return_b
    elif signal_type == "short_a":
        leg_a_side, leg_b_side = "Шорт", "Лонг"
        leg_a_pnl, leg_b_pnl = -price_return_a, price_return_b
    else:
        leg_a_side = leg_b_side = "Ожидание"
        leg_a_pnl = leg_b_pnl = 0.0

    pair_move_pct = leg_a_pnl + leg_b_pnl
    gross_exposure = capital * leverage
    leg_notional = gross_exposure / 2
    gross_pnl = leg_notional * (pair_move_pct / 100)
    gross_return_pct = (
        gross_pnl / capital * 100
        if capital > 0
        else 0.0
    )

    # The pair turns over its full gross exposure once on entry and once
    # on exit. Funding is an estimate; actual exchange rates may be credits.
    commissions = gross_exposure * (taker_fee_pct / 100) * 2
    funding_cost = (
        gross_exposure
        * (funding_rate_8h_pct / 100)
        * hold_days
        * 3
    )
    total_cost = commissions + funding_cost
    net_pnl = gross_pnl - total_cost
    net_return_pct = net_pnl / capital * 100 if capital > 0 else 0.0

    return {
        "complete": True,
        "leg_a_side": leg_a_side,
        "leg_b_side": leg_b_side,
        "price_return_a_pct": round(price_return_a, 4),
        "price_return_b_pct": round(price_return_b, 4),
        "leg_a_pnl_pct": round(leg_a_pnl, 4),
        "leg_b_pnl_pct": round(leg_b_pnl, 4),
        "pair_move_pct": round(pair_move_pct, 4),
        "capital": round(capital, 2),
        "leverage": round(leverage, 2),
        "gross_exposure": round(gross_exposure, 2),
        "leg_notional": round(leg_notional, 2),
        "gross_pnl": round(gross_pnl, 2),
        "gross_return_pct": round(gross_return_pct, 4),
        "commissions": round(commissions, 2),
        "funding_cost": round(funding_cost, 2),
        "total_cost": round(total_cost, 2),
        "net_pnl": round(net_pnl, 2),
        "net_return_pct": round(net_return_pct, 4),
        "taker_fee_pct": taker_fee_pct,
        "funding_rate_pct": funding_rate_8h_pct,
        "hold_days": round(hold_days, 2),
    }


def calc_signal_pnl(
    signal_info: Dict[str, Any],
    capital: float = 1000.0,
    leverage: float = 3.0,
    taker_fee_pct: float = 0.02,
    funding_rate_8h_pct: float = 0.01,
    hold_days: Optional[int] = None,
    avg_hold: Optional[float] = None,
    avg_pnl_z: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Calculate expected P&L for a trading signal on MEXC Perpetual Futures.
    
    Args:
        signal_info: dict with signal data (z_now, corr, etc.)
        capital: position capital in USD
        leverage: leverage multiplier (1-20)
        taker_fee_pct: taker fee percentage per fill
        funding_rate_8h_pct: funding rate per 8h period (%)
        hold_days: expected holding period in days
        avg_hold: average historical holding period
        avg_pnl_z: average P&L in Z-score terms
        
    Returns:
        dict with position_size, commissions, funding, gross_tp, net_tp, etc.
    """
    position_size = capital * leverage
    fills_per_position = 4  # 2 opens + 2 closes
    
    commissions = position_size * (taker_fee_pct / 100) * fills_per_position
    
    days = hold_days if hold_days is not None else (avg_hold if avg_hold is not None else 5)
    funding = position_size * (funding_rate_8h_pct / 100) * (days * 3)  # ~3 8h periods per day
    
    z_move = avg_pnl_z if avg_pnl_z is not None else 2.0
    spread_sd = signal_info.get("spread_sd_pct", 0.05)
    gross_pnl_pct = abs(z_move) * spread_sd * 100
    gross_pnl = position_size * (gross_pnl_pct / 100)
    
    net_pnl = gross_pnl - commissions - funding
    net_pnl_pct = (net_pnl / position_size) * 100
    
    risk_reward = abs(net_pnl / max(commissions + funding, 0.01))
    
    return {
        "capital": round(float(capital), 2),
        "leverage": float(leverage),
        "position_size": round(float(position_size), 2),
        "commissions": round(float(commissions), 2),
        "funding_cost": round(float(funding), 2),
        "total_cost": round(float(commissions + funding), 2),
        "gross_pnl": round(float(gross_pnl), 2),
        "gross_pnl_pct": round(float(gross_pnl_pct), 2),
        "net_pnl": round(float(net_pnl), 2),
        "net_pnl_pct": round(float(net_pnl_pct), 2),
        "risk_reward": round(float(risk_reward), 2),
        "z_move": round(float(z_move), 4),
        "hold_days": int(days),
        "taker_fee_pct": float(taker_fee_pct),
        "funding_rate_pct": float(funding_rate_8h_pct),
        "signal": signal_info.get("signal", "N/A"),
        "signal_type": signal_info.get("signal_type", "wait"),
    }


def compute_position_details(
    pair: Dict[str, Any],
    capital: float = 1000.0,
    leverage: float = 3.0,
    taker_fee: float = 0.02,
    funding_rate: float = 0.01,
) -> Dict[str, Any]:
    """Compute detailed position breakdown for a specific pair."""
    pos_size = capital * leverage
    fills = 4
    commission_total = pos_size * (taker_fee / 100) * fills
    
    avg_hold = pair.get("halflife", 30) if pair.get("halflife") else 30
    days = min(avg_hold, 30)
    funding_cost = pos_size * (funding_rate / 100) * days * 3
    
    return {
        "position_size": round(pos_size, 2),
        "commission_label": f"{commission_total:.2f}",
        "funding_label": f"{funding_cost:.2f}",
        "commission_total": round(commission_total, 2),
        "funding_total": round(funding_cost, 2),
        "total_cost": round(commission_total + funding_cost, 2),
        "leverage": leverage,
        "capital": capital,
        "estimated_days": int(days),
    }
