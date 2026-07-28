"""Прогрев/скачивание OCR-моделей (используется при сборке Docker-образа,
чтобы контейнер стартовал без сети и без задержки на первый запрос)."""

import numpy as np

from app.ocr import get_engine


def main() -> None:
    engine = get_engine()
    # Холостой прогон инициализирует все onnx-сессии.
    dummy = np.full((64, 256, 3), 255, dtype=np.uint8)
    engine(dummy)
    print("OCR models downloaded and warmed up")


if __name__ == "__main__":
    main()
