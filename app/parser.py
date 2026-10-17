"""Парсер структуры чека из результатов OCR.

Логика заточена под ресторанные пречеки/счета на русском. Поддерживаются
два вида раскладки:

1. «Однострочная»: название и цены в одной строке OCR:
       Название блюда           2 x 450.00   900.00
2. «Колоночная» (типичный пречек): название слева, числа в отдельных
   колонках («Кол-во | Сумма» или «Кол-во | Цена | Сумма»). OCR отдаёт
   название и числа отдельными боксами, причём из-за изгиба бумаги их
   вертикальные координаты систематически расходятся. Числовые группы
   сопоставляются со строками названий монотонным выравниванием
   с поправкой на общий сдвиг.

Чаевые/сервисный сбор оставляем обычной позицией — бек сам помечает их
по названию (`чаевые` / `tips` / `service`).
"""

import re
from dataclasses import dataclass

from app.ocr import OcrToken
from app.schemas import ReceiptItem, ReceiptResponse

# --- Ключевые слова -----------------------------------------------------------

# Строка с итоговой суммой чека. Порядок = приоритет.
TOTAL_PATTERNS = [
    re.compile(r"\bк\s*оплате\b", re.IGNORECASE),
    # OCR нередко читает первую «И» как «Н»: «НТОГ».
    re.compile(r"\b[ин]того?\b", re.IGNORECASE),
    re.compile(r"\bс[уы]м\w*\s+бе[з3]\s+н[дoо]с\b", re.IGNORECASE),
    re.compile(r"\bсумм\w*\s*\(?\s*руб\b", re.IGNORECASE),
    re.compile(r"\bподытог\b", re.IGNORECASE),
    re.compile(r"\btotal\b", re.IGNORECASE),
    re.compile(r"\bвсего\b", re.IGNORECASE),
    re.compile(r"\bполн(ая|ой)\s+сумм", re.IGNORECASE),
    re.compile(r"\bсумма\b", re.IGNORECASE),
]

# Служебные строки, которые нельзя принимать за позиции (целиком).
EXCLUDE_PATTERN = re.compile(
    r"(кассир|официант|менеджер|бармен|стол\b|гост(ей|я|и)\b|гостев|зал\b|заказ\s*№?|смена|"
    r"сч[её]т\s*№?|квитанци|инн|кпп|огрн|ккт|фн\s*№?|фд\s*№?|фп\b|рн\s*ккт|зн\s*ккт|"
    r"налич|безнал|карт(ой|а)\b|оплат|сдача|получено|внесено|аванс|кредит|"
    r"скидка|наценка|ндс|налог|округление|"
    r"терминал|мерчант|mir\s*(debit)?|одобрено|биометри|баланс\s+балл|бонусн\w*\s+карт|"
    r"спасибо|благодарим|ждем|ждём|добро\b|торгов\w*\s+объ?ь?ект|"
    r"приходите|до\s*свидания|"
    r"подпись|дата\b|время|телефон|тел[.:]|адрес|www\.|http|@|"
    r"\bг\.?\s*моск|\bд\.\s*\d|\bстр\.?\s*\d|\bпом(ещ)?\.?\s*\d|марьин\w*\s+рощ|"
    r"кассовый|фискальн|документ|операци|прода(жа|жи)|приход\b|отчет|отчёт|"
    r"смс|sms|qr|сайт\b|проверьте|ofd|офд|чаевые\s+или|наведите|scan\b|leave\s+a\s+tip)",
    re.IGNORECASE,
)

# Обрывки меню/контактов, часто прилипающие слева к строке позиции.
NOISE_FRAGMENT_RE = re.compile(
    r"(\+?\d[\d\s\-()]{8,}\d|"
    r"\bм\.[а-яa-zё\-]+|"
    r"\bул[.,]\s*[а-яa-zё\-]+|"
    r"\bпр-?[дт][.,]?\s*[а-яa-zё\-]*|"
    r"\b(пушкинская|тверская|покровк\w*|сухаревск\w*)\b)",
    re.IGNORECASE,
)

# Модификаторы позиции без своей цены («Соус Сырный», «в 2 тарелки»).
# Не трогаем «Хлебная корзина» и подобные полноценные позиции.
MODIFIER_NAME_RE = re.compile(
    r"^(соус|добавк|гарнир|лепешк|хлеб$|в\s+\d+\s+тарел)",
    re.IGNORECASE,
)

DATE_PATTERN = re.compile(r"\b(\d{2})[./\-](\d{2})[./\-](\d{2,4})\b")
DATE_ISO_PATTERN = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
TIME_PATTERN = re.compile(r"\b\d{1,2}:\d{2}(:\d{2})?\b")

# Латинские буквы, которыми OCR часто подменяет кириллицу («Bcero», «CуMмa»,
# «KYPC»). Используется ТОЛЬКО для сопоставления с ключевыми словами.
_KEYWORD_TRANS = str.maketrans(
    {
        "a": "а",
        "b": "в",
        "c": "с",
        "e": "е",
        "h": "н",
        "k": "к",
        "m": "м",
        "o": "о",
        "p": "р",
        "r": "г",
        "t": "т",
        "x": "х",
        "y": "у",
    }
)

# Шапка таблицы позиций: «Блюдо  Кол-во  Сумма» и вариации.
HEADER_ROW_RE = re.compile(
    r"(блюдо|наименование|товар)|(кол[\s.\-]*во.*(сумм|цен))",
)

# Кухонные маркеры подач («1 КУРС») — не позиции и не части названий.
COURSE_ROW_RE = re.compile(r"^[\d\s]*курс\b[\s\d]*$")

# --- Числа --------------------------------------------------------------------

# Частые ошибки OCR внутри числовых токенов.
_DIGIT_FIXES = str.maketrans({"О": "0", "о": "0", "O": "0", "o": "0", "l": "1", "I": "1", "І": "1"})

NUMBER_RE = re.compile(r"\d{1,3}(?:[ \u00A0]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?")

# Простые числа без склейки "2 250.00" в 2250.00 — для колоночных форматов.
# До 3 знаков после запятой: вес в ритейле (0.448 кг), не только деньги (0.00).
SIMPLE_NUMBER_RE = re.compile(r"\d+(?:[.,]\d{1,3})?")

# Количество и цена за единицу: "2 x 450.00", "2 х 450", "1.5*100"
QTY_UNIT_RE = re.compile(
    r"(?P<qty>\d+(?:[.,]\d{1,3})?)\s*[xхX×*]\s*(?P<unit>\d+(?:[ \u00A0]\d{3})*(?:[.,]\d{1,2})?)",
)
# То же, но цена без пробелов-тысячников: нужна как альтернативная интерпретация,
# иначе "3 х 525 1575.00" жадно съедает "525 157" как одно число.
QTY_UNIT_SIMPLE_RE = re.compile(
    r"(?P<qty>\d+(?:[.,]\d{1,3})?)\s*[xхX×*]\s*(?P<unit>\d+(?:[.,]\d{1,2})?)",
)
# Деталь позиции на следующей строке: «3.2 ШТ. × 1075.00».
QTY_UNIT_LABEL_RE = re.compile(
    r"(?P<qty>\d+(?:[.,]\d{1,3})?)\s*(?:ш?[тt]|ut)\.?\s*[xхX×*]\s*"
    r"(?P<unit>\d+(?:[ \u00A0]\d{3})*(?:[.,]\d{1,2})?)",
    re.IGNORECASE,
)
# Ритейл обычно печатает цену до количества: «99.99 *1 =99.99».
UNIT_QTY_TOTAL_RE = re.compile(
    r"(?P<unit>\d+(?:[.,]\d{1,2})?)\s*(?:[xхX×*#№]\s*)"
    r"(?P<qty>\d+(?:[.,]\d{1,3})?)\s*=?\s*"
    r"(?P<total>\d+(?:[.,]\d{1,2})?)",
)
RETAIL_DETAIL_RE = re.compile(r"(?:н|h|n)[дaа][сcзz2]\w*", re.IGNORECASE)

# Токен, состоящий только из чисел/разделителей — числовая колонка чека.
NUMERIC_TOKEN_RE = re.compile(r"[\d\s.,:;%#№*=+xхX×\-−–—]+")
NEGATIVE_AMOUNT_RE = re.compile(r"^\s*[-−–—]")

MAX_REASONABLE_PRICE = 1_000_000
MAX_REASONABLE_QTY = 999
# Количество больше — почти наверняка вес в граммах; для дележа счёта
# трактуем такую позицию как «1 порция за total».
MAX_COLUMN_QTY = 99
# Весовой товар в ритейле: «0.448 × 289.90 = 129.88».
MAX_WEIGHT_QTY = 50.0


def _parse_number(raw: str) -> float | None:
    cleaned = raw.translate(_DIGIT_FIXES).replace(" ", "").replace("\u00a0", "").replace(",", ".")
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return value


def _fix_digit_confusions(text: str) -> str:
    """Чинит О->0 и т.п. только в токенах, состоящих в основном из цифр,
    чтобы не превращать буквы обычных слов в числа."""
    words = []
    for word in text.split(" "):
        digits = sum(c.isdigit() for c in word)
        if digits and digits * 2 >= len(word):
            word = word.translate(_DIGIT_FIXES)
        words.append(word)
    return " ".join(words)


def _extract_numbers(text: str, pattern: re.Pattern = NUMBER_RE) -> list[float]:
    values = []
    for match in pattern.finditer(_fix_digit_confusions(text)):
        value = _parse_number(match.group())
        if value is not None:
            values.append(value)
    return values


def _normalize_keywords(text: str) -> str:
    return text.lower().translate(_KEYWORD_TRANS)


def _is_numeric_token_text(text: str) -> bool:
    fixed = text.strip().translate(_DIGIT_FIXES)
    if not fixed or not any(c.isdigit() for c in fixed):
        return False
    # Полноценные даты/время — не колонка «Кол-во/Сумма».
    # Важно: не путать с ценами вида «1.00» / «13.00».
    if DATE_PATTERN.search(fixed) or DATE_ISO_PATTERN.search(fixed):
        return False
    if TIME_PATTERN.fullmatch(fixed):
        return False
    return bool(NUMERIC_TOKEN_RE.fullmatch(fixed))


def _is_negative_amount_token(text: str) -> bool:
    """Скидки вида «-188,00» — не колонка количества/суммы позиции."""
    return bool(NEGATIVE_AMOUNT_RE.match(text.strip().translate(_DIGIT_FIXES)))


# --- Строки и выравнивание колонок ---------------------------------------------


@dataclass
class _Row:
    tokens: list[OcrToken]
    y: float = 0.0

    def recompute(self) -> None:
        self.tokens.sort(key=lambda t: t.x_min)
        self.y = sum(t.y_center for t in self.tokens) / len(self.tokens)

    @property
    def text(self) -> str:
        return " ".join(t.text for t in self.tokens)

    @property
    def score(self) -> float:
        return sum(t.score for t in self.tokens) / len(self.tokens)


def _x_overlaps(token: OcrToken, row: _Row) -> bool:
    width = token.x_max - token.x_min
    for other in row.tokens:
        overlap = min(token.x_max, other.x_max) - max(token.x_min, other.x_min)
        if overlap > 0.25 * min(width, other.x_max - other.x_min):
            return True
    return False


def _cluster_rows(tokens: list[OcrToken], threshold: float) -> list[_Row]:
    """Кластеризация боксов в строки по вертикали.

    Боксы, перекрывающиеся по горизонтали, никогда не попадают в одну
    строку — иначе соседние названия склеиваются при наклоне бумаги."""
    rows: list[_Row] = []
    for token in sorted(tokens, key=lambda t: t.y_center):
        best: _Row | None = None
        best_dy = threshold
        for row in rows:
            dy = abs(token.y_center - row.y)
            if dy <= best_dy and not _x_overlaps(token, row):
                best, best_dy = row, dy
        if best is None:
            rows.append(_Row(tokens=[token]))
            rows[-1].recompute()
        else:
            best.tokens.append(token)
            best.recompute()
    rows.sort(key=lambda r: r.y)
    return rows


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


def _align_groups_to_rows(
    groups: list[_Row], rows: list[_Row], max_gap: float
) -> tuple[dict[int, int], float]:
    """Монотонное сопоставление числовых групп со строками названий.

    Из-за изгиба бумаги числовые колонки систематически смещены по
    вертикали относительно названий, поэтому сначала оцениваем общий
    сдвиг, затем ищем назначение минимальной суммарной ошибки
    (динамическое программирование, как выравнивание последовательностей).

    Возвращает ({индекс группы -> индекс строки}, сдвиг).
    """
    if not groups or not rows:
        return {}, 0.0

    deltas = []
    for group in groups:
        nearest = min(rows, key=lambda r: abs(group.y - r.y))
        delta = group.y - nearest.y
        if abs(delta) <= 2 * max_gap:
            deltas.append(delta)
    offset = _median(deltas) if len(deltas) >= 3 else 0.0
    if abs(offset) > max_gap:
        offset = 0.0

    n, m = len(groups), len(rows)
    inf = float("inf")
    skip_group = max_gap  # незаматченная группа хуже любого допустимого матча

    dp = [[inf] * (m + 1) for _ in range(n + 1)]
    move = [[0] * (m + 1) for _ in range(n + 1)]  # 1=skip row, 2=skip group, 3=match
    dp[0] = [0.0] * (m + 1)
    for i in range(1, n + 1):
        dp[i][0] = i * skip_group
        move[i][0] = 2
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            dp[i][j] = dp[i][j - 1]
            move[i][j] = 1
            if dp[i - 1][j] + skip_group < dp[i][j]:
                dp[i][j] = dp[i - 1][j] + skip_group
                move[i][j] = 2
            cost = abs(groups[i - 1].y - offset - rows[j - 1].y)
            if cost <= max_gap and dp[i - 1][j - 1] + cost < dp[i][j]:
                dp[i][j] = dp[i - 1][j - 1] + cost
                move[i][j] = 3

    matches: dict[int, int] = {}
    i, j = n, m
    while i > 0 or j > 0:
        step = move[i][j]
        if step == 3:
            matches[i - 1] = j - 1
            i, j = i - 1, j - 1
        elif step == 2:
            i -= 1
        else:
            j -= 1
    return matches, offset


@dataclass
class _Entry:
    y: float
    row: _Row | None
    group: _Row | None

    @property
    def text(self) -> str:
        tokens = []
        if self.row:
            tokens.extend(self.row.tokens)
        if self.group:
            tokens.extend(self.group.tokens)
        tokens.sort(key=lambda t: t.x_min)
        return " ".join(t.text for t in tokens)

    @property
    def score(self) -> float:
        tokens = (self.row.tokens if self.row else []) + (self.group.tokens if self.group else [])
        if not tokens:
            return 0.0
        return sum(t.score for t in tokens) / len(tokens)


def _filter_receipt_tokens(tokens: list[OcrToken]) -> list[OcrToken]:
    """Убирает текст с фона (меню) левее колонки имён и правее колонки сумм."""
    if len(tokens) < 8:
        return tokens

    name_col_x: float | None = None
    sum_col_x: float | None = None
    for token in tokens:
        normalized = _normalize_keywords(token.text)
        if name_col_x is None and ("наименован" in normalized or normalized in {"блюдо", "товар"}):
            name_col_x = token.x_min
        if sum_col_x is None and normalized in {"сумма", "сумм", "итого"}:
            sum_col_x = token.x_min

    if name_col_x is None:
        letter_xs = sorted(
            t.x_min
            for t in tokens
            if sum(ch.isalpha() for ch in t.text) >= 3 and t.score >= 0.7
        )
        if len(letter_xs) >= 5:
            name_col_x = letter_xs[len(letter_xs) // 5]

    if name_col_x is None:
        return tokens

    left = name_col_x - 30
    right = (sum_col_x + 160) if sum_col_x is not None else None
    filtered = []
    for token in tokens:
        if right is not None and token.x_min > right:
            continue
        if _is_numeric_token_text(token.text):
            if token.x_min >= left - 20:
                filtered.append(token)
            continue
        if token.x_min >= left:
            filtered.append(token)
    return filtered if len(filtered) >= 5 else tokens


def _build_entries(tokens: list[OcrToken], median_height: float) -> list[_Entry]:
    tokens = _filter_receipt_tokens(tokens)
    text_tokens = [t for t in tokens if not _is_numeric_token_text(t.text)]
    # Отрицательные суммы скидок не выравниваем к позициям — иначе
    # «-74,00» прилипает к «Соус Сырный» как цена 74.
    numeric_tokens = [
        t
        for t in tokens
        if _is_numeric_token_text(t.text) and not _is_negative_amount_token(t.text)
    ]

    cluster_threshold = 0.6 * median_height
    rows = _cluster_rows(text_tokens, cluster_threshold)
    groups = _cluster_rows(numeric_tokens, cluster_threshold)

    # Кухонные маркеры и модификаторы («Соус…») не забирают чужие числа.
    alignable = [
        r
        for r in rows
        if not COURSE_ROW_RE.match(_normalize_keywords(r.text))
        and not _is_excluded(_strip_line_noise(r.text) or r.text)
        and not MODIFIER_NAME_RE.match(_clean_name(_strip_line_noise(r.text) or r.text) or "")
    ]
    matches, offset = _align_groups_to_rows(groups, alignable, max_gap=1.1 * median_height)

    group_by_row: dict[int, _Row] = {}
    for group_idx, row_idx in matches.items():
        group_by_row[id(alignable[row_idx])] = groups[group_idx]

    matched_groups = {id(groups[i]) for i in matches}
    entries = [_Entry(y=row.y, row=row, group=group_by_row.get(id(row))) for row in rows]
    entries.extend(
        _Entry(y=group.y - offset, row=None, group=group)
        for group in groups
        if id(group) not in matched_groups
    )
    entries.sort(key=lambda e: e.y)
    return entries


# --- Классификация и извлечение ------------------------------------------------


def _is_total_line(text: str) -> bool:
    return any(p.search(text) for p in TOTAL_PATTERNS)


def _total_from_line(text: str) -> tuple[int, float | None] | None:
    """Возвращает приоритет метки и первое денежное число после неё.

    Первое, а не последнее: в строке «ИТОГО БЕЗ СКИДКИ 1551 СКИДКА 161»
    последнее число — скидка, а не итог.
    """
    normalized = _normalize_keywords(text)
    for priority, pattern in enumerate(TOTAL_PATTERNS):
        match = pattern.search(normalized)
        if not match:
            continue
        # Обычная «сумма НДС» не является итогом. «Сумма без НДС» уже
        # поймана более приоритетным специализированным шаблоном.
        if priority == len(TOTAL_PATTERNS) - 1 and re.search(
            r"(ндс|налог|комисси|скидк)", normalized
        ):
            return None
        numbers = _extract_numbers(TIME_PATTERN.sub(" ", text[match.end():]))
        amount = next(
            (value for value in numbers if 0 < value <= MAX_REASONABLE_PRICE),
            None,
        )
        # «Итого без скидки» полезно как запасной кандидат, но обычный
        # «ИТОГ» или «К ОПЛАТЕ» должен побеждать.
        if priority == 1 and re.search(r"без\s+скидк", normalized[match.end():]):
            priority += 5
        return priority, amount
    return None


def _nearby_total_amount(
    entries: list[_Entry], index: int, median_height: float
) -> float | None:
    """Ищет сумму в соседнем OCR-боксе, в том числе слева/выше метки."""
    candidates: list[tuple[float, float]] = []
    current = entries[index]
    for other_index in range(max(0, index - 2), min(len(entries), index + 3)):
        if other_index == index:
            continue
        other = entries[other_index]
        distance = abs(other.y - current.y)
        if distance > 1.8 * median_height:
            continue
        numbers = _extract_numbers(TIME_PATTERN.sub(" ", other.text))
        amounts = [value for value in numbers if 0 < value <= MAX_REASONABLE_PRICE]
        if amounts:
            candidates.append((distance, amounts[-1]))
    return min(candidates, default=(0.0, None), key=lambda pair: pair[0])[1]


def _select_total_candidate(
    candidates: list[tuple[int, float]], entries: list[_Entry]
) -> float | None:
    """Выбирает итог с учётом повторов одной суммы в нижней части чека."""
    if not candidates:
        return None
    all_amounts = [
        value
        for entry in entries
        for value in _extract_numbers(TIME_PATTERN.sub(" ", entry.text))
        if 0 < value <= MAX_REASONABLE_PRICE
    ]

    def rank(candidate: tuple[int, float]) -> tuple[int, int, int]:
        priority, amount = candidate
        repeats = sum(abs(value - amount) < 0.01 for value in all_amounts)
        # Дублированная сумма («ИТОГ», «БЕЗНАЛИЧНЫМИ», банковский слип)
        # надёжнее единичного ошибочного чтения жирного шрифта.
        duplicate_boost = 2 if repeats >= 2 else 0
        return priority - duplicate_boost, priority, -repeats

    return min(candidates, key=rank)[1]


def _is_excluded(text: str) -> bool:
    return bool(EXCLUDE_PATTERN.search(text))


def _strip_line_noise(text: str) -> str:
    """Убирает телефоны/адреса меню, прилипшие к названию позиции."""
    cleaned = NOISE_FRAGMENT_RE.sub(" ", text)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" ,;|-")
    return cleaned


def _extract_date(text: str) -> str | None:
    iso = DATE_ISO_PATTERN.search(text)
    if iso:
        year, month, day = iso.groups()
        if 1 <= int(month) <= 12 and 1 <= int(day) <= 31:
            return f"{year}-{month}-{day}"
    match = DATE_PATTERN.search(text)
    if match:
        day, month, year = match.groups()
        if len(year) == 2:
            year = "20" + year
        if 1 <= int(month) <= 12 and 1 <= int(day) <= 31 and 2000 <= int(year) <= 2100:
            return f"{year}-{month}-{day}"
    return None


def _clean_name(name: str) -> str:
    name = re.sub(r"^[\s\d.№#*=\-–—]+", "", name)
    name = re.sub(r"[\s.=\-–—*]+$", "", name)
    return name.strip()[:255]


def _has_letters(text: str, minimum: int = 2) -> bool:
    return len(re.findall(r"[a-zA-Zа-яА-ЯёЁ]", text)) >= minimum


def _plausible(quantity: float, total_price: float) -> bool:
    return 0 < quantity <= MAX_REASONABLE_QTY and 0 <= total_price <= MAX_REASONABLE_PRICE


@dataclass
class ParsedItem:
    name: str
    quantity: float
    unit_price: float | None
    total_price: float
    score: float
    arithmetic_ok: bool = False


def _parse_qty_pattern(text: str) -> tuple[re.Match, float, float, float, bool] | None:
    """Разбирает "qty x unit ... total", выбирая интерпретацию, где сходится
    арифметика (qty * unit == total). Возвращает (match, qty, unit, total, ok)."""
    best = None
    for pattern in (QTY_UNIT_SIMPLE_RE, QTY_UNIT_RE, QTY_UNIT_LABEL_RE):
        match = pattern.search(text)
        if not match:
            continue
        quantity = _parse_number(match.group("qty"))
        unit_price = _parse_number(match.group("unit"))
        if not quantity or unit_price is None:
            continue
        expected = quantity * unit_price
        tail_numbers = _extract_numbers(text[match.end():])
        total_price = tail_numbers[-1] if tail_numbers else round(expected, 2)
        if not _plausible(quantity, total_price):
            continue
        arithmetic_ok = abs(expected - total_price) <= max(0.02 * total_price, 1.0)
        if arithmetic_ok:
            return match, quantity, unit_price, total_price, True
        if best is None:
            best = (match, quantity, unit_price, total_price, False)
    return best


def _parse_unit_qty_total(text: str) -> tuple[float, float, float] | None:
    """Разбирает ритейловый порядок «цена * количество = сумма»."""
    match = UNIT_QTY_TOTAL_RE.search(_fix_digit_confusions(text))
    if not match:
        return None
    unit = _parse_number(match.group("unit"))
    quantity = _parse_number(match.group("qty"))
    total = _parse_number(match.group("total"))
    if unit is None or quantity is None or total is None or not _looks_like_qty(quantity):
        return None
    if abs(unit * quantity - total) > max(0.02 * total, 0.02):
        return None
    return quantity, unit, total


def _parse_retail_detail(text: str) -> tuple[float, float, float] | None:
    """Строка под названием товара: «22.0 НДС2 149.99 *1 =149.99».

    Налоговая ставка не является количеством. Источник истины здесь —
    маркер количества и сумма после знака равенства.
    """
    normalized = _normalize_keywords(text)
    if not RETAIL_DETAIL_RE.search(normalized):
        return None
    equals = re.findall(r"=\s*(\d+(?:[.,]\d{1,2})?)", _fix_digit_confusions(text))
    if not equals:
        return None
    total = _parse_number(equals[-1])
    if total is None or total <= 0 or total > MAX_REASONABLE_PRICE:
        return None
    quantity_match = re.search(
        r"(?:[*#№xхX×]\s*)(\d+(?:[.,]\d{1,3})?)\s*=",
        _fix_digit_confusions(text),
    )
    quantity = _parse_number(quantity_match.group(1)) if quantity_match else 1.0
    if quantity is None or not _looks_like_qty(quantity):
        quantity = 1.0
    return float(quantity), round(total / quantity, 2), float(total)


def _parse_token_amount(text: str) -> float | None:
    """Один OCR-бокс → одно число. Пробел тысяч внутри бокса допустим («5 661,00»)."""
    fixed = _fix_digit_confusions(text.strip())
    if not fixed or not any(c.isdigit() for c in fixed):
        return None
    # Целиком через NUMBER_RE — «1 358,00» остаётся 1358, а не 1+358.
    matched = list(NUMBER_RE.finditer(fixed))
    if len(matched) == 1:
        digits_in_token = sum(c.isdigit() for c in fixed)
        digits_in_match = sum(c.isdigit() for c in matched[0].group())
        if digits_in_match >= digits_in_token:
            return _parse_number(matched[0].group())
    # Несколько кусков: склеим тысячные группы справа налево.
    simple = list(SIMPLE_NUMBER_RE.finditer(fixed))
    if not simple:
        return None
    if len(simple) == 1:
        return _parse_number(simple[0].group())
    total = _parse_number(simple[-1].group())
    if total is None:
        return None
    for match in reversed(simple[:-1]):
        piece = match.group()
        if "," in piece or "." in piece:
            break
        if len(piece) == 3 and total < 1000:
            head = _parse_number(piece)
            if head is None:
                break
            total = head * 1000 + total
        else:
            break
    return total


def _looks_like_qty(value: float) -> bool:
    """Целое количество (1..99) или вес/дробь (0.001..50), типичные для чеков."""
    if value <= 0:
        return False
    if value == int(value) and value <= MAX_COLUMN_QTY:
        return True
    # Вес в кг / доля упаковки (ритейл CDEK: 0.448, 0.936, …).
    return value < MAX_WEIGHT_QTY and abs(value - round(value)) > 1e-6


def _qty_total_from_group(group: _Row) -> tuple[float | None, float | None]:
    """Колонки «Кол-во | Сумма» по отдельным боксам — без склейки «4»+«780»→4780."""
    amounts: list[float] = []
    for token in sorted(group.tokens, key=lambda t: t.x_min):
        value = _parse_token_amount(token.text)
        if value is not None and value >= 0:
            amounts.append(value)
    if not amounts:
        return None, None
    if len(amounts) == 1:
        return None, amounts[-1]
    total = amounts[-1]
    quantity = amounts[0]
    # qty | unit | total — берём крайние, если среднее похоже на цену за единицу.
    if len(amounts) >= 3 and _looks_like_qty(amounts[0]):
        q, unit, tot = amounts[0], amounts[1], amounts[-1]
        if abs(q * unit - tot) <= max(0.02 * tot, 1.0):
            return q, tot
    if _looks_like_qty(quantity):
        return float(quantity), float(total)
    return None, float(total)


def _parse_qty_sum_from_text(price_part: str) -> tuple[float | None, float] | None:
    """Запасной разбор «4 780,00» / «2 750,00» без склейки в тысячи.

    Между колонками «Кол-во» и «Сумма» пробел — это НЕ разделитель тысяч.
    Тысячи («1 358,00») бывают только внутри одного OCR-бокса.
    """
    fixed = _fix_digit_confusions(price_part)
    parts = list(SIMPLE_NUMBER_RE.finditer(fixed))
    if not parts:
        return None

    def has_decimals(raw: str) -> bool:
        return "," in raw or "." in raw

    last_raw = parts[-1].group()
    total = _parse_number(last_raw)
    if total is None or total <= 0:
        return None
    if not has_decimals(last_raw) and total < 10 and len(parts) < 2:
        return None

    quantity: float | None = None
    if len(parts) >= 2:
        raw = parts[-2].group()
        val = _parse_number(raw)
        if val is not None and _looks_like_qty(val):
            # Целое без запятой — колонка «кол-во»; дробь — вес (0,448).
            if not has_decimals(raw) and val == int(val):
                quantity = float(val)
            elif has_decimals(raw):
                quantity = float(val)
    return quantity, float(total)


def _try_parse_item(
    text: str,
    score: float,
    qty_sum_layout: bool = False,
    numeric_group: _Row | None = None,
) -> ParsedItem | None:
    """Пытается разобрать строку как позицию чека."""
    unit_qty_total = _parse_unit_qty_total(text)
    if unit_qty_total:
        quantity, unit_price, total_price = unit_qty_total
        marker = UNIT_QTY_TOTAL_RE.search(_fix_digit_confusions(text))
        assert marker is not None
        name = _clean_name(text[: marker.start()])
        if name and _has_letters(name, minimum=3):
            return ParsedItem(
                name, quantity, unit_price, total_price, score, arithmetic_ok=True
            )

    parsed_qty = _parse_qty_pattern(text)
    if parsed_qty:
        qty_match, quantity, unit_price, total_price, arithmetic_ok = parsed_qty
        name = _clean_name(text[: qty_match.start()])
        if name and _has_letters(name):
            # Явный "2 x 450 = 900" — почти наверняка позиция.
            return ParsedItem(name, quantity, unit_price, total_price, score, arithmetic_ok)

    # Имя без ценового хвоста: отрезаем по последней букве.
    last_letter = None
    for match in re.finditer(r"[a-zA-Zа-яА-ЯёЁ)»%]", text):
        last_letter = match
    if last_letter is None:
        return None
    name_part = text[: last_letter.end()]
    price_part = text[last_letter.end():]
    name = _clean_name(name_part)
    if not name or not _has_letters(name, minimum=3):
        return None

    # Колоночная раскладка: сначала числа из отдельных OCR-боксов.
    if qty_sum_layout and numeric_group is not None:
        quantity, total_price = _qty_total_from_group(numeric_group)
        if total_price is not None and total_price > 0 and _plausible(quantity or 1.0, total_price):
            if quantity is not None and _looks_like_qty(quantity):
                unit_price = round(total_price / quantity, 2)
                return ParsedItem(name, quantity, unit_price, total_price, score)
            return ParsedItem(name, 1.0, None, total_price, score)

    if qty_sum_layout:
        parsed = _parse_qty_sum_from_text(price_part)
        if parsed is not None:
            quantity, total_price = parsed
            if _plausible(quantity or 1.0, total_price):
                if quantity is not None:
                    unit_price = round(total_price / quantity, 2)
                    return ParsedItem(name, quantity, unit_price, total_price, score)
                return ParsedItem(name, 1.0, None, total_price, score)

    # В раскладке «Кол-во | Сумма» нельзя склеивать "2 750,00" → 2750.
    number_pattern = SIMPLE_NUMBER_RE if qty_sum_layout else NUMBER_RE
    numbers = _extract_numbers(text, number_pattern)
    if not numbers:
        return None

    price_numbers = _extract_numbers(price_part, number_pattern)

    # Колонки "qty | unit | total": проверяем арифметикой на простых числах,
    # чтобы "2 250.00 500.00" не склеилось в 2250.00.
    simple_numbers = _extract_numbers(price_part, SIMPLE_NUMBER_RE)
    if len(simple_numbers) >= 3:
        quantity, unit_price, total_price = simple_numbers[-3], simple_numbers[-2], simple_numbers[-1]
        if (
            _plausible(quantity, total_price)
            and _looks_like_qty(quantity)
            and abs(quantity * unit_price - total_price) <= max(0.02 * total_price, 1.0)
        ):
            return ParsedItem(name, quantity, unit_price, total_price, score, arithmetic_ok=True)
        unit_price, quantity, total_price = (
            simple_numbers[-3],
            simple_numbers[-2],
            simple_numbers[-1],
        )
        if (
            _plausible(quantity, total_price)
            and _looks_like_qty(quantity)
            and abs(quantity * unit_price - total_price) <= max(0.02 * total_price, 1.0)
        ):
            return ParsedItem(name, quantity, unit_price, total_price, score, arithmetic_ok=True)

    if len(price_numbers) >= 3 and not qty_sum_layout:
        quantity, unit_price, total_price = price_numbers[-3], price_numbers[-2], price_numbers[-1]
        if (
            _plausible(quantity, total_price)
            and quantity <= MAX_REASONABLE_QTY
            and abs(quantity * unit_price - total_price) <= max(0.02 * total_price, 1.0)
        ):
            return ParsedItem(name, quantity, unit_price, total_price, score, arithmetic_ok=True)

    if len(price_numbers) >= 2:
        # Последние два числа: остальные — часть названия («пиво 0.33»)
        # либо не прошедшая арифметику колонка.
        first, total_price = price_numbers[-2], price_numbers[-1]
        if total_price < 0:
            return None
        if not _plausible(1, total_price):
            return None
        if qty_sum_layout:
            parsed = _parse_qty_sum_from_text(price_part)
            if parsed is not None:
                quantity, total_price = parsed
                if quantity is not None:
                    unit_price = round(total_price / quantity, 2)
                    return ParsedItem(name, quantity, unit_price, total_price, score)
                return ParsedItem(name, 1.0, None, total_price, score)
            if 0 < first <= MAX_COLUMN_QTY:
                unit_price = round(total_price / first, 2)
                return ParsedItem(name, first, unit_price, total_price, score)
            return ParsedItem(name, 1.0, None, total_price, score)
        if first <= 20 and first == int(first) and first > 0:
            quantity = first
            unit_price = round(total_price / quantity, 2) if quantity else None
            return ParsedItem(name, quantity, unit_price, total_price, score)
        if abs(first - total_price) < 0.01:
            return ParsedItem(name, 1.0, first, total_price, score, arithmetic_ok=True)
        if 0 < first < total_price:
            # "unit total": количество восстанавливаем из отношения сумм.
            ratio = total_price / first
            if abs(ratio - round(ratio)) <= 0.01 and 2 <= round(ratio) <= MAX_REASONABLE_QTY:
                quantity = float(round(ratio))
                return ParsedItem(name, quantity, first, total_price, score, arithmetic_ok=True)
        return ParsedItem(name, 1.0, None, total_price, score)

    if len(price_numbers) == 1:
        total_price = price_numbers[0]
        if total_price < 0 or not _plausible(1, total_price) or total_price == 0:
            return None
        return ParsedItem(name, 1.0, None, total_price, score)

    return None


def _item_confidence(item: ParsedItem) -> float:
    confidence = item.score
    if item.arithmetic_ok:
        confidence = min(1.0, confidence + 0.07)
    return round(confidence, 4)


# --- Основной вход --------------------------------------------------------------


def parse_receipt(tokens: list[OcrToken]) -> ReceiptResponse:
    if not tokens:
        return ReceiptResponse(items=[], confidence=0.0, errors=["unreadable"])

    median_height = _median([t.height for t in tokens]) or 10.0
    entries = _build_entries(tokens, median_height)

    # Шапка таблицы позиций («Блюдо Кол-во Сумма»): всё выше — заголовок
    # чека (заведение, адрес, стол…), а не позиции.
    header_entry: _Entry | None = None
    qty_sum_layout = False
    for entry in entries:
        if entry.row is None:
            continue
        normalized = _normalize_keywords(entry.row.text)
        if HEADER_ROW_RE.search(normalized) and not _extract_numbers(entry.row.text):
            header_entry = entry
            # «Кол-во»/«Сумма» иногда приезжают отдельными боксами той же строки
            # или соседней — смотрим узкое окно вокруг шапки.
            nearby = " ".join(
                _normalize_keywords(e.row.text)
                for e in entries
                if e.row is not None and abs(e.y - entry.y) <= 1.5 * median_height
            )
            qty_sum_layout = (
                ("кол" in nearby and "сумм" in nearby and "цен" not in nearby)
                or ("кол" in normalized and "сумм" in normalized and "цен" not in normalized)
            )
            break

    # Если шапку не прочитали, но видны пары боксов «малое qty | сумма» —
    # включаем колоночный режим, чтобы не склеивать «4 780» в 4780.
    if not qty_sum_layout:
        pair_hits = 0
        for entry in entries:
            if entry.group is None or len(entry.group.tokens) < 2:
                continue
            quantity, total_price = _qty_total_from_group(entry.group)
            if quantity is not None and total_price is not None and total_price >= 10:
                pair_hits += 1
        if pair_hits >= 2:
            qty_sum_layout = True

    merchant: str | None = None
    date: str | None = None
    total: float | None = None
    total_priority = len(TOTAL_PATTERNS)
    total_candidates: list[tuple[int, float]] = []  # (priority, amount)
    pending_total_priority: int | None = None
    items: list[ParsedItem] = []
    pending_name: tuple[str, float] | None = None
    pending_numbers: tuple[str, float, float] | None = None  # text, score, y
    totals_seen = False
    last_item_y: float | None = None
    text_line_index = -1

    for entry_index, entry in enumerate(entries):
        text = entry.text.strip()
        if not text:
            continue
        if entry.row is not None:
            text_line_index += 1
        normalized = _normalize_keywords(text)

        if date is None:
            found_date = _extract_date(text)
            if found_date:
                date = found_date

        if header_entry is not None and entry is header_entry:
            continue

        if COURSE_ROW_RE.match(normalized):
            continue

        total_match = _total_from_line(text) if entry.row is not None else None
        if total_match is not None:
            priority, candidate = total_match
            if candidate is None:
                candidate = _nearby_total_amount(entries, entry_index, median_height)
            if candidate is not None:
                totals_seen = True
                total_candidates.append((priority, candidate))
                if priority < total_priority:
                    total = candidate
                    total_priority = priority
            else:
                # Метка без числа: сумма могла уехать на отдельную строку.
                pending_total_priority = priority
            pending_name = None
            pending_numbers = None
            continue

        # Зона заголовка чека: берём только название заведения и дату.
        if header_entry is not None and entry.y < header_entry.y:
            if (
                merchant is None
                and entry.row is not None
                and entry.group is None
                and _has_letters(text, minimum=3)
                and not _extract_numbers(text)
                and not _is_excluded(text)
            ):
                merchant = _clean_name(text) or None
            continue

        if _extract_date(text) is not None and TIME_PATTERN.search(text):
            pending_name = None
            pending_numbers = None
            continue

        if (
            re.match(r"^\s*\d{5,}", text)
            and sum(ch.isalpha() for ch in text) < 8
            and "=" not in text
            and not re.search(r"[xхX×*]", text)
        ):
            pending_name = None
            pending_numbers = None
            continue

        cleaned = _strip_line_noise(text)
        if cleaned and cleaned != text:
            text = cleaned
            normalized = _normalize_keywords(text)

        retail_detail = _parse_retail_detail(text)
        if _is_excluded(text) and retail_detail is None:
            pending_name = None
            # Отрицательные суммы скидок — не позиции.
            continue

        # После итоговой строки позиций не бывает — только чаевые-реклама и QR.
        if totals_seen:
            continue

        if entry.row is None:
            # Числовая группа без названия рядом.
            if pending_total_priority is not None:
                numbers = _extract_numbers(text)
                if numbers and 0 < numbers[-1] <= MAX_REASONABLE_PRICE:
                    total_candidates.append((pending_total_priority, numbers[-1]))
                    if pending_total_priority < total_priority:
                        total = numbers[-1]
                        total_priority = pending_total_priority
                    totals_seen = True
                pending_total_priority = None
                continue
            number_pattern = SIMPLE_NUMBER_RE if qty_sum_layout else NUMBER_RE
            nums = _extract_numbers(text, number_pattern)
            if nums and nums[-1] < 0:
                pending_numbers = None
                continue
            if pending_name is not None:
                name, name_score = pending_name
                item = _try_parse_item(
                    f"{name} {text}",
                    (entry.score + name_score) / 2,
                    qty_sum_layout,
                    numeric_group=entry.group,
                )
                if item is not None:
                    items.append(item)
                    last_item_y = entry.y
                    if merchant == name:
                        # Строка оказалась названием позиции, а не заведения.
                        merchant = None
                pending_name = None
                pending_numbers = None
            elif nums and nums[-1] > 0:
                # Числа раньше названия (часто при пропуске первой строки OCR).
                pending_numbers = (text, entry.score, entry.y)
            continue

        pending_total_priority = None

        if retail_detail is not None:
            if pending_name is not None:
                quantity, unit_price, total_price = retail_detail
                name, name_score = pending_name
                items.append(
                    ParsedItem(
                        name,
                        quantity,
                        unit_price,
                        total_price,
                        (entry.score + name_score) / 2,
                        arithmetic_ok=True,
                    )
                )
                last_item_y = entry.y
                if merchant == name:
                    merchant = None
            pending_name = None
            pending_numbers = None
            continue

        # Название ритейлового товара может оканчиваться объёмом («0.45»).
        # Если следующая строка явно содержит НДС/цену/количество, текущая
        # строка целиком является названием, а не товаром ценой 0.45 руб.
        next_is_retail_detail = (
            entry_index + 1 < len(entries)
            and _parse_retail_detail(entries[entry_index + 1].text) is not None
        )
        if next_is_retail_detail and _has_letters(text, minimum=3):
            pending_name = (_clean_name(text), entry.score)
            pending_numbers = None
            continue

        next_qty_detail = (
            QTY_UNIT_LABEL_RE.search(_fix_digit_confusions(entries[entry_index + 1].text))
            if entry_index + 1 < len(entries)
            else None
        )
        if (
            next_qty_detail is not None
            and _has_letters(text, minimum=3)
            and "=" not in text
            and (_extract_numbers(text) or not items)
        ):
            pending_name = (_clean_name(text), entry.score)
            pending_numbers = None
            continue

        # В ресторанных чеках количество и цена нередко печатаются строкой
        # под названием/суммой. Уточняем уже созданную позицию и не создаём
        # ложный товар из «3.2 ШТ × 1075».
        detail_qty = QTY_UNIT_LABEL_RE.search(_fix_digit_confusions(text))
        if detail_qty is not None and (items or pending_name is not None):
            quantity = _parse_number(detail_qty.group("qty"))
            unit_price = _parse_number(detail_qty.group("unit"))
            if quantity and unit_price is not None:
                expected = quantity * unit_price
                if pending_name is not None:
                    name, name_score = pending_name
                    items.append(
                        ParsedItem(
                            name,
                            quantity,
                            unit_price,
                            round(expected, 2),
                            (entry.score + name_score) / 2,
                            arithmetic_ok=True,
                        )
                    )
                    last_item_y = entry.y
                    pending_name = None
                    pending_numbers = None
                else:
                    assert items
                    previous = items[-1]
                    if abs(expected - previous.total_price) <= max(
                        0.03 * previous.total_price, 1.0
                    ):
                        previous.quantity = quantity
                        previous.unit_price = unit_price
                        previous.arithmetic_ok = True
                # Даже при повреждённой арифметике это служебная строка
                # количества, а не самостоятельный товар.
                continue

        item = _try_parse_item(text, entry.score, qty_sum_layout, numeric_group=entry.group)
        if item is not None:
            items.append(item)
            pending_name = None
            pending_numbers = None
            last_item_y = entry.y
            continue

        if _has_letters(text, minimum=2):
            name = _clean_name(text)
            if name:
                if (
                    MODIFIER_NAME_RE.match(name)
                    and entry.group is None
                    and pending_numbers is None
                ):
                    # «Соус Сырный» без цены — модификатор, не позиция.
                    continue
                # Продолжение многострочного названия предыдущей позиции
                # (колоночная раскладка: числа стоят у первой строки названия).
                # Строки с прикреплённой числовой группой — всегда кандидаты
                # в позиции, их нельзя приклеивать к чужому названию.
                if (
                    header_entry is not None
                    and entry.group is None
                    and pending_numbers is None
                    and items
                    and last_item_y is not None
                    and entry.y - last_item_y <= 2.4 * median_height
                    and _has_letters(name, minimum=3)
                    and sum(ch.isalpha() for ch in name) >= max(3, len(name) // 2)
                ):
                    items[-1].name = f"{items[-1].name} {name}"[:255]
                    last_item_y = entry.y
                    pending_name = None
                    continue
                if _has_letters(text, minimum=3):
                    if (
                        pending_numbers is not None
                        and abs(entry.y - pending_numbers[2]) <= 2.8 * median_height
                    ):
                        num_text, num_score, _ = pending_numbers
                        item = _try_parse_item(
                            f"{name} {num_text}",
                            (entry.score + num_score) / 2,
                            qty_sum_layout,
                            numeric_group=None,
                        )
                        if item is not None:
                            items.append(item)
                            last_item_y = entry.y
                            pending_name = None
                            pending_numbers = None
                            if merchant == name:
                                merchant = None
                            continue
                    if (
                        merchant is None
                        and text_line_index <= 3
                        and not items
                        and not _extract_numbers(text)
                    ):
                        merchant = name
                    pending_name = (name, entry.score)
                    continue

        pending_name = None

    selected_total = _select_total_candidate(total_candidates, entries)
    if selected_total is not None:
        total = selected_total

    receipt_items = [
        ReceiptItem(
            name=item.name,
            quantity=round(item.quantity, 3),
            unit_price=round(item.unit_price, 2) if item.unit_price is not None else None,
            total_price=round(item.total_price, 2),
            confidence=_item_confidence(item),
        )
        for item in items
        if item.name
    ]

    errors: list[str] = []
    if not receipt_items:
        return ReceiptResponse(
            merchant=merchant,
            date=date,
            total=total,
            items=[],
            confidence=0.0,
            errors=["no_items_found"],
        )

    confidence = sum(i.confidence or 0 for i in receipt_items) / len(receipt_items)

    items_sum = sum(i.total_price for i in receipt_items)
    if total is not None:
        mismatch = abs(items_sum - total)
        if mismatch <= max(0.01 * total, 0.01):
            confidence = min(0.99, confidence + 0.08)
        else:
            # Итог после скидок может не биться с суммой позиций до скидки.
            # Если среди кандидатов («полная сумма» и т.п.) есть совпадение —
            # берём его, иначе обнуляем total.
            matched = next(
                (
                    amount
                    for priority, amount in sorted(total_candidates)
                    if abs(items_sum - amount) <= max(0.01 * amount, 0.01)
                ),
                None,
            )
            if matched is not None:
                total = matched
                confidence = min(0.99, confidence + 0.05)
            else:
                # Печатный «ИТОГО» на чеке важнее суммы позиций (скидки, НДС,
                # весовой ритейл). Оставляем total с чека, помечаем mismatch.
                confidence *= 0.8
                errors.append("total_mismatch")

    confidence = round(min(max(confidence, 0.0), 1.0), 4)
    if confidence < 0.5:
        errors.append("low_confidence")

    return ReceiptResponse(
        merchant=merchant,
        date=date,
        total=total,
        currency="RUB",
        items=receipt_items,
        confidence=confidence,
        errors=errors,
    )
