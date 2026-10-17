"""Скачать cdek-ocr/receipt-ocr-ru и разложить в data/cdek/{split}/.

Формат вывода (совместим с eval / synth labels):
  data/cdek/{train,val,test}/
    images/<stem>.jpg|png|...
    labels/<stem>.json   — контракт Skinemsya

Примеры:
  python -m training.cdek.download_and_prepare
  python -m training.cdek.download_and_prepare --out data/cdek --splits test
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path

from training.cdek.normalize import record_to_label

DATASET_ID = "cdek-ocr/receipt-ocr-ru"
SPLIT_MAP = {"train": "train", "val": "val", "validation": "val", "test": "test"}


def _safe_stem(image_id: str | None, file_name: str | None, index: int) -> str:
    raw = (image_id or file_name or f"sample_{index:05d}").strip()
    raw = Path(raw).stem
    cleaned = re.sub(r"[^\w.\-]+", "_", raw, flags=re.UNICODE).strip("._")
    return cleaned or f"sample_{index:05d}"


_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}


def _save_image(image_obj, dest: Path) -> Path:
    """HF Image / PIL / path → файл на диске."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if hasattr(image_obj, "save"):
        # stem из image_id иногда содержит точки («2025-10-21_16.17») —
        # Path.suffix тогда «.17», PIL падает. Всегда пишем .png.
        dest = dest.with_suffix(".png")
        image_obj.convert("RGB").save(dest)
        return dest
    if isinstance(image_obj, (str, Path)):
        src = Path(image_obj)
        suffix = src.suffix.lower() if src.suffix.lower() in _IMAGE_EXTS else ".jpg"
        dest = dest.with_suffix(suffix)
        shutil.copy2(src, dest)
        return dest
    if isinstance(image_obj, dict) and "path" in image_obj:
        return _save_image(image_obj["path"], dest)
    raise TypeError(f"Неизвестный тип image: {type(image_obj)}")


def prepare_split(dataset, split_name: str, out_root: Path) -> dict:
    out_dir = out_root / split_name
    images_dir = out_dir / "images"
    labels_dir = out_dir / "labels"
    images_dir.mkdir(parents=True, exist_ok=True)
    labels_dir.mkdir(parents=True, exist_ok=True)

    kept = 0
    skipped = 0
    for idx, row in enumerate(dataset):
        as_dict = dict(row)
        label = record_to_label(as_dict)
        if label is None:
            skipped += 1
            continue

        stem = _safe_stem(as_dict.get("image_id"), as_dict.get("file_name"), idx)
        # уникальность при коллизиях имён
        image_path = images_dir / stem
        label_path = labels_dir / f"{stem}.json"
        n = 1
        while label_path.exists():
            alt = f"{stem}_{n}"
            image_path = images_dir / alt
            label_path = labels_dir / f"{alt}.json"
            n += 1

        try:
            saved = _save_image(as_dict["image"], image_path)
        except Exception as exc:  # noqa: BLE001 — один битый файл не валит весь split
            print(f"  skip image {stem}: {exc}")
            skipped += 1
            continue

        label["image"] = saved.name
        label_path.write_text(json.dumps(label, ensure_ascii=False, indent=2), encoding="utf-8")
        kept += 1

    return {"split": split_name, "kept": kept, "skipped": skipped, "path": str(out_dir)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Download & prepare CDEK receipt-ocr-ru")
    parser.add_argument("--out", type=Path, default=Path("data/cdek"))
    parser.add_argument(
        "--splits",
        nargs="+",
        default=["train", "val", "test"],
        help="какие сплиты подготовить (train/val/test)",
    )
    parser.add_argument(
        "--dataset",
        default=DATASET_ID,
        help="HF dataset id",
    )
    args = parser.parse_args()

    try:
        from datasets import load_dataset
    except ImportError as exc:
        raise SystemExit(
            "Нужен extras train: pip install -e '.[train]' "
            "(пакеты datasets, huggingface_hub)"
        ) from exc

    wanted = []
    for name in args.splits:
        key = SPLIT_MAP.get(name, name)
        if key not in wanted:
            wanted.append(key)

    print(f"Loading {args.dataset} …")
    ds = load_dataset(args.dataset)
    available = set(ds.keys())
    # HF может назвать validation вместо val
    alias = {"val": "validation", "validation": "val"}

    summary = []
    for split in wanted:
        hf_split = split if split in available else alias.get(split)
        if hf_split not in available and split not in available:
            print(f"Split '{split}' отсутствует в датасете ({sorted(available)})")
            continue
        actual = hf_split if hf_split in available else split
        out_name = "val" if actual in {"val", "validation"} else actual
        print(f"Preparing {actual} → {args.out / out_name}")
        stats = prepare_split(ds[actual], out_name, args.out)
        summary.append(stats)
        print(f"  kept={stats['kept']} skipped={stats['skipped']}")

    meta = {"dataset": args.dataset, "splits": summary}
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "manifest.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("Done:", args.out)


if __name__ == "__main__":
    main()
