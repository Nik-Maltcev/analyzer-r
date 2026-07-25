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

BINANCE_WS_URL = "wss://stream.binance.com:9443/stream"
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
    prices: Dict[str, float] = {}
    batch_size = 20
    async with httpx.AsyncClient(timeout=10) as client:
        for start in range(0, len(symbols), batch_size):
            batch = symbols[start:start + batch_size]
            try:
                resp = await client.get(
                    f"{BINANCE_REST_URL}/ticker/price",
                    params={"symbols": json.dumps(batch)},
                )
                resp.raise_for_status()
                data = resp.json()
                prices.update({
                    item["symbol"]: float(item["price"])
                    for item in data
                })
            except Exception as exc:
                print(f"[Binance] Failed to fetch price batch: {exc}")
                # One bad or temporarily unavailable symbol must not discard
                # every valid quote in the same batch.
                for symbol in batch:
                    try:
                        resp = await client.get(
                            f"{BINANCE_REST_URL}/ticker/price",
                            params={"symbol": symbol},
                        )
                        resp.raise_for_status()
                        item = resp.json()
                        price = float(item["price"])
                        if price > 0:
                            prices[symbol] = price
                    except Exception as symbol_exc:
                        print(
                            f"[Binance] Failed to fetch {symbol}: "
                            f"{symbol_exc}"
                        )
    return prices


def get_crypto_live_snapshot(
    tickers: Sequence[str] | None = None,
    updated_since: float | None = None,
) -> tuple[dict[str, float], datetime | None]:
    """Return the latest in-memory Binance prices without network I/O."""
    selected = set(tickers) if tickers else set(TICKER_MAP)
    prices = {}
    selected_updates = []
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
            selected_updates.append(updated_at)
            break
    snapshot_updated_at = (
        datetime.fromtimestamp(max(selected_updates), timezone.utc)
        if selected_updates
        else None
    )
    return prices, snapshot_updated_at


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
        cached_prices, updated_at = get_crypto_live_snapshot(
            selected,
            updated_since=(
                time.time() - max(0, ttl_seconds)
                if ttl_seconds > 0
                else time.time()
            ),
        )
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
        refresh_started_at = time.time()
        prices_by_symbol = await fetch_latest_prices(symbols)
        now = time.time()
        for symbol, price in prices_by_symbol.items():
            if price and price > 0:
                live_prices[symbol] = float(price)
                _last_update[symbol] = now

        if prices_by_symbol:
            _crypto_live_fetched_at = time.monotonic()
        refreshed_prices, refreshed_at = get_crypto_live_snapshot(
            selected,
            updated_since=(
                refresh_started_at
                if ttl_seconds <= 0
                else time.time() - ttl_seconds
            ),
        )
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
            # Try only known USD-like quote assets. Prefix matching could map
            # a ticker to another asset with a similar symbol.
            base = ticker.split("/")[0].upper()
            alternatives = [
                f"{base}{quote}"
                for quote in ("USDT", "USDC", "FDUSD")
                if f"{base}{quote}" in all_symbols
            ]
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

    # Subscribe after connecting instead of putting every stream in the URL.
    # This avoids invalid/oversized combined-stream endpoints.
    stream_names = [f"{s.lower()}@miniTicker" for s in all_syms]

    print(f"[Binance] Connecting to {len(stream_names)} streams...")
    try:
        import websockets
    except ImportError:
        print("[Binance] websockets library not installed, using REST polling fallback")
        while True:
            try:
                batch_size = 50
                for i in range(0, len(all_syms), batch_size):
                    prices = await fetch_latest_prices(all_syms[i:i + batch_size])
                    now = time.time()
                    for sym, price in prices.items():
                        live_prices[sym] = price
                        _last_update[sym] = now
                _connected = bool(all_syms)
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                _connected = False
                raise
            except Exception as exc:
                _connected = False
                print(f"[Binance] Poll error: {exc}")
                await asyncio.sleep(10)

    reconnect_delay = 2
    while True:
        try:
            async with websockets.connect(
                BINANCE_WS_URL,
                ping_interval=20,
                ping_timeout=20,
                close_timeout=10,
            ) as ws:
                await ws.send(json.dumps({
                    "method": "SUBSCRIBE",
                    "params": stream_names,
                    "id": 1,
                }))
                _connected = True
                reconnect_delay = 2
                connected_at = time.monotonic()
                print("[Binance] Connected! Streaming prices...")

                async for message in ws:
                    if time.monotonic() - connected_at > 82800:
                        print("[Binance] Periodic reconnect...")
                        break
                    try:
                        envelope = json.loads(message)
                    except json.JSONDecodeError:
                        continue
                    data = envelope.get("data", envelope)
                    if not isinstance(data, dict) or "s" not in data:
                        continue
                    try:
                        symbol = data["s"]
                        price = float(data["c"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if price > 0:
                        live_prices[symbol] = price
                        _last_update[symbol] = time.time()
        except asyncio.CancelledError:
            _connected = False
            raise
        except Exception as exc:
            print(
                f"[Binance] WebSocket error: {exc}; "
                f"reconnecting in {reconnect_delay}s"
            )
        finally:
            _connected = False

        await asyncio.sleep(reconnect_delay)
        reconnect_delay = min(reconnect_delay * 2, 60)


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
