"""Нормализация разметки CDEK → контракт Skinemsya (merchant/date/total/items)."""

from __future__ import annotations

import re
from typing import Any

_BAD_TEXT = re.compile(
    r"^(не\s*распознано|нераспознано|не\s*указано|null|none|-)$",
    re.IGNORECASE,
)
_DATE_DMY = re.compile(
    r"(?P<d>\d{1,2})[.\-/](?P<m>\d{1,2})[.\-/](?P<y>\d{2,4})(?:\s+\d{1,2}:\d{2})?"
)


def is_bad_text(value: Any) -> bool:
    if value is None:
        return True
    text = str(value).strip()
    if not text:
        return True
    return bool(_BAD_TEXT.match(text))


def parse_money(value: Any) -> float | None:
    if value is None or is_bad_text(value):
        return None
    raw = str(value).strip().replace("\u00a0", " ").replace(" ", "").replace(",", ".")
    raw = re.sub(r"[^\d.\-]", "", raw)
    if not raw or raw in {".", "-", "-."}:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def parse_qty(value: Any) -> float | None:
    qty = parse_money(value)
    if qty is None or qty <= 0:
        return None
    return qty


def normalize_date(value: Any) -> str | None:
    if value is None or is_bad_text(value):
        return None
    text = str(value).strip()
    match = _DATE_DMY.search(text)
    if not match:
        return None
    day, month, year = match.group("d"), match.group("m"), match.group("y")
    if len(year) == 2:
        year = "20" + year
    try:
        d, m, y = int(day), int(month), int(year)
    except ValueError:
        return None
    if not (1 <= m <= 12 and 1 <= d <= 31 and 2000 <= y <= 2100):
        return None
    return f"{y:04d}-{m:02d}-{d:02d}"


def _item_from_parallel_lists(
    texts: list[Any] | None,
    quantities: list[Any] | None,
    prices: list[Any] | None,
    sums: list[Any] | None,
    confidences: list[Any] | None,
) -> list[dict]:
    texts = list(texts or [])
    quantities = list(quantities or [])
    prices = list(prices or [])
    sums = list(sums or [])
    confidences = list(confidences or [])
    items: list[dict] = []
    for idx, name_raw in enumerate(texts):
        if is_bad_text(name_raw):
            continue
        name = str(name_raw).strip()[:255]
        if len(re.findall(r"[a-zA-Zа-яА-ЯёЁ]", name)) < 2:
            continue
        qty = parse_qty(quantities[idx] if idx < len(quantities) else None) or 1.0
        unit = parse_money(prices[idx] if idx < len(prices) else None)
        total = parse_money(sums[idx] if idx < len(sums) else None)
        if total is None and unit is not None:
            total = round(unit * qty, 2)
        if total is None or total < 0:
            continue
        if unit is None and qty > 0:
            unit = round(total / qty, 2)
        conf = None
        if idx < len(confidences) and confidences[idx] is not None:
            try:
                conf = float(confidences[idx])
            except (TypeError, ValueError):
                conf = None
        items.append(
            {
                "name": name,
                "quantity": qty,
                "unit_price": unit,
                "total_price": total,
                "confidence": conf,
            }
        )
    return items


def record_to_label(row: dict[str, Any]) -> dict[str, Any] | None:
    """HF/parquet row или jsonl-запись → label контракта. None = пропустить."""
    merchant = row.get("seller_text") or row.get("seller")
    if isinstance(merchant, dict):
        merchant = merchant.get("text")
    if is_bad_text(merchant):
        merchant = None
    else:
        merchant = str(merchant).strip()[:255]

    date_raw = row.get("date_text") or row.get("date")
    if isinstance(date_raw, dict):
        date_raw = date_raw.get("text")
    date = normalize_date(date_raw)

    total_raw = row.get("total_text") or row.get("total")
    if isinstance(total_raw, dict):
        total_raw = total_raw.get("text")
    total = parse_money(total_raw)

    items_field = row.get("items")
    if isinstance(items_field, list) and items_field and isinstance(items_field[0], dict):
        items = []
        for raw in items_field:
            name = raw.get("text") or raw.get("name")
            if is_bad_text(name):
                continue
            qty = parse_qty(raw.get("quantity") or raw.get("qty")) or 1.0
            unit = parse_money(raw.get("price") or raw.get("unit_price"))
            sum_v = parse_money(raw.get("sum") or raw.get("total_price") or raw.get("total"))
            if sum_v is None and unit is not None:
                sum_v = round(unit * qty, 2)
            if sum_v is None:
                continue
            items.append(
                {
                    "name": str(name).strip()[:255],
                    "quantity": qty,
                    "unit_price": unit if unit is not None else round(sum_v / qty, 2),
                    "total_price": sum_v,
                    "confidence": raw.get("confidence"),
                }
            )
    else:
        items = _item_from_parallel_lists(
            row.get("item_texts"),
            row.get("item_quantities"),
            row.get("item_prices"),
            row.get("item_sums"),
            row.get("item_confidences"),
        )

    # Нужен хотя бы total или позиции — иначе нечему учить/мерить.
    if not items and total is None:
        return None

    return {
        "merchant": merchant,
        "date": date,
        "total": total,
        "currency": "RUB",
        "items": items,
        "confidence": None,
        "errors": [],
        "image_id": row.get("image_id") or row.get("file_name"),
    }
