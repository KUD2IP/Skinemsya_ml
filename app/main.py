"""FastAPI-сервис распознавания чеков.

Контракт: бек шлёт POST /api/v1/recognize (multipart, поле `image`)
и ждёт JSON по Skinemsya/docs/integrations/receipt-json-contract.md.
Любая ошибка обработки -> валидный JSON c errors, а не 5xx:
5xx заставит бек делать retry, который упадёт так же.
"""

import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, UploadFile
from fastapi.responses import JSONResponse

from app import ocr, parser, preprocessing
from app.schemas import ReceiptResponse, failed_response

logger = logging.getLogger("skinemsya_ml")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

MAX_IMAGE_BYTES = 10 * 1024 * 1024  # лимит из контракта бека


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Прогреваем модель на старте, чтобы первый запрос не съел таймаут бека.
    started = time.monotonic()
    ocr.get_engine()
    logger.info("OCR engine ready in %.1fs", time.monotonic() - started)
    yield


app = FastAPI(title="Skinemsya ML", version="0.1.0", lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@app.post("/api/v1/recognize", response_model=ReceiptResponse)
async def recognize(image: UploadFile = File(...)) -> ReceiptResponse:
    started = time.monotonic()
    data = await image.read()

    if not data:
        return failed_response("unreadable")
    if len(data) > MAX_IMAGE_BYTES:
        return failed_response("image_too_large")

    try:
        prepared = preprocessing.preprocess(data)
    except preprocessing.ImageDecodeError:
        logger.warning("Failed to decode image (%d bytes)", len(data))
        return failed_response("unreadable")

    try:
        tokens = ocr.run_ocr(prepared)
        response = parser.parse_receipt(tokens)
    except Exception:
        logger.exception("Recognition pipeline failed")
        return failed_response("unreadable")

    logger.info(
        "Recognized receipt: items=%d total=%s confidence=%s errors=%s in %.2fs",
        len(response.items),
        response.total,
        response.confidence,
        response.errors,
        time.monotonic() - started,
    )
    return response


@app.exception_handler(Exception)
async def unhandled_exception_handler(_, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled error", exc_info=exc)
    return JSONResponse(status_code=200, content=failed_response("unreadable").model_dump())
