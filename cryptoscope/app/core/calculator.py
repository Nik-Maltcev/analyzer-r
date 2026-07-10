"""Position size and P&L calculator for MEXC Perpetual Futures."""

import math
from typing import Dict, Any, Optional


def calc_single_performance(
    signal_type: str,
    entry_price: float,
    price_now: float,
    capital: float = 1000.0,
    leverage: float = 1.0,
    taker_fee_pct: float = 0.02,
    funding_rate_8h_pct: float = 0.01,
    hold_days: float = 0.0,
) -> Dict[str, Any]:
    """Calculate net performance for one long or short instrument."""
    values = (entry_price, price_now)
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

    price_return = (float(price_now) / float(entry_price) - 1) * 100
    if signal_type == "short_a":
        side = "Шорт"
        position_move_pct = -price_return
    elif signal_type == "long_a":
        side = "Лонг"
        position_move_pct = price_return
    else:
        side = "Ожидание"
        position_move_pct = 0.0

    gross_exposure = capital * leverage
    gross_pnl = gross_exposure * (position_move_pct / 100)
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
        "leg_a_side": side,
        "price_return_a_pct": round(price_return, 4),
        "leg_a_pnl_pct": round(position_move_pct, 4),
        "pair_move_pct": round(position_move_pct, 4),
        "capital": round(capital, 2),
        "leverage": round(leverage, 2),
        "gross_exposure": round(gross_exposure, 2),
        "gross_pnl": round(gross_pnl, 2),
        "gross_return_pct": round(
            gross_pnl / capital * 100 if capital > 0 else 0.0,
            4,
        ),
        "commissions": round(commissions, 2),
        "funding_cost": round(funding_cost, 2),
        "total_cost": round(total_cost, 2),
        "net_pnl": round(net_pnl, 2),
        "net_return_pct": round(net_return_pct, 4),
        "taker_fee_pct": taker_fee_pct,
        "funding_rate_pct": funding_rate_8h_pct,
        "hold_days": round(hold_days, 2),
    }


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
    hedge_ratio: float = 1.0,
) -> Dict[str, Any]:
    """Calculate hedge-ratio-weighted pair performance and net P&L.

    ``capital`` is the total capital allocated to the pair. Gross exposure is
    divided between the legs using ``1 : abs(hedge_ratio)``. Commission covers
    one complete entry and one complete exit of the gross pair exposure.
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
    hedge_ratio = (
        abs(float(hedge_ratio))
        if isinstance(hedge_ratio, (int, float))
        and math.isfinite(float(hedge_ratio))
        else 1.0
    )
    if hedge_ratio <= 0:
        hedge_ratio = 1.0

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
    weight_a = 1.0 / (1.0 + hedge_ratio)
    weight_b = hedge_ratio / (1.0 + hedge_ratio)
    leg_a_notional = gross_exposure * weight_a
    leg_b_notional = gross_exposure * weight_b
    weighted_pair_return_pct = (
        leg_a_pnl * weight_a + leg_b_pnl * weight_b
    )
    gross_pnl = gross_exposure * (weighted_pair_return_pct / 100)
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
        "weighted_pair_return_pct": round(weighted_pair_return_pct, 4),
        "capital": round(capital, 2),
        "leverage": round(leverage, 2),
        "gross_exposure": round(gross_exposure, 2),
        "leg_notional": round(gross_exposure / 2, 2),
        "leg_a_notional": round(leg_a_notional, 2),
        "leg_b_notional": round(leg_b_notional, 2),
        "hedge_ratio": round(hedge_ratio, 6),
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
    capital = max(float(capital), 0.0)
    leverage = max(float(leverage), 0.0)
    position_size = capital * leverage

    # position_size is already the total exposure of both legs. It turns over
    # once on entry and once on exit, so multiplying it by four double-counted
    # commissions in the old calculator.
    commissions = position_size * (taker_fee_pct / 100) * 2
    
    days = hold_days if hold_days is not None else (avg_hold if avg_hold is not None else 5)
    funding = position_size * (funding_rate_8h_pct / 100) * (days * 3)  # ~3 8h periods per day
    
    z_move = avg_pnl_z
    if z_move is None and bool(signal_info.get("backtest_validated")):
        z_move = signal_info.get("backtest_avg_pnl_sigma")
    if z_move is None:
        z_now = signal_info.get("z_now")
        if isinstance(z_now, (int, float)) and math.isfinite(float(z_now)):
            z_move = max(abs(float(z_now)) - 0.5, 0.0)
    spread_sd = signal_info.get("spread_sd_pct")
    if z_move is None or spread_sd is None:
        return {
            "complete": False,
            "error": "Недостаточно данных о волатильности спреда",
        }

    hedge_ratio = signal_info.get("hedge_ratio", 1.0)
    try:
        hedge_ratio = abs(float(hedge_ratio))
    except (TypeError, ValueError):
        hedge_ratio = 1.0
    if not math.isfinite(hedge_ratio) or hedge_ratio <= 0:
        hedge_ratio = 1.0
    spread_sd = abs(float(spread_sd))
    portfolio_spread_sd = spread_sd / (1.0 + hedge_ratio)
    gross_pnl_pct = float(z_move) * portfolio_spread_sd * 100
    gross_pnl = position_size * (gross_pnl_pct / 100)
    
    net_pnl = gross_pnl - commissions - funding
    net_pnl_pct = (net_pnl / capital) * 100 if capital > 0 else 0.0
    
    risk_reward = abs(net_pnl / max(commissions + funding, 0.01))
    
    return {
        "complete": True,
        "capital": round(float(capital), 2),
        "leverage": float(leverage),
        "position_size": round(float(position_size), 2),
        "commissions": round(float(commissions), 2),
        "funding_cost": round(float(funding), 2),
        "total_cost": round(float(commissions + funding), 2),
        "gross_pnl": round(float(gross_pnl), 2),
        "gross_pnl_pct": round(float(gross_pnl_pct), 2),
        "gross_return_pct": round(
            float(gross_pnl / capital * 100 if capital > 0 else 0.0), 2
        ),
        "net_pnl": round(float(net_pnl), 2),
        "net_pnl_pct": round(float(net_pnl_pct), 2),
        "net_return_pct": round(float(net_pnl_pct), 2),
        "risk_reward": round(float(risk_reward), 2),
        "z_move": round(float(z_move), 4),
        "spread_sd_pct": round(float(spread_sd), 8),
        "portfolio_spread_sd_pct": round(float(portfolio_spread_sd), 8),
        "hedge_ratio": round(float(hedge_ratio), 6),
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
    commission_total = pos_size * (taker_fee / 100) * 2
    
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
