"""Small server-side localization layer for rendered HTML."""

from __future__ import annotations

from html import escape
from html.parser import HTMLParser
import re

from fastapi import Request

from app.product import BASE_PRODUCT_NAME, ProductProfile, get_product_profile
from app.translations import TRANSLATIONS

LOCALE_COOKIE_NAME = "cryptoscope_locale"
TRANSLATABLE_ATTRIBUTES = {
    "title",
    "placeholder",
    "aria-label",
    "alt",
    "content",
}
TRANSLATION_PATTERNS = {
    locale: re.compile(
        "|".join(
            re.escape(source)
            for source in sorted(phrases, key=len, reverse=True)
        )
    )
    for locale, phrases in TRANSLATIONS.items()
}


def request_locale(
    request: Request,
    profile: ProductProfile | None = None,
) -> str:
    profile = profile or get_product_profile()
    requested = request.query_params.get("lang") or request.cookies.get(
        LOCALE_COOKIE_NAME
    )
    return (
        requested
        if requested in profile.supported_locales
        else profile.locale
    )


# Russian source phrases are used as stable message IDs while the legacy
# templates are progressively migrated to explicit translation keys.
def translate_text(text: str, locale: str, profile: ProductProfile) -> str:
    if locale == "ru":
        return text
    phrases = TRANSLATIONS.get(locale, {})
    pattern = TRANSLATION_PATTERNS.get(locale)
    translated = (
        pattern.sub(lambda match: phrases[match.group(0)], text)
        if pattern
        else text
    )
    if (
        profile.name != BASE_PRODUCT_NAME
        and BASE_PRODUCT_NAME in translated
        and profile.name not in translated
    ):
        translated = translated.replace(BASE_PRODUCT_NAME, profile.name)
    return translated


class _LocalizedHTMLParser(HTMLParser):
    def __init__(self, locale: str, profile: ProductProfile):
        super().__init__(convert_charrefs=False)
        self.locale = locale
        self.profile = profile
        self.output: list[str] = []
        self.raw_text_depth = 0

    def handle_decl(self, decl):
        self.output.append(f"<!{decl}>")

    def handle_comment(self, data):
        self.output.append(f"<!--{data}-->")

    def handle_starttag(self, tag, attrs):
        rendered_attrs = []
        for name, value in attrs:
            if value is None:
                rendered_attrs.append(name)
                continue
            if name in TRANSLATABLE_ATTRIBUTES:
                value = translate_text(value, self.locale, self.profile)
            rendered_attrs.append(f'{name}="{escape(value, quote=True)}"')
        suffix = f" {' '.join(rendered_attrs)}" if rendered_attrs else ""
        self.output.append(f"<{tag}{suffix}>")
        if tag in {"script", "style"}:
            self.raw_text_depth += 1

    def handle_startendtag(self, tag, attrs):
        rendered_attrs = []
        for name, value in attrs:
            if value is None:
                rendered_attrs.append(name)
                continue
            if name in TRANSLATABLE_ATTRIBUTES:
                value = translate_text(value, self.locale, self.profile)
            rendered_attrs.append(f'{name}="{escape(value, quote=True)}"')
        suffix = f" {' '.join(rendered_attrs)}" if rendered_attrs else ""
        self.output.append(f"<{tag}{suffix}/>")

    def handle_endtag(self, tag):
        self.output.append(f"</{tag}>")
        if tag in {"script", "style"} and self.raw_text_depth:
            self.raw_text_depth -= 1

    def handle_data(self, data):
        if self.raw_text_depth:
            self.output.append(data)
        else:
            self.output.append(
                translate_text(data, self.locale, self.profile)
            )

    def handle_entityref(self, name):
        self.output.append(f"&{name};")

    def handle_charref(self, name):
        self.output.append(f"&#{name};")

    def handle_pi(self, data):
        self.output.append(f"<?{data}>")

    def handle_unknown_decl(self, data):
        self.output.append(f"<![{data}]>")


def localize_html(
    html: str,
    locale: str,
    profile: ProductProfile,
) -> str:
    if locale == "ru" and profile.name == BASE_PRODUCT_NAME:
        return html
    parser = _LocalizedHTMLParser(locale, profile)
    parser.feed(html)
    parser.close()
    return "".join(parser.output)
