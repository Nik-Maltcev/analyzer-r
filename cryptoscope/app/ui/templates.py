"""Shared localized Jinja templates instance."""

from __future__ import annotations

from fastapi.templating import Jinja2Templates

from app.access import is_admin_user
from app.i18n import localize_html, request_locale
from app.pricing import get_subscription_pricing
from app.product import get_product_profile
from app.translations import TRANSLATIONS


def product_context(request):
    profile = get_product_profile()
    locale = request_locale(request, profile)
    enabled_markets = getattr(
        request.state,
        "enabled_markets",
        profile.enabled_markets,
    )
    user = getattr(request.state, "current_user", None)
    return {
        "product": profile,
        "pricing": get_subscription_pricing(profile),
        "locale": locale,
        "translations": TRANSLATIONS.get(locale, {}),
        "enabled_markets": enabled_markets,
        "is_admin": is_admin_user(user),
        "market_names": {
            "crypto": "Crypto",
            "stocks": "Акции/ETF",
            "ru": "RU",
            "br": "BR · B3",
            "id": "ID · IDX",
            "au": "AU · ASX",
        },
    }


class LocalizedJinja2Templates(Jinja2Templates):
    def TemplateResponse(self, *args, **kwargs):
        request = kwargs.get("request")
        if request is None and args:
            request = args[0]
        response = super().TemplateResponse(*args, **kwargs)
        if request is None or not response.body:
            return response

        profile = get_product_profile()
        locale = request_locale(request, profile)
        charset = getattr(response, "charset", None) or "utf-8"
        original = response.body.decode(charset)
        localized = localize_html(original, locale, profile)
        if localized != original:
            response.body = localized.encode(charset)
            response.headers["content-length"] = str(len(response.body))
        return response


templates = LocalizedJinja2Templates(
    directory="app/templates",
    context_processors=[product_context],
)
