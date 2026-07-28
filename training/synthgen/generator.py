"""Генератор синтетических ресторанных пречеков.

Выдаёт три вида артефактов:
  images/NNNNN.jpg      — фото-подобный чек (аугментированный)
  labels/NNNNN.json     — ground truth в формате контракта бека
  lines/NNNNN_LL.png    — кропы строк + rec_gt.txt для дообучения rec-модели
"""

import json
import random
import re
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from training.synthgen import vocab
from training.synthgen.augment import augment_line, augment_receipt

FONT_CANDIDATES = [
    # macOS
    "/System/Library/Fonts/Menlo.ttc",
    "/System/Library/Fonts/Monaco.ttf",
    "/System/Library/Fonts/Supplemental/Courier New.ttf",
    "/System/Library/Fonts/Supplemental/PTMono.ttc",
    # Linux / Colab
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
    "/usr/share/fonts/truetype/freefont/FreeMono.ttf",
]


def available_fonts(extra_dir: Path | None = None) -> list[str]:
    fonts = [p for p in FONT_CANDIDATES if Path(p).exists()]
    if extra_dir and extra_dir.exists():
        fonts.extend(str(p) for p in extra_dir.glob("*.tt[fc]"))
    if not fonts:
        raise RuntimeError("Не найден ни один шрифт с кириллицей; положите .ttf в data/fonts")
    return fonts


@dataclass
class GroundTruthItem:
    name: str
    quantity: float
    unit_price: float
    total_price: float


@dataclass
class SyntheticReceipt:
    merchant: str
    date: str  # YYYY-MM-DD
    items: list[GroundTruthItem]
    total: float
    text_lines: list[str] = field(default_factory=list)

    def to_contract_json(self) -> dict:
        return {
            "merchant": self.merchant,
            "date": self.date,
            "total": round(self.total, 2),
            "currency": "RUB",
            "items": [
                {
                    "name": item.name,
                    "quantity": item.quantity,
                    "unit_price": round(item.unit_price, 2),
                    "total_price": round(item.total_price, 2),
                    "confidence": None,
                }
                for item in self.items
            ],
            "confidence": None,
            "errors": [],
        }


def _price(rng: random.Random) -> float:
    base = rng.choice([90, 120, 150, 180, 200, 250, 290, 320, 350, 390, 450, 520, 590, 650, 790, 890, 990, 1200])
    return float(base + rng.choice([0, 0, 0, 5, 10, 20, 50]))


def _fmt_money(value: float, rng: random.Random) -> str:
    if rng.random() < 0.75:
        return f"{value:.2f}".replace(".", rng.choice([".", "."]))
    return f"{value:g}"


def _make_item_lines(item: GroundTruthItem, width: int, rng: random.Random) -> list[str]:
    """Строки чека для одной позиции в одном из типовых форматов."""
    qty = f"{item.quantity:g}"
    unit = _fmt_money(item.unit_price, rng)
    total = _fmt_money(item.total_price, rng)
    x_char = rng.choice(["x", "х", "*"])
    layout = rng.random()

    if layout < 0.4:
        left = f"{item.name} {qty} {x_char} {unit}"
        return [f"{left}{' ' * max(2, width - len(left) - len(total))}{total}"]
    if layout < 0.6:
        # Количество переносится на следующую строку
        second = f"  {qty} {x_char} {unit} = {total}"
        return [item.name, second]
    if layout < 0.8 and item.quantity == int(item.quantity):
        # Колонки: name qty unit total
        left = f"{item.name}"
        cols = f"{qty:>3} {unit:>9} {total:>9}"
        return [f"{left}{' ' * max(2, width - len(left) - len(cols))}{cols}"]
    # Просто имя и сумма (qty=1 или вес уже в названии)
    left = item.name
    return [f"{left}{' ' * max(2, width - len(left) - len(total))}{total}"]


def make_receipt(rng: random.Random) -> SyntheticReceipt:
    merchant = rng.choice(vocab.MERCHANTS)
    day, month, year = rng.randint(1, 28), rng.randint(1, 12), rng.randint(2024, 2026)
    date_iso = f"{year}-{month:02d}-{day:02d}"
    date_display = rng.choice([f"{day:02d}.{month:02d}.{year}", f"{day:02d}.{month:02d}.{year % 100:02d}"])
    time_display = f"{rng.randint(10, 23)}:{rng.randint(0, 59):02d}"

    width = rng.choice([38, 40, 42, 44])

    items = []
    for _ in range(rng.randint(2, 9)):
        name = rng.choice(vocab.DISHES)
        quantity = float(rng.choice([1, 1, 1, 1, 2, 2, 3, 4]))
        unit_price = _price(rng)
        items.append(GroundTruthItem(name, quantity, unit_price, quantity * unit_price))

    total = sum(i.total_price for i in items)

    # Иногда добавляем чаевые/сервисный сбор — важный кейс для проекта.
    if rng.random() < 0.35:
        tips_name = rng.choice(vocab.TIPS_LINES)
        tips_value = round(total * rng.choice([0.1, 0.1, 0.12, 0.15]), 2)
        items.append(GroundTruthItem(tips_name, 1.0, tips_value, tips_value))
        total += tips_value

    lines: list[str] = [merchant.center(width)]
    header = rng.choice(vocab.SERVICE_HEADER_TEMPLATES).format(
        bill=rng.randint(1, 300),
        table=rng.randint(1, 25),
        guests=rng.randint(1, 8),
        waiter=rng.choice(vocab.WAITERS),
        time=time_display,
    )
    lines.append(header)
    if rng.random() < 0.7:
        lines.append(f"Официант: {rng.choice(vocab.WAITERS)}")
    lines.append(f"{date_display} {time_display}")
    lines.append("-" * width)

    for item in items:
        lines.extend(_make_item_lines(item, width, rng))

    lines.append("-" * width)
    total_label = rng.choice(vocab.TOTAL_LABELS)
    total_str = _fmt_money(total, rng)
    lines.append(f"{total_label}{' ' * max(2, width - len(total_label) - len(total_str))}{total_str}")
    if rng.random() < 0.6:
        lines.append(rng.choice(vocab.FOOTER_LINES).center(width))

    return SyntheticReceipt(
        merchant=merchant, date=date_iso, items=items, total=round(total, 2), text_lines=lines
    )


# Сегмент — кусок строки без двойных пробелов. OCR-детектор на реальном чеке
# режет строку именно так (название отдельно, цена отдельно), поэтому и
# rec-модель дообучаем на сегментах, а не на целых строках.
_SEGMENT_RE = re.compile(r"\S+(?: \S+)*")


def render_receipt(
    receipt: SyntheticReceipt, font_path: str, rng: random.Random
) -> tuple[np.ndarray, list[tuple[str, tuple[int, int, int, int]]]]:
    """Рендер чека. Возвращает BGR-изображение и (текст сегмента, bbox)."""
    font_size = rng.choice([20, 22, 24])
    font = ImageFont.truetype(font_path, font_size)
    line_height = int(font_size * rng.uniform(1.35, 1.6))
    margin = rng.randint(18, 36)

    max_line_px = max(int(font.getlength(line)) for line in receipt.text_lines if line.strip()) if receipt.text_lines else 400
    width = max_line_px + margin * 2
    height = margin * 2 + line_height * len(receipt.text_lines)

    paper = rng.randint(242, 255)
    img = Image.new("RGB", (width, height), (paper, paper, max(paper - rng.randint(0, 6), 235)))
    draw = ImageDraw.Draw(img)

    ink = rng.randint(15, 70)
    boxes: list[tuple[str, tuple[int, int, int, int]]] = []
    y = margin
    for line in receipt.text_lines:
        if line.strip():
            draw.text((margin, y), line, fill=(ink, ink, ink), font=font)
            for match in _SEGMENT_RE.finditer(line):
                x_start = margin + int(font.getlength(line[: match.start()]))
                x_end = margin + int(font.getlength(line[: match.end()]))
                boxes.append((match.group(), (x_start, y, x_end, y + font_size + 6)))
        y += line_height

    bgr = cv2.cvtColor(np.asarray(img), cv2.COLOR_RGB2BGR)
    return bgr, boxes


def generate_dataset(
    out_dir: Path,
    count: int,
    seed: int = 42,
    fonts_dir: Path | None = None,
    make_line_crops: bool = True,
) -> None:
    rng = random.Random(seed)
    fonts = available_fonts(fonts_dir)

    images_dir = out_dir / "images"
    labels_dir = out_dir / "labels"
    lines_dir = out_dir / "lines"
    for directory in (images_dir, labels_dir, lines_dir):
        directory.mkdir(parents=True, exist_ok=True)

    rec_gt_path = out_dir / "rec_gt.txt"
    charset: set[str] = set()

    with open(rec_gt_path, "w", encoding="utf-8") as rec_gt:
        for index in range(count):
            receipt = make_receipt(rng)
            font_path = rng.choice(fonts)
            clean, boxes = render_receipt(receipt, font_path, rng)

            photo = augment_receipt(clean.copy(), rng)
            image_name = f"{index:05d}.jpg"
            cv2.imwrite(str(images_dir / image_name), photo, [cv2.IMWRITE_JPEG_QUALITY, 90])

            label = receipt.to_contract_json()
            label["image"] = image_name
            (labels_dir / f"{index:05d}.json").write_text(
                json.dumps(label, ensure_ascii=False, indent=2), encoding="utf-8"
            )

            if make_line_crops:
                for line_index, (text, (x1, y1, x2, y2)) in enumerate(boxes):
                    text = text.strip()
                    if not text or set(text) == {"-"}:
                        continue
                    pad = 4
                    crop = clean[max(0, y1 - pad):y2 + pad, max(0, x1 - pad):x2 + pad]
                    if crop.size == 0:
                        continue
                    crop = augment_line(crop, rng)
                    # Убираем художественные пробелы выравнивания — rec-модель
                    # учится на нормализованном тексте.
                    normalized = " ".join(text.split())
                    crop_name = f"{index:05d}_{line_index:02d}.png"
                    cv2.imwrite(str(lines_dir / crop_name), crop)
                    rec_gt.write(f"lines/{crop_name}\t{normalized}\n")
                    charset.update(normalized)

    charset.discard(" ")
    (out_dir / "charset.txt").write_text(
        "\n".join(sorted(charset)) + "\n", encoding="utf-8"
    )
    print(f"Сгенерировано {count} чеков в {out_dir}")
