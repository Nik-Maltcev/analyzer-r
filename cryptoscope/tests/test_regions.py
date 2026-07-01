"""Regional product profile and localization tests."""

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import get_settings
from app.db.database import set_db_path
from app.product import get_product_profile


@pytest.fixture
def app(temp_db):
    from app.main import app

    set_db_path(temp_db)
    return app


def set_variant(monkeypatch, variant):
    settings = get_settings()
    overrides = {
        "app_variant": variant,
        "app_name": "",
        "app_locale": "",
        "supported_locales": "",
        "enabled_markets": "",
        "default_market": "",
        "app_timezone": "",
        "app_currency": "",
    }
    for name, value in overrides.items():
        monkeypatch.setattr(settings, name, value)


def test_product_profile_defaults(monkeypatch):
    set_variant(monkeypatch, "br")
    brazil = get_product_profile()
    assert brazil.name == "MEANX"
    assert brazil.locale == "pt-BR"
    assert brazil.enabled_markets == ("crypto", "stocks", "br")

    set_variant(monkeypatch, "id")
    indonesia = get_product_profile()
    assert indonesia.name == "MEANX"
    assert indonesia.supported_locales == ("id", "en")
    assert indonesia.enabled_markets == ("crypto", "stocks", "id")


def test_legacy_product_name_is_migrated(monkeypatch):
    set_variant(monkeypatch, "br")
    monkeypatch.setattr(get_settings(), "app_name", "CryptoScope Brasil")

    assert get_product_profile().name == "MEANX"


@pytest.mark.asyncio
async def test_brazil_edition_limits_markets_and_localizes(
    app,
    monkeypatch,
):
    set_variant(monkeypatch, "br")
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="https://test",
    ) as client:
        landing = await client.get("/")
        terminal = await client.get("/app?market=ru")
        blocked = await client.get("/api/signals?market=ru")
        allowed = await client.get("/api/signals?market=br")

    assert landing.status_code == 200
    assert '<html lang="pt-BR">' in landing.text
    assert "MEANX" in landing.text
    assert "Recursos" in landing.text
    assert "Rússia" not in landing.text
    assert "paypal.com/sdk/js" not in landing.text

    assert terminal.status_code == 200
    assert 'window.CRYPTOSCOPE_INITIAL_MARKET = "br"' in terminal.text
    assert 'data-market="crypto"' in terminal.text
    assert 'data-market="stocks"' in terminal.text
    assert 'data-market="br"' in terminal.text
    assert 'data-market="ru"' not in terminal.text
    assert 'data-market="id"' not in terminal.text
    assert blocked.status_code == 404
    assert allowed.status_code == 200


@pytest.mark.asyncio
async def test_indonesia_edition_switches_between_id_and_english(
    app,
    monkeypatch,
):
    set_variant(monkeypatch, "id")
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="https://test",
    ) as client:
        default_page = await client.get("/")
        locale_response = await client.post("/api/locale?lang=en")
        english_page = await client.get("/app")
        unsupported = await client.post("/api/locale?lang=pt-BR")

    assert default_page.status_code == 200
    assert '<html lang="id">' in default_page.text
    assert "Buka aplikasi" in default_page.text
    assert "Harga Binance live" in default_page.text

    assert locale_response.status_code == 200
    assert "cryptoscope_locale=en" in locale_response.headers["set-cookie"]
    assert '<html lang="en"' in english_page.text
    assert "Signals" in english_page.text
    assert 'data-market="id"' in english_page.text
    assert 'data-market="ru"' not in english_page.text
    assert unsupported.status_code == 400
