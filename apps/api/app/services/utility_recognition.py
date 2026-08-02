"""Распознавание коммунальных документов: из текста бумажки — в структуру, без догадок.

ЧТО ЭТО. Детерминированные парсеры двух документов, которые приходят от коммунальщиков каждый
месяц: счёт Водоканала и акт приёма-передачи электроэнергии. Раньше они жили в локальном
Telegram-боте приёмки платежей и там же были обкатаны на живых фотографиях счетов; сюда
перенесена ровно распознающая часть.

ПОЧЕМУ БЕЗ LLM. Суммы к оплате нельзя доверять модели, которая может ошибиться незаметно:
цена ошибки — платёж не на ту сумму и расхождение с поставщиком. Регулярки ошибаются иначе —
они либо находят структуру, либо честно не находят её, и тогда документ уходит человеку с
причиной в ``reasons``. Пустое поле здесь всегда лучше правдоподобного числа.

МОДУЛЬ ЧИСТЫЙ. На входе — текст (уже распознанный чем-то извне) и словарь метаданных, на
выходе — структура. Ни OCR, ни файлов, ни сети, ни базы: всё, что модуль знает о мире, лежит
в переданной строке. Отсюда и дешёвые тесты — золотые фикстуры с реальным OCR-выхлопом лежат
в ``tests/fixtures/utility``.

ГЛАВНОЕ ПРО ВОДУ — СТРОКИ 4-7. Счёт Волгодонского Водоканала выставляется на физлицо
(договор оформлен на владельца) и содержит СЕМЬ строк: 1-3 — перерасчёт за прошлый период,
4-7 — начисление за текущий. Бизнес платит только текущий период, поэтому сумма к оплате
берётся строго как сумма строк 4-7, а НЕ из «Всего к оплате» внизу счёта: та включает
перерасчёт и завышает платёж. Правило жёсткое: если хотя бы одна из строк 4-7 не собралась,
модуль отдаёт ``amount = None`` с причиной, а не подставляет итог документа.

ГЛАВНОЕ ПРО ЭЛЕКТРИЧЕСТВО — ПАРА «ФАКТ + АВАНС». Энергетик присылает два акта за один визит:
фактический акт за прошедший месяц (электроэнергия + потери, минус ранее внесённый аванс —
остаётся «К оплате») и авансовый акт за следующий месяц. Это разные вещи: расход периода
признаётся по фактическому акту целиком (``electricity_period_amount``), а платить надо
остаток по факту плюс новый аванс. Поэтому парсер сначала определяет вид акта, и только потом
считает суммы — перепутать их значит либо задвоить расход, либо заплатить лишнее.

ОБА АКТА ПРИХОДЯТ ОДНИМ ФАЙЛОМ (подтверждено владельцем 02.08.2026), а вид акта определяется
для всего текста сразу — поэтому на склейке парсер видел только первый и молча терял второй,
причём с высокой уверенностью. Отсюда ``split_utility_documents``: файл режется по заголовкам
актов, и каждый кусок разбирается своим проходом. Точка входа для этого случая —
``recognize_utility_documents`` (множественная); одиночная осталась для одного документа.

ЧЕГО ЗДЕСЬ НЕТ. Денежной сшивки пары в один платёж: два акта — два документа с разными
периодами, а собирает их в один банковский черновик слой выше. Здесь только разбор бумаги.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from app.services import clock

__all__ = [
    "ElectricityActParser",
    "UtilityRecognition",
    "WaterUtilityInvoiceParser",
    "recognize_utility_document",
    "recognize_utility_documents",
    "split_utility_documents",
]

# Ниже этого порога распознавание считается ненадёжным: вызывающий код показывает документ
# человеку целиком, а не подставляет суммы в платёж. Порог унаследован от бота, где он был
# подобран по живому потоку загрузок.
LOW_CONFIDENCE_THRESHOLD = 0.65

# Поля, без которых из распознанного не собрать платёжку. Их заполненность формирует треть
# итоговой уверенности — детекция говорит «что это за документ», полнота говорит «хватит ли
# этого, чтобы действовать».
PAYMENT_READY_FIELDS = (
    "counterparty_name",
    "inn",
    "bank_bik",
    "bank_account",
    "amount",
    "payment_purpose_suggestion",
)

MONTHS_RU = {
    "январь": 1,
    "января": 1,
    "февраль": 2,
    "февраля": 2,
    "март": 3,
    "марта": 3,
    "апрель": 4,
    "апреля": 4,
    "май": 5,
    "мая": 5,
    "июнь": 6,
    "июня": 6,
    "июль": 7,
    "июля": 7,
    "август": 8,
    "августа": 8,
    "сентябрь": 9,
    "сентября": 9,
    "октябрь": 10,
    "октября": 10,
    "ноябрь": 11,
    "ноября": 11,
    "декабрь": 12,
    "декабря": 12,
}

DATE_PATTERN = r"\d{1,2}[.,\-/]\d{1,2}[.,\-/]\d{2,4}|\d{4}-\d{2}-\d{2}"


@dataclass(frozen=True)
class DetectionResult:
    """Результат опознания семейства документа: чем выше ``confidence``, тем увереннее."""

    parser_name: str
    source_type: str
    confidence: float
    reasons: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "parser_name": self.parser_name,
            "source_type": self.source_type,
            "confidence": round(self.confidence, 3),
            "reasons": list(self.reasons),
        }


# ---------------------------------------------------------------------------
# Нормализация текста, денег и дат
# ---------------------------------------------------------------------------


def clean_digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def normalize_text(value: str) -> str:
    """Приводит OCR-выхлоп к виду, в котором работают все остальные регулярки.

    ``№`` заменяется на ``N`` намеренно: распознаватели читают этот знак десятком разных
    способов (``No``, ``Nº``, ``N°``), и держать вариативность в каждой регулярке дороже, чем
    свести её к одному символу здесь.
    """
    text = value.replace("\xa0", " ")
    text = text.replace("№", "N")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def normalize_key(value: str) -> str:
    """Ключ для сравнения слов: регистр, ``ё`` и пробелы не должны решать ничего."""
    text = normalize_text(value).casefold()
    text = text.replace("ё", "е")
    return re.sub(r"\s+", " ", text)


def money(value: Any) -> Decimal:
    """Деньги из строки OCR. Мусор превращается в 0 — это ЗНАЧИМОЕ значение.

    Ноль здесь означает «числа нет», и вызывающий код везде проверяет ``> 0`` прежде чем
    что-то с суммой делать. Бросать исключение нельзя: в тексте счёта десятки чисел, часть
    из которых заведомо нечитаема, и разбор не должен падать из-за соседней ячейки таблицы.
    Отдельно чинится OCR-подмена разделителя копеек: ``1234:56`` и ``1234_56`` — это рубли.
    """
    if isinstance(value, Decimal):
        return value
    if value is None or value == "":
        return Decimal("0")
    text = str(value).strip()
    text = text.replace("\xa0", " ").replace(" ", "").replace(",", ".")
    text = re.sub(r"(?<=\d)[:_](?=\d{2}\D*$)", ".", text)
    text = re.sub(r"[^0-9.\-]", "", text)
    if text.count(".") > 1:
        parts = text.split(".")
        text = "".join(parts[:-1]) + "." + parts[-1]
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return Decimal("0")


def fmt_money(value: Any) -> str:
    amount = money(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return str(amount)


def normalize_date(value: str) -> str:
    """Дата к виду ``ДД.ММ.ГГГГ``. Точку OCR часто читает как запятую или дефис — принимаем все."""
    text = (value or "").strip()
    if not text:
        return ""
    match = re.fullmatch(r"(\d{1,2})[.,\-/](\d{1,2})[.,\-/](\d{2}|\d{4})", text)
    if match:
        day, month, year = match.groups()
        if len(year) == 2:
            year = f"20{year}"
        return f"{int(day):02d}.{int(month):02d}.{year}"
    match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", text)
    if match:
        year, month, day = match.groups()
        return f"{day}.{month}.{year}"
    return text


def iso_date_from_ru(value: str) -> str:
    date_value = normalize_date(value)
    if re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", date_value):
        day, month, year = date_value.split(".")
        return f"{year}-{month}-{day}"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value
    return ""


def month_bounds(year: int, month: int) -> tuple[str, str]:
    start = date(year, month, 1)
    end = date(year, 12, 31) if month == 12 else date(year, month + 1, 1) - timedelta(days=1)
    return start.isoformat(), end.isoformat()


def first_match(text: str, patterns: list[str], flags: int = re.IGNORECASE | re.MULTILINE) -> str:
    for pattern in patterns:
        match = re.search(pattern, text, flags)
        if match:
            for group in match.groups():
                if group:
                    return group.strip()
    return ""


# ---------------------------------------------------------------------------
# Реквизиты: ИНН/КПП/БИК, счета, банк, контрагент, документ, НДС, период
# ---------------------------------------------------------------------------


def find_labeled_digits(text: str, labels: list[str], lengths: tuple[int, ...]) -> str:
    """Число заданной длины рядом с меткой (``ИНН``, ``КПП``, ``БИК``).

    Длина — единственный надёжный признак: OCR роняет и добавляет пробелы, но не меняет
    количество цифр. Если рядом с меткой ничего подходящего нет, берётся первое в тексте
    число нужной длины — на счетах это почти всегда искомый реквизит.
    """
    max_len = max(lengths)
    min_len = min(lengths)
    for label in labels:
        pattern = rf"{label}\D{{0,50}}(\d[\d\s\-]{{{min_len - 1},{max_len + 10}}})"
        for match in re.finditer(pattern, text, re.IGNORECASE):
            digits = clean_digits(match.group(1))
            if len(digits) in lengths:
                return digits
    alternatives = "|".join(rf"\d{{{length}}}" for length in lengths)
    fallback = re.search(rf"\b({alternatives})\b", text)
    return fallback.group(1) if fallback else ""


ACCOUNT_TOKEN_INNER = r"\d[\d \-]{18,28}\d"
ACCOUNT_TOKEN_RE = re.compile(rf"(?<!\d)({ACCOUNT_TOKEN_INNER})(?!\d)")


def _strip_ocr_table_prefix(digits: str, valid_lengths: set[int]) -> str:
    """Снимает лишнюю ведущую единицу — след вертикальной границы таблицы.

    Tesseract регулярно приклеивает ``|`` границы ячейки к началу числа как ``1``, и счёт
    из 20 цифр становится 21-значным. Чинить это можно только при однозначной длине: если
    число длиннее допустимого ровно на одну цифру и эта цифра — ``1``, она лишняя. Во всех
    остальных случаях возвращается пустая строка: угадывать номер счёта нельзя.
    """
    if len(digits) in valid_lengths:
        return digits
    if len(digits) - 1 in valid_lengths and digits[0] == "1":
        return digits[1:]
    return ""


def _find_account_in_text(
    text: str,
    *,
    labels: list[str],
    valid_lengths: set[int],
    exclude_before: tuple[str, ...] = (),
    must_start_with: str = "",
    must_not_start_with: tuple[str, ...] = (),
) -> str:
    for label in labels:
        pattern = rf"{label}\D{{0,50}}({ACCOUNT_TOKEN_INNER})"
        for match in re.finditer(pattern, text, re.IGNORECASE):
            before = text[max(0, match.start() - 24) : match.start()].casefold()
            if any(needle in before for needle in exclude_before):
                continue
            digits = clean_digits(match.group(1))
            candidate = _strip_ocr_table_prefix(digits, valid_lengths)
            if not candidate:
                continue
            if must_start_with and not candidate.startswith(must_start_with):
                continue
            if any(candidate.startswith(prefix) for prefix in must_not_start_with):
                continue
            return candidate

    for match in ACCOUNT_TOKEN_RE.finditer(text):
        digits = clean_digits(match.group(1))
        candidate = _strip_ocr_table_prefix(digits, valid_lengths)
        if not candidate:
            continue
        before = text[max(0, match.start() - 32) : match.start()].casefold()
        if any(needle in before for needle in exclude_before):
            continue
        if must_start_with and not candidate.startswith(must_start_with):
            continue
        if any(candidate.startswith(prefix) for prefix in must_not_start_with):
            continue
        return candidate
    return ""


PAYEE_ACCOUNT_LABELS = [
    r"р\/с",
    r"р\.?\s*с\.?",
    r"расч[её]тн(?:ый|ого)?\s+сч[её]т",
    r"сч[её]т\s+получателя",
    r"банковск(?:ий|ого)\s+сч[её]т",
    r"номер\s+сч[её]та",
    r"payee\s+account",
    r"(?:Сч|Cu|Сy|Су|Cч)\.?\s*(?:N|№|No|Nº|°|o)",
]

CORR_ACCOUNT_LABELS = [
    r"к\/с",
    r"к\.?\s*с\.?",
    r"кор\.?\s*сч[её]т",
    r"корреспондентск(?:ий|ого)\s+сч[её]т",
]


def find_bank_account(text: str) -> str:
    """Расчётный счёт получателя. Корсчёт (начинается на 301) отсекается явно.

    На счёте оба номера стоят рядом и одинаковой длины, поэтому перепутать их легко —
    а платёж уйдёт в никуда.
    """
    return _find_account_in_text(
        text,
        labels=PAYEE_ACCOUNT_LABELS,
        valid_lengths={20, 22},
        exclude_before=("к/с", "кор"),
        must_not_start_with=("301",),
    )


def find_corr_account(text: str) -> str:
    return _find_account_in_text(
        text,
        labels=CORR_ACCOUNT_LABELS,
        valid_lengths={20},
        must_start_with="301",
    )


def find_bank_name(text: str) -> str:
    patterns = [
        r"(?:Банк получателя|Банк)\s*[:\-]\s*([^\n\r]+)",
        r"(?:в банке)\s+([^\n\r]+)",
        r"(?:Получатель.*?Банк)\s+([^\n\r]+)",
        r"^([^\n\r]*(?:Банк|Банка)[^\n\r]+?)\s+БИК\b",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE | re.MULTILINE):
            labeled = next((group.strip() for group in match.groups() if group), "")
            candidate = re.split(
                r"\s+(?:БИК|к\/с|к\.с\.|кор\.?\s*сч[её]т)\b",
                labeled,
                maxsplit=1,
                flags=re.IGNORECASE,
            )[0].strip(" ,.;")
            if candidate and not re.match(r"^(?:сч\.?\s*N?|\d)", candidate, re.IGNORECASE):
                return candidate
    lines = [re.sub(r"\s+", " ", line).strip(" ,.;") for line in text.splitlines()]
    for index, line in enumerate(lines[:-1]):
        if (
            line
            and re.search(r"\bБИК\b", lines[index + 1], re.IGNORECASE)
            and re.search(r"\b(?:Банк|Банка)\b", line, re.IGNORECASE)
            and normalize_key(line) != "банк получателя"
        ):
            return line
    return ""


def find_amount(text: str) -> str:
    """Итоговая сумма документа по меткам «к оплате» / «итого».

    Для воды это значение — лишь черновик: настоящая сумма считается по строкам 4-7 и
    перезаписывает найденное здесь (см. ``WaterUtilityInvoiceParser.parse``). Строки со
    словом «НДС» пропускаются, иначе за итог легко принять налог.
    """
    patterns = [
        r"(?:итого\s+к\s+оплате|всего\s+к\s+оплате|к\s+оплате|сумма\s+к\s+оплате)"
        r"\D{0,50}([0-9][0-9\s]*[,.]\d{2}|[0-9][0-9\s]*)",
        r"(?:сумма\s+платежа|сумма\s+документа|сумма\s+ордера)"
        r"\D{0,50}([0-9][0-9\s]*[,.]\d{2}|[0-9][0-9\s]*)",
        r"(?:итог|итого|всего|total)\D{0,50}([0-9][0-9\s]*[,.]\d{2})",
    ]
    for pattern in patterns:
        values: list[Decimal] = []
        for match in re.finditer(pattern, text, re.IGNORECASE):
            line = text[max(0, match.start() - 80) : min(len(text), match.end() + 80)].casefold()
            if "ндс" in line and "итого" not in line and "всего" not in line:
                continue
            amount = money(match.group(1))
            if amount > 0:
                values.append(amount)
        if values:
            return fmt_money(max(values))
    return ""


def clean_name(value: str) -> str:
    text = re.sub(r"\s+", " ", value or "").strip(" ,.;")
    text = re.split(
        r"\s+(?:ИНН|КПП|БИК|р\/с|к\/с|сч[её]т|банк|адрес)\b",
        text,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    return text.strip(" ,.;")


def find_counterparty_name(text: str) -> str:
    for label in (r"Получатель", r"Поставщик", r"Исполнитель", r"Продавец"):
        labeled = first_match(
            text,
            [
                rf"{label}\s*[:\-]\s*([^\n\r]+)",
                rf"{label}\s+([^\n\r]+)",
            ],
        )
        if labeled and "банк" not in labeled.casefold():
            return clean_name(labeled)

    legal = first_match(
        text,
        [
            r"\b((?:ООО|АО|ПАО|ОАО|ЗАО)\s+[«\"A-ZА-ЯЁ0-9][^\n\r]{2,120})",
            r"\b(ИП\s+[А-ЯЁ][А-ЯЁа-яё\-\s]{4,120})",
        ],
    )
    return clean_name(legal)


def find_document(text: str) -> tuple[str, str, str]:
    """Тип, номер и дата документа. Для воды результат уточняется отдельным парсером."""
    patterns = [
        rf"(Сч[её]т(?:\s+на\s+оплату)?|Сч[её]т-договор)\s*(?:N|№|No)?"
        rf"\s*([A-Za-zА-Яа-я0-9/_\-]+)(?:\s*от\s*({DATE_PATTERN}))?",
        rf"(Сч[её]т(?:\s+на\s+оплату)?|Сч[её]т-договор|УПД|Товарная\s+накладная|Накладная|"
        rf"Акт)\s*(?:N|№|No)?\s*([A-Za-zА-Яа-я0-9/_\-]+)(?:\s*от\s*({DATE_PATTERN}))?",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
        if not match:
            continue
        groups = [group.strip() if group else "" for group in match.groups()]
        doc_type = re.sub(r"\s+", " ", groups[0]).strip() if groups else ""
        doc_number = groups[1] if len(groups) > 1 else ""
        doc_date = normalize_date(groups[2] if len(groups) > 2 else "")
        return doc_type, doc_number or "", doc_date
    return "", "", ""


def find_vat(text: str) -> tuple[str, str]:
    lower = text.casefold()
    if re.search(r"без\s+ндс|ндс\s+не\s+облага", lower):
        return "none", "0.00"
    match = re.search(
        r"(?:в\s+т\.?\s*ч\.?\s*)?ндс(?:\s*\d{1,2}%?)?\D{0,30}([0-9][0-9\s]*[,.]\d{2})",
        text,
        re.IGNORECASE,
    )
    if match:
        return "included", fmt_money(match.group(1))
    if "ндс" in lower:
        return "included_unknown_amount", ""
    return "", ""


def find_service_period(text: str) -> tuple[str, str, str]:
    """Период услуги: либо явный диапазон дат, либо «за <месяц> <год>».

    Период — не украшение: расход коммуналки признаётся в месяце потребления, а не в месяце
    оплаты. Если период не найден, документ обязан уйти человеку (``require_service_period``),
    а не быть проведённым «по дате счёта».
    """
    match = re.search(
        rf"(?:период|за\s+период)\D{{0,20}}(?:с\s*)?({DATE_PATTERN})\s*(?:по|\-)\s*({DATE_PATTERN})",
        text,
        re.IGNORECASE,
    )
    if match:
        label = f"{normalize_date(match.group(1))} - {normalize_date(match.group(2))}"
        return iso_date_from_ru(match.group(1)), iso_date_from_ru(match.group(2)), label

    month_names = "|".join(sorted(MONTHS_RU, key=len, reverse=True))
    match = re.search(
        rf"(?:за|период)\s+({month_names})(?:\s+месяц(?:а)?)?\s+(\d{{4}})\s*(?:г\.?)?",
        text,
        re.IGNORECASE,
    )
    if match:
        month_name = normalize_key(match.group(1))
        year = int(match.group(2))
        start, end = month_bounds(year, MONTHS_RU[month_name])
        return start, end, f"{match.group(1)} {year}"
    return "", "", ""


# ---------------------------------------------------------------------------
# Пруфы для человека
# ---------------------------------------------------------------------------


def mask_digits(value: str) -> str:
    """Прячет длинные числа в пруфах: человеку показывают строку счёта, а не номер счёта."""

    def repl(match: re.Match[str]) -> str:
        digits = clean_digits(match.group(0))
        if len(digits) in {20, 22}:
            return f"<account:...{digits[-4:]}>"
        if len(digits) in {10, 12}:
            return f"<inn:...{digits[-4:]}>"
        if len(digits) == 9:
            return f"<digits9:...{digits[-3:]}>"
        return f"<digits:{len(digits)}>"

    return re.sub(r"\b\d[\d\s\-]{8,29}\b", repl, value)


def evidence_line(text: str, pattern: str) -> str:
    match = re.search(pattern, text, re.IGNORECASE)
    if not match:
        return ""
    start = text.rfind("\n", 0, match.start()) + 1
    end = text.find("\n", match.end())
    if end == -1:
        end = min(len(text), match.end() + 120)
    line = re.sub(r"\s+", " ", text[start:end]).strip()
    return mask_digits(line[:180])


def build_evidence(text: str, basis: dict[str, Any]) -> list[dict[str, str]]:
    """Строки исходника, по которым взято каждое поле: человек должен проверять, а не верить."""
    candidates = [
        ("counterparty_name", r"Получатель|Поставщик|Исполнитель|Продавец|Пользователь"),
        ("inn", r"ИНН"),
        ("kpp", r"КПП"),
        ("bank_bik", r"БИК"),
        ("bank_account", r"р\/с|расч[её]тн|сч[её]т\s+получателя"),
        ("corr_account", r"к\/с|кор"),
        ("amount", r"итого|всего|к\s+оплате|сумма"),
        ("document", r"сч[её]т|упд|накладная|чек|плат[её]ж"),
        ("vat", r"ндс"),
        ("service_period", r"период|за\s+[а-яё]+\s+\d{4}"),
    ]
    evidence: list[dict[str, str]] = []
    for field, pattern in candidates:
        if field != "document" and not basis.get(field):
            continue
        snippet = evidence_line(text, pattern)
        if snippet:
            evidence.append(
                {"field": field, "kind": "regex", "pattern": pattern, "snippet": snippet}
            )
    return evidence[:12]


def build_default_purpose(basis: dict[str, Any]) -> str:
    doc_type = str(basis.get("document_type") or "документу")
    doc_number = str(basis.get("document_number") or "")
    doc_date = str(basis.get("document_date") or "")
    period_label = str(basis.get("service_period_label") or "")

    if doc_number:
        purpose = f"Оплата по {doc_type.lower()} N {doc_number}"
        if doc_date:
            purpose = f"{purpose} от {doc_date}"
    else:
        purpose = "Оплата по документу"

    if period_label:
        purpose = f"{purpose} за {period_label}"

    vat_mode = str(basis.get("vat_mode") or "")
    vat_amount = str(basis.get("vat_amount") or "")
    if vat_mode == "none":
        purpose = f"{purpose}. Без НДС"
    elif vat_amount:
        purpose = f"{purpose}. В т.ч. НДС {vat_amount}"
    elif vat_mode:
        purpose = f"{purpose}. НДС согласно документу"
    return re.sub(r"\s+", " ", purpose).strip()[:210]


def require_service_period(basis: dict[str, Any], reason: str = "service_period_not_found") -> None:
    """Без периода услуги документ не проводится сам — только через человека.

    Выдумать период по дате документа нельзя: счёт за январь приходит в феврале, и расход,
    посаженный в февраль, тихо искажает P&L обоих месяцев.
    """
    if basis.get("service_period_start") and basis.get("service_period_end"):
        return
    basis["requires_owner_review"] = True
    basis.setdefault("owner_review_reasons", []).append(reason)


# ---------------------------------------------------------------------------
# Вода: таблица счёта Водоканала и правило строк 4-7
# ---------------------------------------------------------------------------

WATER_UTILITY_SELECTED_ROWS = (4, 5, 6, 7)
MONEY_TOKEN_RE = re.compile(r"(?<!\d)(?:\d{1,3}(?:[ \xa0]\d{3})+|\d+)[,.:_]\d{2}(?!\d)")

# Порядок строк в счёте Водоканала постоянен: 1-3 — перерасчёт прошлого периода,
# 4-7 — текущий. Знание этого порядка позволяет вернуть строку на место, когда OCR
# потерял или переврал её номер.
WATER_ROW_LABEL_SEQUENCE = (
    "water_supply",
    "wastewater",
    "negative_impact",
    "water_supply",
    "wastewater",
    "negative_impact",
    "pollutants_discharge",
)
WATER_FIRST_ROW_LABEL = WATER_ROW_LABEL_SEQUENCE[0]


def _expected_water_row_for_label(rows: dict[int, str], label: str) -> int | None:
    """Первый незанятый номер строки 1..7, чей ожидаемый заголовок совпал с найденным."""
    if not label:
        return None
    for index, expected_label in enumerate(WATER_ROW_LABEL_SEQUENCE, start=1):
        if expected_label == label and index not in rows:
            return index
    return None


def water_row_label(text: str) -> str:
    lower = normalize_key(text)
    if "сброс" in lower and "загрязн" in lower:
        return "pollutants_discharge"
    if "негатив" in lower and "воздейств" in lower:
        return "negative_impact"
    if "водоотвед" in lower:
        return "wastewater"
    if "водоснаб" in lower:
        return "water_supply"
    return ""


WATER_MONEY_TOKEN_RE = re.compile(
    r"(?<!\d)(?:\d{1,3}(?:[ \xa0]\d{3})+|\d+)[,.:_]\d{2}(?!\d)|(?<!\d)\d{4,7}(?!\d)"
)


def water_money_token(value: str) -> Decimal:
    """Число из ячейки таблицы, где OCR мог съесть разделитель копеек.

    ``123456`` в денежной колонке — это ``1234.56``: у счёта фиксированная разрядность,
    и потерянная точка встречается на каждом втором скане.
    """
    token = str(value or "").strip(" [](),.;")
    if re.fullmatch(r"\d{4,7}", token):
        token = f"{token[:-2]}.{token[-2:]}"
    return money(token)


def water_money_values(text: str) -> list[Decimal]:
    values: list[Decimal] = []
    for token in WATER_MONEY_TOKEN_RE.findall(text):
        amount = water_money_token(token)
        if amount > 0:
            values.append(amount)
    return values


def money_tokens(text: str) -> list[Decimal]:
    return [money(token) for token in MONEY_TOKEN_RE.findall(text)]


def money_close(left: Decimal, right: Decimal, tolerance: Decimal = Decimal("0.02")) -> bool:
    return abs(left - right) <= tolerance


def plausible_vat_ratio(base: Decimal, vat: Decimal) -> bool:
    """НДС правдоподобен, если он 5-35 % базы: 20 % с запасом на округления и льготы."""
    if base <= 0 or vat < 0:
        return False
    ratio = vat / base
    return Decimal("0.05") <= ratio <= Decimal("0.35")


def shift_by_thousands(
    value: Decimal,
    target: Decimal,
    tolerance: Decimal = Decimal("0.20"),
) -> Decimal | None:
    """Подбирает потерянную или лишнюю тысячу: OCR теряет ведущую цифру разряда.

    Сдвиг допускается, только если после него равенство «база + НДС = итог» сходится с
    точностью до копеек — то есть у починки есть независимое подтверждение внутри строки.
    """
    if value <= 0 or target <= 0:
        return None
    best: Decimal | None = None
    best_delta: Decimal | None = None
    for step in range(-9, 10):
        candidate = value + Decimal(step * 1000)
        if candidate <= 0:
            continue
        delta = abs(candidate - target)
        if delta <= tolerance and (best_delta is None or delta < best_delta):
            best = candidate
            best_delta = delta
    return best


def water_row_amounts(text: str) -> tuple[str, str, str]:
    """Три последних числа строки — база, НДС и итог. Расхождение чинится по их же связи."""
    values = water_money_values(text)
    if len(values) < 3:
        return "", "", ""
    base, vat, total = values[-3], values[-2], values[-1]
    if not money_close(base + vat, total):
        shifted_base = shift_by_thousands(base, total - vat)
        shifted_total = shift_by_thousands(total, base + vat)
        derived_vat = total - base

        if shifted_base is not None and plausible_vat_ratio(shifted_base, vat):
            base = shifted_base
        elif shifted_total is not None and plausible_vat_ratio(base, vat):
            total = shifted_total
        elif total < base and plausible_vat_ratio(base, vat):
            total = base + vat
        elif derived_vat > 0 and plausible_vat_ratio(base, derived_vat):
            vat = derived_vat
    return fmt_money(base), fmt_money(vat), fmt_money(total)


def collect_numbered_table_rows(text: str) -> dict[int, str]:
    """Собирает пронумерованные строки таблицы 1..7, вытаскивая их из OCR-шума.

    Три починки, каждая появилась из живого счёта: перенос строки-продолжения к своей строке;
    номер ``6``, прочитанный как ``©`` или как дубль ``5``; строка вовсе без номера — её
    возвращают на место по заголовку и ожидаемому порядку ``WATER_ROW_LABEL_SEQUENCE``.
    """
    rows: dict[int, str] = {}
    current: int | None = None
    stop_pattern = re.compile(
        r"^(?:итого|сумма\s+ндс|всего\s+с\s+ндс|задолженность|начислено|перерасч[её]ты|"
        r"оплачено|всего\s+к\s+оплате)\b",
        re.IGNORECASE,
    )
    header_pattern = re.compile(r"^(?:N|№|товары|кол-во|ед\.?|цена|сумма)\b", re.IGNORECASE)
    # Строка, начинающаяся сразу с заголовка позиции, — верный признак того, что OCR потерял
    # её номер. Приклеивать такую строку к предыдущей позиции нельзя.
    row_marker_pattern = re.compile(
        r"^[^\w\dА-Яа-яЁё]+\s*(?:\[)?\s*(Водо|Плата\s+за\s+нег|Сброс\s+загрязн|Acт)",
        re.IGNORECASE,
    )

    for raw_line in text.splitlines():
        line = re.sub(r"\s+", " ", raw_line.replace("|", " ")).strip()
        if not line:
            continue
        if stop_pattern.search(line):
            current = None
            continue

        match = re.match(r"^[^\w\d]*(\d{1,2})\s*[\[\]_'`.,:;|()-]*\s*(.*)$", line)
        if match:
            row_number = int(match.group(1))
            row_text = match.group(2).strip()
            candidate_label = water_row_label(row_text)
            # OCR читает `6` как `5` — номер предыдущей строки. Если пятая уже занята,
            # а заголовок говорит «негативное воздействие», это шестая.
            if (
                row_number == 5
                and row_number in rows
                and 6 not in rows
                and candidate_label == "negative_impact"
            ):
                row_number = 6
            if row_number == 0 or row_number > 7:
                current = None
                continue
            existing_label = water_row_label(rows.get(row_number, ""))
            if existing_label and (not candidate_label or candidate_label != existing_label):
                current = None
                continue
            inline_row_6 = re.search(r"\s+[@©Cc][\W_]+(.+)$", row_text)
            if inline_row_6 and re.search(r"негативн", inline_row_6.group(1), re.IGNORECASE):
                rows[row_number] = row_text[: inline_row_6.start()].strip()
                rows[6] = inline_row_6.group(1).strip()
                current = 6
            else:
                rows[row_number] = row_text
                current = row_number
            continue

        # Строка без ведущего номера, но с заголовком позиции и тремя денежными колонками —
        # это потерянная строка таблицы. Ставим её на первое подходящее свободное место.
        candidate_label = water_row_label(line)
        if candidate_label and len(water_money_values(line)) >= 3:
            inferred_row = _expected_water_row_for_label(rows, candidate_label)
            # Первую строку восстанавливаем только в начале таблицы: иначе случайная
            # сноска с тем же заголовком встанет на её место.
            if inferred_row == 1 and current not in (None, 1):
                inferred_row = None
            if inferred_row is not None:
                stripped = re.sub(r"^[^\wа-яёА-ЯЁ]+", "", line).strip()
                rows[inferred_row] = stripped or line
                current = inferred_row
                continue

        if (
            current is not None
            and not header_pattern.search(line)
            and not row_marker_pattern.search(line)
        ):
            rows[current] = f"{rows[current]} {line}".strip()

    return rows


def water_invoice_summary_amount(text: str, label_pattern: str) -> str:
    pattern = rf"{label_pattern}\s*[:\-]?\s*([0-9][0-9 \xa0]*(?:[,.]\d{{2}})?)"
    for match in re.finditer(pattern, text, re.IGNORECASE):
        amount = money(match.group(1))
        if amount > 0:
            return fmt_money(amount)
    return ""


def water_invoice_total_with_vat(text: str) -> str:
    return water_invoice_summary_amount(text, r"Всего\s+с\s+(?:НДС|НД\.?|HAC|HAС)")


def water_invoice_vat_total(text: str) -> str:
    return water_invoice_summary_amount(text, r"Сумма\s+(?:НДС|НД\.?|HAC|HAС)")


def _column_after_header(text: str, header_pattern: str, count: int) -> list[Decimal]:
    """Первые ``count`` денежных чисел после заголовка колонки, стоящего на отдельной строке.

    Разбор колоночного OCR: macOS Vision выдаёт таблицу не по строкам, а по столбцам.
    """
    header_match = re.search(
        rf"^[ \t]*{header_pattern}[ \t]*$",
        text,
        re.IGNORECASE | re.MULTILINE,
    )
    if not header_match:
        return []
    tail = text[header_match.end() :]
    stop = re.search(
        r"^[ \t]*(?:Кол-во|Ед\.?|Цена|Сумма(?:\s+(?:НДС|с\s+НДС))?|Всего|Итого|Задолженность|"
        r"Начислено|Оплачено|Перерасч|по\s+счету)\b",
        tail,
        re.IGNORECASE | re.MULTILINE,
    )
    section = tail[: stop.start()] if stop else tail
    values: list[Decimal] = []
    for token in WATER_MONEY_TOKEN_RE.findall(section):
        amount = water_money_token(token)
        if amount <= 0:
            continue
        values.append(amount)
        if len(values) >= count:
            break
    return values


def parse_water_column_major_rows(text: str) -> dict[int, dict[str, Decimal]]:
    """Восстанавливает строки 1-7 из колоночного OCR (типично для macOS Vision).

    Каждая денежная колонка идёт как заголовок на своей строке и ровно семь чисел под ним —
    по одному на строку счёта. Если подписи такой формы нет, возвращается пустой словарь:
    тогда вызывающий продолжит пробовать другие стратегии.
    """
    totals = _column_after_header(text, r"Сумма\s+с\s+(?:НДС|HAC|HAС)", 7)
    vats = _column_after_header(text, r"Сумма\s+(?:НДС|HAC|HAС)", 7)
    if len(totals) != 7 or len(vats) != 7:
        return {}
    bases = _column_after_header(text, r"Сумма", 7)
    if len(bases) != 7:
        bases = [Decimal("0")] * 7
    return {
        index + 1: {
            "base": bases[index],
            "vat": vats[index],
            "total": totals[index],
        }
        for index in range(7)
    }


def _reference_row_wins(row: dict[str, Any], reference: dict[str, Any]) -> bool:
    """Верить ли «сестре» строки из блока перерасчёта вместо самой строки 4-7.

    Строки 1-3 и 4-7 — одни и те же услуги за соседние периоды, суммы у них близкие.
    Поэтому расхождение втрое означает не изменившееся потребление, а сбой OCR в одной из
    ячеек. Второй случай тоньше: суммы сошлись до рубля, но НДС строки 4-7 неправдоподобен,
    а у её сестры — правдоподобен; тогда сестра и есть верно прочитанная копия.
    """
    if row["total"] > reference["total"] * Decimal("3"):
        return True
    return money_close(row["total"], reference["total"], Decimal("1.00")) and plausible_vat_ratio(
        reference["base"], reference["vat"]
    )


def water_selected_row_amounts(text: str) -> tuple[str, str, list[int]]:
    """Сумма и НДС по строкам 4-7 — основной путь.

    Возвращает пустые суммы, если собрались НЕ все четыре строки: частичная сумма хуже
    отсутствия, потому что выглядит как настоящая. Дополнительно строка 4-7 сверяется со
    своей «сестрой» из блока перерасчёта (1-3): если OCR раздул её втрое или, наоборот,
    строки совпали до рубля при неправдоподобном НДС, берётся более достоверная из пары.
    """
    rows = collect_numbered_table_rows(text)
    parsed: dict[int, dict[str, Any]] = {}
    first_by_label: dict[str, dict[str, Any]] = {}

    for row_number, row_text in rows.items():
        base, vat, total = water_row_amounts(row_text)
        label = water_row_label(row_text)
        if not total:
            continue
        row = {
            "label": label,
            "base": money(base),
            "vat": money(vat),
            "total": money(total),
        }
        parsed[row_number] = row
        if label and label not in first_by_label:
            first_by_label[label] = row

    selected_rows: list[int] = []
    total = Decimal("0")
    vat_total = Decimal("0")
    for row_number in WATER_UTILITY_SELECTED_ROWS:
        row = parsed.get(row_number)
        if not row:
            continue
        reference = first_by_label.get(str(row.get("label") or ""))
        if reference and reference is not row and _reference_row_wins(row, reference):
            row = reference
        selected_rows.append(row_number)
        total += row["total"]
        vat_total += row["vat"]

    if selected_rows != list(WATER_UTILITY_SELECTED_ROWS):
        return "", "", selected_rows
    if all(row_number in parsed for row_number in range(1, 8)):
        # Если разобрались все семь строк, итог документа даёт независимую сверку:
        # строки 4-7 = «Всего с НДС» − строки 1-3. Расхождение в пределах допуска
        # означает, что OCR слегка переврал колонку, и точнее верить арифметике счёта.
        invoice_total = money(water_invoice_total_with_vat(text))
        parsed_invoice_total = sum(parsed[row_number]["total"] for row_number in range(1, 8))
        invoice_total_matches_rows = (
            invoice_total > 0 and abs(parsed_invoice_total - invoice_total) <= Decimal("5.00")
        )
        if invoice_total_matches_rows:
            previous_total = sum(parsed[row_number]["total"] for row_number in (1, 2, 3))
            selected_by_invoice_total = invoice_total - previous_total
            if (
                selected_by_invoice_total > 0
                and abs(selected_by_invoice_total - total) <= Decimal("100.00")
            ):
                total = selected_by_invoice_total

        invoice_vat = money(water_invoice_vat_total(text))
        if invoice_total_matches_rows and invoice_vat > 0:
            previous_vat = sum(parsed[row_number]["vat"] for row_number in (1, 2, 3))
            selected_vat_by_invoice_total = invoice_vat - previous_vat
            if (
                selected_vat_by_invoice_total > 0
                and abs(selected_vat_by_invoice_total - vat_total) <= Decimal("20.00")
            ):
                vat_total = selected_vat_by_invoice_total
    return fmt_money(total), fmt_money(vat_total), selected_rows


def _parse_water_row_totals(text: str) -> dict[int, dict[str, Decimal]]:
    parsed: dict[int, dict[str, Decimal]] = {}
    for row_number, row_text in collect_numbered_table_rows(text).items():
        base, vat, total = water_row_amounts(row_text)
        if not total:
            continue
        parsed[row_number] = {
            "base": money(base),
            "vat": money(vat),
            "total": money(total),
        }
    return parsed


def _derive_selected_from_invoice_totals(
    text: str,
    parsed: dict[int, dict[str, Decimal]],
) -> tuple[str, str, list[int]]:
    """Запасной путь: строки 4-7 = «Всего с НДС» − строки 1-3.

    Работает, когда блок перерасчёта (1-3) распознан целиком, а текущие строки — нет.
    Это по-прежнему НЕ итог документа: перерасчёт вычтен явно. Если хоть одна из
    распознанных строк 4-7 не влезает в полученную разницу, путь считается неприменимым.
    """
    invoice_total = money(water_invoice_total_with_vat(text))
    if invoice_total <= 0:
        return "", "", []
    if not all(row_number in parsed for row_number in (1, 2, 3)):
        return "", "", []

    previous_total = sum((parsed[rn]["total"] for rn in (1, 2, 3)), Decimal("0"))
    derived_total = invoice_total - previous_total
    if derived_total <= 0:
        return "", "", []
    known_4_7 = sum(
        (parsed[rn]["total"] for rn in WATER_UTILITY_SELECTED_ROWS if rn in parsed),
        Decimal("0"),
    )
    if known_4_7 > derived_total + Decimal("1.00"):
        return "", "", []

    derived_vat = Decimal("0")
    invoice_vat = money(water_invoice_vat_total(text))
    if invoice_vat > 0:
        previous_vat = sum((parsed[rn]["vat"] for rn in (1, 2, 3)), Decimal("0"))
        candidate_vat = invoice_vat - previous_vat
        if candidate_vat > 0:
            derived_vat = candidate_vat

    return fmt_money(derived_total), fmt_money(derived_vat), list(WATER_UTILITY_SELECTED_ROWS)


def _selected_from_column_major(text: str) -> tuple[str, str, list[int]]:
    """Строки 4-7 из колоночного OCR. Пусто, если признаков колоночной раскладки нет."""
    parsed = parse_water_column_major_rows(text)
    if not parsed:
        return "", "", []
    total = sum((parsed[rn]["total"] for rn in WATER_UTILITY_SELECTED_ROWS), Decimal("0"))
    vat_total = sum((parsed[rn]["vat"] for rn in WATER_UTILITY_SELECTED_ROWS), Decimal("0"))
    if total <= 0:
        return "", "", []
    return fmt_money(total), fmt_money(vat_total), list(WATER_UTILITY_SELECTED_ROWS)


def water_utility_selected_row_totals(text: str) -> tuple[str, str, list[int]]:
    """Сумма к оплате по счёту Водоканала — четыре стратегии, от точной к грубой.

    Порядок важен: сначала честно собранные строки 4-7, затем вычитание блока перерасчёта
    из итога документа, затем колоночный OCR, и лишь в конце — грубый разбор строк по трём
    последним числам. Ни одна из стратегий не подставляет «Всего к оплате» как сумму:
    итог включает перерасчёт прошлого периода, который бизнес не платит.
    """
    selected_amount, selected_vat, selected_rows = water_selected_row_amounts(text)
    if selected_amount:
        return selected_amount, selected_vat, selected_rows

    parsed = _parse_water_row_totals(text)
    derived_amount, derived_vat, derived_rows = _derive_selected_from_invoice_totals(text, parsed)
    if derived_amount:
        return derived_amount, derived_vat, derived_rows

    column_amount, column_vat, column_rows = _selected_from_column_major(text)
    if column_amount:
        return column_amount, column_vat, column_rows

    rows = collect_numbered_table_rows(text)
    fallback_rows: list[int] = []
    fallback_total = Decimal("0")
    fallback_vat_total = Decimal("0")
    for row_number in WATER_UTILITY_SELECTED_ROWS:
        values = money_tokens(rows.get(row_number, ""))
        if len(values) < 3:
            continue
        fallback_rows.append(row_number)
        fallback_vat_total += values[-2]
        fallback_total += values[-1]
    if fallback_rows != list(WATER_UTILITY_SELECTED_ROWS):
        return "", "", selected_rows or fallback_rows
    return fmt_money(fallback_total), fmt_money(fallback_vat_total), fallback_rows


_PARTY_LABEL_PREFIX_RE = re.compile(
    r"^\s*(?:Покупатель|Поставщик|Получатель|Плательщик|Продавец|Исполнитель)\s*[:\-]?\s*",
    re.IGNORECASE,
)


def find_water_utility_counterparty_name(text: str) -> str:
    """Имя поставщика воды. Покупатель (физлицо-владелец) намеренно отсекается.

    Договор с Водоканалом оформлен на физлицо, поэтому в шапке счёта две стороны подряд, и
    общий поиск контрагента иногда цепляет покупателя вместо поставщика.
    """
    match = re.search(
        r"^\s*Поставщик\s*:?\s*(.+?)"
        r"(?=\n\s*(?:Покупатель|Договор\s*/|Договор|N\s+Товары|№\s+Товары)|\Z)",
        text,
        re.IGNORECASE | re.DOTALL | re.MULTILINE,
    )
    if match:
        candidate = clean_name(match.group(1))
        while True:
            stripped = _PARTY_LABEL_PREFIX_RE.sub("", candidate, count=1).strip()
            if stripped == candidate:
                break
            candidate = stripped
        if candidate and "журенков" not in candidate.casefold():
            return candidate
    match = re.search(
        r"((?:Муниципальное|МУП)\s+унитарное\s+предприятие[^\n\r]*?Водоканал[\"”])",
        text,
        re.IGNORECASE,
    )
    if match:
        return clean_name(match.group(1))
    return ""


def find_water_utility_document(text: str) -> tuple[str, str, str]:
    """Номер и дата счёта.

    ``N`` часто приходит с хвостом ``º``/``°``/``o`` — распознаватель так читает ``№``.
    Хвост обязан остаться частью знака номера, иначе он приклеится к самому номеру счёта.
    """
    match = re.search(
        rf"\b(Сч[еёe]т)\s*(?:N[º°o]?|No|№)\s*([0-9][A-Za-zА-Яа-я0-9/_\-]*)\s*от\s*({DATE_PATTERN})",
        text,
        re.IGNORECASE,
    )
    if not match:
        return "", "", ""
    return "Счет", match.group(2), normalize_date(match.group(3))


def build_water_utility_purpose(basis: dict[str, Any]) -> str:
    """Назначение платежа с обязательной оговоркой «по строкам 4-7».

    Оговорка нужна поставщику: он видит платёж меньше суммы счёта и должен понимать, что
    это не недоплата, а сознательный отказ платить чужой перерасчёт.
    """
    doc_number = str(basis.get("document_number") or "")
    doc_date = str(basis.get("document_date") or "")
    period_label = str(basis.get("service_period_label") or "")

    if doc_number and doc_date:
        purpose = (
            f"Оплата услуг водоснабжения и водоотведения по счету N {doc_number} от {doc_date}"
        )
    elif doc_number:
        purpose = f"Оплата услуг водоснабжения и водоотведения по счету N {doc_number}"
    else:
        purpose = "Оплата услуг водоснабжения и водоотведения"
    if period_label:
        purpose = f"{purpose} за {period_label}"
    purpose = f"{purpose} по строкам 4-7"

    vat_amount = str(basis.get("vat_amount") or "")
    if vat_amount:
        purpose = f"{purpose}. В т.ч. НДС {vat_amount}"
    elif basis.get("vat_mode"):
        purpose = f"{purpose}. НДС согласно документу"
    return re.sub(r"\s+", " ", purpose).strip()[:210]


# ---------------------------------------------------------------------------
# Электричество: акт факта и акт аванса
# ---------------------------------------------------------------------------

ELECTRICITY_DOC_TYPE = "Акт приема-передачи электроэнергии"


def first_money_after(text: str, pattern: str) -> str:
    match = re.search(
        rf"{pattern}\D{{0,40}}([0-9][0-9 \xa0]*(?:[,.:_]\d{{2}})?)",
        text,
        re.IGNORECASE,
    )
    if not match:
        return ""
    amount = money(match.group(1))
    return fmt_money(amount) if amount > 0 else ""


def line_amount(text: str, pattern: str, *, min_amount: Decimal = Decimal("0")) -> str:
    """Последнее число подходящей строки — это сумма позиции, а не цена за кВт·ч.

    В строке акта четыре числа: количество, цена, иногда тариф и сумма. Порог ``min_amount``
    отсекает цену (единицы рублей) от суммы (тысячи), когда OCR перепутал колонки.
    """
    for raw_line in text.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not re.search(pattern, line, re.IGNORECASE):
            continue
        values = [value for value in money_tokens(line) if value >= min_amount]
        if values:
            return fmt_money(values[-1])
    return ""


def find_electricity_supplier_name(text: str) -> str:
    match = re.search(
        r"Поставщик\s+(.+?)(?=\s+ИНН\b|\n\s*ИНН\b|\n\s*Адрес\b|\n\s*АКТ\b|$)",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if match:
        return clean_name(match.group(1))
    return ""


def find_electricity_document_date(text: str) -> str:
    match = re.search(
        rf"Акт\s+при[её]ма-передачи\s+электроэнергии\s+от\s+({DATE_PATTERN})",
        text,
        re.IGNORECASE,
    )
    if match:
        return normalize_date(match.group(1))
    match = re.search(
        rf"\bот\s+({DATE_PATTERN})\s*[гr]?\.?\s+по\s+договору",
        text,
        re.IGNORECASE,
    )
    return normalize_date(match.group(1)) if match else ""


def electricity_act_kind(text: str) -> str:
    """Вид акта: ``actual`` (факт прошедшего месяца) или ``advance`` (аванс следующего).

    Различие определяет ВСЁ остальное: у факта есть потери, зачёт ранее внесённого аванса и
    строка «К оплате», у аванса — одна строка с процентом от прогноза. Спутать их значит
    либо задвоить расход месяца, либо заплатить аванс дважды.
    """
    lower = normalize_key(text)
    if "электроэнерг" in lower and (
        re.search(r"\bпотер", lower)
        or re.search(r"\bк\s+оплат[еа]\b", lower)
        or re.search(r"\bоплачен", lower)
    ):
        return "actual"
    if re.search(r"электроэнерг\w*\s+\d+\s*%\s+за\s+[а-яё]+.*аванс|\bаванс\b", lower):
        return "advance"
    return ""


def resolve_electricity_period_year(kind: str, period_month: int, document_date: str) -> int:
    """Год периода по названию месяца: акт года не пишет, и на стыке лет это ловушка.

    Факт всегда за месяц ДО даты акта (акт за декабрь подписан в январе), аванс — за месяц
    ПОСЛЕ. Отсюда обе поправки; без них январский акт за декабрь попадёт в будущий декабрь.
    """
    year = clock.moscow_today().year
    if re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", document_date):
        _, doc_month_text, doc_year_text = document_date.split(".")
        doc_month = int(doc_month_text)
        year = int(doc_year_text)
        if kind == "actual" and period_month > doc_month:
            year -= 1
        elif kind == "advance" and period_month < doc_month:
            year += 1
    return year


def find_electricity_service_period(
    text: str,
    kind: str,
    document_date: str,
) -> tuple[str, str, str]:
    month_names = "|".join(sorted(MONTHS_RU, key=len, reverse=True))
    patterns = [
        rf"Электро\w*нерг\w*(?:\s+\d+\s*%)?\s+за\s+({month_names})",
        rf"Потер\w*\s+({month_names})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        month_label = match.group(1)
        month_number = MONTHS_RU[normalize_key(month_label)]
        year = resolve_electricity_period_year(kind, month_number, document_date)
        start, end = month_bounds(year, month_number)
        return start, end, f"{month_label} {year}"
    return "", "", ""


def find_electricity_period_total(text: str) -> str:
    for pattern in (r"\bИТОГО\b", r"\bИтого\b", r"\bВсего\b"):
        amount = first_money_after(text, pattern)
        if amount:
            return amount
    return ""


def find_electricity_paid_advance_amount(text: str) -> str:
    """Ранее внесённый аванс: строка «Оплачено», либо число с датой платежа рядом."""
    amount = first_money_after(text, r"Оплачено(?:\s+аванс)?")
    if amount:
        return amount
    match = re.search(
        rf"([0-9][0-9 \xa0]*(?:[,.:_]\d{{2}})?)\s*[рp]\.?\s*[-–]\s*{DATE_PATTERN}",
        text,
        re.IGNORECASE,
    )
    if not match:
        return ""
    amount = money(match.group(1))
    return fmt_money(amount) if amount > 0 else ""


def find_electricity_actual_amounts(text: str) -> tuple[str, str, str, str, str]:
    """Суммы фактического акта: энергия, потери, итог периода, зачтённый аванс, к оплате.

    Итог периода и «к оплате» — РАЗНЫЕ числа, и их нельзя смешивать: первое идёт в расход
    месяца целиком, второе уходит в банк. Если в акте нет строки «К оплате», остаток
    выводится вычитанием, и только когда он положительный — отрицательная разница означала
    бы переплату, а её здесь угадывать нечем.
    """
    row_total_floor = Decimal("100")
    energy_amount = line_amount(
        text,
        r"Электро\w*нерг\w*\s+за\s+[а-яё]+",
        min_amount=row_total_floor,
    )
    losses_amount = line_amount(text, r"Потер\w*\s+[а-яё]+", min_amount=row_total_floor)
    period_total = find_electricity_period_total(text)
    period_amount = period_total
    if not period_amount and (energy_amount or losses_amount):
        period_amount = fmt_money(money(energy_amount) + money(losses_amount))
    paid_advance = find_electricity_paid_advance_amount(text)
    amount_due = first_money_after(text, r"К\s+оплате")
    if not amount_due and period_amount and paid_advance:
        inferred_due = money(period_amount) - money(paid_advance)
        if inferred_due > 0:
            amount_due = fmt_money(inferred_due)
    return energy_amount, losses_amount, period_amount, paid_advance, amount_due


def find_electricity_advance_amount(text: str) -> str:
    amount = line_amount(text, r"Электроэнерг\w*\s+\d+\s*%\s+за\s+[а-яё]+.*аванс")
    if amount:
        return amount
    return first_money_after(text, r"(?:ИТОГО|Итого|Всего)")


def build_electricity_actual_purpose(basis: dict[str, Any]) -> str:
    period_label = str(basis.get("service_period_label") or "")
    doc_date = str(basis.get("document_date") or "")
    purpose = "Оплата электроэнергии и потерь"
    if period_label:
        purpose = f"{purpose} за {period_label}"
    if doc_date:
        purpose = f"{purpose} по акту от {doc_date}"
    return re.sub(r"\s+", " ", purpose).strip()[:210]


def build_electricity_advance_purpose(basis: dict[str, Any]) -> str:
    period_label = str(basis.get("service_period_label") or "")
    doc_date = str(basis.get("document_date") or "")
    purpose = "Аванс за электроэнергию"
    if period_label:
        purpose = f"{purpose} за {period_label}"
    if doc_date:
        purpose = f"{purpose} по акту от {doc_date}"
    return re.sub(r"\s+", " ", purpose).strip()[:210]


# ---------------------------------------------------------------------------
# Общий каркас разбора
# ---------------------------------------------------------------------------


def requisites_score(text: str) -> tuple[float, list[str]]:
    """Вклад найденных реквизитов в уверенность: чем больше сходится, тем это вероятнее счёт."""
    score = 0.0
    reasons: list[str] = []
    checks = [
        ("inn", bool(find_labeled_digits(text, [r"ИНН"], (10, 12))), 0.12),
        ("kpp", bool(find_labeled_digits(text, [r"КПП"], (9,))), 0.06),
        ("bik", bool(find_labeled_digits(text, [r"БИК"], (9,))), 0.12),
        ("account", bool(find_bank_account(text)), 0.15),
        ("amount", bool(find_amount(text)), 0.12),
    ]
    for reason, ok, points in checks:
        if ok:
            score += points
            reasons.append(f"found_{reason}")
    return score, reasons


def empty_basis(parser_name: str, source_type: str, metadata: dict[str, Any]) -> dict[str, Any]:
    """Каркас результата: все поля объявлены пустыми строками, отсутствующих ключей нет.

    Так вызывающий код никогда не спотыкается о ``KeyError``, а пустая строка честно
    означает «не нашли», а не «забыли положить».
    """
    return {
        "source_type": source_type,
        "parser_name": parser_name,
        "recognition_confidence": 0.0,
        "counterparty_name": "",
        "inn": "",
        "kpp": "",
        "bank_bik": "",
        "bank_account": "",
        "corr_account": "",
        "bank_name": "",
        "amount": "",
        "vat_mode": "",
        "vat_amount": "",
        "document_type": "",
        "document_number": "",
        "document_date": "",
        "service_period_start": "",
        "service_period_end": "",
        "service_period_label": "",
        "payment_purpose_suggestion": "",
        "requires_owner_review": False,
        "owner_review_reasons": [],
        "raw_evidence": [],
        "source_file": str(metadata.get("source_file") or ""),
    }


def common_basis(
    text: str,
    metadata: dict[str, Any],
    parser_name: str,
    source_type: str,
) -> dict[str, Any]:
    basis = empty_basis(parser_name, source_type, metadata)
    doc_type, doc_number, doc_date = find_document(text)
    period_start, period_end, period_label = find_service_period(text)
    vat_mode, vat_amount = find_vat(text)

    basis.update(
        {
            "counterparty_name": find_counterparty_name(text),
            "inn": find_labeled_digits(text, [r"ИНН"], (10, 12)),
            "kpp": find_labeled_digits(text, [r"КПП"], (9,)),
            "bank_bik": find_labeled_digits(text, [r"БИК"], (9,)),
            "bank_account": find_bank_account(text),
            "corr_account": find_corr_account(text),
            "bank_name": find_bank_name(text),
            "amount": find_amount(text),
            "vat_mode": vat_mode,
            "vat_amount": vat_amount,
            "document_type": doc_type,
            "document_number": doc_number,
            "document_date": doc_date,
            "service_period_start": period_start,
            "service_period_end": period_end,
            "service_period_label": period_label,
        }
    )
    # У ИП КПП не бывает, но платёжное поручение требует поле заполненным — банк ждёт «0».
    if not basis["kpp"] and len(basis["inn"]) == 12:
        basis["kpp"] = "0"
    basis["payment_purpose_suggestion"] = build_default_purpose(basis)
    basis["raw_evidence"] = build_evidence(text, basis)
    return basis


def completeness_score(basis: dict[str, Any]) -> float:
    found = 0
    total = len(PAYMENT_READY_FIELDS)
    for field in PAYMENT_READY_FIELDS:
        if basis.get(field) not in (None, ""):
            found += 1
    if basis.get("inn") and len(clean_digits(basis.get("inn"))) == 10:
        total += 1
        if basis.get("kpp") not in (None, ""):
            found += 1
    return found / total if total else 0.0


class WaterUtilityInvoiceParser:
    """Счёт Водоканала: сумма к оплате — строго строки 4-7 таблицы.

    Отдельный класс, а не ветка в общем разборе счетов, ровно из-за этого правила: любой
    другой счёт платится на итоговую сумму, а этот — на часть. Держать исключение внутри
    общего парсера значило бы, что однажды его применят не к тому документу.
    """

    name = "water_utility_invoice_v1"
    source_type = "invoice"
    utility_kind = "water"

    def detect(self, text: str, metadata: dict[str, Any]) -> DetectionResult:
        lower = normalize_key(text)
        score = 0.0
        reasons: list[str] = []
        checks = [
            (r"сч[еe]т\s+N?\s*\d+", 0.12, "invoice_number"),
            (r"водоканал", 0.22, "water_utility_supplier"),
            (r"водоснабжен", 0.18, "water_supply_row"),
            (r"водоотведен", 0.18, "water_disposal_row"),
            (r"негативн\w+\s+воздейств", 0.08, "negative_impact_row"),
            (r"состав[а-яё\s]+сточн\w+\s+вод", 0.08, "wastewater_pollutants_row"),
        ]
        for pattern, points, reason in checks:
            if re.search(pattern, lower, re.IGNORECASE):
                score += points
                reasons.append(reason)
        _, _, selected_rows = water_utility_selected_row_totals(text)
        if selected_rows == list(WATER_UTILITY_SELECTED_ROWS):
            score += 0.16
            reasons.append("selected_rows_4_7")
        req_score, req_reasons = requisites_score(text)
        score += req_score
        reasons.extend(req_reasons)
        return DetectionResult(self.name, self.source_type, min(score, 0.99), reasons)

    def parse(self, text: str, metadata: dict[str, Any]) -> dict[str, Any]:
        basis = common_basis(text, metadata, self.name, self.source_type)
        supplier_name = find_water_utility_counterparty_name(text)
        if supplier_name:
            basis["counterparty_name"] = supplier_name
        doc_type, doc_number, doc_date = find_water_utility_document(text)
        if doc_number:
            basis["document_type"] = doc_type
            basis["document_number"] = doc_number
            basis["document_date"] = doc_date
        basis["counterparty_alias"] = "Водоканал"
        basis["dds_article_candidate"] = "Аренда торговых точек"
        basis["pnl_article_candidate"] = "Коммунальные платежи"
        require_service_period(basis)

        selected_amount, selected_vat, selected_rows = water_utility_selected_row_totals(text)
        if selected_amount:
            basis["amount"] = selected_amount
            basis["vat_mode"] = "included"
            basis["vat_amount"] = selected_vat
            basis["payment_purpose_suggestion"] = build_water_utility_purpose(basis)
            basis["raw_evidence"] = build_evidence(text, basis)
            basis["raw_evidence"].append(
                {
                    "field": "amount",
                    "kind": "source_rule",
                    "pattern": "water_utility_rows_4_7",
                    "snippet": "selected_rows=4,5,6,7",
                }
            )
        else:
            # Итог документа сюда НЕ подставляется намеренно: он включает перерасчёт
            # прошлого периода. Лучше отдать документ человеку без суммы.
            basis["amount"] = ""
            basis["vat_amount"] = ""
            basis["payment_purpose_suggestion"] = build_water_utility_purpose(basis)
            basis["raw_evidence"] = build_evidence(text, basis)
            basis["requires_owner_review"] = True
            basis.setdefault("owner_review_reasons", []).append(
                "water_utility_rows_4_7_not_found"
            )
            if selected_rows:
                basis["raw_evidence"].append(
                    {
                        "field": "amount",
                        "kind": "source_rule",
                        "pattern": "water_utility_rows_4_7",
                        "snippet": "selected_rows_partial",
                    }
                )
        return basis


class ElectricityActParser:
    """Акт приёма-передачи электроэнергии: сначала вид акта, потом суммы.

    Фактический акт несёт две суммы сразу: расход периода (электроэнергия + потери) и
    остаток к доплате после зачёта ранее внесённого аванса. Авансовый — одну. Обе ветки
    помечаются как требующие человека: платёж собирается из пары актов, а пары у одного
    документа по определению нет.
    """

    name = "electricity_act_v1"
    source_type = "utility_act"
    utility_kind = "electricity"

    def detect(self, text: str, metadata: dict[str, Any]) -> DetectionResult:
        lower = normalize_key(text)
        score = 0.0
        reasons: list[str] = []
        checks = [
            (r"акт\s+при[еe]ма-передачи\s+электроэнерг", 0.34, "electricity_act_title"),
            (r"электроэнерг", 0.16, "electricity_row"),
            (r"\bпотери\b", 0.08, "losses_row"),
            (r"\bаванс\b", 0.08, "advance_marker"),
            (r"поставщик|потребитель", 0.08, "supplier_consumer_labels"),
            (r"по\s+договору", 0.06, "contract_marker"),
        ]
        for pattern, points, reason in checks:
            if re.search(pattern, lower, re.IGNORECASE):
                score += points
                reasons.append(reason)
        req_score, req_reasons = requisites_score(text)
        score += req_score
        reasons.extend(req_reasons)
        return DetectionResult(self.name, self.source_type, min(score, 0.99), reasons)

    def parse(self, text: str, metadata: dict[str, Any]) -> dict[str, Any]:
        basis = empty_basis(self.name, self.source_type, metadata)
        kind = electricity_act_kind(text)
        document_date = find_electricity_document_date(text)
        (
            energy_amount,
            losses_amount,
            period_amount,
            paid_advance,
            amount_due,
        ) = find_electricity_actual_amounts(text)
        advance_amount = find_electricity_advance_amount(text)
        if not kind:
            # Заголовки не сработали — судим по составу сумм: остаток и зачёт бывают
            # только у факта, одинокая итоговая сумма — признак аванса.
            if period_amount and (losses_amount or amount_due or paid_advance):
                kind = "actual"
            elif advance_amount:
                kind = "advance"
        period_start, period_end, period_label = find_electricity_service_period(
            text,
            kind,
            document_date,
        )
        supplier_name = find_electricity_supplier_name(text) or find_counterparty_name(text)
        inn = find_labeled_digits(text, [r"ИНН"], (10, 12))

        basis.update(
            {
                "counterparty_name": supplier_name,
                "inn": inn,
                "kpp": "0" if len(inn) == 12 else "",
                "bank_bik": find_labeled_digits(text, [r"БИК"], (9,)),
                "bank_account": find_bank_account(text),
                "corr_account": find_corr_account(text),
                "bank_name": find_bank_name(text),
                "document_type": ELECTRICITY_DOC_TYPE,
                "document_number": "",
                "document_date": document_date,
                "service_period_start": period_start,
                "service_period_end": period_end,
                "service_period_label": period_label,
                "dds_article_candidate": "Аренда торговых точек",
                "pnl_article_candidate": "Коммунальные платежи",
                "electricity_act_kind": kind,
                "electricity_energy_amount": energy_amount,
                "electricity_losses_amount": losses_amount,
                "electricity_period_amount": "",
                "electricity_paid_advance_amount": "",
                "electricity_amount_due": "",
                "electricity_advance_amount": "",
            }
        )

        if kind == "advance":
            basis.update(
                {
                    "amount": advance_amount,
                    "electricity_advance_amount": advance_amount,
                    "payment_purpose_suggestion": build_electricity_advance_purpose(basis),
                    "requires_owner_review": True,
                }
            )
            basis.setdefault("owner_review_reasons", []).append(
                "electricity_pair_waiting_for_actual"
            )
        else:
            basis.update(
                {
                    "amount": amount_due,
                    "electricity_period_amount": period_amount,
                    "electricity_paid_advance_amount": paid_advance,
                    "electricity_amount_due": amount_due,
                    "payment_purpose_suggestion": build_electricity_actual_purpose(basis),
                    "requires_owner_review": True,
                }
            )
            basis.setdefault("owner_review_reasons", []).append(
                "electricity_pair_waiting_for_advance"
            )

        if not kind:
            basis.setdefault("owner_review_reasons", []).append(
                "electricity_act_kind_not_detected"
            )
        require_service_period(basis)
        if kind == "actual" and not basis.get("electricity_amount_due"):
            basis.setdefault("owner_review_reasons", []).append(
                "electricity_amount_due_not_found"
            )
        if kind == "advance" and not basis.get("electricity_advance_amount"):
            basis.setdefault("owner_review_reasons", []).append(
                "electricity_advance_amount_not_found"
            )

        basis["raw_evidence"] = build_evidence(text, basis)
        if kind:
            basis["raw_evidence"].append(
                {
                    "field": "electricity_act_kind",
                    "kind": "source_rule",
                    "pattern": "electricity_actual_or_advance",
                    "snippet": kind,
                }
            )
        return basis


# ---------------------------------------------------------------------------
# Публичный контракт
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UtilityRecognition:
    """Распознанный коммунальный документ.

    ``amount`` и даты — уже типизированные (``Decimal``/``date``), а не строки: приводить их
    в каждом вызывающем месте — верный способ однажды сложить строку с числом. ``None`` в
    любом поле означает «в документе этого не нашли», и причина всегда лежит в ``reasons``.
    ``raw`` отдаёт всё извлечённое как есть — для экрана, где человек проверяет разбор.
    """

    kind: str
    amount: Decimal | None
    period_start: date | None
    period_end: date | None
    document_number: str | None
    document_date: date | None
    confidence: float
    reasons: list[str]
    raw: dict[str, Any]


_UTILITY_PARSERS: tuple[WaterUtilityInvoiceParser | ElectricityActParser, ...] = (
    WaterUtilityInvoiceParser(),
    ElectricityActParser(),
)


def _looks_like_water(normalized: str) -> bool:
    lower = normalize_key(normalized)
    if "водоканал" in lower:
        return True
    return "водоснабжен" in lower and "водоотведен" in lower


def _looks_like_electricity(normalized: str) -> bool:
    lower = normalize_key(normalized)
    if not re.search(r"электро\w*нерг", lower):
        return False
    if re.search(r"акт\s+при[еe]ма-передачи\s+электро", lower):
        return True
    return bool(electricity_act_kind(normalized))


def _pick_parser(normalized: str) -> WaterUtilityInvoiceParser | ElectricityActParser | None:
    """Выбор парсера по обязательным приметам домена, а не по одной лишь уверенности.

    Реквизиты (ИНН, БИК, счёт, сумма) есть у любого счёта и сами по себе поднимают
    уверенность до 0,6 — на них нельзя опираться, иначе накладная от поставщика продуктов
    поедет в парсер Водоканала и получит сумму «по строкам 4-7» из чужой таблицы. Поэтому
    сначала жёсткая примета (Водоканал / акт электроэнергии), и только потом баллы.
    """
    candidates: list[tuple[float, Any]] = []
    if _looks_like_water(normalized):
        parser = _UTILITY_PARSERS[0]
        candidates.append((parser.detect(normalized, {}).confidence, parser))
    if _looks_like_electricity(normalized):
        parser = _UTILITY_PARSERS[1]
        candidates.append((parser.detect(normalized, {}).confidence, parser))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _decimal_field(basis: dict[str, Any], field: str, reasons: list[str]) -> Decimal | None:
    """Строковая сумма → ``Decimal``. Непустая, но нечитаемая строка попадает в ``reasons``."""
    raw_value = str(basis.get(field) or "").strip()
    if not raw_value:
        return None
    try:
        return Decimal(raw_value)
    except (InvalidOperation, ValueError):
        reasons.append(f"{field}_unparsable:{raw_value[:40]}")
        return None


def _iso_date_field(basis: dict[str, Any], field: str, reasons: list[str]) -> date | None:
    raw_value = str(basis.get(field) or "").strip()
    if not raw_value:
        return None
    try:
        return date.fromisoformat(raw_value)
    except ValueError:
        reasons.append(f"{field}_unparsable:{raw_value[:40]}")
        return None


def _ru_date_field(basis: dict[str, Any], field: str, reasons: list[str]) -> date | None:
    """Дата ``ДД.ММ.ГГГГ`` → ``date``. Формат уже нормализован при разборе, но не гарантирован."""
    raw_value = str(basis.get(field) or "").strip()
    if not raw_value:
        return None
    match = re.fullmatch(r"(\d{2})\.(\d{2})\.(\d{4})", raw_value)
    if not match:
        reasons.append(f"{field}_unparsable:{raw_value[:40]}")
        return None
    day, month, year = (int(part) for part in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        reasons.append(f"{field}_unparsable:{raw_value[:40]}")
        return None


# Заголовок акта приёма-передачи: по нему один файл режется на отдельные документы.
# Требуем оба слова, потому что «АКТ» в одиночку встречается в тексте акта не раз (шапка,
# подписи, ссылки на договор), и резать по нему значило бы крошить документ на обрывки.
_ACT_HEADER_RE = re.compile(
    r"^[ \t]*акт\b.{0,120}?при[еe]ма[- ]передачи\s+электроэнерг",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)


def split_utility_documents(text: str) -> list[str]:
    """Разбить текст ОДНОГО файла на отдельные документы.

    ЗАЧЕМ. Энергетик присылает за визит два акта — фактический за прошедший месяц и авансовый
    на следующий — и приходят они одним файлом (подтверждено владельцем 02.08.2026). Парсер же
    определяет вид акта для всего текста сразу: на склейке он видит только первый и молча
    теряет второй. Стоит это ровно суммы аванса: платёж уходит заниженным, а месяц остаётся
    без предоплаты, которую следующий факт-акт должен будет зачесть.

    Режем по заголовку акта. Преамбулу (всё до первого заголовка — реквизиты поставщика и
    потребителя) приклеиваем к каждому куску: она общая для обоих актов, а без неё второй
    документ остался бы без ИНН и без имени контрагента.

    Один документ или незнакомый формат — возвращается список из одного исходного текста:
    вызывающему коду не нужно знать, делили мы что-то или нет.
    """
    if not text:
        return []
    starts = [m.start() for m in _ACT_HEADER_RE.finditer(text)]
    if len(starts) < 2:
        return [text]
    preamble = text[: starts[0]].strip()
    parts: list[str] = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(text)
        chunk = text[start:end].strip()
        parts.append(f"{preamble}\n\n{chunk}" if preamble else chunk)
    return parts


def recognize_utility_documents(
    text: str,
    metadata: dict[str, Any] | None = None,
) -> list[UtilityRecognition]:
    """Все коммунальные документы из одного файла. Пустой список — файл не коммунальный.

    Отдельная точка входа рядом с ``recognize_utility_document`` появилась не для симметрии:
    у электричества один файл несёт ДВА документа с разными периодами и разными ролями
    (факт признаёт расход, аванс только платится). Возвращать из них один — терять второй.
    """
    documents = [
        recognized
        for part in split_utility_documents(normalize_text(text or ""))
        if (recognized := recognize_utility_document(part, metadata)) is not None
    ]
    if documents:
        return documents
    # Разбиение ничего не дало (файл не резался или куски не опознались) — пробуем целиком.
    single = recognize_utility_document(text, metadata)
    return [single] if single is not None else []


def recognize_utility_document(
    text: str,
    metadata: dict[str, Any] | None = None,
) -> UtilityRecognition | None:
    """Единственная точка входа: текст документа → структура или ``None``.

    ``None`` означает «это не коммунальный документ» — не ошибку и не низкую уверенность.
    Коммунальный документ, разобранный плохо, возвращается со своими пустыми полями и
    списком причин: решение о нём принимает человек, а не молчаливый ``None``.

    ``metadata`` не участвует в разборе (текст — единственный источник фактов) и нужна
    только для того, чтобы вернуться в ``raw`` рядом с распознанным: чем именно был файл и
    откуда пришёл, полезно видеть на экране проверки.
    """
    normalized = normalize_text(text or "")
    if not normalized:
        return None
    parser = _pick_parser(normalized)
    if parser is None:
        return None

    payload = dict(metadata or {})
    detection = parser.detect(normalized, payload)
    basis = parser.parse(normalized, payload)
    confidence = round(
        min(1.0, detection.confidence * 0.65 + completeness_score(basis) * 0.35),
        3,
    )
    basis["recognition_confidence"] = confidence

    reasons: list[str] = list(detection.reasons)
    reasons.extend(str(reason) for reason in basis.get("owner_review_reasons") or [])

    amount = _decimal_field(basis, "amount", reasons)
    if amount is None and not str(basis.get("amount") or "").strip():
        reasons.append("amount_not_found")
    period_start = _iso_date_field(basis, "service_period_start", reasons)
    period_end = _iso_date_field(basis, "service_period_end", reasons)
    document_date = _ru_date_field(basis, "document_date", reasons)
    document_number = str(basis.get("document_number") or "").strip() or None

    raw = dict(basis)
    raw["metadata"] = payload
    raw["detection"] = detection.as_dict()

    return UtilityRecognition(
        kind=parser.utility_kind,
        amount=amount,
        period_start=period_start,
        period_end=period_end,
        document_number=document_number,
        document_date=document_date,
        confidence=confidence,
        reasons=list(dict.fromkeys(reasons)),
        raw=raw,
    )
