import numpy as np
import pytest

from app.ocr import OcrToken


def make_tokens(lines: list[str], score: float = 0.95) -> list[OcrToken]:
    """Строит OCR-токены из текстовых строк: одна строка = один токен."""
    tokens = []
    for i, text in enumerate(lines):
        y = 10 + i * 40
        box = np.array([[10, y], [500, y], [500, y + 30], [10, y + 30]], dtype=float)
        tokens.append(OcrToken(box=box, text=text, score=score))
    return tokens


@pytest.fixture
def tokens_factory():
    return make_tokens
