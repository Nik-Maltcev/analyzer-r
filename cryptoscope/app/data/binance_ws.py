"""Binance WebSocket client for real-time price streaming.

Uses Binance's public market data streams — no API key required.
Stores latest prices in a global dict for instant access.
"""

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Dict, Optional, Sequence
from collections import defaultdict

import httpx

BINANCE_WS_URL = "wss://stream.binance.com:9443/ws"
BINANCE_REST_URL = "https://api.binance.com/api/v3"
CRYPTO_LIVE_CACHE_SECONDS = 30

# Global cache of latest prices: {"BTCUSDT": 98765.4, ...}
live_prices: Dict[str, float] = {}
_last_update: Dict[str, float] = {}
_connected: bool = False
_start_time: float = 0.0
_crypto_live_fetched_at = 0.0
_crypto_live_lock = asyncio.Lock()

# Mapping: our tickers → possible Binance symbols
TICKER_MAP: Dict[str, list] = {}

# Track all known ticker symbols we've seen
_all_symbols: set = set()


def _latest_live_updated_at() -> datetime | None:
    if not _last_update:
        return None
    return datetime.fromtimestamp(max(_last_update.values()), timezone.utc)


def normalize_binance_symbol(ticker: str) -> str:
    """Convert our ticker format to Binance symbol.

    BTC/USD → BTCUSDT, ETH/USD → ETHUSDT
    For non-crypto tickers (stocks, RU), returns None.
    """
    if "/" not in ticker:
        return None
    base, quote = ticker.split("/", 1)
    if quote.upper() in ("USD", "USDT"):
        return f"{base.upper()}USDT"
    return f"{base.upper()}{quote.upper()}"


async def fetch_exchange_info() -> list:
    """Fetch all available Binance symbols to map our tickers."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{BINANCE_REST_URL}/exchangeInfo")
            resp.raise_for_status()
            data = resp.json()
            return [s["symbol"] for s in data.get("symbols", []) if s["status"] == "TRADING"]
    except Exception as e:
        print(f"[Binance] Failed to fetch exchange info: {e}")
        return []


async def fetch_latest_prices(symbols: list) -> Dict[str, float]:
    """Fetch latest prices via REST API (fallback/initial load)."""
    if not symbols:
        return {}
    try:
        symbols_str = json.dumps(symbols)
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{BINANCE_REST_URL}/ticker/price", params={"symbols": symbols_str})
            resp.raise_for_status()
            data = resp.json()
            return {item["symbol"]: float(item["price"]) for item in data}
    except Exception as e:
        print(f"[Binance] Failed to fetch prices: {e}")
        return {}


def get_crypto_live_snapshot(
    tickers: Sequence[str] | None = None,
) -> tuple[dict[str, float], datetime | None]:
    """Return the latest in-memory Binance prices without network I/O."""
    selected = set(tickers) if tickers else set(TICKER_MAP)
    prices = {}
    for ticker in selected:
        price = get_live_price(ticker)
        if price is not None and price > 0:
            prices[ticker] = float(price)
    return prices, _latest_live_updated_at()


async def refresh_crypto_live_prices(
    tickers: Sequence[str],
    ttl_seconds: int = CRYPTO_LIVE_CACHE_SECONDS,
) -> dict:
    """Fetch Binance quotes for selected crypto tickers and update live cache."""
    global TICKER_MAP, _all_symbols, _crypto_live_fetched_at

    selected = {
        str(ticker).upper()
        for ticker in tickers
        if ticker and "/" in str(ticker)
    }
    if not selected:
        return {
            "prices": {},
            "updated_at": _latest_live_updated_at(),
            "cached": True,
        }

    async with _crypto_live_lock:
        cache_age = time.monotonic() - _crypto_live_fetched_at
        cached_prices, updated_at = get_crypto_live_snapshot(selected)
        if (
            _crypto_live_fetched_at
            and cache_age < max(0, ttl_seconds)
            and len(cached_prices) == len(selected)
        ):
            return {
                "prices": cached_prices,
                "updated_at": updated_at,
                "cached": True,
            }

        if not _all_symbols or any(ticker not in TICKER_MAP for ticker in selected):
            all_symbols = await fetch_exchange_info()
            if all_symbols:
                _all_symbols = set(all_symbols)
                TICKER_MAP.update(build_ticker_map(list(selected), _all_symbols))

        symbols_by_ticker = {
            ticker: TICKER_MAP.get(ticker, [])[:1]
            for ticker in selected
        }
        symbols = sorted({
            symbol
            for ticker_symbols in symbols_by_ticker.values()
            for symbol in ticker_symbols
        })
        prices_by_symbol = await fetch_latest_prices(symbols)
        now = time.time()
        for symbol, price in prices_by_symbol.items():
            if price and price > 0:
                live_prices[symbol] = float(price)
                _last_update[symbol] = now

        _crypto_live_fetched_at = time.monotonic()
        refreshed_prices, refreshed_at = get_crypto_live_snapshot(selected)
        return {
            "prices": refreshed_prices,
            "updated_at": refreshed_at,
            "cached": False,
        }


def build_ticker_map(tickers: list, all_symbols: set) -> Dict[str, list]:
    """Build mapping from our tickers to Binance symbols."""
    result = {}
    for ticker in tickers:
        sym = normalize_binance_symbol(ticker)
        if sym is None:
            continue
        # Try exact match first
        if sym in all_symbols:
            result[ticker] = [sym]
        else:
            # Try alternatives (some coins trade against different quotes)
            base = ticker.split("/")[0].upper()
            alternatives = [s for s in all_symbols if s.startswith(base) and s in all_symbols]
            if alternatives:
                result[ticker] = alternatives[:3]  # Max 3 alternatives
    return result


async def connect_binance_ws(tickers: Optional[list] = None):
    """Connect to Binance WebSocket and maintain live price feed."""
    global live_prices, _connected, _start_time, TICKER_MAP, _all_symbols

    _start_time = time.time()
    all_symbols_list = await fetch_exchange_info()
    _all_symbols = set(all_symbols_list)

    if tickers:
        TICKER_MAP = build_ticker_map(tickers, _all_symbols)
    else:
        # Track ALL crypto tickers from tickers.py
        from app.data.tickers import CRYPTO_TICKERS
        TICKER_MAP = build_ticker_map(CRYPTO_TICKERS, _all_symbols)

    # Get all unique Binance symbols we need to track
    all_syms = []
    for syms in TICKER_MAP.values():
        all_syms.extend(syms)
    all_syms = list(set(all_syms))

    print(f"[Binance] Tracking {len(all_syms)} symbols for {len(TICKER_MAP)} tickers")

    # Initial price load via REST
    initial = await fetch_latest_prices(all_syms)
    for sym, price in initial.items():
        live_prices[sym] = price
        _last_update[sym] = time.time()
    print(f"[Binance] Loaded {len(initial)} initial prices via REST")

    # Build stream string: btcusdt@miniTicker/ethusdt@miniTicker/...
    stream_names = [f"{s.lower()}@miniTicker" for s in all_syms]
    streams = "/".join(stream_names)
    ws_endpoint = f"{BINANCE_WS_URL}/{streams}"

    print(f"[Binance] Connecting to {len(stream_names)} streams...")
    _connected = True

    # Use httpx for WebSocket (Python 3.11+ has ws support via httpx)
    # Fall back to websockets library approach
    try:
        import websockets
        async with websockets.connect(ws_endpoint, ping_interval=30, ping_timeout=10) as ws:
            print(f"[Binance] Connected! Streaming prices...")

            # Track last reconnect
            last_reconnect = time.time()

            async for message in ws:
                try:
                    data = json.loads(message)
                except json.JSONDecodeError:
                    continue

                if isinstance(data, dict) and "s" in data:
                    symbol = data["s"]
                    price = float(data["c"])  # Last price (miniTicker)
                    live_prices[symbol] = price
                    _last_update[symbol] = time.time()

                # Periodic reconnection (every 23h to avoid stale streams)
                if time.time() - last_reconnect > 82800:  # 23 hours
                    print("[Binance] Periodic reconnect...")
                    break

    except ImportError:
        print("[Binance] websockets library not installed, using REST polling fallback")
        # Fallback: poll REST every 5 seconds
        while _connected:
            try:
                batch_size = 50
                for i in range(0, len(all_syms), batch_size):
                    batch = all_syms[i:i + batch_size]
                    prices = await fetch_latest_prices(batch)
                    for sym, price in prices.items():
                        live_prices[sym] = price
                        _last_update[sym] = time.time()
                await asyncio.sleep(5)
            except Exception as e:
                print(f"[Binance] Poll error: {e}")
                await asyncio.sleep(10)

    except Exception as e:
        print(f"[Binance] WebSocket error: {e}, reconnecting in 5s...")
        _connected = False
        await asyncio.sleep(5)

    # Reconnect loop
    _connected = False
    await asyncio.sleep(2)
    asyncio.create_task(connect_binance_ws(tickers))


def get_live_price(ticker: str) -> Optional[float]:
    """Get latest Binance price for a ticker.

    Returns None if ticker not tracked or no price available.
    """
    if ticker not in TICKER_MAP:
        return None

    symbols = TICKER_MAP[ticker]
    # Return first available price from mapped symbols
    for sym in symbols:
        if sym in live_prices:
            return live_prices[sym]
    return None


def get_all_live_tickers() -> Dict[str, float]:
    """Get latest prices for all tracked tickers.

    Returns dict: {'BTC/USD': 98765.4, 'ETH/USD': 3456.7, ...}
    """
    result = {}
    for ticker, symbols in TICKER_MAP.items():
        for sym in symbols:
            if sym in live_prices:
                result[ticker] = live_prices[sym]
                break
    return result


def is_connected() -> bool:
    """Check if Binance WS is connected."""
    return _connected


def get_uptime() -> float:
    """Get Binance feed uptime in seconds."""
    return time.time() - _start_time if _start_time > 0 else 0
