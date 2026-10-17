"""Экспорт строк для fine-tune PP-OCR rec из CDEK (+ опционально merge с synth).

Берём только достаточно уверенные короткие поля (seller/date/total/items),
кропаем по нормализованным bbox, пишем:
  <out>/lines/*.png
  <out>/rec_gt.txt          — path\\ttext (как synthgen)
  <out>/manifest.json

  python -m training.cdek.export_rec_lines --cdek data/cdek --out data/cdek_lines
  python -m training.cdek.export_rec_lines --cdek data/cdek --synth data/synth --out data/rec_mix
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import shutil
from pathlib import Path

import numpy as np
from PIL import Image

from training.cdek.normalize import is_bad_text

_MIN_CONF = 0.8
_MAX_TEXT_LEN = 40
_PAD = 0.01  # доли от размера картинки вокруг bbox


def _parse_bbox(raw) -> list[list[float]] | None:
    if raw is None or raw == "" or raw == []:
        return None
    if isinstance(raw, str):
        try:
            raw = ast.literal_eval(raw)
        except (SyntaxError, ValueError):
            return None
    if not isinstance(raw, (list, tuple)) or len(raw) < 4:
        return None
    pts = []
    for p in raw:
        if not isinstance(p, (list, tuple)) or len(p) < 2:
            return None
        pts.append([float(p[0]), float(p[1])])
    return pts


def _crop_normalized(image: Image.Image, bbox: list[list[float]], pad: float = _PAD) -> Image.Image | None:
    w, h = image.size
    xs = [p[0] for p in bbox]
    ys = [p[1] for p in bbox]
    # HF: обычно нормированные 0..1; если уже пиксели — >1.5
    if max(xs) <= 1.5 and max(ys) <= 1.5:
        x0 = max(0, int((min(xs) - pad) * w))
        x1 = min(w, int((max(xs) + pad) * w))
        y0 = max(0, int((min(ys) - pad) * h))
        y1 = min(h, int((max(ys) + pad) * h))
    else:
        x0 = max(0, int(min(xs) - pad * w))
        x1 = min(w, int(max(xs) + pad * w))
        y0 = max(0, int(min(ys) - pad * h))
        y1 = min(h, int(max(ys) + pad * h))
    if x1 - x0 < 4 or y1 - y0 < 4:
        return None
    return image.crop((x0, y0, x1, y1))


def _ok_text(text: str) -> bool:
    if is_bad_text(text):
        return False
    text = text.strip()
    if not text or len(text) > _MAX_TEXT_LEN:
        return False
    if len(re.findall(r"[a-zA-Zа-яА-ЯёЁ0-9]", text)) < 1:
        return False
    return True


def _add_field(
    rows: list[tuple[str, str, float | None, list | None]],
    text,
    conf,
    bbox,
) -> None:
    if text is None:
        return
    t = str(text).strip()
    if not _ok_text(t):
        return
    c = float(conf) if conf is not None else None
    if c is not None and c < _MIN_CONF:
        return
    rows.append((t, t, c, _parse_bbox(bbox)))


def export_from_hf_split(dataset, split_name: str, out_lines: Path, prefix: str) -> list[str]:
    """Возвращает строки rec_gt (относительный path\\ttext)."""
    gt_lines: list[str] = []
    out_lines.mkdir(parents=True, exist_ok=True)
    n = 0
    for idx, row in enumerate(dataset):
        as_dict = dict(row)
        image_obj = as_dict.get("image")
        if image_obj is None:
            continue
        if hasattr(image_obj, "convert"):
            pil = image_obj.convert("RGB")
        else:
            continue

        candidates: list[tuple[str, str, float | None, list | None]] = []
        _add_field(
            candidates,
            as_dict.get("seller_text"),
            as_dict.get("seller_confidence"),
            as_dict.get("seller_bbox"),
        )
        _add_field(
            candidates,
            as_dict.get("date_text"),
            as_dict.get("date_confidence"),
            as_dict.get("date_bbox"),
        )
        _add_field(
            candidates,
            as_dict.get("total_text"),
            as_dict.get("total_confidence"),
            as_dict.get("total_bbox"),
        )
        texts = list(as_dict.get("item_texts") or [])
        confs = list(as_dict.get("item_confidences") or [])
        for i, text in enumerate(texts):
            conf = confs[i] if i < len(confs) else 0.85
            # item bbox в parquet часто нет — пропускаем кроп без bbox, только текст не рисуем
            _add_field(candidates, text, conf, None)

        for j, (text, _dup, conf, bbox) in enumerate(candidates):
            if bbox is None:
                continue
            crop = _crop_normalized(pil, bbox)
            if crop is None:
                continue
            # слишком узкие/высокие кропы отсекаем
            cw, ch = crop.size
            if cw < 8 or ch < 8 or cw / max(ch, 1) < 0.5:
                continue
            name = f"{prefix}_{split_name}_{idx:04d}_{j:02d}.png"
            crop.save(out_lines / name)
            gt_lines.append(f"lines/{name}\t{text}")
            n += 1
    print(f"  {split_name}: {n} line crops")
    return gt_lines


def merge_synth(synth_dir: Path, out_lines: Path) -> list[str]:
    """Копирует synth lines + rec_gt в общий out."""
    src_lines = synth_dir / "lines"
    src_gt = synth_dir / "rec_gt.txt"
    if not src_gt.exists():
        print(f"  synth: нет {src_gt}")
        return []
    out_lines.mkdir(parents=True, exist_ok=True)
    gt: list[str] = []
    for line in src_gt.read_text(encoding="utf-8").splitlines():
        if "\t" not in line:
            continue
        rel, text = line.split("\t", 1)
        src = synth_dir / rel
        if not src.exists():
            # иногда path без lines/
            src = src_lines / Path(rel).name
        if not src.exists():
            continue
        name = f"synth_{src.name}"
        shutil.copy2(src, out_lines / name)
        gt.append(f"lines/{name}\t{text}")
    print(f"  synth: {len(gt)} lines")
    return gt


def main() -> None:
    parser = argparse.ArgumentParser(description="Export CDEK (+synth) lines for OCR rec finetune")
    parser.add_argument("--out", type=Path, default=Path("data/rec_mix"))
    parser.add_argument("--no-hf", action="store_true", help="только synth, без HF")
    parser.add_argument("--synth", type=Path, help="каталог data/synth с lines/ + rec_gt.txt")
    parser.add_argument("--dataset", default="cdek-ocr/receipt-ocr-ru")
    parser.add_argument("--splits", nargs="+", default=["train", "val"])
    parser.add_argument("--prefix", default="cdek")
    args = parser.parse_args()

    out = args.out
    lines_dir = out / "lines"
    if lines_dir.exists():
        shutil.rmtree(lines_dir)
    lines_dir.mkdir(parents=True, exist_ok=True)

    all_gt: list[str] = []

    if not args.no_hf:
        try:
            from datasets import load_dataset
        except ImportError as exc:
            raise SystemExit("pip install -e '.[train]'") from exc
        print(f"Loading {args.dataset} …")
        ds = load_dataset(args.dataset)
        alias = {"val": "validation", "validation": "val"}
        for split in args.splits:
            actual = split if split in ds else alias.get(split)
            if actual not in ds:
                print(f"  skip missing split {split}")
                continue
            all_gt.extend(export_from_hf_split(ds[actual], split, lines_dir, args.prefix))

    if args.synth:
        all_gt.extend(merge_synth(args.synth, lines_dir))

    # перемешать стабильно
    rng = np.random.default_rng(42)
    order = rng.permutation(len(all_gt))
    shuffled = [all_gt[i] for i in order]

    (out / "rec_gt.txt").write_text("\n".join(shuffled) + ("\n" if shuffled else ""), encoding="utf-8")
    manifest = {
        "lines": len(shuffled),
        "out": str(out),
        "synth": str(args.synth) if args.synth else None,
        "dataset": None if args.no_hf else args.dataset,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {len(shuffled)} lines → {out / 'rec_gt.txt'}")
    print(f"Zip hint: cd {out} && zip -r ../cdek_synth.zip lines rec_gt.txt")


if __name__ == "__main__":
    main()
