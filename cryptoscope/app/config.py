from pydantic_settings import BaseSettings
from functools import lru_cache
import os


class Settings(BaseSettings):
    db_path: str = "/data/market.db" if os.name != "nt" else "data/market.db"
    csv_path: str = "/opt/seed/all_markets_3yr.csv"
    ru_csv_path: str = "/opt/seed/tinkoff_ru_2yr.csv"
    hourly_path: str = "/opt/seed/hourly_6coins_2yr.csv"
    port: int = 3000
    host: str = "0.0.0.0"

    deepseek_api_key: str = ""
    deepseek_api_url: str = "https://api.deepseek.com/chat/completions"

    supabase_url: str = ""
    supabase_anon_key: str = ""

    resend_api_key: str = ""
    resend_from_email: str = "CryptoScope <onboarding@resend.dev>"
    app_base_url: str = ""
    magic_link_ttl_minutes: int = 15
    auth_session_days: int = 30
    auth_legacy_owner_email: str = ""

    twelve_data_api_key: str = ""
    pyth_api_key: str = ""

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    log_level: str = "info"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


@lru_cache()
def get_settings() -> Settings:
    return Settings()
