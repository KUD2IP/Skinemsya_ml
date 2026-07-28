"""Pydantic-модели, зеркалящие контракт бека.

Источник: Skinemsya/docs/integrations/receipt-json-contract.md и
Java-записи MlReceiptResponse / MlReceiptItem (snake_case через @JsonProperty).
"""

from pydantic import BaseModel, Field


class ReceiptItem(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    quantity: float = Field(..., gt=0)
    unit_price: float | None = None
    total_price: float = Field(..., ge=0)
    confidence: float | None = Field(None, ge=0, le=1)


class ReceiptResponse(BaseModel):
    merchant: str | None = None
    date: str | None = None
    total: float | None = None
    currency: str | None = "RUB"
    items: list[ReceiptItem] = Field(default_factory=list)
    confidence: float | None = Field(None, ge=0, le=1)
    errors: list[str] = Field(default_factory=list)


def failed_response(*errors: str) -> ReceiptResponse:
    return ReceiptResponse(
        merchant=None,
        date=None,
        total=None,
        currency="RUB",
        items=[],
        confidence=0.0,
        errors=list(errors),
    )
