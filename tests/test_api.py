"""Контрактные тесты HTTP API (end-to-end с реальным OCR).

Первый запуск скачивает модели PP-OCRv5 (~30 МБ), дальше — кэш.
"""

import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.main import app

CONTRACT_TOP_LEVEL_KEYS = {"merchant", "date", "total", "currency", "items", "confidence", "errors"}
CONTRACT_ITEM_KEYS = {"name", "quantity", "unit_price", "total_price", "confidence"}


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_healthz(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz(client):
    # После lifespan OCR должен быть готов (или ещё loading на очень медленной машине).
    response = client.get("/readyz")
    assert response.status_code in (200, 503)
    assert response.json()["status"] in ("ok", "loading", "error")


def test_recognize_precheck_fixture(client):
    with open("tests/fixtures/simple_precheck.png", "rb") as f:
        response = client.post(
            "/api/v1/recognize", files={"image": ("receipt.jpg", f, "image/png")}
        )
    assert response.status_code == 200
    body = response.json()

    # Форма ответа строго по контракту (snake_case, все ключи на месте).
    assert set(body.keys()) == CONTRACT_TOP_LEVEL_KEYS
    for item in body["items"]:
        assert set(item.keys()) == CONTRACT_ITEM_KEYS

    assert body["errors"] == []
    assert body["total"] == 2255.0
    assert body["currency"] == "RUB"
    assert body["confidence"] >= 0.7
    names = [item["name"] for item in body["items"]]
    assert len(names) == 5
    assert any("борщ" in n.lower() for n in names)
    # Сервисный сбор должен попасть в позиции — бек сам пометит его как чаевые.
    assert any("сервисный" in n.lower() for n in names)


def test_recognize_garbage_bytes_returns_unreadable(client):
    response = client.post(
        "/api/v1/recognize", files={"image": ("receipt.jpg", io.BytesIO(b"not an image"), "image/jpeg")}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["confidence"] == 0.0
    assert "unreadable" in body["errors"]


def test_recognize_blank_image_no_items(client):
    buffer = io.BytesIO()
    Image.new("RGB", (600, 800), (255, 255, 255)).save(buffer, format="JPEG")
    buffer.seek(0)
    response = client.post(
        "/api/v1/recognize", files={"image": ("receipt.jpg", buffer, "image/jpeg")}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["errors"], "пустой чек должен вернуть непустой errors"
