"""External data fetching: Twelve Data API."""

import httpx
import pandas as pd
import time
from typing import List, Optional, Dict, Any
from datetime import datetime, timezone

TWELVEDATA_BASE = "https://api.twelvedata.com/time_series"


async def fetch_batch(symbols: List[str], api_key: str, outputsize: int = 5) -> pd.DataFrame:
    """Fetch batch of symbols from Twelve Data API (max 8 per request)."""
    results = []
    
    for batch in [symbols[i:i+8] for i in range(0, len(symbols), 8)]:
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
            
            if isinstance(data, dict) and "values" in data:
                ticker = data.get("meta", {}).get("symbol", symbol_str)
                for entry in data["values"]:
                    results.append({
                        "ticker": ticker,
                        "date": entry["datetime"],
                        "close": float(entry["close"]),
                        "volume": float(entry.get("volume", 0) or 0),
                    })
            elif isinstance(data, list):
                for item in data:
                    meta = item.get("meta", {})
                    ticker = meta.get("symbol", "unknown")
                    for entry in item.get("values", []):
                        results.append({
                            "ticker": ticker,
                            "date": entry["datetime"],
                            "close": float(entry["close"]),
                            "volume": float(entry.get("volume", 0) or 0),
                        })
            
        except Exception as e:
            print(f"Error fetching {symbol_str}: {e}")
        
        time.sleep(75)  # Rate limit for free tier
    
    if not results:
        return pd.DataFrame(columns=["ticker", "date", "close", "volume"])
    
    return pd.DataFrame(results)
