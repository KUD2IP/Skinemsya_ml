# ML-сервис распознавания чеков. Рассчитан на CPU-сервер с 1 ГБ RAM.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    # Модели храним в отдельном каталоге, а не внутри site-packages,
    # чтобы дообученную модель можно было подменить volume-ом.
    ML_MODEL_ROOT_DIR=/models \
    ML_OCR_THREADS=1

# libglib нужен opencv-python-headless
RUN apt-get update \
    && apt-get install -y --no-install-recommends libglib2.0-0 curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY pyproject.toml ./
COPY app ./app
# rapidocr тянет полный opencv-python (GUI-зависимости); в контейнере
# он не нужен — заменяем на headless.
RUN pip install . \
    && pip uninstall -y opencv-python opencv-contrib-python \
    && pip install --force-reinstall "opencv-python-headless>=4.10"

# Скачиваем и прогреваем модели PP-OCRv5 на этапе сборки:
# контейнеру в рантайме сеть для моделей не нужна.
COPY scripts/download_models.py ./scripts/download_models.py
RUN python scripts/download_models.py

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -fs http://localhost:8000/healthz || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
