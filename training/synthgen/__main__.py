"""CLI: python -m training.synthgen --count 500 --out data/synth"""

import argparse
from pathlib import Path

from training.synthgen.generator import generate_dataset


def main() -> None:
    parser = argparse.ArgumentParser(description="Генерация синтетических пречеков")
    parser.add_argument("--count", type=int, default=200, help="сколько чеков сгенерировать")
    parser.add_argument("--out", type=Path, default=Path("data/synth"), help="каталог вывода")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--fonts-dir", type=Path, default=Path("data/fonts"), help="доп. шрифты .ttf")
    parser.add_argument("--no-lines", action="store_true", help="не делать кропы строк для rec-обучения")
    args = parser.parse_args()

    generate_dataset(
        out_dir=args.out,
        count=args.count,
        seed=args.seed,
        fonts_dir=args.fonts_dir,
        make_line_crops=not args.no_lines,
    )


if __name__ == "__main__":
    main()
