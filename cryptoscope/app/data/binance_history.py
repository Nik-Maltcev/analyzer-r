"""Historical Binance spot candles for long-horizon research."""

from __future__ import annotations

import asyncio
import math
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import pandas as pd

BINANCE_DATA_URL = "https://data-api.binance.vision/api/v3"
COINGECKO_URL = "https://api.coingecko.com/api/v3"
DAY_MS = 86_400_000
KLINE_LIMIT = 1000
INTERVAL_MS = {
    "4h": 4 * 60 * 60 * 1000,
    "1d": DAY_MS,
}

# Stablecoins, liquid-staking receipts, and wrapped copies are not independent
# long-term investment candidates. The list is deliberately conservative.
EXCLUDED_SYMBOLS = {
    "BUSD",
    "DAI",
    "FDUSD",
    "FRAX",
    "PYUSD",
    "TUSD",
    "USDC",
    "USDD",
    "USDE",
    "USDP",
    "USDS",
    "USDT",
    "WBTC",
    "WETH",
}
EXCLUDED_ID_FRAGMENTS = (
    "bridged-",
    "wrapped-",
    "staked-",
    "liquid-staked-",
)


def _utc_ms(value: date) -> int:
    return int(datetime(value.year, value.month, value.day, tzinfo=UTC).timestamp() * 1000)


def _history_start(end_date: date, years: int) -> date:
    try:
        return end_date.replace(year=end_date.year - years)
    except ValueError:
        return end_date.replace(year=end_date.year - years, day=28)


async def _get_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    attempts: int = 4,
) -> Any:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            response = await client.get(url, params=params)
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            last_error = exc
            if attempt + 1 < attempts:
                await asyncio.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Historical data request failed: {url}: {last_error}")


async def fetch_binance_symbols(client: httpx.AsyncClient) -> set[str]:
    """Return currently tradable Binance spot symbols."""
    payload = await _get_json(client, f"{BINANCE_DATA_URL}/exchangeInfo")
    result = set()
    for row in payload.get("symbols", []):
        if row.get("status") != "TRADING":
            continue
        if row.get("isSpotTradingAllowed") is False:
            continue
        symbol = str(row.get("symbol") or "").upper()
        if symbol:
            result.add(symbol)
    return result


def _eligible_market_row(row: dict[str, Any], binance_symbols: set[str]) -> bool:
    symbol = str(row.get("symbol") or "").upper()
    coin_id = str(row.get("id") or "").lower()
    if not symbol or symbol in EXCLUDED_SYMBOLS:
        return False
    if any(fragment in coin_id for fragment in EXCLUDED_ID_FRAGMENTS):
        return False
    return f"{symbol}USDT" in binance_symbols


async def fetch_ranked_binance_candidates(
    client: httpx.AsyncClient,
    *,
    candidate_count: int = 40,
) -> list[dict[str, Any]]:
    """Rank current non-stable Binance assets by CoinGecko market cap."""
    binance_symbols = await fetch_binance_symbols(client)
    payload = await _get_json(
        client,
        f"{COINGECKO_URL}/coins/markets",
        params={
            "vs_currency": "usd",
            "order": "market_cap_desc",
            "per_page": min(max(candidate_count * 2, 50), 250),
            "page": 1,
            "sparkline": "false",
        },
    )
    result = []
    for row in payload:
        if not _eligible_market_row(row, binance_symbols):
            continue
        symbol = str(row["symbol"]).upper()
        result.append({
            "coin_id": str(row["id"]),
            "name": str(row.get("name") or symbol),
            "base_asset": symbol,
            "binance_symbol": f"{symbol}USDT",
            "market_cap_rank": int(row.get("market_cap_rank") or 0),
            "market_cap_usd": float(row.get("market_cap") or 0),
        })
        if len(result) >= candidate_count:
            break
    return result


async def fetch_binance_klines(
    client: httpx.AsyncClient,
    symbol: str,
    start_date: date,
    end_date: date,
    *,
    interval: str = "1d",
) -> pd.DataFrame:
    """Fetch completed UTC bars, paginating Binance's 1000-row limit."""
    if interval not in INTERVAL_MS:
        raise ValueError(f"Unsupported Binance interval: {interval}")
    interval_ms = INTERVAL_MS[interval]
    cursor_ms = _utc_ms(start_date)
    end_ms = _utc_ms(end_date + timedelta(days=1)) - 1
    rows: list[dict[str, Any]] = []

    while cursor_ms <= end_ms:
        payload = await _get_json(
            client,
            f"{BINANCE_DATA_URL}/klines",
            params={
                "symbol": symbol,
                "interval": interval,
                "startTime": cursor_ms,
                "endTime": end_ms,
                "limit": KLINE_LIMIT,
            },
        )
        if not payload:
            break
        for candle in payload:
            if not isinstance(candle, list) or len(candle) < 11:
                continue
            try:
                open_ms = int(candle[0])
                values = [float(candle[index]) for index in range(1, 6)]
                quote_volume = float(candle[7])
                trades = int(candle[8])
            except (TypeError, ValueError):
                continue
            if not all(math.isfinite(value) for value in values):
                continue
            rows.append({
                "date": datetime.fromtimestamp(open_ms / 1000, UTC).isoformat(),
                "open": values[0],
                "high": values[1],
                "low": values[2],
                "close": values[3],
                "volume": values[4],
                "quote_volume": quote_volume,
                "trades": trades,
            })
        last_open_ms = int(payload[-1][0])
        next_cursor = last_open_ms + interval_ms
        if next_cursor <= cursor_ms or len(payload) < KLINE_LIMIT:
            break
        cursor_ms = next_cursor
        await asyncio.sleep(0.08)

    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    return (
        frame.drop_duplicates("date", keep="last")
        .sort_values("date")
        .reset_index(drop=True)
    )


async def fetch_binance_daily_klines(
    client: httpx.AsyncClient,
    symbol: str,
    start_date: date,
    end_date: date,
) -> pd.DataFrame:
    """Fetch completed UTC daily bars."""
    frame = await fetch_binance_klines(
        client,
        symbol,
        start_date,
        end_date,
        interval="1d",
    )
    if not frame.empty:
        frame["date"] = pd.to_datetime(frame["date"]).dt.date.astype(str)
    return frame


async def build_binance_top_dataset(
    *,
    top_n: int = 10,
    years: int = 7,
    end_date: date | None = None,
    fixed_candidates: list[dict[str, Any]] | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Build a top-N or previously frozen universe with complete Binance history."""
    end_date = end_date or (datetime.now(UTC).date() - timedelta(days=1))
    start_date = _history_start(end_date, years)
    expected_days = (end_date - start_date).days + 1
    minimum_rows = math.floor(expected_days * 0.97)
    timeout = httpx.Timeout(45, connect=15)
    headers = {"User-Agent": "MEANX-long-term-research/1.0"}
    selected: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    frames: list[pd.DataFrame] = []

    async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
        if fixed_candidates is not None:
            candidates = fixed_candidates
        else:
            candidates = await fetch_ranked_binance_candidates(
                client,
                candidate_count=max(30, top_n * 3),
            )
        for candidate in candidates:
            frame = await fetch_binance_daily_klines(
                client,
                candidate["binance_symbol"],
                start_date,
                end_date,
            )
            if len(frame) < minimum_rows:
                if fixed_candidates is not None:
                    raise RuntimeError(
                        f"Frozen asset {candidate['binance_symbol']} has only "
                        f"{len(frame)}/{expected_days} requested daily bars"
                    )
                excluded.append({
                    **candidate,
                    "reason": "insufficient_history",
                    "rows": int(len(frame)),
                })
                continue
            enriched = frame.copy()
            enriched.insert(0, "ticker", f"{candidate['base_asset']}/USD")
            enriched.insert(1, "binance_symbol", candidate["binance_symbol"])
            enriched["coin_id"] = candidate["coin_id"]
            enriched["name"] = candidate["name"]
            enriched["market_cap_rank"] = candidate["market_cap_rank"]
            enriched["provider"] = "binance"
            frames.append(enriched)
            selected.append({**candidate, "rows": int(len(frame))})
            if len(selected) >= top_n:
                break

    if len(selected) < top_n:
        raise RuntimeError(
            f"Only {len(selected)}/{top_n} assets have at least {minimum_rows} daily bars"
        )

    dataset = pd.concat(frames, ignore_index=True)
    dataset = dataset.sort_values(["ticker", "date"]).reset_index(drop=True)
    metadata = {
        "selection_method": (
            "frozen_universe"
            if fixed_candidates is not None
            else "current_market_cap_rank_with_full_binance_history"
        ),
        "selection_date": end_date.isoformat(),
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "years": years,
        "top_n": top_n,
        "rows": int(len(dataset)),
        "selected": selected,
        "excluded_before_selection_complete": excluded,
        "survivorship_bias_warning": (
            "The universe was frozen from a current ranking and therefore is suitable "
            "for stable forward monitoring, not for an unbiased historical top-N index."
        ),
    }
    return dataset, metadata
