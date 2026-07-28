"""Отладка пайплайна на одном фото: печатает OCR-токены и результат парсера.

Использование: python scripts/debug_image.py path/to/photo.jpg
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import ocr, parser, preprocessing  # noqa: E402


def main() -> None:
    image_path = Path(sys.argv[1])
    data = image_path.read_bytes()

    t0 = time.perf_counter()
    image = preprocessing.preprocess(data)
    t1 = time.perf_counter()
    tokens = ocr.run_ocr(image)
    t2 = time.perf_counter()
    response = parser.parse_receipt(tokens)
    t3 = time.perf_counter()

    print(f"=== OCR tokens ({len(tokens)}) ===")
    for token in tokens:
        print(f"  [{token.score:.2f}] y={token.y_center:7.1f} x={token.x_min:6.1f} {token.text!r}")

    print("\n=== Parsed response ===")
    print(json.dumps(response.model_dump(), ensure_ascii=False, indent=2))

    print(
        f"\npreprocess={t1 - t0:.2f}s ocr={t2 - t1:.2f}s parse={t3 - t2:.2f}s total={t3 - t0:.2f}s"
    )


if __name__ == "__main__":
    main()
