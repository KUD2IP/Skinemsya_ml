# Датасет CDEK receipt-ocr-ru

Источник: [cdek-ocr/receipt-ocr-ru](https://huggingface.co/datasets/cdek-ocr/receipt-ocr-ru) (~1000 русских чеков, MIT).

Используем как:
1. **бенчмарк** текущего пайплайна (`preprocess → OCR → parser`);
2. **источник строк** для дообучения PP-OCRv5 rec (вместе с synthgen).

## Ограничения

- Текст и bbox часто из OCR, **без ручной вычитки** (встречается «не распознано», обрезанные названия).
- Много ритейла/столовых; ресторанные пречеки лучше добивать синтетикой `training.synthgen`.
- Donut-чекпоинт с HF **не используем** — слишком тяжёлый для VPS 1 ГБ. Остаёмся на RapidOCR + rule parser.

## Установка extras

```bash
pip install -e '.[train]'
```

## 1. Скачать и нормализовать

```bash
python -m training.cdek.download_and_prepare --out data/cdek
# или только тест: --splits test
```

Структура:

```
data/cdek/{train,val,test}/
  images/*.jpg|png
  labels/*.json    # контракт Skinemsya (merchant/date/total/items)
data/cdek/manifest.json
```

Каталог `data/` в git не коммитится.

## 2. Бенчмарк текущего сервиса

```bash
python -m training.eval_receipts --dataset data/cdek/test --verbose \
  --report data/cdek/test_report.json
```

Метрики: item recall/precision/F1, price/qty accuracy + MAE, total accuracy/MAE, доля `errors`, latency.

С дообученной моделью:

```bash
python -m training.eval_receipts --dataset data/cdek/test \
  --rec-model data/models/receipt_rec.onnx \
  --rec-keys data/models/receipt_rec_keys.txt
```

Зафиксированный результат на test split (148 чеков) после правок парсера:

- item F1: `0.4751` (baseline до правок: `0.4458`);
- price accuracy: `0.7048` (было `0.5694`);
- quantity accuracy: `0.6286` (было `0.2176`);
- total accuracy: `0.5878` (было `0.5405`);
- crashes: `0`.

Полученная в первой итерации fine-tuned rec-модель в production не включается:
она снизила item F1 до `0.398` и total accuracy до `0.169`. Перед следующей
итерацией нужно сбалансировать реальные CDEK-кропы и синтетику, а валидацию
разделить по исходным чекам, чтобы строки одного чека не попадали одновременно
в train и val.

## 3. Строки для fine-tune rec

```bash
python -m training.synthgen --count 2000 --out data/synth
python -m training.cdek.export_rec_lines --synth data/synth --out data/rec_mix
cd data/rec_mix && zip -r ../cdek_synth.zip lines rec_gt.txt && cd ../..
```

Экспорт кропает по bbox только поля с достаточной уверенностью (seller/date/total и т.п.), отбрасывает «не распознано» и слишком длинные строки.

## 4. Colab → ONNX

Откройте [`training/colab/finetune_ppocrv5_rec.ipynb`](../../training/colab/finetune_ppocrv5_rec.ipynb), загрузите `cdek_synth.zip`, обучите, скачайте `receipt_rec.onnx` + `receipt_rec_keys.txt`.

Подключение:

```bash
ML_REC_MODEL_PATH=data/models/receipt_rec.onnx \
ML_REC_KEYS_PATH=data/models/receipt_rec_keys.txt \
uvicorn app.main:app --port 8000
```

В Docker — переменные + volume в `docker-compose.yml` / `deploy/docker-compose.prod.yml`.

## Связанные правки парсера

Под ритейл CDEK в `app/parser.py` учтены дробные количества (вес `0.448` кг) и до 3 знаков после запятой в простых числах колонок — без склейки `0.44`+`8`.
