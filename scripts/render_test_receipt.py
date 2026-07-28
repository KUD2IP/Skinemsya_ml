"""Рендер простого тестового пречека для smoke-теста пайплайна."""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

LINES = [
    ("Кафе Пушкинъ", "center"),
    ("Счет: 45  Стол: 7", "left"),
    ("Официант: Мария", "left"),
    ("15.07.2026 19:42", "left"),
    ("-" * 38, "left"),
    ("Борщ с говядиной      2 x 450.00  900.00", "left"),
    ("Пельмени домашние     1 x 520.00  520.00", "left"),
    ("Морс клюквенный       3 x 150.00  450.00", "left"),
    ("Хлебная корзина                   180.00", "left"),
    ("Сервисный сбор 10%                205.00", "left"),
    ("-" * 38, "left"),
    ("ИТОГО:                           2255.00", "left"),
    ("Спасибо за визит!", "center"),
]


def render(path: str) -> None:
    width, line_height, margin = 620, 34, 24
    height = margin * 2 + line_height * len(LINES)
    img = Image.new("RGB", (width, height), (250, 250, 248))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", 22)
    except OSError:
        font = ImageFont.load_default()

    y = margin
    for text, align in LINES:
        if align == "center":
            text_width = draw.textlength(text, font=font)
            x = (width - text_width) / 2
        else:
            x = margin
        draw.text((x, y), text, fill=(30, 30, 30), font=font)
        y += line_height

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    img.save(path)
    print("saved", path)


if __name__ == "__main__":
    render("tests/fixtures/simple_precheck.png")
