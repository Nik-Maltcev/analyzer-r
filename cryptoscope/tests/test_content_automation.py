import sqlite3
from types import SimpleNamespace

import numpy as np
import pandas as pd

from app.content.automation import (
    _clean_telegram_text,
    _generate_initial_text,
    _initial_fallback,
    _republish_latest_active,
    _threads_card_url,
    _threads_jpeg,
    _threads_topic_tag,
    _update_active_publications,
    directional_return_pct,
    select_candidate,
)
from app.db.schema import (
    CREATE_CONTENT_PUBLICATIONS,
    CREATE_SCANNER_SIGNAL_PERIODS,
)


def test_directional_return_respects_position_side():
    assert directional_return_pct("long", 100, 110) == 10
    assert directional_return_pct("short", 100, 90) == 10
    assert directional_return_pct("short", 100, 110) == -10


def test_content_text_is_plain_russian_without_long_dashes():
    cleaned = _clean_telegram_text("**ZEC/USD — Long**\n\nТекст – без `markdown`.")
    assert cleaned == "ZEC/USD - лонг\n\nТекст - без markdown."

    text = _initial_fallback({
        "scanner": "drawdown",
        "ticker": "ZEC/USD",
        "direction": "long",
        "signal_age_days": 2,
        "review_in_days": 8,
        "entry_price": 570.94,
    })
    assert "рассмотреть лонг" in text
    assert "Сканер просадок" in text
    assert "—" not in text
    assert "**" not in text


def test_generated_content_keeps_fixed_structure():
    class Provider:
        api_key = "configured"
        text_model = "configured"

        @staticmethod
        def generate_text(*_):
            return "**Сценарий — Long**\nЦена может восстановиться после просадки."

        @staticmethod
        def response_json():
            return "{}"

    payload = {
        "scanner": "drawdown",
        "ticker": "ZEC/USD",
        "direction": "long",
        "signal_age_days": 2,
        "review_in_days": 8,
        "entry_price": 570.94,
    }
    text, _ = _generate_initial_text(Provider(), payload)

    assert text.startswith("ZEC/USD: рассмотреть лонг")
    assert "Сценарий - лонг" in text
    assert "Цена при публикации: $570.94" in text
    assert "—" not in text
    assert "**" not in text


def test_deploy_preview_republishes_latest_active_signal(tmp_path):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(CREATE_CONTENT_PUBLICATIONS)
    conn.execute(
        """
        INSERT INTO content_publications (
            market, scanner, ticker, direction, confidence, first_seen_date,
            data_date, signal_age_days, review_in_days, entry_price, status,
            telegram_message_id, telegram_chat_id
        ) VALUES (
            'crypto', 'drawdown', 'ZEC/USD', 'long', 'high', '2026-07-15',
            '2026-07-16', 2, 8, 570.94, 'active', 5, '-100-old'
        )
        """
    )

    class Provider:
        api_key = ""
        text_model = ""
        image_model = ""

    class Telegram:
        chat_id = "@meanx_trade"

        @staticmethod
        def send_photo(_path, _text):
            return 77

    result = _republish_latest_active(
        conn,
        SimpleNamespace(content_card_dir=str(tmp_path)),
        Provider(),
        Telegram(),
    )

    row = conn.execute(
        "SELECT telegram_message_id, telegram_chat_id FROM content_publications"
    ).fetchone()
    assert result["status"] == "deploy_preview_published"
    assert row["telegram_message_id"] == 77
    assert row["telegram_chat_id"] == "@meanx_trade"


def test_threads_card_uses_railway_domain_and_jpeg(tmp_path, monkeypatch):
    from PIL import Image

    png_path = tmp_path / "signal.png"
    Image.new("RGB", (20, 20), "#102030").save(png_path)
    jpeg_path = _threads_jpeg(png_path)
    monkeypatch.setenv(
        "RAILWAY_PUBLIC_DOMAIN",
        "analyzer-r-production.up.railway.app",
    )
    settings = SimpleNamespace(
        content_public_asset_base_url="",
        app_base_url="https://www.meanx.pro",
    )

    url = _threads_card_url(settings, jpeg_path)

    assert jpeg_path.suffix == ".jpg"
    assert jpeg_path.is_file()
    assert url.startswith(
        "https://analyzer-r-production.up.railway.app/"
        "api/public/content/cards/signal.threads.jpg?v="
    )


def test_threads_scanner_signals_use_crypto_topic():
    assert _threads_topic_tag({
        "ticker": "BTC/USD", "scanner": "drawdown", "direction": "long"
    }) == "Криптовалюты"
    assert _threads_topic_tag({
        "ticker": "SOL/USD", "scanner": "momentum", "direction": "long"
    }) == "Криптовалюты"
    assert _threads_topic_tag({
        "ticker": "ZEC/USD", "scanner": "momentum", "direction": "short"
    }) == "Криптовалюты"


def test_select_candidate_uses_active_high_confidence_crypto_period(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(CREATE_SCANNER_SIGNAL_PERIODS)
    conn.execute(CREATE_CONTENT_PUBLICATIONS)
    conn.execute(
        """
        INSERT INTO scanner_signal_periods (
            market, scanner, signal_key, ticker_a, direction,
            first_seen_date, last_seen_date, observation_count, status
        ) VALUES ('crypto', 'momentum', 'AAA/USD', 'AAA/USD', 'long',
                  '2026-07-13', '2026-07-14', 2, 'active')
        """
    )
    frame = pd.DataFrame([{
        "ticker": "AAA/USD",
        "recommendation_class": "long",
        "pct_3d": 12.0,
        "pct_7d": 18.0,
        "pct_14d": 21.0,
        "volatility_7d": 2.0,
        "momentum_score": 17.0,
    }])
    monkeypatch.setattr("app.content.automation.momentum_scan", lambda *_: frame)
    monkeypatch.setattr("app.content.automation.drawdown_scan", lambda *_: pd.DataFrame())
    wide = pd.DataFrame(
        np.linspace(1, 2, 100),
        index=pd.date_range("2026-04-06", periods=100).strftime("%Y-%m-%d"),
        columns=["AAA/USD"],
    )

    candidate = select_candidate(conn, wide, repeat_days=30)

    assert candidate is not None
    assert candidate.ticker == "AAA/USD"
    assert candidate.signal_age_days == 2
    assert candidate.review_in_days == 3


def test_select_candidate_does_not_repeat_recent_ticker(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(CREATE_SCANNER_SIGNAL_PERIODS)
    conn.execute(CREATE_CONTENT_PUBLICATIONS)
    conn.execute(
        """
        INSERT INTO scanner_signal_periods (
            market, scanner, signal_key, ticker_a, direction,
            first_seen_date, last_seen_date, observation_count, status
        ) VALUES ('crypto', 'momentum', 'AAA/USD', 'AAA/USD', 'long',
                  '2026-07-14', '2026-07-14', 1, 'active')
        """
    )
    conn.execute(
        """
        INSERT INTO content_publications (
            market, scanner, ticker, direction, confidence, first_seen_date,
            data_date, signal_age_days, entry_price, status
        ) VALUES ('crypto', 'momentum', 'AAA/USD', 'long', 'high',
                  '2026-07-10', '2026-07-10', 1, 1.5, 'closed')
        """
    )
    frame = pd.DataFrame([{
        "ticker": "AAA/USD", "recommendation_class": "long",
        "pct_3d": 12, "pct_7d": 14, "pct_14d": 16,
        "volatility_7d": 2, "momentum_score": 14,
    }])
    monkeypatch.setattr("app.content.automation.momentum_scan", lambda *_: frame)
    monkeypatch.setattr("app.content.automation.drawdown_scan", lambda *_: pd.DataFrame())
    wide = pd.DataFrame(
        np.linspace(1, 2, 100),
        index=pd.date_range("2026-04-06", periods=100).strftime("%Y-%m-%d"),
        columns=["AAA/USD"],
    )

    assert select_candidate(conn, wide, repeat_days=30) is None


def test_evening_update_keeps_telegram_text_and_threads_image(tmp_path):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(CREATE_SCANNER_SIGNAL_PERIODS)
    conn.execute(CREATE_CONTENT_PUBLICATIONS)
    conn.execute(
        """
        INSERT INTO scanner_signal_periods (
            market, scanner, signal_key, ticker_a, direction,
            first_seen_date, last_seen_date, observation_count, status
        ) VALUES ('crypto', 'momentum', 'AAA/USD', 'AAA/USD', 'long',
                  '2026-07-13', '2026-07-15', 3, 'active')
        """
    )
    conn.execute(
        """
        INSERT INTO content_publications (
            market, scanner, ticker, direction, confidence, first_seen_date,
            data_date, signal_age_days, review_in_days, entry_price, status,
            telegram_message_id, threads_post_id
        ) VALUES ('crypto', 'momentum', 'AAA/USD', 'long', 'high',
                  '2026-07-13', '2026-07-14', 2, 3, 100, 'active', 77,
                  'threads-original-77')
        """
    )
    wide = pd.DataFrame(
        {"AAA/USD": [100.0, 104.0]},
        index=["2026-07-14", "2026-07-15"],
    )

    class Provider:
        api_key = ""
        text_model = ""

    class Telegram:
        sent = []

        @classmethod
        def send_message(cls, text, reply_to_message_id=None):
            cls.sent.append((text, reply_to_message_id))
            return 88

    class Threads:
        configured = True
        sent = []

        @classmethod
        def send_image(cls, *args):
            cls.sent.append(args)
            return "threads-update-88"

    updates = _update_active_publications(
        conn,
        wide,
        SimpleNamespace(
            content_card_dir=str(tmp_path),
            content_public_asset_base_url="https://example.com",
            app_base_url="",
        ),
        Provider(),
        Telegram(),
        Threads(),
    )

    assert updates[0]["published"] is True
    assert Telegram.sent[0][1] == 77
    assert Threads.sent[0][4] == "threads-original-77"
    assert "update-momentum-AAA-USD.threads.jpg" in Threads.sent[0][0]
