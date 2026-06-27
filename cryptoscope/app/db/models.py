"""Pydantic models for API responses."""

from pydantic import BaseModel
from typing import Optional
from datetime import datetime


class PairAnalysis(BaseModel):
    id: Optional[int] = None
    market: str
    ticker_a: str
    ticker_b: str
    corr: Optional[float] = None
    halflife: Optional[int] = None
    t_stat: Optional[float] = None
    is_coint: bool = False
    hedge_ratio: Optional[float] = None
    score: Optional[float] = None
    z_now: Optional[float] = None
    z_forecast: Optional[float] = None
    signal: str = "Ждать"
    signal_type: str = "wait"
    strength: str = "Нет"
    signal_started_at: Optional[str] = None
    computed_at: Optional[str] = None


class SignalCard(BaseModel):
    pair_id: str
    ticker_a: str
    ticker_b: str
    signal: str
    signal_type: str
    strength: str
    z_now: Optional[float] = None
    z_forecast: Optional[float] = None
    corr: Optional[float] = None
    is_coint: bool = False
    halflife: Optional[int] = None
    score: Optional[float] = None
    is_favorite: bool = False
    signal_started_at: Optional[str] = None
    signal_expected_end_at: Optional[str] = None
    signal_days_remaining: Optional[int] = None


class FavoritePosition(BaseModel):
    id: int
    pair: str
    ticker_a: str
    ticker_b: str
    signal: Optional[str] = None
    signal_type: Optional[str] = None
    z_at_entry: Optional[float] = None
    price_a_entry: Optional[float] = None
    price_b_entry: Optional[float] = None
    entry_time: Optional[str] = None
    status: str = "active"
    corr: Optional[float] = None
    halflife: Optional[int] = None
    user_id: str = "local"


class ScannerResult(BaseModel):
    scanner: str
    ticker: Optional[str] = None
    ticker_a: Optional[str] = None
    ticker_b: Optional[str] = None
    signal: str = "Ждать"
    score: Optional[float] = None


class DBStatus(BaseModel):
    n_tickers: int = 0
    n_rows: int = 0
    date_min: Optional[str] = None
    date_max: Optional[str] = None
    n_pairs: int = 0
    n_coint: int = 0
    n_active_signals: int = 0
    last_analysis: Optional[str] = None
    last_update: Optional[str] = None


class CalculatorSettings(BaseModel):
    capital: float = 1000.0
    leverage: float = 3.0
    taker_fee_pct: float = 0.02
    funding_rate_pct: float = 0.01


class ForecastTrade(BaseModel):
    pair: str
    ticker_a: str
    ticker_b: str
    signal: str
    signal_type: str
    z_now: Optional[float] = None
    win_rate: Optional[float] = None
    n_similar: int = 0
    avg_pnl_pct: Optional[float] = None
    avg_hold_days: Optional[float] = None
    expected_exit_date: Optional[str] = None
    net_forecast: Optional[float] = None
    best_pnl: Optional[float] = None
    worst_pnl: Optional[float] = None


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str = "1.0.0"
    db_connected: bool = True
    db_tickers: int = 0
    last_analysis: Optional[str] = None
    uptime_seconds: float = 0.0
    timestamp: str = ""
