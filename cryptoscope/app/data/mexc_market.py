"""MEXC public spot prices with an in-memory REST polling cache."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Dict, Optional, Sequence

import httpx

from app.data.mexc import (
    MEXC_REST_URL,
    fetch_mexc_spot_symbols,
    normalize_mexc_symbol,
)

CRYPTO_LIVE_CACHE_SECONDS = 30
POLL_INTERVAL_SECONDS = 10

live_prices: Dict[str, float] = {}
_last_update: Dict[str, float] = {}
_connected = False
_start_time = 0.0
_crypto_live_fetched_at = 0.0
_crypto_live_lock = asyncio.Lock()
TICKER_MAP: Dict[str, list[str]] = {}
_all_symbols: set[str] = set()
_mapped_tickers: set[str] = set()


def _latest_live_updated_at() -> datetime | None:
    if not _last_update:
        return None
    return datetime.fromtimestamp(max(_last_update.values()), UTC)


def build_ticker_map(
    tickers: Sequence[str],
    all_symbols: set[str],
) -> Dict[str, list[str]]:
    result = {}
    for ticker in tickers:
        normalized = str(ticker).upper()
        symbol = normalize_mexc_symbol(normalized)
        if symbol and symbol in all_symbols:
            result[normalized] = [symbol]
    return result


async def fetch_latest_prices(
    symbols: Sequence[str],
) -> Dict[str, float]:
    """Fetch all MEXC spot prices once and keep only selected symbols."""
    selected = set(symbols)
    if not selected:
        return {}
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(f"{MEXC_REST_URL}/ticker/price")
        response.raise_for_status()
        payload = response.json()
    if isinstance(payload, dict):
        payload = [payload]
    prices = {}
    for row in payload if isinstance(payload, list) else []:
        symbol = str(row.get("symbol") or "").upper()
        if symbol not in selected:
            continue
        try:
            price = float(row.get("price"))
        except (TypeError, ValueError):
            continue
        if price > 0:
            prices[symbol] = price
    return prices


def get_crypto_live_snapshot(
    tickers: Sequence[str] | None = None,
    updated_since: float | None = None,
) -> tuple[dict[str, float], datetime | None]:
    selected = {
        str(ticker).upper()
        for ticker in (tickers or TICKER_MAP)
    }
    prices = {}
    updates = []
    for ticker in selected:
        for symbol in TICKER_MAP.get(ticker, []):
            updated_at = _last_update.get(symbol, 0.0)
            price = live_prices.get(symbol)
            if (
                price is None
                or price <= 0
                or (
                    updated_since is not None
                    and updated_at < updated_since
                )
            ):
                continue
            prices[ticker] = float(price)
            updates.append(updated_at)
            break
    return (
        prices,
        (
            datetime.fromtimestamp(max(updates), UTC)
            if updates
            else None
        ),
    )


async def _ensure_mapping(tickers: Sequence[str]) -> None:
    global _all_symbols, _mapped_tickers, TICKER_MAP
    selected = [str(ticker).upper() for ticker in tickers]
    if not _all_symbols or any(
        ticker not in _mapped_tickers
        for ticker in selected
    ):
        symbols = await fetch_mexc_spot_symbols()
        if symbols:
            _all_symbols = symbols
            TICKER_MAP.update(build_ticker_map(selected, symbols))
            _mapped_tickers.update(selected)


async def refresh_crypto_live_prices(
    tickers: Sequence[str],
    ttl_seconds: int = CRYPTO_LIVE_CACHE_SECONDS,
) -> dict:
    """Fetch fresh MEXC quotes for selected application tickers."""
    global _crypto_live_fetched_at
    selected = sorted({
        str(ticker).upper()
        for ticker in tickers
        if ticker and "/" in str(ticker)
    })
    if not selected:
        return {
            "prices": {},
            "updated_at": _latest_live_updated_at(),
            "cached": True,
            "provider": "mexc",
        }

    async with _crypto_live_lock:
        cache_age = time.monotonic() - _crypto_live_fetched_at
        cached, cached_at = get_crypto_live_snapshot(
            selected,
            updated_since=(
                time.time() - ttl_seconds
                if ttl_seconds > 0
                else time.time()
            ),
        )
        if (
            _crypto_live_fetched_at
            and cache_age < max(0, ttl_seconds)
            and len(cached) == len(selected)
        ):
            return {
                "prices": cached,
                "updated_at": cached_at,
                "cached": True,
                "provider": "mexc",
            }

        await _ensure_mapping(selected)
        symbols = [
            TICKER_MAP[ticker][0]
            for ticker in selected
            if TICKER_MAP.get(ticker)
        ]
        refresh_started_at = time.time()
        fetched = await fetch_latest_prices(symbols)
        now = time.time()
        for symbol, price in fetched.items():
            live_prices[symbol] = price
            _last_update[symbol] = now
        if fetched:
            _crypto_live_fetched_at = time.monotonic()
        refreshed, refreshed_at = get_crypto_live_snapshot(
            selected,
            updated_since=(
                refresh_started_at
                if ttl_seconds <= 0
                else time.time() - ttl_seconds
            ),
        )
        return {
            "prices": refreshed,
            "updated_at": refreshed_at,
            "cached": False,
            "provider": "mexc",
        }


async def connect_mexc_market(tickers: Optional[list[str]] = None):
    """Keep public MEXC prices warm without requiring API credentials."""
    global _connected, _start_time, _crypto_live_fetched_at, TICKER_MAP
    from app.data.tickers import CRYPTO_TICKERS

    selected = tickers or CRYPTO_TICKERS
    _start_time = time.time()
    delay = 2
    while True:
        try:
            await _ensure_mapping(selected)
            TICKER_MAP = build_ticker_map(selected, _all_symbols)
            symbols = [
                mapped[0]
                for mapped in TICKER_MAP.values()
                if mapped
            ]
            fetched = await fetch_latest_prices(symbols)
            now = time.time()
            for symbol, price in fetched.items():
                live_prices[symbol] = price
                _last_update[symbol] = now
            _connected = bool(fetched)
            if fetched:
                _crypto_live_fetched_at = time.monotonic()
                delay = 2
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            _connected = False
            raise
        except Exception as exc:
            _connected = False
            print(f"[MEXC] Price poll failed: {exc}; retrying in {delay}s")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)


def get_live_price(ticker: str) -> Optional[float]:
    for symbol in TICKER_MAP.get(str(ticker).upper(), []):
        price = live_prices.get(symbol)
        if price and price > 0:
            return float(price)
    return None


def get_all_live_tickers() -> Dict[str, float]:
    result = {}
    for ticker in TICKER_MAP:
        price = get_live_price(ticker)
        if price is not None:
            result[ticker] = price
    return result


def is_connected() -> bool:
    return _connected


def get_uptime() -> float:
    return time.time() - _start_time if _start_time else 0.0
