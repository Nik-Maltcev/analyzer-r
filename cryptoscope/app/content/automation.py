"""Daily crypto-only Telegram content workflow."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pandas as pd
from PIL import Image

from app.config import Settings, get_settings
from app.content.card import render_signal_card
from app.content.providers import OpenRouterClient
from app.content.telegram import TelegramPublisher
from app.content.threads import ThreadsPublisher
from app.core.scanner_history import SCANNER_HORIZONS
from app.core.scanners import drawdown_scan, momentum_scan
from app.db.schema import (
    CREATE_CONTENT_PUBLICATION_INDICES,
    CREATE_CONTENT_PUBLICATIONS,
    CREATE_FAVORITES,
    CREATE_SCANNER_SIGNAL_INDICES,
    CREATE_SCANNER_SIGNAL_PERIODS,
)

HIGH_CONFIDENCE = "high"


@dataclass(frozen=True)
class ContentCandidate:
    scanner: str
    ticker: str
    direction: str
    first_seen_date: str
    data_date: str
    signal_age_days: int
    review_in_days: int
    entry_price: float
    rank: float
    facts: dict[str, Any]


def directional_return_pct(direction: str, entry: float, current: float) -> float:
    if entry <= 0 or current <= 0:
        return 0.0
    raw = (current / entry - 1) * 100
    return round(raw if direction == "long" else -raw, 4)


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(CREATE_FAVORITES)
    conn.execute(CREATE_SCANNER_SIGNAL_PERIODS)
    for statement in CREATE_SCANNER_SIGNAL_INDICES:
        conn.execute(statement)
    conn.execute(CREATE_CONTENT_PUBLICATIONS)
    publication_columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(content_publications)").fetchall()
    }
    if "threads_post_id" not in publication_columns:
        conn.execute("ALTER TABLE content_publications ADD COLUMN threads_post_id TEXT")
    for statement in CREATE_CONTENT_PUBLICATION_INDICES:
        conn.execute(statement)
    conn.commit()


def _price_frame(conn: sqlite3.Connection) -> pd.DataFrame:
    prices = pd.read_sql_query(
        """
        SELECT ticker, date, close
        FROM prices
        WHERE market = 'crypto' AND close > 0
        ORDER BY date, ticker
        """,
        conn,
    )
    if prices.empty:
        return pd.DataFrame()
    return prices.pivot(index="date", columns="ticker", values="close").sort_index()


def _periods(conn: sqlite3.Connection) -> dict[tuple[str, str, str], dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT scanner, ticker_a, direction, first_seen_date,
               last_seen_date, observation_count
        FROM scanner_signal_periods
        WHERE market = 'crypto'
          AND scanner IN ('momentum', 'drawdown')
          AND status = 'active'
        """
    ).fetchall()
    return {
        (str(row["scanner"]), str(row["ticker_a"]), str(row["direction"])): dict(row)
        for row in rows
    }


def _is_high_confidence(scanner: str, row: dict[str, Any]) -> bool:
    direction = str(row.get("recommendation_class") or "")
    if scanner == "momentum":
        moves = [float(row.get(key) or 0) for key in ("pct_3d", "pct_7d", "pct_14d")]
        sign = 1 if direction == "long" else -1 if direction == "short" else 0
        return bool(
            sign
            and all((move * sign) > 0 for move in moves)
            and abs(float(row.get("momentum_score") or 0)) >= 10
            and float(row.get("volatility_7d") or 0) < 8
        )
    return bool(
        direction == "long"
        and float(row.get("pct_3d") or 0) >= 3
        and float(row.get("pct_7d") or 0) >= 3
        and float(row.get("drawdown_pct") or 0) < 30
    )


def _recent_tickers(
    conn: sqlite3.Connection,
    data_date: str,
    repeat_days: int,
) -> set[str]:
    cutoff = (date.fromisoformat(data_date) - timedelta(days=max(0, repeat_days))).isoformat()
    rows = conn.execute(
        """
        SELECT DISTINCT ticker
        FROM content_publications
        WHERE market = 'crypto' AND data_date >= ?
        """,
        (cutoff,),
    ).fetchall()
    return {str(row["ticker"]) for row in rows}


def select_candidate(
    conn: sqlite3.Connection,
    wide: pd.DataFrame,
    repeat_days: int,
) -> ContentCandidate | None:
    """Select one deterministic, active, high-confidence crypto scanner signal."""
    if wide.empty:
        return None
    data_date = str(wide.index.max())[:10]
    periods = _periods(conn)
    recent = _recent_tickers(conn, data_date, repeat_days)
    tickers = list(wide.columns)
    frames = {
        "momentum": momentum_scan(wide.values, tickers, list(wide.index.astype(str))),
        "drawdown": drawdown_scan(wide.values, tickers),
    }
    candidates: list[ContentCandidate] = []
    for scanner, frame in frames.items():
        if frame.empty:
            continue
        for record in frame.to_dict(orient="records"):
            if not _is_high_confidence(scanner, record):
                continue
            ticker = str(record["ticker"])
            direction = str(record["recommendation_class"])
            period = periods.get((scanner, ticker, direction))
            if (
                not period
                or str(period.get("last_seen_date") or "")[:10] != data_date
                or ticker in recent
            ):
                continue
            latest = wide[ticker].dropna()
            if latest.empty or float(latest.iloc[-1]) <= 0:
                continue
            age = max(1, int(period.get("observation_count") or 1))
            horizon = SCANNER_HORIZONS[scanner]
            rank = (
                abs(float(record.get("momentum_score") or 0))
                if scanner == "momentum"
                else float(record.get("pct_3d") or 0) + float(record.get("pct_7d") or 0)
            )
            candidates.append(ContentCandidate(
                scanner=scanner,
                ticker=ticker,
                direction=direction,
                first_seen_date=str(period["first_seen_date"])[:10],
                data_date=data_date,
                signal_age_days=age,
                review_in_days=max(0, horizon - age),
                entry_price=float(latest.iloc[-1]),
                rank=round(rank, 4),
                facts={
                    key: record.get(key)
                    for key in (
                        "pct_3d", "pct_7d", "pct_14d", "volatility_7d",
                        "momentum_score", "drawdown_pct", "days_from_high",
                    )
                    if key in record
                },
            ))
    return max(candidates, key=lambda item: item.rank, default=None)


def _favorite_for_candidate(
    conn: sqlite3.Connection,
    candidate: ContentCandidate,
    user_id: str,
) -> int:
    pair = (
        f"CONTENT|{candidate.scanner}|{candidate.ticker}|"
        f"{candidate.direction}|{candidate.first_seen_date}"
    )
    existing = conn.execute(
        """
        SELECT id FROM favorites
        WHERE pair = ? AND user_id = ? AND market = 'crypto'
        ORDER BY id DESC LIMIT 1
        """,
        (pair, user_id),
    ).fetchone()
    if existing:
        return int(existing["id"])
    cursor = conn.execute(
        """
        INSERT INTO favorites (
            pair, market, position_kind, source, ticker_a, ticker_b,
            signal, signal_type, price_a_entry, price_b_entry,
            entry_time, status, halflife, user_id
        ) VALUES (?, 'crypto', 'single', ?, ?, '', ?, ?, ?, 0,
                  datetime('now'), 'active', ?, ?)
        """,
        (
            pair,
            f"scanner_{candidate.scanner}",
            candidate.ticker,
            "Content monitoring",
            "long_a" if candidate.direction == "long" else "short_a",
            candidate.entry_price,
            SCANNER_HORIZONS[candidate.scanner],
            user_id,
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def _create_draft(
    conn: sqlite3.Connection,
    candidate: ContentCandidate,
    user_id: str,
) -> int:
    favorite_id = _favorite_for_candidate(conn, candidate, user_id)
    payload = asdict(candidate)
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO content_publications (
            market, scanner, ticker, direction, confidence,
            first_seen_date, data_date, signal_age_days, review_in_days,
            entry_price, current_price, favorite_id, generation_payload
        ) VALUES ('crypto', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            candidate.scanner,
            candidate.ticker,
            candidate.direction,
            HIGH_CONFIDENCE,
            candidate.first_seen_date,
            candidate.data_date,
            candidate.signal_age_days,
            candidate.review_in_days,
            candidate.entry_price,
            candidate.entry_price,
            favorite_id,
            json.dumps(payload, ensure_ascii=False),
        ),
    )
    conn.commit()
    if cursor.lastrowid:
        return int(cursor.lastrowid)
    row = conn.execute(
        """
        SELECT id FROM content_publications
        WHERE market = 'crypto' AND scanner = ? AND ticker = ?
          AND direction = ? AND first_seen_date = ?
        """,
        (
            candidate.scanner,
            candidate.ticker,
            candidate.direction,
            candidate.first_seen_date,
        ),
    ).fetchone()
    return int(row["id"])


def _payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "scanner": str(row["scanner"]),
        "ticker": str(row["ticker"]),
        "direction": str(row["direction"]),
        "first_seen_date": str(row["first_seen_date"]),
        "data_date": str(row["data_date"]),
        "signal_age_days": int(row["signal_age_days"]),
        "review_in_days": int(row["review_in_days"] or 0),
        "entry_price": float(row["entry_price"]),
    }


def _clean_telegram_text(value: str) -> str:
    """Normalize model output to the plain-text style used by the channel."""
    text = value.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("—", "-").replace("–", "-").replace("−", "-")
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"__(.*?)__", r"\1", text, flags=re.DOTALL)
    text = text.replace("**", "").replace("__", "").replace("`", "")
    text = re.sub(r"(?m)^\s*#{1,6}\s*", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"(?i)\bLong\b", "лонг", text)
    text = re.sub(r"(?i)\bShort\b", "шорт", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _scenario_explanation(payload: dict[str, Any]) -> str:
    if payload["scanner"] == "drawdown":
        return (
            "Сканер просадок отметил актив после заметного снижения. "
            "Идея состоит в наблюдении за возможным восстановлением цены."
        )
    return (
        "Сканер импульса подтвердил движение на нескольких временных горизонтах. "
        "Идея состоит в наблюдении за продолжением текущего направления."
    )


def _compose_initial_text(payload: dict[str, Any], explanation: str) -> str:
    side = "рассмотреть лонг" if payload["direction"] == "long" else "рассмотреть шорт"
    return _clean_telegram_text(
        f"{payload['ticker']}: {side}\n\n"
        f"{explanation}\n\n"
        f"Цена при публикации: ${payload['entry_price']:.6g}\n"
        f"Сигнал активен: {payload['signal_age_days']} дн.\n"
        f"Следующая проверка: примерно через {payload['review_in_days']} дн.\n\n"
        "MEANX проверяет сигнал ежедневно. Если условие исчезнет или направление "
        "изменится, мы опубликуем обновление.\n\n"
        "Не является индивидуальной инвестиционной рекомендацией."
    )


def _initial_fallback(payload: dict[str, Any]) -> str:
    return _compose_initial_text(payload, _scenario_explanation(payload))


def _generate_initial_text(
    provider: OpenRouterClient,
    payload: dict[str, Any],
) -> tuple[str, str | None]:
    fallback = _initial_fallback(payload)
    if not provider.api_key or not provider.text_model:
        return fallback, None
    system = (
        "Ты редактор Telegram-канала MEANX. По фактам из JSON напиши только два "
        "коротких предложения, которые простыми словами объясняют смысл сигнала. "
        "Не повторяй тикер, цену, срок, направление и дисклеймер. Не добавляй новости, "
        "причины движения, прогноз доходности или гарантии. Не используй Markdown, "
        "эмодзи, заголовки, списки и длинное тире. Используй обычный дефис. "
        "Текст должен быть на русском языке и занимать не более 240 символов."
    )
    try:
        explanation = _clean_telegram_text(
            provider.generate_text(system, json.dumps(payload, ensure_ascii=False))
        )[:240]
        explanation = re.sub(r"\s+", " ", explanation).strip()
        if not explanation:
            explanation = _scenario_explanation(payload)
        return _compose_initial_text(payload, explanation), provider.response_json()
    except Exception as exc:
        return fallback, json.dumps({"text_error": str(exc)}, ensure_ascii=False)


def _generate_background(provider: OpenRouterClient, payload: dict[str, Any]) -> tuple[bytes | None, str | None]:
    if not provider.api_key or not provider.image_model:
        return None, None
    prompt = (
        "Create a premium editorial background for a vertical financial market card, "
        "dark graphite and teal, subtle crypto market structure, restrained, professional, "
        "large quiet areas for text. No words, letters, numbers, logos, coins, currency symbols, "
        f"or UI. Directional mood: {payload['direction']}."
    )
    try:
        return provider.generate_background(prompt), provider.response_json()
    except Exception as exc:
        return None, json.dumps({"image_error": str(exc)}, ensure_ascii=False)


def _threads_card_url(settings: Settings, card_path: Path) -> str:
    configured_base = str(
        getattr(settings, "content_public_asset_base_url", "") or ""
    ).strip()
    railway_domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
    base_url = configured_base
    if not base_url and railway_domain:
        base_url = f"https://{railway_domain}"
    if not base_url:
        base_url = settings.app_base_url.strip()
    base_url = base_url.rstrip("/")
    if not base_url:
        raise RuntimeError(
            "CONTENT_PUBLIC_ASSET_BASE_URL or APP_BASE_URL is required for "
            "Threads image publishing"
        )
    cache_key = card_path.stat().st_mtime_ns
    return (
        f"{base_url}/api/public/content/cards/{quote(card_path.name)}"
        f"?v={cache_key}"
    )


def _threads_jpeg(card_path: Path) -> Path:
    jpeg_path = card_path.with_name(f"{card_path.stem}.threads.jpg")
    with Image.open(card_path) as source:
        source.convert("RGB").save(
            jpeg_path,
            format="JPEG",
            quality=92,
            optimize=True,
        )
    return jpeg_path


def _threads_alt_text(payload: dict[str, Any]) -> str:
    side = "лонг" if payload["direction"] == "long" else "шорт"
    return (
        f"Карточка MEANX: {payload['ticker']}, рассмотреть {side}. "
        f"Сканер {payload['scanner']}, цена при публикации "
        f"{payload['entry_price']:.6g} доллара."
    )


def _threads_topic_tag(payload: Any) -> str:
    ticker = str(payload["ticker"]).upper()
    scanner = str(payload["scanner"]).lower()
    direction = str(payload["direction"]).lower()
    if ticker.startswith(("BTC/", "WBTC/")):
        return "Биткоин"
    if scanner == "momentum":
        return "Трейдинг"
    if scanner == "drawdown" and direction == "long":
        return "Инвестиции"
    return "Криптовалюты"


def _send_threads_image(
    threads: ThreadsPublisher,
    settings: Settings,
    payload: dict[str, Any],
    card_path: Path,
    text: str,
) -> str | None:
    if not threads.configured:
        return None
    image_path = _threads_jpeg(card_path)
    image_url = _threads_card_url(settings, image_path)
    topic_tag = _threads_topic_tag(payload)
    print(f"Threads image URL: {image_url} (topic: {topic_tag})")
    try:
        return threads.send_image(
            image_url,
            text,
            _threads_alt_text(payload),
            topic_tag,
        )
    except RuntimeError as exc:
        print(f"Threads image publication failed; trying text fallback: {exc}")
        return threads.send_text(text, topic_tag)


def _publish_draft(
    conn: sqlite3.Connection,
    publication_id: int,
    settings: Settings,
    provider: OpenRouterClient,
    telegram: TelegramPublisher,
    threads: ThreadsPublisher,
) -> int:
    claimed = conn.execute(
        """
        UPDATE content_publications
        SET status = 'publishing', updated_at = datetime('now')
        WHERE id = ? AND status = 'draft'
        """,
        (publication_id,),
    )
    conn.commit()
    if claimed.rowcount != 1:
        return 0
    row = conn.execute(
        "SELECT * FROM content_publications WHERE id = ?",
        (publication_id,),
    ).fetchone()
    if not row:
        return 0
    try:
        payload = _payload(row)
        text, text_response = _generate_initial_text(provider, payload)
        background, image_response = _generate_background(provider, payload)
        card_path = Path(settings.content_card_dir) / (
            f"{payload['data_date']}-{payload['scanner']}-{payload['ticker'].replace('/', '-')}.png"
        )
        render_signal_card(payload, card_path, background)
        message_id = telegram.send_photo(card_path, text)
        threads_post_id = None
        threads_error = None
        try:
            threads_post_id = _send_threads_image(
                threads, settings, payload, card_path, text
            )
        except Exception as exc:
            threads_error = str(exc)
            print(f"Threads initial publication failed: {threads_error}")
        provider_response = json.dumps(
            {
                "text": text_response,
                "image": image_response,
                "threads_error": threads_error,
            },
            ensure_ascii=False,
        )[:20000]
        conn.execute(
            """
            UPDATE content_publications
            SET status = 'active', telegram_message_id = ?, telegram_chat_id = ?,
                threads_post_id = ?, card_path = ?, initial_text = ?, provider_response = ?,
                published_at = datetime('now'), updated_at = datetime('now')
            WHERE id = ? AND status = 'publishing'
            """,
            (
                message_id,
                telegram.chat_id,
                threads_post_id,
                str(card_path),
                text,
                provider_response,
                publication_id,
            ),
        )
        conn.commit()
        return message_id
    except Exception:
        conn.execute(
            """
            UPDATE content_publications
            SET status = 'draft', updated_at = datetime('now')
            WHERE id = ? AND status = 'publishing'
            """,
            (publication_id,),
        )
        conn.commit()
        raise


def _republish_latest_active(
    conn: sqlite3.Connection,
    settings: Settings,
    provider: OpenRouterClient,
    telegram: TelegramPublisher,
    threads: ThreadsPublisher | None = None,
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT * FROM content_publications
        WHERE market = 'crypto' AND status = 'active'
        ORDER BY published_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    if not row:
        return None

    payload = _payload(row)
    text, text_response = _generate_initial_text(provider, payload)
    background, image_response = _generate_background(provider, payload)
    card_path = Path(settings.content_card_dir) / (
        f"deploy-preview-{payload['data_date']}-{payload['scanner']}-"
        f"{payload['ticker'].replace('/', '-')}.png"
    )
    render_signal_card(payload, card_path, background)
    message_id = telegram.send_photo(card_path, text)
    threads_post_id = None
    threads_error = None
    if threads and threads.configured:
        try:
            threads_post_id = _send_threads_image(
                threads, settings, payload, card_path, text
            )
        except Exception as exc:
            threads_error = str(exc)
            print(f"Threads deploy preview failed: {threads_error}")
    provider_response = json.dumps(
        {
            "text": text_response,
            "image": image_response,
            "deploy_preview": True,
            "threads_error": threads_error,
        },
        ensure_ascii=False,
    )[:20000]
    conn.execute(
        """
        UPDATE content_publications
        SET telegram_message_id = ?, telegram_chat_id = ?,
            threads_post_id = COALESCE(?, threads_post_id), card_path = ?,
            initial_text = ?, provider_response = ?,
            published_at = datetime('now'), updated_at = datetime('now')
        WHERE id = ?
        """,
        (
            message_id,
            telegram.chat_id,
            threads_post_id,
            str(card_path),
            text,
            provider_response,
            int(row["id"]),
        ),
    )
    conn.commit()
    return {
        "status": "deploy_preview_published",
        "publication_id": int(row["id"]),
        "message_id": message_id,
        "threads_post_id": threads_post_id,
        "ticker": payload["ticker"],
    }


def _backfill_latest_threads_post(
    conn: sqlite3.Connection,
    settings: Settings,
    threads: ThreadsPublisher,
) -> dict[str, Any] | None:
    if not threads.configured:
        return None
    row = conn.execute(
        """
        SELECT * FROM content_publications
        WHERE market = 'crypto' AND status = 'active'
          AND COALESCE(threads_post_id, '') = ''
          AND COALESCE(card_path, '') != ''
          AND COALESCE(initial_text, '') != ''
        ORDER BY published_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    if not row:
        return None
    card_path = Path(str(row["card_path"]))
    if not card_path.is_file():
        return None
    payload = _payload(row)
    post_id = _send_threads_image(
        threads,
        settings,
        payload,
        card_path,
        str(row["initial_text"]),
    )
    conn.execute(
        """
        UPDATE content_publications
        SET threads_post_id = ?, updated_at = datetime('now')
        WHERE id = ?
        """,
        (post_id, int(row["id"])),
    )
    conn.commit()
    return {
        "publication_id": int(row["id"]),
        "threads_post_id": post_id,
        "ticker": str(row["ticker"]),
    }


def _active_period_exists(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    found = conn.execute(
        """
        SELECT 1 FROM scanner_signal_periods
        WHERE market = 'crypto' AND scanner = ? AND ticker_a = ?
          AND direction = ? AND first_seen_date = ? AND status = 'active'
        LIMIT 1
        """,
        (row["scanner"], row["ticker"], row["direction"], row["first_seen_date"]),
    ).fetchone()
    return found is not None


def _update_fallback(row: sqlite3.Row, current: float, return_pct: float, closed: bool) -> str:
    state = (
        "Условие сканера исчезло. Наблюдение завершено."
        if closed
        else "Сигнал остается активным. Продолжаем ежедневное наблюдение."
    )
    return _clean_telegram_text(
        f"{row['ticker']}: обновление сигнала\n\n"
        f"Цена сейчас: ${current:.6g}\n"
        f"Результат сценария с момента публикации: {return_pct:+.2f}%\n\n"
        f"{state}\n\n"
        "Не является индивидуальной инвестиционной рекомендацией."
    )


def _generate_update_text(
    provider: OpenRouterClient,
    row: sqlite3.Row,
    current: float,
    return_pct: float,
    closed: bool,
    data_date: str,
) -> tuple[str, str | None]:
    fallback = _update_fallback(row, current, return_pct, closed)
    facts = {
        "ticker": row["ticker"],
        "direction": row["direction"],
        "entry_price": row["entry_price"],
        "current_price": current,
        "direction_adjusted_return_pct": return_pct,
        "data_date": data_date,
        "status": "closed" if closed else "active",
    }
    if not provider.api_key or not provider.text_model:
        return fallback, None
    system = (
        "Ты редактор Telegram-канала MEANX. Напиши краткое обновление на русском "
        "только по фактам из JSON. Плюс в return означает движение в пользу сценария. "
        "Не добавляй причины, новости, новые прогнозы или гарантии. Если status=closed, "
        "прямо скажи, что условие исчезло и наблюдение завершено. Не используй Markdown, "
        "эмодзи и длинное тире. Используй обычный дефис. До 350 символов."
    )
    try:
        text = _clean_telegram_text(
            provider.generate_text(system, json.dumps(facts, ensure_ascii=False))
        )
        return text[:4096] or fallback, provider.response_json()
    except Exception as exc:
        return fallback, json.dumps({"update_error": str(exc)}, ensure_ascii=False)


def _close_favorite(
    conn: sqlite3.Connection,
    favorite_id: int | None,
    current: float,
    return_pct: float,
) -> None:
    if not favorite_id:
        return
    conn.execute(
        """
        UPDATE favorites
        SET status = 'closed', exit_time = datetime('now'), exit_price_a = ?,
            exit_price_b = 0, exit_pnl_pct = ?, exit_pair_move_pct = ?
        WHERE id = ? AND status = 'active'
        """,
        (current, return_pct, return_pct, int(favorite_id)),
    )


def _update_active_publications(
    conn: sqlite3.Connection,
    wide: pd.DataFrame,
    provider: OpenRouterClient,
    telegram: TelegramPublisher,
    threads: ThreadsPublisher,
) -> list[dict[str, Any]]:
    if wide.empty:
        return []
    data_date = str(wide.index.max())[:10]
    rows = conn.execute(
        "SELECT * FROM content_publications WHERE market = 'crypto' AND status = 'active'"
    ).fetchall()
    updated: list[dict[str, Any]] = []
    for row in rows:
        if data_date <= str(row["data_date"]):
            continue
        if row["last_update_data_date"] and data_date <= str(row["last_update_data_date"]):
            continue
        ticker = str(row["ticker"])
        if ticker not in wide.columns:
            continue
        values = wide[ticker].dropna()
        if values.empty:
            continue
        current = float(values.iloc[-1])
        return_pct = directional_return_pct(str(row["direction"]), float(row["entry_price"]), current)
        closed = not _active_period_exists(conn, row)
        text, response = _generate_update_text(
            provider, row, current, return_pct, closed, data_date
        )
        telegram.send_message(text, int(row["telegram_message_id"] or 0) or None)
        threads_reply_id = None
        threads_error = None
        if threads.configured and row["threads_post_id"]:
            try:
                threads_reply_id = threads.send_reply(
                    text,
                    str(row["threads_post_id"]),
                    _threads_topic_tag(row),
                )
            except Exception as exc:
                threads_error = str(exc)
                print(f"Threads update failed: {threads_error}")
        status = "closed" if closed else "active"
        conn.execute(
            """
            UPDATE content_publications
            SET current_price = ?, return_pct = ?, status = ?, last_update_text = ?,
                provider_response = ?, last_update_data_date = ?,
                closed_at = CASE WHEN ? THEN datetime('now') ELSE closed_at END,
                updated_at = datetime('now')
            WHERE id = ?
            """,
            (current, return_pct, status, text, response, data_date, int(closed), int(row["id"])),
        )
        if closed:
            _close_favorite(conn, row["favorite_id"], current, return_pct)
        conn.commit()
        updated.append({
            "id": int(row["id"]),
            "ticker": ticker,
            "status": status,
            "threads_reply_id": threads_reply_id,
            "threads_error": threads_error,
        })
    return updated


def run_content_automation(
    settings: Settings | None = None,
    deploy_preview: bool = False,
) -> dict[str, Any]:
    settings = settings or get_settings()
    if not settings.content_automation_enabled:
        return {"status": "disabled"}
    telegram = TelegramPublisher(
        settings.content_telegram_bot_token or settings.telegram_bot_token,
        settings.content_telegram_chat_id or settings.telegram_chat_id,
    )
    if not telegram.configured:
        raise RuntimeError("CONTENT_TELEGRAM_BOT_TOKEN and CONTENT_TELEGRAM_CHAT_ID are required")
    provider = OpenRouterClient(
        settings.content_openrouter_api_key,
        settings.content_openrouter_text_model,
        settings.content_openrouter_image_model,
    )
    threads = ThreadsPublisher(
        settings.content_threads_access_token if settings.content_threads_enabled else "",
        settings.content_threads_api_version,
    )
    if settings.content_threads_enabled:
        if not threads.configured:
            raise RuntimeError("CONTENT_THREADS_ACCESS_TOKEN is required")
        if not (
            settings.content_public_asset_base_url.strip()
            or os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
            or settings.app_base_url.strip()
        ):
            raise RuntimeError(
                "CONTENT_PUBLIC_ASSET_BASE_URL or APP_BASE_URL is required for "
                "Threads image publishing"
            )

    with sqlite3.connect(settings.db_path) as conn:
        conn.row_factory = sqlite3.Row
        _ensure_schema(conn)
        wide = _price_frame(conn)
        if wide.empty:
            return {"status": "no_crypto_prices"}

        threads_backfill = None
        if threads.configured:
            try:
                threads_backfill = _backfill_latest_threads_post(
                    conn, settings, threads
                )
            except Exception as exc:
                threads_backfill = {"error": str(exc)}
                print(f"Threads backfill failed: {exc}")

        if deploy_preview and settings.content_deploy_preview_enabled:
            preview = _republish_latest_active(
                conn, settings, provider, telegram, threads
            )
            if preview:
                preview["threads_backfill"] = threads_backfill
                return preview

        updates = _update_active_publications(
            conn, wide, provider, telegram, threads
        )
        draft = conn.execute(
            """
            SELECT id FROM content_publications
            WHERE market = 'crypto' AND status = 'draft'
            ORDER BY id LIMIT 1
            """
        ).fetchone()
        if draft:
            message_id = _publish_draft(
                conn, int(draft["id"]), settings, provider, telegram, threads
            )
            return {
                "status": "draft_published",
                "message_id": message_id,
                "threads_backfill": threads_backfill,
                "updates": updates,
            }

        data_date = str(wide.index.max())[:10]
        already_published = conn.execute(
            """
            SELECT 1 FROM content_publications
            WHERE market = 'crypto' AND data_date = ? AND status IN ('active', 'closed')
            LIMIT 1
            """,
            (data_date,),
        ).fetchone()
        if already_published:
            return {
                "status": "already_published",
                "data_date": data_date,
                "threads_backfill": threads_backfill,
                "updates": updates,
            }

        candidate = select_candidate(conn, wide, settings.content_repeat_ticker_days)
        if not candidate:
            return {
                "status": "no_candidate",
                "data_date": data_date,
                "threads_backfill": threads_backfill,
                "updates": updates,
            }
        publication_id = _create_draft(conn, candidate, settings.content_bot_user_id)
        message_id = _publish_draft(
            conn, publication_id, settings, provider, telegram, threads
        )
        return {
            "status": "published",
            "publication_id": publication_id,
            "message_id": message_id,
            "ticker": candidate.ticker,
            "threads_backfill": threads_backfill,
            "updates": updates,
        }
