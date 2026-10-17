"""Юнит-тесты парсера структуры чека (без OCR-моделей)."""

from app.parser import _extract_date, _parse_number, parse_receipt
from tests.conftest import make_tokens


class TestNumbers:
    def test_decimal_comma(self):
        assert _parse_number("450,50") == 450.5

    def test_thousands_space(self):
        assert _parse_number("1 250.00") == 1250.0

    def test_ocr_confusion(self):
        assert _parse_number("45О") == 450  # кириллическая О вместо нуля

    def test_garbage(self):
        assert _parse_number("abc") is None


class TestDate:
    def test_dotted(self):
        assert _extract_date("15.07.2026 19:42") == "2026-07-15"

    def test_two_digit_year(self):
        assert _extract_date("01.12.25") == "2025-12-01"

    def test_iso(self):
        assert _extract_date("2026-07-15") == "2026-07-15"

    def test_invalid_month(self):
        assert _extract_date("15.13.2026") is None


class TestReceiptParsing:
    def test_full_precheck(self):
        response = parse_receipt(
            make_tokens(
                [
                    "Кафе Пушкинъ",
                    "Официант: Мария",
                    "15.07.2026 19:42",
                    "Борщ с говядиной 2 x 450.00 900.00",
                    "Хлебная корзина 180.00",
                    "Чаевые 200.00",
                    "ИТОГО: 1280.00",
                ]
            )
        )
        assert response.merchant == "Кафе Пушкинъ"
        assert response.date == "2026-07-15"
        assert response.total == 1280.0
        assert response.currency == "RUB"
        assert response.errors == []
        assert len(response.items) == 3

        borsch = response.items[0]
        assert borsch.name == "Борщ с говядиной"
        assert borsch.quantity == 2.0
        assert borsch.unit_price == 450.0
        assert borsch.total_price == 900.0

        # Чаевые остаются обычной позицией — бек пометит их по имени.
        tips = response.items[2]
        assert tips.name == "Чаевые"
        assert tips.total_price == 200.0

    def test_quantity_on_next_line(self):
        response = parse_receipt(
            make_tokens(
                [
                    "Стейк из лосося",
                    "1 х 890.00 = 890.00",
                    "Итого 890.00",
                ]
            )
        )
        assert len(response.items) == 1
        item = response.items[0]
        assert item.name == "Стейк из лосося"
        assert item.quantity == 1.0
        assert item.unit_price == 890.0
        assert item.total_price == 890.0

    def test_columns_qty_unit_total(self):
        response = parse_receipt(make_tokens(["Капучино 2 250.00 500.00"]))
        assert len(response.items) == 1
        item = response.items[0]
        assert item.quantity == 2.0
        assert item.unit_price == 250.0
        assert item.total_price == 500.0

    def test_service_lines_excluded(self):
        response = parse_receipt(
            make_tokens(
                [
                    "Кассир: Иванова 1",
                    "Стол 12",
                    "Оплата картой 500.00",
                    "НДС 20% 83.33",
                    "Сдача 0.00",
                    "Латте 500.00",
                ]
            )
        )
        assert len(response.items) == 1
        assert response.items[0].name == "Латте"

    def test_total_mismatch_keeps_printed_total(self):
        response = parse_receipt(
            make_tokens(
                [
                    "Суп дня 300.00",
                    "ИТОГО: 999.00",
                ]
            )
        )
        # Итог не бьётся с позициями (скидки и т.п.): оставляем печатный ИТОГО,
        # помечаем mismatch — бек видит сумму к оплате и флаг.
        assert response.total == 999.0
        assert "total_mismatch" in response.errors
        assert len(response.items) == 1

    def test_total_within_tolerance_keeps_total(self):
        response = parse_receipt(
            make_tokens(
                [
                    "Суп дня 300.00",
                    "Салат 200.00",
                    "ИТОГО: 500.00",
                ]
            )
        )
        assert response.total == 500.0
        assert response.errors == []

    def test_no_items(self):
        response = parse_receipt(make_tokens(["Спасибо за покупку!", "До свидания"]))
        assert response.items == []
        assert response.confidence == 0.0
        assert "no_items_found" in response.errors

    def test_empty_tokens_unreadable(self):
        response = parse_receipt([])
        assert response.items == []
        assert response.confidence == 0.0
        assert "unreadable" in response.errors

    def test_low_confidence_flag(self):
        response = parse_receipt(make_tokens(["Кофе 150.00"], score=0.4))
        assert len(response.items) == 1
        assert "low_confidence" in response.errors
        assert response.confidence is not None and response.confidence < 0.5

    def test_k_oplate_preferred_over_vsego(self):
        response = parse_receipt(
            make_tokens(
                [
                    "Пицца 1000.00",
                    "Всего позиций: 1",
                    "К оплате: 1000.00",
                ]
            )
        )
        assert response.total == 1000.0
        assert response.errors == []

    def test_qty_sum_columns_no_thousands_glue(self):
        """«2 750,00» в колонках Кол-во|Сумма — это qty=2 и 750, не 2750."""
        response = parse_receipt(
            make_tokens(
                [
                    "Наименование Кол-во Сумма",
                    "Сельдь на Тостах 2 750,00",
                    "Картофель фри 1 295,00",
                    "ИТОГО К ОПЛАТЕ: 1045,00",
                ]
            )
        )
        assert len(response.items) == 2
        assert response.items[0].name == "Сельдь на Тостах"
        assert response.items[0].quantity == 2.0
        assert response.items[0].total_price == 750.0
        assert response.items[1].quantity == 1.0
        assert response.items[1].total_price == 295.0
        assert response.total == 1045.0

    def test_discount_lines_excluded(self):
        response = parse_receipt(
            make_tokens(
                [
                    "Наименование Кол-во Сумма",
                    "Сельдь на Тостах 2 750,00",
                    "Скидка Студентам 25% -188,00",
                    "ИТОГО К ОПЛАТЕ: 562,00",
                ]
            )
        )
        assert len(response.items) == 1
        assert response.items[0].total_price == 750.0
        assert response.total == 562.0  # печатный итог после скидки
        assert "total_mismatch" in response.errors

    def test_qty_not_glued_to_price(self):
        """Регресс: «4 780,00» в колонках — qty=4 и 780, НЕ 4780."""
        response = parse_receipt(
            make_tokens(
                [
                    "Наименование Кол-во Сумма",
                    "Пиво Олд Бобби 4 780,00",
                    "Картофель фри 1 295,00",
                    "Ребра Свиные 2 1358,00",
                    "Полная сумма: 2433,00",
                ]
            )
        )
        assert [(i.quantity, i.total_price) for i in response.items] == [
            (4.0, 780.0),
            (1.0, 295.0),
            (2.0, 1358.0),
        ]
        assert response.total == 2433.0
        assert response.errors == []

    def test_weight_fractional_qty(self):
        """Ритейл CDEK: весовой товар 0.448 кг × цена = сумма."""
        response = parse_receipt(
            make_tokens(
                [
                    "Наименование Кол-во Сумма",
                    "БАНАНЫ 1КГ 0.448 129.88",
                    "ИТОГО: 129.88",
                ]
            )
        )
        assert len(response.items) == 1
        assert abs(response.items[0].quantity - 0.448) < 0.001
        assert abs(response.items[0].total_price - 129.88) < 0.01

    def test_retail_unit_before_quantity(self):
        response = parse_receipt(
            make_tokens(
                [
                    "ПИВО КРОМБАХЕР ПИЛС 0,5 ЖБ 179.99 *1 =179.99",
                    "ПИВО ШПАТЕН МЮНХЕН 134.99 2 =269.98",
                    "ИТОГ =449.97",
                ]
            )
        )
        assert [(item.quantity, item.unit_price, item.total_price) for item in response.items] == [
            (1.0, 179.99, 179.99),
            (2.0, 134.99, 269.98),
        ]
        assert response.total == 449.97
        assert response.errors == []

    def test_retail_name_then_tax_detail(self):
        response = parse_receipt(
            make_tokens(
                [
                    "МАГНОЛИЯ",
                    "СИДР ДАБЛ ТРИ ЯБЛОКО П/СУХ. 0.45",
                    "22.0 НДС2 119.99 *1 =119.99",
                    "ЧИПСЫ ЛЕЙЗ ПАПРИКА 225Г",
                    "22.0 НДС2 269.99 *1 =269.99",
                    "ИТОГ =389.98",
                ]
            )
        )
        assert len(response.items) == 2
        assert response.items[0].name.endswith("0.45")
        assert response.items[0].quantity == 1.0
        assert response.items[0].total_price == 119.99
        assert response.items[1].total_price == 269.99
        assert response.total == 389.98

    def test_discount_on_same_line_is_not_total(self):
        response = parse_receipt(
            make_tokens(
                [
                    "Гренки 99.99 *1 =99.99",
                    "Пиво 179.99 *1 =179.99",
                    "ИТОГО БЕЗ СКИДКИ =300.00 СКИДКА =20.02",
                    "НТОГ =279.98",
                    "БЕЗНАЛИЧНЫМИ =279.98",
                ]
            )
        )
        assert response.total == 279.98
        assert response.errors == []

    def test_vat_amount_is_not_total(self):
        response = parse_receipt(
            make_tokens(
                [
                    "Товар 500.00",
                    "СУММА(РУБ): 500.00",
                    "СУММА НДС 22% =90.16",
                ]
            )
        )
        assert response.total == 500.0

    def test_quantity_detail_on_following_line(self):
        response = parse_receipt(
            make_tokens(
                [
                    "ПИЛСНЕР 400 МЛ РОЗЛИВ =3440.00",
                    "3.2 ШТ. × 1075.00",
                    "БАРСУК МЕЛОМЕЛЬ ЕЖЕВИКА 0.5",
                    "1 ШТ. × 660.00",
                    "ИТОГ =4100.00",
                ]
            )
        )
        assert [(item.quantity, item.unit_price, item.total_price) for item in response.items] == [
            (3.2, 1075.0, 3440.0),
            (1.0, 660.0, 660.0),
        ]
        assert response.total == 4100.0
        assert response.errors == []


class TestBackendValidationRules:
    """Правила из receipt-json-contract.md: что бек требует от каждого item."""

    def test_items_satisfy_backend_rules(self):
        response = parse_receipt(
            make_tokens(
                [
                    "Ресторан Тест",
                    "Паста карбонара 2 x 390.00 780.00",
                    "Тирамису 320.00",
                    "Итого 1100.00",
                ]
            )
        )
        for item in response.items:
            assert item.name.strip(), "name обязателен и непустой"
            assert len(item.name) <= 255
            assert item.quantity > 0
            assert item.total_price >= 0
            if item.confidence is not None:
                assert 0 <= item.confidence <= 1

        if response.total is not None:
            items_sum = sum(i.total_price for i in response.items)
            assert abs(items_sum - response.total) <= 0.01 * response.total
