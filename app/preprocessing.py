"""Подготовка фото чека перед OCR.

Фото с телефона: произвольная ориентация (EXIF + поворот на 90°),
перекос, тени, лишний фон. Здесь только дешёвые операции —
сервис работает на 1 vCPU / 1 ГБ RAM.
"""

from __future__ import annotations

import io
import re

import cv2
import numpy as np
from PIL import Image, ImageOps

# Крупнее не нужно: PP-OCRv5 det сам ресайзит, а память ограничена.
MAX_SIDE = 1600

# Уменьшенная копия для выбора ориентации: полный OCR на 4 углах
# слишком дорог, на ~900px хватает уверенного выбора.
_ORIENT_MAX_SIDE = 900

_HEADER_HINT = re.compile(
    r"(гостев|наименован|ресторан|кафе|бар\b|чек\b|сч[её]т\b|стол\b|заказ)",
    re.IGNORECASE,
)
_FOOTER_HINT = re.compile(
    r"(итого|к\s*оплате|подытог|спасибо|чаевые|qr|наведите)",
    re.IGNORECASE,
)
_LATIN_TO_CYR = str.maketrans(
    {
        "a": "а",
        "b": "в",
        "c": "с",
        "e": "е",
        "h": "н",
        "k": "к",
        "m": "м",
        "o": "о",
        "p": "р",
        "t": "т",
        "x": "х",
        "y": "у",
    }
)


class ImageDecodeError(Exception):
    pass


def decode_image(data: bytes) -> np.ndarray:
    """bytes (JPEG/PNG/WebP) -> BGR ndarray с учётом EXIF-ориентации."""
    try:
        pil = Image.open(io.BytesIO(data))
        pil = ImageOps.exif_transpose(pil)
        pil = pil.convert("RGB")
    except Exception as exc:
        raise ImageDecodeError(str(exc)) from exc
    return cv2.cvtColor(np.asarray(pil), cv2.COLOR_RGB2BGR)


def _resize_max_side(image: np.ndarray, max_side: int = MAX_SIDE) -> np.ndarray:
    h, w = image.shape[:2]
    scale = max_side / max(h, w)
    if scale >= 1.0:
        return image
    return cv2.resize(image, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)


def _deskew_small_angle(image: np.ndarray) -> np.ndarray:
    """Выправляет небольшой (< ~10°) наклон по доминирующему направлению строк.

    Чеки обычно фотографируют почти вертикально; большие повороты на 90°
    закрывает EXIF + `_auto_rotate_cardinal`, а сильную перспективу оставляем
    детектору OCR.
    """
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 50, 150)
    lines = cv2.HoughLinesP(
        edges, 1, np.pi / 180, threshold=80, minLineLength=gray.shape[1] // 4, maxLineGap=10
    )
    if lines is None:
        return image

    angles = []
    for x1, y1, x2, y2 in np.asarray(lines).reshape(-1, 4):
        angle = np.degrees(np.arctan2(y2 - y1, x2 - x1))
        if abs(angle) < 10:  # интересуют только почти горизонтальные строки
            angles.append(angle)
    if len(angles) < 5:
        return image

    angle = float(np.median(angles))
    if abs(angle) < 0.5:
        return image

    h, w = image.shape[:2]
    matrix = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    return cv2.warpAffine(
        image, matrix, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE
    )


def _enhance_contrast(image: np.ndarray) -> np.ndarray:
    """CLAHE по каналу яркости: вытягивает бледную термопечать, не пережигая фото."""
    lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB)
    l_channel, a, b = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    l_channel = clahe.apply(l_channel)
    return cv2.cvtColor(cv2.merge((l_channel, a, b)), cv2.COLOR_LAB2BGR)


def _rotate_cardinal(image: np.ndarray, degrees: int) -> np.ndarray:
    if degrees == 0:
        return image
    if degrees == 90:
        return cv2.rotate(image, cv2.ROTATE_90_CLOCKWISE)
    if degrees == 180:
        return cv2.rotate(image, cv2.ROTATE_180)
    if degrees == 270:
        return cv2.rotate(image, cv2.ROTATE_90_COUNTERCLOCKWISE)
    raise ValueError(f"unsupported rotation: {degrees}")


def _normalize_hint(text: str) -> str:
    return text.lower().translate(_LATIN_TO_CYR)


def _orientation_score(tokens: list, image_height: float) -> float:
    """Насколько похоже, что текст чека читается сверху вниз."""
    if not tokens:
        return -1.0

    avg_score = sum(t.score for t in tokens) / len(tokens)
    cyr_words = 0
    header_top = 0
    footer_bottom = 0
    header_bottom = 0
    footer_top = 0

    for token in tokens:
        text = token.text or ""
        letters = sum(ch.isalpha() for ch in text)
        if letters >= 3 and any("\u0400" <= ch <= "\u04ff" for ch in text):
            cyr_words += 1
        normalized = _normalize_hint(text)
        y_ratio = token.y_center / max(image_height, 1.0)
        if _HEADER_HINT.search(normalized):
            if y_ratio < 0.45:
                header_top += 1
            elif y_ratio > 0.55:
                header_bottom += 1
        if _FOOTER_HINT.search(normalized):
            if y_ratio > 0.55:
                footer_bottom += 1
            elif y_ratio < 0.45:
                footer_top += 1

    # Штрафуем «вверх ногами»: шапка внизу / подвал сверху.
    return (
        avg_score * 12.0
        + cyr_words
        + header_top * 4.0
        + footer_bottom * 4.0
        - header_bottom * 5.0
        - footer_top * 5.0
    )


def _auto_rotate_cardinal(image: np.ndarray) -> np.ndarray:
    """Выбирает ориентацию 0/90/180/270 по лёгкому OCR на уменьшенной копии.

    Альбомные фото чеков (телефон горизонтально) — самый частый провал:
    без поворота парсер не видит позиций. Для портретных снимков обычно
    достаточно EXIF; 180° проверяем только если шапка не читается.
    """
    # Ленивый импорт: иначе цикл preprocessing ↔ ocr при загрузке модулей.
    from app import ocr

    height, width = image.shape[:2]
    probe = _resize_max_side(image, _ORIENT_MAX_SIDE)

    if width > height * 1.05:
        # Альбомный кадр: либо чек снят боком (нужен 90/270), либо это
        # широкий, но уже «правильный» скан — проверяем все 4 угла.
        candidates = (0, 90, 180, 270)
    else:
        tokens0 = ocr.run_ocr(probe)
        score0 = _orientation_score(tokens0, probe.shape[0])
        if score0 >= 10.0:
            return image
        candidates = (0, 180)

    best_degrees = candidates[0]
    best_score = float("-inf")
    for degrees in candidates:
        rotated = _rotate_cardinal(probe, degrees)
        tokens = ocr.run_ocr(rotated)
        score = _orientation_score(tokens, rotated.shape[0])
        if score > best_score:
            best_score = score
            best_degrees = degrees

    if best_degrees == 0:
        return image
    return _rotate_cardinal(image, best_degrees)


def preprocess(data: bytes) -> np.ndarray:
    """Полный препроцессинг: decode -> resize -> автоповорот -> deskew -> контраст."""
    image = decode_image(data)
    image = _resize_max_side(image)
    image = _auto_rotate_cardinal(image)
    image = _deskew_small_angle(image)
    image = _enhance_contrast(image)
    return image
