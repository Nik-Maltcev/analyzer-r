"""Idempotent Telegram notifications for the Reversal forward journal."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from app.config import get_settings
from app.content.telegram import TelegramPublisher
from app.core.reversal_lab import STAKE_USD, STRATEGY_VERSION, ensure_reversal_schema


def _price(value: float) -> str:
    number = float(value)
    decimals = 10 if abs(number) < 0.001 else 8
    return f"{number:.{decimals}f}".rstrip("0").rstrip(".")


def _time_label(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, UTC).strftime("%d.%m.%Y %H:%M UTC")


def _message(row: sqlite3.Row) -> str:
    direction = "ЛОНГ" if row["direction"] == "long" else "ШОРТ"
    ticker = str(row["ticker"])
    if row["event_type"] == "opened":
        return (
            "MEANX · REVERSAL FORWARD\n\n"
            f"Открыта тестовая позиция: {direction} {ticker}\n"
            f"Цена входа: ${_price(row['entry_price'])}\n"
            f"Время: {_time_label(int(row['entry_time']))}\n"
            f"Импульс: {float(row['residual_return_pct']):+.2f}% · "
            f"Z {abs(float(row['shock_z'])):.1f} · объём {float(row['volume_ratio']):.1f}x\n\n"
            "План: цель +0.80%, стоп -0.80%, максимум 30 минут.\n"
            f"Модельная позиция: ${STAKE_USD:.0f}. Расходы 0.30% уже заложены.\n\n"
            "Это forward-тест гипотезы, не торговая рекомендация."
        )
    reasons = {"target": "достигнута цель", "stop": "сработал стоп", "time": "истекли 30 минут"}
    return (
        "MEANX · REVERSAL FORWARD\n\n"
        f"Закрыта тестовая позиция: {direction} {ticker}\n"
        f"${_price(row['entry_price'])} → ${_price(row['exit_price'])}\n"
        f"Причина: {reasons.get(row['exit_reason'], row['exit_reason'])}\n"
        f"Результат после расходов: {float(row['net_return_pct']):+.2f}% "
        f"({float(row['cash_result']):+.2f} $ при ${STAKE_USD:.0f})\n"
        f"Время закрытия: {_time_label(int(row['exit_time']))}\n\n"
        "Результат зафиксирован и больше не пересчитывается."
    )


def dispatch_reversal_notifications(db_path: str) -> dict:
    """Deliver pending journal events once; failed events remain retryable."""
    settings = get_settings()
    if not settings.reversal_telegram_notifications_enabled:
        return {"status": "disabled", "sent": 0, "failed": 0}
    publisher = TelegramPublisher(
        settings.content_telegram_bot_token or settings.telegram_bot_token,
        settings.content_telegram_chat_id or settings.telegram_chat_id,
    )
    if not publisher.configured:
        return {"status": "not_configured", "sent": 0, "failed": 0}

    conn = sqlite3.connect(db_path, timeout=60)
    conn.row_factory = sqlite3.Row
    ensure_reversal_schema(conn)
    rows = conn.execute(
        """
        SELECT notification.id AS notification_id, notification.event_type,
               trade.*
        FROM reversal_forward_notifications AS notification
        JOIN reversal_forward_trades AS trade ON trade.id=notification.trade_id
        WHERE notification.strategy_version=?
          AND notification.status IN ('pending', 'failed')
          AND notification.attempts < 10
        ORDER BY notification.id
        LIMIT 20
        """,
        (STRATEGY_VERSION,),
    ).fetchall()
    sent = 0
    failed = 0
    try:
        for row in rows:
            notification_id = int(row["notification_id"])
            try:
                message_id = publisher.send_message(_message(row))
                conn.execute(
                    """
                    UPDATE reversal_forward_notifications
                    SET status='sent', attempts=attempts+1,
                        telegram_message_id=?, last_error=NULL,
                        sent_at=datetime('now')
                    WHERE id=? AND status!='sent'
                    """,
                    (message_id, notification_id),
                )
                sent += 1
            except Exception as exc:
                conn.execute(
                    """
                    UPDATE reversal_forward_notifications
                    SET status='failed', attempts=attempts+1, last_error=?
                    WHERE id=? AND status!='sent'
                    """,
                    (str(exc)[:1000], notification_id),
                )
                failed += 1
            conn.commit()
    finally:
        conn.close()
    return {"status": "processed", "sent": sent, "failed": failed}
