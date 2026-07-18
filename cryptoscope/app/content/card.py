"""Deterministic social card renderer; AI imagery never owns factual text."""

from __future__ import annotations

from datetime import datetime
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageOps

WIDTH = 1200
HEIGHT = 1500


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    font_paths = (
        (
            "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "C:/Windows/Fonts/arialbd.ttf",
        )
        if bold
        else (
            "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "C:/Windows/Fonts/arial.ttf",
        )
    )
    for font_path in font_paths:
        try:
            return ImageFont.truetype(font_path, size=size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def _gradient() -> Image.Image:
    image = Image.new("RGB", (WIDTH, HEIGHT))
    pixels = image.load()
    for y in range(HEIGHT):
        t = y / max(1, HEIGHT - 1)
        for x in range(WIDTH):
            side = x / max(1, WIDTH - 1)
            pixels[x, y] = (
                int(7 + 9 * t),
                int(14 + 17 * side),
                int(20 + 13 * (1 - t)),
            )
    return image


def _background(background_bytes: bytes | None) -> Image.Image:
    if not background_bytes:
        return _gradient()
    try:
        source = Image.open(BytesIO(background_bytes)).convert("RGB")
        source = ImageOps.fit(source, (WIDTH, HEIGHT), method=Image.Resampling.LANCZOS)
        source = ImageEnhance.Contrast(source).enhance(0.8)
        overlay = Image.new("RGBA", (WIDTH, HEIGHT), (3, 9, 14, 185))
        return Image.alpha_composite(source.convert("RGBA"), overlay).convert("RGB")
    except Exception:
        return _gradient()


def _price(value: float) -> str:
    if value >= 1000:
        return f"${value:,.2f}"
    if value >= 1:
        return f"${value:,.3f}".rstrip("0").rstrip(".")
    return f"${value:.6f}".rstrip("0").rstrip(".")


def _date(value: str) -> str:
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        return value


def _scanner_name(value: str) -> str:
    return {
        "momentum": "Импульс",
        "drawdown": "Просадка",
    }.get(value.lower(), value.title())


def render_signal_card(
    payload: dict,
    output_path: str | Path,
    background_bytes: bytes | None = None,
) -> Path:
    image = _background(background_bytes)
    draw = ImageDraw.Draw(image, "RGBA")
    green = (61, 217, 166, 255)
    red = (255, 104, 112, 255)
    white = (242, 247, 250, 255)
    muted = (151, 171, 184, 255)
    accent = green if payload["direction"] == "long" else red

    draw.rounded_rectangle(
        (64, 60, 1136, 1440),
        radius=32,
        fill=(5, 12, 18, 226),
        outline=(69, 94, 108, 170),
        width=2,
    )
    draw.rounded_rectangle((92, 92, 160, 160), radius=14, fill=green)
    draw.text((114, 100), "M", font=_font(42, True), fill=(4, 22, 20, 255))
    draw.text((182, 98), "MEANX", font=_font(42, True), fill=white)
    draw.text((182, 145), "КРИПТОСИГНАЛ", font=_font(20, True), fill=muted)

    draw.text((92, 250), _date(payload["data_date"]), font=_font(25, True), fill=muted)
    draw.text(
        (92, 305),
        payload["ticker"].replace("/USD", ""),
        font=_font(104, True),
        fill=white,
    )
    side = "РАССМОТРЕТЬ ЛОНГ" if payload["direction"] == "long" else "РАССМОТРЕТЬ ШОРТ"
    draw.rounded_rectangle(
        (92, 445, 690, 525),
        radius=15,
        fill=(*accent[:3], 38),
        outline=accent,
        width=2,
    )
    draw.text((120, 465), side, font=_font(31, True), fill=accent)

    current_price = float(payload.get("current_price") or payload["entry_price"])
    return_pct = float(
        payload.get("return_pct")
        if payload.get("return_pct") is not None
        else 0
    )
    move_color = green if return_pct >= 0 else red
    draw.text((92, 605), "Цена входа", font=_font(25), fill=muted)
    draw.text(
        (92, 650),
        _price(float(payload["entry_price"])),
        font=_font(56, True),
        fill=white,
    )
    draw.text((620, 605), "Сейчас", font=_font(25), fill=muted)
    draw.text(
        (620, 650),
        _price(current_price),
        font=_font(56, True),
        fill=white,
    )
    draw.text(
        (620, 720),
        f"по сценарию {return_pct:+.2f}%",
        font=_font(24, True),
        fill=move_color,
    )

    draw.line((92, 790, 1108, 790), fill=(63, 83, 95, 180), width=2)
    metrics = [
        ("Сканер", _scanner_name(str(payload["scanner"]))),
        ("Уверенность", "Высокая"),
        ("Сигнал активен", f"{payload['signal_age_days']} дн."),
        ("Следующая проверка", f"через ~{payload['review_in_days']} дн."),
    ]
    for index, (label, value) in enumerate(metrics):
        x = 92 if index % 2 == 0 else 620
        y = 830 if index < 2 else 1030
        draw.text((x, y), label, font=_font(23), fill=muted)
        draw.text((x, y + 46), value, font=_font(35, True), fill=white)

    draw.line((92, 1240, 1108, 1240), fill=(63, 83, 95, 180), width=2)
    draw.text(
        (92, 1285),
        "Данные аналитической модели. Не является",
        font=_font(22),
        fill=muted,
    )
    draw.text(
        (92, 1320),
        "индивидуальной инвестиционной рекомендацией.",
        font=_font(22),
        fill=muted,
    )

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format="PNG", optimize=True)
    return path


def render_update_card(
    payload: dict,
    output_path: str | Path,
) -> Path:
    image = _gradient()
    draw = ImageDraw.Draw(image, "RGBA")
    green = (61, 217, 166, 255)
    red = (255, 104, 112, 255)
    white = (242, 247, 250, 255)
    muted = (151, 171, 184, 255)
    return_pct = float(payload["return_pct"])
    move_color = green if return_pct >= 0 else red
    status_color = red if payload["closed"] else green

    draw.rounded_rectangle(
        (64, 60, 1136, 1440),
        radius=32,
        fill=(5, 12, 18, 232),
        outline=(69, 94, 108, 170),
        width=2,
    )
    draw.rounded_rectangle((92, 92, 160, 160), radius=14, fill=green)
    draw.text((114, 100), "M", font=_font(42, True), fill=(4, 22, 20, 255))
    draw.text((182, 98), "MEANX", font=_font(42, True), fill=white)
    draw.text((182, 145), "ОБНОВЛЕНИЕ СИГНАЛА", font=_font(20, True), fill=muted)

    draw.text((92, 250), _date(payload["data_date"]), font=_font(25, True), fill=muted)
    draw.text(
        (92, 305),
        str(payload["ticker"]).replace("/USD", ""),
        font=_font(104, True),
        fill=white,
    )
    status = "РЕКОМЕНДАЦИЯ ЗАКРЫТЬ" if payload["closed"] else "СИГНАЛ АКТИВЕН"
    draw.rounded_rectangle(
        (92, 445, 760, 525),
        radius=15,
        fill=(*status_color[:3], 38),
        outline=status_color,
        width=2,
    )
    draw.text((120, 465), status, font=_font(29, True), fill=status_color)

    draw.text((92, 605), "Текущая цена", font=_font(25), fill=muted)
    draw.text(
        (92, 650),
        _price(float(payload["current_price"])),
        font=_font(68, True),
        fill=white,
    )

    draw.line((92, 770, 1108, 770), fill=(63, 83, 95, 180), width=2)
    direction = "Лонг" if payload["direction"] == "long" else "Шорт"
    metrics = [
        ("Движение от входа", f"{return_pct:+.2f}%", move_color),
        ("Направление", direction, white),
        ("Цена при публикации", _price(float(payload["entry_price"])), white),
        ("Сканер", _scanner_name(str(payload["scanner"])), white),
    ]
    for index, (label, value, color) in enumerate(metrics):
        x = 92 if index % 2 == 0 else 620
        y = 830 if index < 2 else 1030
        draw.text((x, y), label, font=_font(23), fill=muted)
        draw.text((x, y + 46), value, font=_font(35, True), fill=color)

    draw.line((92, 1240, 1108, 1240), fill=(63, 83, 95, 180), width=2)
    draw.text(
        (92, 1285),
        "Данные аналитической модели. Не является",
        font=_font(22),
        fill=muted,
    )
    draw.text(
        (92, 1320),
        "индивидуальной инвестиционной рекомендацией.",
        font=_font(22),
        fill=muted,
    )

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format="PNG", optimize=True)
    return path
