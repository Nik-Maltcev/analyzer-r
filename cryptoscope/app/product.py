"""Product editions and market access rules for regional deployments."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException

from app.config import Settings, get_settings

BASE_PRODUCT_NAME = "MEANX"
ALL_MARKETS = ("crypto", "stocks", "ru", "br", "id")
MARKET_NAMES = {
    "crypto": "Crypto",
    "stocks": "Акции/ETF",
    "ru": "RU",
    "br": "BR · B3",
    "id": "ID · IDX",
}


@dataclass(frozen=True)
class ProductProfile:
    variant: str
    name: str
    locale: str
    supported_locales: tuple[str, ...]
    enabled_markets: tuple[str, ...]
    default_market: str
    timezone: str
    currency: str


PROFILE_DEFAULTS = {
    "global": ProductProfile(
        variant="global",
        name=BASE_PRODUCT_NAME,
        locale="ru",
        supported_locales=("ru",),
        enabled_markets=("crypto", "stocks", "ru"),
        default_market="crypto",
        timezone="Europe/Moscow",
        currency="RUB",
    ),
    "br": ProductProfile(
        variant="br",
        name=BASE_PRODUCT_NAME,
        locale="pt-BR",
        supported_locales=("pt-BR",),
        enabled_markets=("crypto", "stocks", "br"),
        default_market="br",
        timezone="America/Sao_Paulo",
        currency="BRL",
    ),
    "id": ProductProfile(
        variant="id",
        name=BASE_PRODUCT_NAME,
        locale="id",
        supported_locales=("id", "en"),
        enabled_markets=("crypto", "stocks", "id"),
        default_market="id",
        timezone="Asia/Jakarta",
        currency="IDR",
    ),
}


def _csv_values(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def get_product_profile(settings: Settings | None = None) -> ProductProfile:
    settings = settings or get_settings()
    variant = (settings.app_variant or "global").lower()
    default = PROFILE_DEFAULTS.get(variant, PROFILE_DEFAULTS["global"])

    enabled_markets = (
        _csv_values(settings.enabled_markets)
        if settings.enabled_markets
        else default.enabled_markets
    )
    enabled_markets = tuple(
        market for market in enabled_markets
        if market in default.enabled_markets
    )
    if not enabled_markets:
        enabled_markets = default.enabled_markets

    supported_locales = (
        _csv_values(settings.supported_locales)
        if settings.supported_locales
        else default.supported_locales
    )
    locale = settings.app_locale or default.locale
    if locale not in supported_locales:
        supported_locales = (locale, *supported_locales)

    default_market = settings.default_market or default.default_market
    if default_market not in enabled_markets:
        default_market = enabled_markets[0]

    configured_name = settings.app_name.strip()
    if configured_name.lower().startswith("cryptoscope"):
        configured_name = BASE_PRODUCT_NAME

    return ProductProfile(
        variant=variant if variant in PROFILE_DEFAULTS else "global",
        name=configured_name or default.name,
        locale=locale,
        supported_locales=supported_locales,
        enabled_markets=enabled_markets,
        default_market=default_market,
        timezone=settings.app_timezone or default.timezone,
        currency=settings.app_currency or default.currency,
    )


def normalize_market(
    market: str | None,
    profile: ProductProfile | None = None,
) -> str:
    profile = profile or get_product_profile()
    return (
        market
        if market in profile.enabled_markets
        else profile.default_market
    )


def require_market_enabled(
    market: str,
    profile: ProductProfile | None = None,
) -> str:
    profile = profile or get_product_profile()
    if market not in profile.enabled_markets:
        raise HTTPException(status_code=404, detail="Market is not available")
    return market
