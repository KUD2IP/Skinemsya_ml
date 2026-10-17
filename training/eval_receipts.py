"""Бенчмарк OCR → parser на размеченном наборе (CDEK / synth / fixtures).

Каталог: images/* + labels/*.json (поле image = имя файла, контракт Skinemsya).

  python -m training.eval_receipts --dataset data/cdek/test
  python -m training.eval_receipts --dataset data/cdek/test --limit 20 --verbose
  python -m training.eval_receipts --dataset data/cdek/test --report data/cdek/test_report.json
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import time
from pathlib import Path


def normalize_name(name: str) -> str:
    return " ".join(name.lower().replace("ё", "е").split())


def name_similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, normalize_name(a), normalize_name(b)).ratio()


def match_items(predicted: list[dict], expected: list[dict], threshold: float = 0.55):
    pairs = []
    used_pred: set[int] = set()
    used_exp: set[int] = set()
    candidates = [
        (name_similarity(p["name"], e["name"]), pi, ei)
        for pi, p in enumerate(predicted)
        for ei, e in enumerate(expected)
    ]
    for similarity, pi, ei in sorted(candidates, reverse=True):
        if similarity < threshold or pi in used_pred or ei in used_exp:
            continue
        used_pred.add(pi)
        used_exp.add(ei)
        pairs.append((predicted[pi], expected[ei], similarity))
    return pairs


def resolve_image(dataset: Path, label: dict, label_file: Path) -> Path:
    name = label.get("image") or label.get("image_file")
    if name:
        path = dataset / "images" / name
        if path.exists():
            return path
    # fallback: same stem as label
    for ext in (".jpg", ".jpeg", ".png", ".JPG", ".JPEG", ".PNG", ".webp"):
        candidate = dataset / "images" / f"{label_file.stem}{ext}"
        if candidate.exists():
            return candidate
    raise FileNotFoundError(f"Нет изображения для {label_file.name}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Eval OCR+parser на чеках")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--rec-model", type=Path)
    parser.add_argument("--rec-keys", type=Path)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--report", type=Path, help="сохранить JSON-отчёт")
    parser.add_argument("--name-threshold", type=float, default=0.55)
    args = parser.parse_args()

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

    ocr.get_engine()

    total_expected = total_predicted = total_matched = 0
    price_correct = qty_correct = 0
    price_abs_err = qty_abs_err = total_abs_err = 0.0
    price_err_n = qty_err_n = 0
    receipts_with_correct_total = receipts_with_total = 0
    receipts_with_errors = 0
    empty_pred = 0
    latencies: list[float] = []
    failures: list[dict] = []

    for label_file in label_files:
        expected = json.loads(label_file.read_text(encoding="utf-8"))
        try:
            image_path = resolve_image(args.dataset, expected, label_file)
        except FileNotFoundError as exc:
            print(f"WARN: {exc}")
            continue

        data = image_path.read_bytes()
        started = time.monotonic()
        try:
            image = preprocessing.preprocess(data)
            tokens = ocr.run_ocr(image)
            result = receipt_parser.parse_receipt(tokens).model_dump()
        except Exception as exc:  # noqa: BLE001
            latencies.append(time.monotonic() - started)
            failures.append({"file": label_file.name, "error": str(exc)})
            if args.verbose:
                print(f"{label_file.name}: CRASH {exc}")
            continue
        latency = time.monotonic() - started
        latencies.append(latency)

        if result.get("errors"):
            receipts_with_errors += 1
        if not result.get("items"):
            empty_pred += 1

        pairs = match_items(result["items"], expected.get("items") or [], args.name_threshold)
        total_expected += len(expected.get("items") or [])
        total_predicted += len(result["items"])
        total_matched += len(pairs)

        for predicted_item, expected_item, _ in pairs:
            pe = abs(float(predicted_item["total_price"]) - float(expected_item["total_price"]))
            qe = abs(float(predicted_item["quantity"]) - float(expected_item["quantity"]))
            price_abs_err += pe
            qty_abs_err += qe
            price_err_n += 1
            qty_err_n += 1
            if pe < 0.05:
                price_correct += 1
            if qe < 0.02:
                qty_correct += 1

        exp_total = expected.get("total")
        if exp_total is not None:
            receipts_with_total += 1
            pred_total = result.get("total")
            if pred_total is not None:
                terr = abs(float(pred_total) - float(exp_total))
                total_abs_err += terr
                if terr < 0.05:
                    receipts_with_correct_total += 1

        if args.verbose:
            missed = len(expected.get("items") or []) - len(pairs)
            extra = len(result["items"]) - len(pairs)
            if missed or extra or result.get("errors"):
                print(
                    f"{label_file.name}: miss={missed} extra={extra} "
                    f"errors={result.get('errors')} "
                    f"total_gt={exp_total} total_pred={result.get('total')} ({latency:.1f}s)"
                )

    recall = total_matched / total_expected if total_expected else 0.0
    precision = total_matched / total_predicted if total_predicted else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    n = len(latencies) or 1

    report = {
        "dataset": str(args.dataset),
        "receipts": len(label_files),
        "crashes": len(failures),
        "item_recall": round(recall, 4),
        "item_precision": round(precision, 4),
        "item_f1": round(f1, 4),
        "matched": total_matched,
        "expected_items": total_expected,
        "predicted_items": total_predicted,
        "price_accuracy": round(price_correct / total_matched, 4) if total_matched else None,
        "qty_accuracy": round(qty_correct / total_matched, 4) if total_matched else None,
        "price_mae": round(price_abs_err / price_err_n, 4) if price_err_n else None,
        "qty_mae": round(qty_abs_err / qty_err_n, 4) if qty_err_n else None,
        "total_accuracy": (
            round(receipts_with_correct_total / receipts_with_total, 4)
            if receipts_with_total
            else None
        ),
        "total_mae": (
            round(total_abs_err / receipts_with_total, 4) if receipts_with_total else None
        ),
        "receipts_with_errors_pct": round(receipts_with_errors / len(label_files), 4),
        "empty_predictions_pct": round(empty_pred / len(label_files), 4),
        "latency_avg_s": round(sum(latencies) / n, 3),
        "latency_min_s": round(min(latencies), 3) if latencies else None,
        "latency_max_s": round(max(latencies), 3) if latencies else None,
        "failures": failures,
    }

    print()
    print(f"Dataset:          {args.dataset}")
    print(f"Чеков:            {report['receipts']} (crashes={report['crashes']})")
    print(f"Item recall:      {report['item_recall']:.3f}  ({total_matched}/{total_expected})")
    print(f"Item precision:   {report['item_precision']:.3f}  ({total_matched}/{total_predicted})")
    print(f"Item F1:          {report['item_f1']:.3f}")
    if total_matched:
        print(f"Price accuracy:   {report['price_accuracy']:.3f}  (MAE {report['price_mae']})")
        print(f"Qty accuracy:     {report['qty_accuracy']:.3f}  (MAE {report['qty_mae']})")
    if receipts_with_total:
        print(
            f"Total accuracy:   {report['total_accuracy']:.3f}  "
            f"({receipts_with_correct_total}/{receipts_with_total}, MAE {report['total_mae']})"
        )
    print(f"Errors rate:      {report['receipts_with_errors_pct']:.3f}")
    print(f"Empty pred rate:  {report['empty_predictions_pct']:.3f}")
    print(
        f"Latency avg:      {report['latency_avg_s']:.2f}s "
        f"(min {report['latency_min_s']:.2f}s, max {report['latency_max_s']:.2f}s)"
    )

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Report:           {args.report}")


if __name__ == "__main__":
    main()
