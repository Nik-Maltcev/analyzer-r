"""Market data and cached forecasts for daily Polymarket direction markets."""

from __future__ import annotations

import asyncio
import bisect
import math
import time
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from urllib.parse import quote

import aiohttp

from app.config import get_settings
from app.core.direction import MIN_PRICE_POINTS, forecast_next_close

PYTH_HISTORY_URL = "https://pyth.dourolabs.app/v1/fixed_rate@200ms/history"
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
FORECAST_CACHE_SECONDS = 30 * 60
FORCE_REFRESH_FLOOR_SECONDS = 60
MOSCOW_TIMEZONE = timezone(timedelta(hours=3))
MOSCOW_BOUNDARY_HOUR = 23
MAX_BOUNDARY_STALENESS_SECONDS = 4 * 24 * 60 * 60


@dataclass(frozen=True)
class PolymarketAsset:
    key: str
    label: str
    category: str
    category_label: str
    yahoo_symbol: str
    pyth_symbol: str | None = None


POLYMARKET_ASSETS = (
    PolymarketAsset("SPY", "S&P 500 ETF", "equities", "ETF", "SPY", "Equity.US.SPY/USD"),
    PolymarketAsset("PLTR", "Palantir", "equities", "Акции", "PLTR", "Equity.US.PLTR/USD"),
    PolymarketAsset("GOOGL", "Alphabet", "equities", "Акции", "GOOGL", "Equity.US.GOOGL/USD"),
    PolymarketAsset("NVDA", "NVIDIA", "equities", "Акции", "NVDA", "Equity.US.NVDA/USD"),
    PolymarketAsset("AMZN", "Amazon", "equities", "Акции", "AMZN", "Equity.US.AMZN/USD"),
    PolymarketAsset("MSFT", "Microsoft", "equities", "Акции", "MSFT", "Equity.US.MSFT/USD"),
    PolymarketAsset("META", "Meta", "equities", "Акции", "META", "Equity.US.META/USD"),
    PolymarketAsset("ABNB", "Airbnb", "equities", "Акции", "ABNB", "Equity.US.ABNB/USD"),
    PolymarketAsset("COIN", "Coinbase", "equities", "Акции", "COIN", "Equity.US.COIN/USD"),
    PolymarketAsset("TSLA", "Tesla", "equities", "Акции", "TSLA", "Equity.US.TSLA/USD"),
    PolymarketAsset("RKLB", "Rocket Lab", "equities", "Акции", "RKLB", "Equity.US.RKLB/USD"),
    PolymarketAsset("AAPL", "Apple", "equities", "Акции", "AAPL", "Equity.US.AAPL/USD"),
    PolymarketAsset("SPX", "S&P 500 Index", "indices", "Индексы", "^GSPC"),
    PolymarketAsset("HSI", "Hang Seng", "indices", "Индексы", "^HSI"),
    PolymarketAsset("NIK", "Nikkei 225", "indices", "Индексы", "^N225"),
    PolymarketAsset("DJIA", "Dow Jones", "indices", "Индексы", "^DJI"),
    PolymarketAsset("UKX", "FTSE 100", "indices", "Индексы", "^FTSE"),
    PolymarketAsset("DAX", "DAX", "indices", "Индексы", "^GDAXI"),
    PolymarketAsset("RUT", "Russell 2000", "indices", "Индексы", "^RUT"),
    PolymarketAsset("NYA", "NYSE Composite", "indices", "Индексы", "^NYA"),
    PolymarketAsset("WTI", "WTI Crude Oil", "commodities", "Сырьё", "CL=F"),
    PolymarketAsset("XAUUSD", "Gold", "commodities", "Металлы", "GC=F", "Metal.XAU/USD"),
    PolymarketAsset("XAGUSD", "Silver", "commodities", "Металлы", "SI=F", "Metal.XAG/USD"),
    PolymarketAsset("NG", "Natural Gas", "commodities", "Сырьё", "NG=F"),
)

_forecast_cache: dict | None = None
_forecast_cached_at = 0.0
_forecast_lock = asyncio.Lock()


def normalize_history(
    timestamps,
    prices,
) -> tuple[list[int], list[float]]:
    points = {}
    for timestamp, price in zip(timestamps or [], prices or []):
        try:
            timestamp_value = int(timestamp)
            price_value = float(price)
        except (TypeError, ValueError):
            continue
        if timestamp_value > 0 and math.isfinite(price_value) and price_value > 0:
            points[timestamp_value] = price_value
    ordered = sorted(points.items())
    return [point[0] for point in ordered], [point[1] for point in ordered]


def parse_pyth_history(payload: dict) -> tuple[list[int], list[float]]:
    if payload.get("s") != "ok":
        return [], []
    return normalize_history(payload.get("t"), payload.get("c"))


def parse_yahoo_history(payload: dict) -> tuple[list[int], list[float]]:
    chart = payload.get("chart") or {}
    results = chart.get("result") or []
    if not results:
        return [], []
    result = results[0]
    indicators = result.get("indicators") or {}
    adjusted = indicators.get("adjclose") or []
    quotes = indicators.get("quote") or []
    prices = adjusted[0].get("adjclose") if adjusted else None
    if not prices and quotes:
        prices = quotes[0].get("close")
    return normalize_history(result.get("timestamp"), prices)


def sample_moscow_23_boundaries(
    timestamps,
    prices,
    now: datetime | None = None,
) -> tuple[list[int], list[float]]:
    """Sample the latest completed hourly candle at each 23:00 Moscow boundary."""
    clean_timestamps, clean_prices = normalize_history(timestamps, prices)
    if not clean_timestamps:
        return [], []

    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current_moscow = current.astimezone(MOSCOW_TIMEZONE)
    last_date = current_moscow.date()
    if current_moscow.hour < MOSCOW_BOUNDARY_HOUR:
        last_date -= timedelta(days=1)

    first_moscow_date = datetime.fromtimestamp(
        clean_timestamps[0],
        timezone.utc,
    ).astimezone(MOSCOW_TIMEZONE).date()
    sampled_timestamps = []
    sampled_prices = []
    completed_timestamps = [
        timestamp + 60 * 60
        for timestamp in clean_timestamps
    ]
    boundary_date: date = first_moscow_date

    while boundary_date <= last_date:
        boundary = datetime.combine(
            boundary_date,
            datetime_time(
                MOSCOW_BOUNDARY_HOUR,
                tzinfo=MOSCOW_TIMEZONE,
            ),
        )
        boundary_timestamp = int(boundary.timestamp())
        point_index = bisect.bisect_right(
            completed_timestamps,
            boundary_timestamp,
        ) - 1
        if point_index >= 0:
            completed_timestamp = completed_timestamps[point_index]
            staleness = boundary_timestamp - completed_timestamp
            if 0 <= staleness <= MAX_BOUNDARY_STALENESS_SECONDS:
                sampled_timestamps.append(boundary_timestamp)
                sampled_prices.append(clean_prices[point_index])
        boundary_date += timedelta(days=1)

    return sampled_timestamps, sampled_prices


async def _fetch_pyth_history(
    session: aiohttp.ClientSession,
    asset: PolymarketAsset,
    start: int,
    end: int,
) -> tuple[list[int], list[float]]:
    headers = {}
    api_key = get_settings().pyth_api_key
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    params = {
        "symbol": asset.pyth_symbol,
        "from": start,
        "to": end,
        "resolution": "60",
    }
    async with session.get(
        PYTH_HISTORY_URL,
        params=params,
        headers=headers,
    ) as response:
        response.raise_for_status()
        return parse_pyth_history(await response.json())


async def _fetch_yahoo_history(
    session: aiohttp.ClientSession,
    asset: PolymarketAsset,
    start: int,
    end: int,
) -> tuple[list[int], list[float]]:
    url = YAHOO_CHART_URL.format(symbol=quote(asset.yahoo_symbol, safe=""))
    params = {
        "period1": start,
        "period2": end,
        "interval": "1h",
        "events": "history",
        "includeAdjustedClose": "true",
    }
    async with session.get(url, params=params) as response:
        response.raise_for_status()
        return parse_yahoo_history(await response.json())


def _forecast_quality(forecast: dict) -> str:
    if not forecast.get("validated_edge", False):
        return "Не подтверждён"
    if forecast["edge_pp"] >= 7:
        return "Сильный"
    if forecast["edge_pp"] >= 4:
        return "Умеренный"
    return "Слабый"


async def _build_asset_forecast(
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    asset: PolymarketAsset,
    start: int,
    end: int,
) -> dict:
    async with semaphore:
        source = "Yahoo proxy"
        source_kind = "proxy"
        timestamps: list[int] = []
        prices: list[float] = []
        pyth_error = None

        if asset.pyth_symbol:
            try:
                timestamps, prices = await _fetch_pyth_history(
                    session,
                    asset,
                    start,
                    end,
                )
                timestamps, prices = sample_moscow_23_boundaries(
                    timestamps,
                    prices,
                )
                if len(prices) >= MIN_PRICE_POINTS:
                    source = "Pyth · 23:00"
                    source_kind = "pyth"
            except Exception as exc:
                pyth_error = exc

        if len(prices) < MIN_PRICE_POINTS:
            timestamps, prices = await _fetch_yahoo_history(
                session,
                asset,
                start,
                end,
            )
            timestamps, prices = sample_moscow_23_boundaries(
                timestamps,
                prices,
            )
            source = "Yahoo proxy · ≤23:00"
            source_kind = "proxy"

        if len(prices) < MIN_PRICE_POINTS:
            detail = f": {pyth_error}" if pyth_error else ""
            raise ValueError(f"Недостаточно истории{detail}")

        forecast = forecast_next_close(prices, timestamps)
        forecast.update({
            "key": asset.key,
            "label": asset.label,
            "category": asset.category,
            "category_label": asset.category_label,
            "source": source,
            "source_kind": source_kind,
            "pyth_symbol": asset.pyth_symbol,
            "quality": _forecast_quality(forecast),
            "window": "23:00 МСК → 23:00 МСК",
            "available": True,
        })
        return forecast


async def get_polymarket_forecasts(force: bool = False) -> dict:
    """Build and cache forecasts for every supported Polymarket underlying."""
    global _forecast_cache, _forecast_cached_at

    async with _forecast_lock:
        cache_age = time.monotonic() - _forecast_cached_at
        cache_limit = (
            FORCE_REFRESH_FLOOR_SECONDS
            if force
            else FORECAST_CACHE_SECONDS
        )
        if _forecast_cache is not None and cache_age < cache_limit:
            return {**_forecast_cache, "cached": True}

        now = datetime.now(timezone.utc)
        end = int((now + timedelta(days=1)).timestamp())
        start = int((now - timedelta(days=600)).timestamp())
        timeout = aiohttp.ClientTimeout(
            total=15,
            connect=5,
            sock_read=12,
        )
        connector = aiohttp.TCPConnector(limit=8)
        headers = {"User-Agent": "MEANX/1.0"}
        semaphore = asyncio.Semaphore(8)

        async with aiohttp.ClientSession(
            timeout=timeout,
            connector=connector,
            headers=headers,
        ) as session:
            tasks = [
                _build_asset_forecast(
                    session,
                    semaphore,
                    asset,
                    start,
                    end,
                )
                for asset in POLYMARKET_ASSETS
            ]
            raw_results = await asyncio.gather(*tasks, return_exceptions=True)

        forecasts = []
        for asset, result in zip(POLYMARKET_ASSETS, raw_results):
            if isinstance(result, Exception):
                forecasts.append({
                    "key": asset.key,
                    "label": asset.label,
                    "category": asset.category,
                    "category_label": asset.category_label,
                    "source": "Недоступно",
                    "source_kind": "error",
                    "available": False,
                    "error": str(result) or "Источник данных недоступен",
                    "edge_pp": -1,
                })
            else:
                forecasts.append(result)

        forecasts.sort(
            key=lambda item: (
                not item.get("available", False),
                not item.get("validated_edge", False),
                -float(item.get("edge_pp", -1)),
                item["key"],
            )
        )
        available = [item for item in forecasts if item.get("available")]
        validated = [
            item for item in available if item.get("validated_edge", False)
        ]
        leader = validated[0] if validated else None
        _forecast_cache = {
            "forecasts": forecasts,
            "leader": leader,
            "available_count": len(available),
            "validated_count": len(validated),
            "total_count": len(forecasts),
            "updated_at": now,
            "cached": False,
        }
        _forecast_cached_at = time.monotonic()
        return _forecast_cache
