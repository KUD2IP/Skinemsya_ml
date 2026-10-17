"""Оценка качества распознавания на размеченном наборе чеков.

Набор — каталог с images/*.jpg и labels/*.json (формат контракта бека,
такой создаёт training/synthgen; для реальных фото разметку в том же
формате можно написать руками).

Метрики:
  item recall/precision/F1 — сопоставление позиций по похожести названия;
  price accuracy           — доля сопоставленных позиций с верной суммой;
  qty accuracy             — доля сопоставленных позиций с верным количеством;
  total accuracy           — доля чеков с верным итогом;
  latency                  — среднее время обработки одного чека.

Сравнение базовой и дообученной модели:
  python scripts/evaluate.py --dataset data/synth_eval
  python scripts/evaluate.py --dataset data/synth_eval \
      --rec-model data/models/receipt_rec.onnx --rec-keys data/models/receipt_rec_keys.txt
"""

import argparse
import difflib
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def normalize_name(name: str) -> str:
    return " ".join(name.lower().replace("ё", "е").split())


def name_similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, normalize_name(a), normalize_name(b)).ratio()


def match_items(predicted: list[dict], expected: list[dict], threshold: float = 0.6):
    """Жадное сопоставление позиций по похожести названий."""
    pairs = []
    used_pred: set[int] = set()
    candidates = [
        (name_similarity(p["name"], e["name"]), pi, ei)
        for pi, p in enumerate(predicted)
        for ei, e in enumerate(expected)
    ]
    used_exp: set[int] = set()
    for similarity, pi, ei in sorted(candidates, reverse=True):
        if similarity < threshold or pi in used_pred or ei in used_exp:
            continue
        used_pred.add(pi)
        used_exp.add(ei)
        pairs.append((predicted[pi], expected[ei], similarity))
    return pairs


def main() -> None:
    parser = argparse.ArgumentParser(description="Оценка качества распознавания чеков")
    parser.add_argument("--dataset", type=Path, required=True, help="каталог с images/ и labels/")
    parser.add_argument("--rec-model", type=Path, help="дообученная rec-модель (.onnx)")
    parser.add_argument("--rec-keys", type=Path, help="словарь символов для неё (.txt)")
    parser.add_argument("--limit", type=int, help="ограничить число чеков")
    parser.add_argument("--verbose", action="store_true", help="печатать расхождения по каждому чеку")
    args = parser.parse_args()

    # Модель подключается через env до импорта app.ocr.
    if args.rec_model:
        os.environ["ML_REC_MODEL_PATH"] = str(args.rec_model.resolve())
    if args.rec_keys:
        os.environ["ML_REC_KEYS_PATH"] = str(args.rec_keys.resolve())

    from app import ocr, parser as receipt_parser, preprocessing

    label_files = sorted((args.dataset / "labels").glob("*.json"))
    if args.limit:
        label_files = label_files[: args.limit]
    if not label_files:
        raise SystemExit(f"Нет labels/*.json в {args.dataset}")

    ocr.get_engine()  # прогрев вне замера латентности

    total_expected = total_predicted = total_matched = 0
    price_correct = qty_correct = 0
    receipts_with_correct_total = receipts_with_total = 0
    latencies = []

    for label_file in label_files:
        expected = json.loads(label_file.read_text(encoding="utf-8"))
        image_name = expected.get("image") or expected.get("image_file") or f"{label_file.stem}.jpg"
        image_path = args.dataset / "images" / image_name
        if not image_path.exists():
            print(f"WARN: нет файла {image_path}")
            continue
        data = image_path.read_bytes()

        started = time.monotonic()
        image = preprocessing.preprocess(data)
        tokens = ocr.run_ocr(image)
        result = receipt_parser.parse_receipt(tokens).model_dump()
        latencies.append(time.monotonic() - started)

        pairs = match_items(result["items"], expected["items"])
        total_expected += len(expected["items"])
        total_predicted += len(result["items"])
        total_matched += len(pairs)

        for predicted_item, expected_item, _ in pairs:
            if abs(predicted_item["total_price"] - expected_item["total_price"]) < 0.01:
                price_correct += 1
            if abs(predicted_item["quantity"] - expected_item["quantity"]) < 0.001:
                qty_correct += 1

        if expected.get("total") is not None:
            receipts_with_total += 1
            if result["total"] is not None and abs(result["total"] - expected["total"]) < 0.01:
                receipts_with_correct_total += 1

        if args.verbose:
            missed = len(expected["items"]) - len(pairs)
            extra = len(result["items"]) - len(pairs)
            if missed or extra:
                print(f"{label_file.name}: пропущено {missed}, лишних {extra}")

    recall = total_matched / total_expected if total_expected else 0
    precision = total_matched / total_predicted if total_predicted else 0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0

    print()
    print(f"Чеков:            {len(label_files)}")
    print(f"Item recall:      {recall:.3f}  ({total_matched}/{total_expected})")
    print(f"Item precision:   {precision:.3f}  ({total_matched}/{total_predicted})")
    print(f"Item F1:          {f1:.3f}")
    if total_matched:
        print(f"Price accuracy:   {price_correct / total_matched:.3f}")
        print(f"Qty accuracy:     {qty_correct / total_matched:.3f}")
    if receipts_with_total:
        print(f"Total accuracy:   {receipts_with_correct_total / receipts_with_total:.3f}"
              f"  ({receipts_with_correct_total}/{receipts_with_total})")
    print(f"Latency avg:      {sum(latencies) / len(latencies):.2f}s"
          f"  (min {min(latencies):.2f}s, max {max(latencies):.2f}s)")


if __name__ == "__main__":
    main()
