import os
from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    db_path: str = "/data/market.db" if os.name != "nt" else "data/market.db"
    csv_path: str = "/opt/seed/all_markets_3yr.csv"
    ru_csv_path: str = "/opt/seed/tinkoff_ru_2yr.csv"
    hourly_path: str = "/opt/seed/hourly_6coins_2yr.csv"
    port: int = 3000
    host: str = "0.0.0.0"

    app_variant: str = "global"
    app_name: str = ""
    app_locale: str = ""
    supported_locales: str = ""
    enabled_markets: str = ""
    default_market: str = ""
    app_timezone: str = ""
    app_currency: str = ""

    supabase_url: str = ""
    supabase_anon_key: str = ""

    resend_api_key: str = ""
    resend_from_email: str = "MEANX <onboarding@resend.dev>"
    app_base_url: str = ""
    magic_link_ttl_minutes: int = 15
    auth_session_days: int = 30
    auth_legacy_owner_email: str = ""
    auth_admin_emails: str = ""
    trial_days: int = 3

    payanyway_account_id: str = ""
    payanyway_integrity_code: str = ""
    payanyway_test_mode: bool = False

    paypal_client_id: str = ""
    paypal_client_secret: str = ""
    paypal_mode: str = "sandbox"

    twelve_data_api_key: str = ""
    pyth_api_key: str = ""

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    content_automation_enabled: bool = False
    content_bot_user_id: str = "content-bot"
    content_telegram_bot_token: str = ""
    content_telegram_chat_id: str = ""
    content_openrouter_api_key: str = ""
    content_openrouter_text_model: str = ""
    content_openrouter_image_model: str = ""
    content_card_dir: str = "/data/content_cards"
    content_repeat_ticker_days: int = 30
    content_deploy_preview_enabled: bool = False
    content_threads_enabled: bool = False
    content_threads_access_token: str = ""
    content_threads_api_version: str = "v1.0"
    content_threads_deploy_preview_enabled: bool = False

    log_level: str = "info"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


@lru_cache
def get_settings() -> Settings:
    return Settings()
