"""External data fetching: Twelve Data API."""

import asyncio

import httpx
import pandas as pd

TWELVEDATA_BASE = "https://api.twelvedata.com/time_series"


def parse_time_series_response(data, fallback_symbol: str = "") -> pd.DataFrame:
    """Normalize single and keyed batch responses from Twelve Data."""
    series_payloads = []
    if isinstance(data, dict) and "values" in data:
        series_payloads.append((fallback_symbol, data))
    elif isinstance(data, dict):
        for identifier, payload in data.items():
            if isinstance(payload, dict) and "values" in payload:
                series_payloads.append((str(identifier), payload))
    elif isinstance(data, list):
        for payload in data:
            if isinstance(payload, dict) and "values" in payload:
                series_payloads.append((fallback_symbol, payload))

    results = []
    for identifier, payload in series_payloads:
        ticker = payload.get("meta", {}).get("symbol") or identifier
        for entry in payload.get("values", []):
            try:
                results.append({
                    "ticker": ticker,
                    "date": entry["datetime"],
                    "close": float(entry["close"]),
                    "volume": float(entry.get("volume", 0) or 0),
                })
            except (KeyError, TypeError, ValueError):
                continue

    if not results:
        return pd.DataFrame(columns=["ticker", "date", "close", "volume"])
    return pd.DataFrame(results)


async def fetch_batch(symbols: list[str], api_key: str, outputsize: int = 5) -> pd.DataFrame:
    """Fetch batch of symbols from Twelve Data API (max 8 per request)."""
    results = []
    batches = [symbols[i:i + 8] for i in range(0, len(symbols), 8)]
    
    for idx, batch in enumerate(batches):
        symbol_str = ",".join(batch)
        params = {
            "symbol": symbol_str,
            "interval": "1day",
            "outputsize": outputsize,
            "apikey": api_key,
        }
        
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(TWELVEDATA_BASE, params=params)
                resp.raise_for_status()
                data = resp.json()
            
            if "status" in data and data.get("status") == "error":
                message = data.get("message", "Unknown error")
                print(f"Twelve Data error for {symbol_str}: {message}")
                continue
            
            normalized = parse_time_series_response(data, symbol_str)
            if not normalized.empty:
                results.extend(normalized.to_dict(orient="records"))
            
        except Exception as e:
            print(f"Error fetching {symbol_str}: {e}")
        
        if idx < len(batches) - 1:
            await asyncio.sleep(75)  # Rate limit for free tier
    
    if not results:
        return pd.DataFrame(columns=["ticker", "date", "close", "volume"])
    
    return pd.DataFrame(results)
