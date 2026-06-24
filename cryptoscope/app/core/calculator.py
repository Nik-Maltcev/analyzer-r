"""Position size and P&L calculator for MEXC Perpetual Futures."""

from typing import Dict, Any, Optional


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
