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
    re.compile(r"\bитого?\b", re.IGNORECASE),
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
    r"спасибо|благодарим|ждем|ждём|добро\s*пожаловать|приходите|до\s*свидания|"
    r"подпись|дата\b|время|телефон|тел[.:]|адрес|www\.|http|@|"
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
SIMPLE_NUMBER_RE = re.compile(r"\d+(?:[.,]\d{1,2})?")

# Количество и цена за единицу: "2 x 450.00", "2 х 450", "1.5*100"
QTY_UNIT_RE = re.compile(
    r"(?P<qty>\d+(?:[.,]\d{1,3})?)\s*[xхX×*]\s*(?P<unit>\d+(?:[ \u00A0]\d{3})*(?:[.,]\d{1,2})?)",
)
# То же, но цена без пробелов-тысячников: нужна как альтернативная интерпретация,
# иначе "3 х 525 1575.00" жадно съедает "525 157" как одно число.
QTY_UNIT_SIMPLE_RE = re.compile(
    r"(?P<qty>\d+(?:[.,]\d{1,3})?)\s*[xхX×*]\s*(?P<unit>\d+(?:[.,]\d{1,2})?)",
)

# Токен, состоящий только из чисел/разделителей — числовая колонка чека.
NUMERIC_TOKEN_RE = re.compile(r"[\d\s.,:;%#№*=+xхX×\-−–—]+")
NEGATIVE_AMOUNT_RE = re.compile(r"^\s*[-−–—]")

MAX_REASONABLE_PRICE = 1_000_000
MAX_REASONABLE_QTY = 999
# Количество больше — почти наверняка вес в граммах; для дележа счёта
# трактуем такую позицию как «1 порция за total».
MAX_COLUMN_QTY = 99


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
    for pattern in (QTY_UNIT_SIMPLE_RE, QTY_UNIT_RE):
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
    if len(amounts) >= 3 and 0 < amounts[0] <= MAX_COLUMN_QTY:
        q, unit, tot = amounts[0], amounts[1], amounts[-1]
        if abs(q * unit - tot) <= max(0.02 * tot, 1.0):
            return q, tot
    if 0 < quantity <= MAX_COLUMN_QTY and quantity == int(quantity):
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
    if total is None or total < 0:
        return None
    if not has_decimals(last_raw) and total < 10 and len(parts) < 2:
        return None

    quantity: float | None = None
    if len(parts) >= 2:
        raw = parts[-2].group()
        val = _parse_number(raw)
        if (
            val is not None
            and not has_decimals(raw)
            and 0 < val <= MAX_COLUMN_QTY
            and val == int(val)
        ):
            quantity = float(val)
    return quantity, float(total)


def _try_parse_item(
    text: str,
    score: float,
    qty_sum_layout: bool = False,
    numeric_group: _Row | None = None,
) -> ParsedItem | None:
    """Пытается разобрать строку как позицию чека."""
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
            if quantity is not None and quantity <= MAX_COLUMN_QTY:
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
            and quantity <= MAX_REASONABLE_QTY
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

    for entry in entries:
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

        if entry.row is not None and _is_total_line(normalized):
            numbers = _extract_numbers(TIME_PATTERN.sub(" ", text))
            if numbers:
                totals_seen = True
                for priority, pattern in enumerate(TOTAL_PATTERNS):
                    if pattern.search(normalized):
                        candidate = numbers[-1]
                        if 0 < candidate <= MAX_REASONABLE_PRICE:
                            total_candidates.append((priority, candidate))
                            if priority < total_priority:
                                total = candidate
                                total_priority = priority
                        break
            else:
                # Метка без числа: сумма могла уехать на отдельную строку.
                for priority, pattern in enumerate(TOTAL_PATTERNS):
                    if pattern.search(normalized):
                        pending_total_priority = priority
                        break
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

        cleaned = _strip_line_noise(text)
        if cleaned and cleaned != text:
            text = cleaned
            normalized = _normalize_keywords(text)

        if _is_excluded(text):
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
                if MODIFIER_NAME_RE.match(name) and entry.group is None:
                    # «Соус Сырный» без цены — модификатор, не позиция.
                    continue
                # Продолжение многострочного названия предыдущей позиции
                # (колоночная раскладка: числа стоят у первой строки названия).
                # Строки с прикреплённой числовой группой — всегда кандидаты
                # в позиции, их нельзя приклеивать к чужому названию.
                if (
                    header_entry is not None
                    and entry.group is None
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
                if not _extract_numbers(text) and _has_letters(text, minimum=3):
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
                    if merchant is None and text_line_index <= 3 and not items:
                        merchant = name
                    pending_name = (name, entry.score)
                    continue

        pending_name = None

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
                total = None
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
