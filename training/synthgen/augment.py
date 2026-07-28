"""Аугментации «фото чека на телефон»: перспектива, тени, блюр, выцветание.

Без тяжёлых зависимостей — только numpy + OpenCV.
"""

import random

import cv2
import numpy as np


def thermal_fade(image: np.ndarray, rng: random.Random) -> np.ndarray:
    """Имитация выцветшей термопечати: случайное ослабление «чернил»."""
    if rng.random() < 0.5:
        return image
    gray = image.astype(np.float32)
    fade = rng.uniform(0.15, 0.45)
    noise = np.random.default_rng(rng.randrange(1 << 30)).uniform(
        1 - fade, 1.0, size=image.shape[:2]
    )[..., None]
    faded = 255 - (255 - gray) * noise
    return np.clip(faded, 0, 255).astype(np.uint8)


def shadow_gradient(image: np.ndarray, rng: random.Random) -> np.ndarray:
    """Неравномерное освещение / тень от руки или телефона."""
    if rng.random() < 0.4:
        return image
    h, w = image.shape[:2]
    axis = rng.choice(["x", "y", "diag"])
    strength = rng.uniform(0.12, 0.35)
    xs = np.linspace(0, 1, w)[None, :]
    ys = np.linspace(0, 1, h)[:, None]
    if axis == "x":
        mask = xs * np.ones((h, 1))
    elif axis == "y":
        mask = ys * np.ones((1, w))
    else:
        mask = (xs + ys) / 2
    if rng.random() < 0.5:
        mask = 1 - mask
    factor = 1 - strength * mask
    return np.clip(image.astype(np.float32) * factor[..., None], 0, 255).astype(np.uint8)


def perspective_warp(image: np.ndarray, rng: random.Random, max_shift: float = 0.04) -> np.ndarray:
    """Небольшая перспектива, как при съёмке под углом."""
    if rng.random() < 0.3:
        return image
    h, w = image.shape[:2]
    dx, dy = w * max_shift, h * max_shift
    src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    dst = np.float32(
        [
            [rng.uniform(0, dx), rng.uniform(0, dy)],
            [w - rng.uniform(0, dx), rng.uniform(0, dy)],
            [w - rng.uniform(0, dx), h - rng.uniform(0, dy)],
            [rng.uniform(0, dx), h - rng.uniform(0, dy)],
        ]
    )
    matrix = cv2.getPerspectiveTransform(src, dst)
    return cv2.warpPerspective(image, matrix, (w, h), borderMode=cv2.BORDER_REPLICATE)


def small_rotation(image: np.ndarray, rng: random.Random, max_angle: float = 3.0) -> np.ndarray:
    if rng.random() < 0.4:
        return image
    h, w = image.shape[:2]
    angle = rng.uniform(-max_angle, max_angle)
    matrix = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    return cv2.warpAffine(image, matrix, (w, h), borderMode=cv2.BORDER_REPLICATE)


def blur_and_noise(image: np.ndarray, rng: random.Random) -> np.ndarray:
    if rng.random() < 0.6:
        k = rng.choice([3, 3, 5])
        image = cv2.GaussianBlur(image, (k, k), 0)
    if rng.random() < 0.5:
        sigma = rng.uniform(2, 8)
        noise = np.random.default_rng(rng.randrange(1 << 30)).normal(0, sigma, image.shape)
        image = np.clip(image.astype(np.float32) + noise, 0, 255).astype(np.uint8)
    return image


def jpeg_compress(image: np.ndarray, rng: random.Random) -> np.ndarray:
    if rng.random() < 0.4:
        return image
    quality = rng.randint(40, 85)
    _, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return cv2.imdecode(encoded, cv2.IMREAD_COLOR)


def augment_receipt(image: np.ndarray, rng: random.Random) -> np.ndarray:
    """Полная цепочка для целого чека."""
    image = thermal_fade(image, rng)
    image = shadow_gradient(image, rng)
    image = small_rotation(image, rng)
    image = perspective_warp(image, rng)
    image = blur_and_noise(image, rng)
    image = jpeg_compress(image, rng)
    return image


def augment_line(image: np.ndarray, rng: random.Random) -> np.ndarray:
    """Лёгкая цепочка для строк (rec-обучение): без сильной геометрии."""
    image = thermal_fade(image, rng)
    image = small_rotation(image, rng, max_angle=1.5)
    image = blur_and_noise(image, rng)
    image = jpeg_compress(image, rng)
    return image
