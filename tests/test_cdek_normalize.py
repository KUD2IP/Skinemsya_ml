"""Юнит-тесты нормализации CDEK → контракт."""

from training.cdek.normalize import normalize_date, parse_money, parse_qty, record_to_label


def test_normalize_date_two_digit():
    assert normalize_date("10.05.24") == "2024-05-10"
    assert normalize_date("20.10.25 13:28") == "2025-10-20"


def test_parse_money_and_qty():
    assert parse_money("1 299,90") == 1299.9
    assert parse_qty("0.448") == 0.448
    assert parse_qty("не распознано") is None


def test_record_to_label_items():
    label = record_to_label(
        {
            "image_id": "x",
            "seller_text": 'ООО "Лента"',
            "date_text": "21.04.25",
            "total_text": "100.00",
            "item_texts": ["Хлеб", "не распознано", "Молоко"],
            "item_quantities": ["1", "1", "2"],
            "item_prices": ["50.00", "10", "25.00"],
            "item_sums": ["50.00", "10", "50.00"],
            "item_confidences": [0.9, 0.9, 0.9],
        }
    )
    assert label is not None
    assert label["merchant"] == 'ООО "Лента"'
    assert label["date"] == "2025-04-21"
    assert label["total"] == 100.0
    assert [i["name"] for i in label["items"]] == ["Хлеб", "Молоко"]
    assert label["items"][1]["quantity"] == 2.0
