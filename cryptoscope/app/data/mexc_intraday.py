"""Incremental completed 5-minute candles for the Reversal Lab."""

from __future__ import annotations

import asyncio
import math
import sqlite3
import time
from datetime import UTC, datetime, timedelta

import httpx

from app.data.mexc import MEXC_REST_URL, fetch_mexc_spot_symbols, normalize_mexc_symbol
from app.data.tickers import CRYPTO_TICKERS

# The intraday research universe follows the application's complete configured
# crypto list. Unsupported MEXC pairs are reported and skipped by the collector.
INTRADAY_TICKERS = tuple(CRYPTO_TICKERS[:100])
# Backwards-compatible name for the retired Reversal Lab internals.
REVERSAL_TICKERS = INTRADAY_TICKERS
INTERVAL_MS = 5 * 60 * 1000
# Five-minute candles are only needed for live entries and exits. Historical
# comparisons use the separate hourly store below.
HISTORY_DAYS = 10
# Keep a small indicator warm-up buffer beyond the three-year report window.
# The loader is incremental, so only the first refresh needs the full backfill.
SHORT_TERM_HISTORY_DAYS = 1125
# MEXC documents 1000 as the maximum, but the live endpoint currently caps
# kline responses at 500. Pagination must use the effective limit; otherwise a
# 500-row response looks like the final page and leaves the dataset stale.
PAGE_LIMIT = 500
HISTORICAL_PAGE_ATTEMPTS = 4
HISTORICAL_RETRY_BASE_SECONDS = 1.0


async def refresh_reversal_candles(
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
) -> dict:
    """Upsert completed MEXC candles, resuming from each ticker's last bar."""
    now = now or datetime.now(UTC)
    completed_before = int(now.timestamp() * 1000) // INTERVAL_MS * INTERVAL_MS
    initial_start = int((now - timedelta(days=HISTORY_DAYS)).timestamp() * 1000)
    coverage = {
        str(row[0]): (int(row[1]), int(row[2]))
        for row in conn.execute(
            "SELECT ticker, MIN(open_time), MAX(open_time) "
            "FROM reversal_candles GROUP BY ticker"
        ).fetchall()
        if row[1] is not None and row[2] is not None
    }
    inserted = 0
    failures: list[str] = []
    request_at = 0.0

    async with httpx.AsyncClient(timeout=30) as client:
        available = await fetch_mexc_spot_symbols(client)
        eligible_tickers = [
            ticker for ticker in INTRADAY_TICKERS
            if normalize_mexc_symbol(ticker) in available
        ]
        unavailable_tickers = sorted(set(INTRADAY_TICKERS) - set(eligible_tickers))
        failures.extend(f"{ticker}: unavailable on MEXC spot" for ticker in unavailable_tickers)
        for ticker in eligible_tickers:
            symbol = normalize_mexc_symbol(ticker)
            try:
                if ticker in coverage:
                    earliest, latest = coverage[ticker]
                    ranges = [(max(initial_start, latest - INTERVAL_MS), completed_before)]
                    if earliest > initial_start + INTERVAL_MS:
                        ranges.append((initial_start, earliest))
                else:
                    ranges = [(initial_start, completed_before)]
                for range_start, range_end in ranges:
                    cursor = range_start
                    while cursor < range_end:
                        delay = 0.15 - (time.monotonic() - request_at)
                        if delay > 0:
                            await asyncio.sleep(delay)
                        response = await client.get(
                            f"{MEXC_REST_URL}/klines",
                            params={
                                "symbol": symbol,
                                "interval": "5m",
                                "startTime": cursor,
                                "endTime": min(
                                    range_end, cursor + PAGE_LIMIT * INTERVAL_MS
                                ) - 1,
                                "limit": PAGE_LIMIT,
                            },
                        )
                        request_at = time.monotonic()
                        response.raise_for_status()
                        payload = response.json()
                        if not isinstance(payload, list) or not payload:
                            break
                        batch = []
                        last_open = cursor
                        for candle in payload:
                            if not isinstance(candle, list) or len(candle) < 6:
                                continue
                            open_time = int(candle[0])
                            values = [float(candle[index] or 0) for index in range(1, 6)]
                            if open_time >= range_end or not all(
                                math.isfinite(value) for value in values
                            ):
                                continue
                            quote_volume = 0.0
                            if len(candle) > 7:
                                try:
                                    quote_volume = float(candle[7] or 0)
                                except (TypeError, ValueError):
                                    quote_volume = 0.0
                            batch.append((
                                ticker, open_time, values[0], values[1], values[2],
                                values[3], values[4], quote_volume, "mexc",
                            ))
                            last_open = max(last_open, open_time)
                        if batch:
                            before = conn.total_changes
                            conn.executemany(
                                """
                                INSERT INTO reversal_candles (
                                    ticker, open_time, open, high, low, close,
                                    volume, quote_volume, provider
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                                ON CONFLICT(ticker, open_time) DO UPDATE SET
                                    open=excluded.open, high=excluded.high,
                                    low=excluded.low, close=excluded.close,
                                    volume=excluded.volume,
                                    quote_volume=excluded.quote_volume,
                                    provider=excluded.provider,
                                    imported_at=datetime('now')
                                """,
                                batch,
                            )
                            inserted += conn.total_changes - before
                            conn.commit()
                        next_cursor = last_open + INTERVAL_MS
                        if next_cursor <= cursor or len(payload) < PAGE_LIMIT:
                            break
                        cursor = next_cursor
            except Exception as exc:
                failures.append(f"{ticker}: {str(exc)[:120]}")

    count, ticker_count, minimum, maximum = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT ticker), MIN(open_time), MAX(open_time) FROM reversal_candles"
    ).fetchone()
    return {
        "inserted": inserted,
        "candle_count": int(count or 0),
        "ticker_count": int(ticker_count or 0),
        "requested_ticker_count": len(INTRADAY_TICKERS),
        "eligible_ticker_count": len(eligible_tickers),
        "data_start": minimum,
        "data_end": maximum,
        "completed_before": completed_before,
        "failures": failures,
    }


async def refresh_short_term_execution_candles(
    conn: sqlite3.Connection,
    candidates: list[dict],
    *,
    now: datetime | None = None,
) -> dict:
    """Cache exact 5-minute execution windows required by a backtest.

    The hourly store is sufficient for signal generation. Downloading complete
    5-minute history for the whole universe is wasteful, so this collector only
    fetches the entry-to-exit windows of candidates that can actually be traded.
    """
    now = now or datetime.now(UTC)
    completed_before = int(now.timestamp() * 1000) // INTERVAL_MS * INTERVAL_MS
    windows_by_ticker: dict[str, list[tuple[int, int]]] = {}
    for candidate in candidates:
        start = int(candidate["signal_time"])
        horizon = start + int(candidate["hold_minutes"]) * 60_000
        if horizon >= completed_before:
            continue
        windows_by_ticker.setdefault(str(candidate["ticker"]), []).append(
            (start, horizon + INTERVAL_MS)
        )

    merged_by_ticker: dict[str, list[tuple[int, int]]] = {}
    for ticker, windows in windows_by_ticker.items():
        merged: list[list[int]] = []
        for start, end in sorted(windows):
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        merged_by_ticker[ticker] = [(start, end) for start, end in merged]

    inserted = 0
    requested_windows = 0
    request_at = 0.0
    failures: list[str] = []
    async with httpx.AsyncClient(timeout=30) as client:
        for ticker, windows in merged_by_ticker.items():
            symbol = normalize_mexc_symbol(ticker)
            for range_start, range_end in windows:
                expected = (range_end - range_start) // INTERVAL_MS
                existing = conn.execute(
                    "SELECT COUNT(*) FROM reversal_candles "
                    "WHERE ticker=? AND open_time>=? AND open_time<?",
                    (ticker, range_start, range_end),
                ).fetchone()[0]
                if int(existing or 0) >= expected:
                    continue
                requested_windows += 1
                cursor = range_start
                try:
                    while cursor < range_end:
                        page_end = min(range_end, cursor + PAGE_LIMIT * INTERVAL_MS)
                        delay = 0.10 - (time.monotonic() - request_at)
                        if delay > 0:
                            await asyncio.sleep(delay)
                        response = await client.get(
                            f"{MEXC_REST_URL}/klines",
                            params={
                                "symbol": symbol,
                                "interval": "5m",
                                "startTime": cursor,
                                "endTime": page_end - 1,
                                "limit": PAGE_LIMIT,
                            },
                        )
                        request_at = time.monotonic()
                        response.raise_for_status()
                        payload = response.json()
                        if not isinstance(payload, list) or not payload:
                            break
                        batch = []
                        for candle in payload:
                            if not isinstance(candle, list) or len(candle) < 6:
                                continue
                            open_time = int(candle[0])
                            if not range_start <= open_time < range_end:
                                continue
                            values = [float(candle[index] or 0) for index in range(1, 6)]
                            if not all(math.isfinite(value) for value in values):
                                continue
                            try:
                                quote_volume = float(candle[7] or 0) if len(candle) > 7 else 0.0
                            except (TypeError, ValueError):
                                quote_volume = 0.0
                            batch.append((
                                ticker, open_time, values[0], values[1], values[2],
                                values[3], values[4], quote_volume, "mexc-execution",
                            ))
                        if batch:
                            before = conn.total_changes
                            conn.executemany(
                                """
                                INSERT INTO reversal_candles (
                                    ticker, open_time, open, high, low, close,
                                    volume, quote_volume, provider
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                                ON CONFLICT(ticker, open_time) DO UPDATE SET
                                    open=excluded.open, high=excluded.high,
                                    low=excluded.low, close=excluded.close,
                                    volume=excluded.volume,
                                    quote_volume=excluded.quote_volume,
                                    provider=excluded.provider,
                                    imported_at=datetime('now')
                                """,
                                batch,
                            )
                            inserted += conn.total_changes - before
                            conn.commit()
                        cursor = page_end
                except Exception as exc:
                    failures.append(f"{ticker} {range_start}: {str(exc)[:120]}")

    missing_windows: list[str] = []
    for ticker, windows in merged_by_ticker.items():
        for range_start, range_end in windows:
            expected = (range_end - range_start) // INTERVAL_MS
            existing = conn.execute(
                "SELECT COUNT(*) FROM reversal_candles "
                "WHERE ticker=? AND open_time>=? AND open_time<?",
                (ticker, range_start, range_end),
            ).fetchone()[0]
            if int(existing or 0) != expected:
                missing_windows.append(
                    f"{ticker}:{range_start}-{range_end} ({existing}/{expected})"
                )
    return {
        "candidate_count": len(candidates),
        "merged_window_count": sum(len(items) for items in merged_by_ticker.values()),
        "requested_window_count": requested_windows,
        "inserted": inserted,
        "missing_window_count": len(missing_windows),
        "missing_windows": missing_windows[:20],
        "failures": failures,
    }


async def refresh_short_term_hourly_candles(
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
) -> dict:
    """Incrementally maintain full hourly history for long-window backtests."""
    now = now or datetime.now(UTC)
    interval_ms = 60 * 60 * 1000
    completed_before = int(now.timestamp() * 1000) // interval_ms * interval_ms
    raw_initial_start = int(
        (now - timedelta(days=SHORT_TERM_HISTORY_DAYS)).timestamp() * 1000
    )
    # Klines are aligned to whole hours. Starting at an arbitrary minute leaves
    # a final partial-hour request on the next refresh; MEXC correctly returns
    # no candle for it, which used to look like a broken historical backfill.
    initial_start = (
        (raw_initial_start + interval_ms - 1) // interval_ms * interval_ms
    )
    coverage = {
        str(row[0]): (int(row[1]), int(row[2]))
        for row in conn.execute(
            "SELECT ticker, MIN(open_time), MAX(open_time) "
            "FROM short_term_hourly_candles GROUP BY ticker"
        ).fetchall()
        if row[1] is not None and row[2] is not None
    }
    inserted = 0
    failures: list[str] = []
    request_at = 0.0

    def upsert(payload: list, ticker: str, range_end: int) -> int:
        batch = []
        for candle in payload:
            if not isinstance(candle, list) or len(candle) < 6:
                continue
            open_time = int(candle[0])
            values = [float(candle[index] or 0) for index in range(1, 6)]
            if open_time >= range_end or not all(math.isfinite(value) for value in values):
                continue
            try:
                quote_volume = float(candle[7] or 0) if len(candle) > 7 else 0.0
            except (TypeError, ValueError):
                quote_volume = 0.0
            batch.append((
                ticker, open_time, values[0], values[1], values[2], values[3],
                values[4], quote_volume, "mexc-1h",
            ))
        if not batch:
            return 0
        before = conn.total_changes
        conn.executemany(
            """
            INSERT INTO short_term_hourly_candles (
                ticker, open_time, open, high, low, close,
                volume, quote_volume, provider
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker, open_time) DO UPDATE SET
                open=excluded.open, high=excluded.high, low=excluded.low,
                close=excluded.close, volume=excluded.volume,
                quote_volume=excluded.quote_volume, provider=excluded.provider,
                imported_at=datetime('now')
            """,
            batch,
        )
        changed = conn.total_changes - before
        conn.commit()
        return changed

    async with httpx.AsyncClient(timeout=30) as client:
        available = await fetch_mexc_spot_symbols(client)
        eligible_tickers = [
            ticker for ticker in INTRADAY_TICKERS
            if normalize_mexc_symbol(ticker) in available
        ]
        # BTC defines the report's historical coverage. Load it first so an
        # interrupted backfill fails early instead of doing work for every alt.
        eligible_tickers.sort(key=lambda ticker: ticker != "BTC/USD")
        for ticker in eligible_tickers:
            symbol = normalize_mexc_symbol(ticker)
            try:
                async def fetch_page(page_start: int, page_end: int) -> list:
                    nonlocal request_at
                    last_error: Exception | None = None
                    for attempt in range(HISTORICAL_PAGE_ATTEMPTS):
                        delay = 0.10 - (time.monotonic() - request_at)
                        if delay > 0:
                            await asyncio.sleep(delay)
                        try:
                            response = await client.get(
                                f"{MEXC_REST_URL}/klines",
                                params={
                                    "symbol": symbol,
                                    "interval": "60m",
                                    "startTime": page_start,
                                    "endTime": page_end - 1,
                                    "limit": PAGE_LIMIT,
                                },
                            )
                            request_at = time.monotonic()
                            response.raise_for_status()
                            payload = response.json()
                            if isinstance(payload, list) and payload:
                                return payload
                            last_error = RuntimeError("MEXC returned an empty page")
                        except (httpx.HTTPError, ValueError) as exc:
                            last_error = exc
                        if attempt + 1 < HISTORICAL_PAGE_ATTEMPTS:
                            await asyncio.sleep(
                                HISTORICAL_RETRY_BASE_SECONDS * (2 ** attempt)
                            )
                    if isinstance(last_error, RuntimeError):
                        return []
                    raise RuntimeError(
                        "MEXC historical page failed after retries: "
                        f"{last_error or 'invalid response'}"
                    )

                if ticker in coverage:
                    earliest, latest = coverage[ticker]
                    cursor = max(initial_start, latest - interval_ms)
                    while cursor < completed_before:
                        page_end = min(
                            completed_before, cursor + PAGE_LIMIT * interval_ms
                        )
                        payload = await fetch_page(cursor, page_end)
                        inserted += upsert(payload, ticker, page_end)
                        cursor = page_end
                    backfill_end = earliest
                else:
                    backfill_end = completed_before

                # Walk backwards in fixed-width pages. MEXC may return an empty
                # array for one oversized historical request even when every
                # individual page is available.
                while backfill_end > initial_start:
                    page_start = max(
                        initial_start, backfill_end - PAGE_LIMIT * interval_ms
                    )
                    payload = await fetch_page(page_start, backfill_end)
                    if not payload:
                        if ticker == "BTC/USD":
                            start_label = datetime.fromtimestamp(
                                page_start / 1000, tz=UTC
                            ).isoformat()
                            end_label = datetime.fromtimestamp(
                                backfill_end / 1000, tz=UTC
                            ).isoformat()
                            raise RuntimeError(
                                "MEXC returned no BTC hourly history for "
                                f"{start_label}..{end_label}; saved progress is intact"
                            )
                        # A recently listed altcoin legitimately has no candles
                        # before its listing date. Its shorter history must not
                        # block the BTC-defined three-year report window.
                        break
                    inserted += upsert(payload, ticker, backfill_end)
                    backfill_end = page_start
                if ticker == "BTC/USD":
                    # A previous interrupted request can leave a hole between
                    # the oldest and newest stored candle. The forward/backward
                    # cursors only extend the edges, so explicitly repair every
                    # missing hour before declaring the three-year history ready.
                    btc_open_times = [
                        int(row[0])
                        for row in conn.execute(
                            "SELECT open_time FROM short_term_hourly_candles "
                            "WHERE ticker='BTC/USD' AND open_time>=? AND open_time<? "
                            "ORDER BY open_time",
                            (initial_start, completed_before),
                        ).fetchall()
                    ]
                    missing_ranges: list[tuple[int, int]] = []
                    expected_open = initial_start
                    for open_time in btc_open_times:
                        if open_time > expected_open:
                            missing_ranges.append((expected_open, open_time))
                        expected_open = max(expected_open, open_time + interval_ms)
                    if expected_open < completed_before:
                        missing_ranges.append((expected_open, completed_before))

                    if missing_ranges:
                        missing_hours = sum(
                            (gap_end - gap_start) // interval_ms
                            for gap_start, gap_end in missing_ranges
                        )
                        print(
                            "[MEXC 1h] repairing BTC history: "
                            f"{missing_hours} missing hours in "
                            f"{len(missing_ranges)} ranges",
                            flush=True,
                        )
                    for gap_start, gap_end in missing_ranges:
                        cursor = gap_start
                        while cursor < gap_end:
                            page_end = min(
                                gap_end, cursor + PAGE_LIMIT * interval_ms
                            )
                            payload = await fetch_page(cursor, page_end)
                            if not payload:
                                start_label = datetime.fromtimestamp(
                                    cursor / 1000, tz=UTC
                                ).isoformat()
                                end_label = datetime.fromtimestamp(
                                    page_end / 1000, tz=UTC
                                ).isoformat()
                                raise RuntimeError(
                                    "MEXC returned no BTC candles while repairing "
                                    f"{start_label}..{end_label}; saved progress is intact"
                                )
                            inserted += upsert(payload, ticker, page_end)
                            cursor = page_end

                    btc_range = conn.execute(
                        "SELECT MIN(open_time), MAX(open_time) "
                        "FROM short_term_hourly_candles WHERE ticker='BTC/USD'"
                    ).fetchone()
                    if btc_range and btc_range[0] is not None and btc_range[1] is not None:
                        btc_days = int(
                            (int(btc_range[1]) - int(btc_range[0]))
                            // (24 * interval_ms)
                        ) + 1
                        print(
                            f"[MEXC 1h] BTC history saved: {btc_days} days",
                            flush=True,
                        )
            except Exception as exc:
                failures.append(f"{ticker}: {str(exc)[:120]}")
                if ticker == "BTC/USD":
                    break

    count, ticker_count, minimum, maximum = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT ticker), MIN(open_time), MAX(open_time) "
        "FROM short_term_hourly_candles"
    ).fetchone()
    btc = conn.execute(
        "SELECT MIN(open_time), MAX(open_time), "
        "SUM(CASE WHEN open_time>=? AND open_time<? THEN 1 ELSE 0 END) "
        "FROM short_term_hourly_candles WHERE ticker='BTC/USD'",
        (initial_start, completed_before),
    ).fetchone()
    btc_start = int(btc[0]) if btc and btc[0] is not None else None
    btc_end = int(btc[1]) if btc and btc[1] is not None else None
    btc_candle_count = int(btc[2] or 0) if btc else 0
    btc_expected_candle_count = max(
        0, (completed_before - initial_start) // interval_ms
    )
    btc_coverage_ratio = (
        btc_candle_count / btc_expected_candle_count
        if btc_expected_candle_count else 0.0
    )
    coverage_days = (
        int((btc_end - btc_start) // (24 * interval_ms)) + 1
        if btc_start is not None and btc_end is not None else 0
    )
    return {
        "inserted": inserted,
        "candle_count": int(count or 0),
        "ticker_count": int(ticker_count or 0),
        "eligible_ticker_count": len(eligible_tickers),
        "data_start": minimum,
        "data_end": maximum,
        "btc_start": btc_start,
        "btc_end": btc_end,
        "btc_candle_count": btc_candle_count,
        "btc_expected_candle_count": btc_expected_candle_count,
        "btc_coverage_ratio": btc_coverage_ratio,
        "coverage_days": coverage_days,
        "failures": failures,
    }
