# Skinemsya ML — распознавание чеков

Python-сервис для «Скинемся»: принимает фото чека, распознаёт позиции (название, количество, цена за единицу, сумма), итог, дату и название заведения, и возвращает JSON по контракту бека. Чаевые и сервисные сборы распознаются как обычные позиции — бек помечает их сам по названию (`чаевые` / `tips` / `service`).

Рассчитан на слабый сервер: CPU-only, ~150–300 МБ RAM, ответ за доли секунды — секунды (лимит бека 30 с).

## Как это работает

```
Java backend ── POST /api/v1/recognize (multipart image) ──► FastAPI
   ▲                                                            │
   │                                              препроцессинг (OpenCV)
   │                                                            │
   │                                              OCR: PP-OCRv5 mobile ONNX
   │                                              (RapidOCR, rec-модель eslav)
   │                                                            │
   └────────── JSON по контракту ◄──────────── парсер структуры чека
                                               (позиции, итог, confidence)
```

- Контракт ответа: `Skinemsya/docs/integrations/receipt-json-contract.md` (репозиторий бека) — поля `merchant`, `date`, `total`, `currency`, `items[]` (`name`, `quantity`, `unit_price`, `total_price`, `confidence`), `confidence`, `errors`.
- Ошибки обработки возвращаются валидным JSON с `errors` (`unreadable`, `no_items_found`, `low_confidence`, `total_mismatch`), а не 5xx.
- Если распознанный итог не сходится с суммой позиций более чем на 1%, печатный
  `total` сохраняется, а ответ получает ошибку `total_mismatch`: скидки и
  пропущенные OCR-строки не должны скрывать фактическую сумму к оплате.

## Быстрый старт (локально)

```bash
python3.11 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/uvicorn app.main:app --port 8000
```

При первом запуске модели PP-OCRv5 (~40 МБ) скачаются автоматически.

Проверка:

```bash
curl -F "image=@tests/fixtures/simple_precheck.png" http://localhost:8000/api/v1/recognize
```

Тесты (юнит + контрактные):

```bash
.venv/bin/pytest
```

## Docker

```bash
docker compose up -d --build   # сервис на http://localhost:8100
```

Модели скачиваются на этапе сборки — в рантайме контейнеру сеть не нужна. Лимит памяти в compose — 768 МБ (реально используется ~150–300 МБ).

### Подключение к беку

У бека задаётся `ML_SERVICE_URL`:

- бек в Docker, ML в Docker на том же хосте: `ML_SERVICE_URL=http://host.docker.internal:8100` (или общая docker-сеть и `http://ml:8000`, см. комментарии в `docker-compose.yml`);
- бек локально через `mvn spring-boot:run`: `ML_SERVICE_URL=http://localhost:8100`.

Дальше обычный флоу: фронт загружает фото (`POST /api/v1/files?purpose=receipt`), бек шлёт его сюда и создаёт позиции.

## CI/CD

Тот же Git Flow, что у бека и фронта: PR → `develop` → `release/X.Y.Z` → `master` (автодеплой).

| Событие | Что происходит |
| --- | --- |
| PR | pytest (парсер + API/OCR), dependency-review |
| `release/X.Y.Z` | тесты + push образа в GHCR (`*-rc`) + draft GitHub Release |
| push в `master` | тесты + push образа + deploy на VPS + GitHub Release |

Образ: `ghcr.io/<owner>/skinemsya-ml`. Деплой: `/opt/skinemsya/deploy/ml/`, health на `127.0.0.1:8001/healthz`.

Подробности и список секретов: [`docs/deployment/ci-cd.md`](docs/deployment/ci-cd.md). Шаблон env: [`deploy/env.production.example`](deploy/env.production.example).

После деплоя в `PRODUCTION_ENV` бека должно быть:

```bash
ML_SERVICE_URL=http://skinemsya-ml:8000
```

## Дообучение модели под русские чеки

Rec-модель дообучается на **синтетике + реальных строках** из [cdek-ocr/receipt-ocr-ru](https://huggingface.co/datasets/cdek-ocr/receipt-ocr-ru). Полный гайд: [`docs/training/cdek-receipt-ocr-ru.md`](docs/training/cdek-receipt-ocr-ru.md).

```bash
pip install -e '.[train]'

# CDEK → бенчмарк
python -m training.cdek.download_and_prepare --out data/cdek
python -m training.eval_receipts --dataset data/cdek/test

# строки для Colab (CDEK crops + synth)
python -m training.synthgen --count 2000 --out data/synth
python -m training.cdek.export_rec_lines --synth data/synth --out data/rec_mix
cd data/rec_mix && zip -r ../cdek_synth.zip lines rec_gt.txt && cd ../..

# Colab: training/colab/finetune_ppocrv5_rec.ipynb → receipt_rec.onnx
ML_REC_MODEL_PATH=data/models/receipt_rec.onnx \
ML_REC_KEYS_PATH=data/models/receipt_rec_keys.txt \
uvicorn app.main:app --port 8000
```

### Базовые метрики (синтетический eval, 40 чеков, модель без дообучения)

| Метрика | Значение |
| --- | --- |
| Item F1 | 0.971 |
| Price accuracy | 0.979 |
| Qty accuracy | 0.915 |
| Total accuracy | 0.700 |
| Латентность (M-серия Mac, CPU) | ~0.4 с/чек |

Оставшиеся ошибки — в основном пропуски OCR на сильно зашумлённых чеках; именно это должно улучшить дообучение.

## Переменные окружения

| Переменная | Назначение | По умолчанию |
| --- | --- | --- |
| `ML_REC_MODEL_PATH` | своя rec-модель (.onnx) | встроенная eslav PP-OCRv5 |
| `ML_REC_KEYS_PATH` | словарь символов к ней (.txt) | встроенный |
| `ML_OCR_THREADS` | потоки onnxruntime | 2 (в Docker — 1) |
| `ML_MODEL_ROOT_DIR` | каталог кэша моделей | site-packages rapidocr |

## Структура репозитория

```
app/                  # сервис: main (FastAPI), schemas (контракт), preprocessing, ocr, parser
training/synthgen/    # генератор синтетических пречеков (+ python -m training.synthgen)
training/colab/       # ноутбук дообучения PP-OCRv5 rec в Colab
scripts/evaluate.py   # метрики качества на размеченном наборе
scripts/download_models.py  # прогрев моделей (используется в Docker build)
tests/                # юнит-тесты парсера + контрактные тесты API
docs/colab-guide.md   # Colab с нуля: пошагово
docs/deployment/      # CI/CD и деплой на VPS
.github/              # GitHub Actions (как у бека/фронта)
deploy/               # docker-compose.prod.yml для VPS
```

## Путь апгрейда: VLM после переезда на 4 ГБ RAM

Текущий пайплайн (OCR + правила) — осознанный выбор под 1 ГБ RAM. Когда сервер вырастет до ~4 ГБ, качество на «сложных» чеках (мятых, рукописных дописках, нестандартных макетах) можно поднять, заменив парсер на компактную VLM, которая сразу выдаёт JSON:

1. **PaddleOCR-VL (0.9B)** — специализированная документная VLM, state-of-the-art на документных бенчмарках при минимальном размере; квантованная работает на CPU в ~2–3 ГБ RAM. Первый кандидат.
2. **Qwen3-VL 2B (LoRA)** — дообучается на парах «фото чека → JSON» (синтетика из `training/synthgen` уже отдаёт такие пары: `images/` + `labels/`); квантование в GGUF Q4 и запуск через llama.cpp. Требует замера латентности на целевом CPU — на 2 vCPU может не влезть в 30-секундный таймаут.

Что уже готово к этому переходу:

- контракт и HTTP-слой не меняются — заменяется только внутренность `recognize`;
- генератор синтетики уже выдаёт разметку в формате контракта (годится для SFT «изображение → JSON»);
- `scripts/evaluate.py` сравнит старый и новый пайплайн на одном наборе.

Решение о переходе принимать по метрикам: если после дообучения rec-модели качество на реальных чеках устраивает — VLM можно не спешить внедрять.
