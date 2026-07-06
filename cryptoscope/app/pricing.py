"""Edition-specific subscription pricing."""

from __future__ import annotations

from dataclasses import dataclass

from app.product import ProductProfile, get_product_profile


@dataclass(frozen=True)
class SubscriptionPricing:
    month_display: str
    year_display: str
    checkout_currency: str
    checkout_month_amount: str
    checkout_year_amount: str
    checkout_uses_display_currency: bool


PRICING_BY_VARIANT = {
    "global": SubscriptionPricing(
        month_display="990 ₽",
        year_display="7 900 ₽",
        checkout_currency="RUB",
        checkout_month_amount="990.00",
        checkout_year_amount="7900.00",
        checkout_uses_display_currency=True,
    ),
    "br": SubscriptionPricing(
        month_display="R$ 51,44",
        year_display="R$ 514,42",
        checkout_currency="BRL",
        checkout_month_amount="51.44",
        checkout_year_amount="514.42",
        checkout_uses_display_currency=True,
    ),
    "id": SubscriptionPricing(
        month_display="Rp178.141",
        year_display="Rp1.781.406",
        checkout_currency="USD",
        checkout_month_amount="9.90",
        checkout_year_amount="99.00",
        checkout_uses_display_currency=False,
    ),
}


def get_subscription_pricing(
    profile: ProductProfile | None = None,
) -> SubscriptionPricing:
    profile = profile or get_product_profile()
    return PRICING_BY_VARIANT.get(
        profile.variant,
        PRICING_BY_VARIANT["global"],
    )
