#!/usr/bin/env python3
"""Deterministic payment-basis parsers for T-Bank payment intake.

Runtime recognition is deliberately LLM-free: source-specific parser classes
detect document families, extract fields with regex/templates, enrich known
counterparties, and persist private intake records in SQLite.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Protocol


PAYMENT_BASIS_FIELDS = (
    "source_type",
    "parser_name",
    "recognition_confidence",
    "counterparty_name",
    "inn",
    "kpp",
    "bank_bik",
    "bank_account",
    "corr_account",
    "bank_name",
    "amount",
    "vat_mode",
    "vat_amount",
    "document_type",
    "document_number",
    "document_date",
    "service_period_start",
    "service_period_end",
    "service_period_label",
    "payment_purpose_suggestion",
    "dds_article_candidate",
    "pnl_article_candidate",
    "electricity_act_kind",
    "electricity_period_amount",
    "electricity_paid_advance_amount",
    "electricity_amount_due",
    "electricity_advance_amount",
    "electricity_payment_amount",
    "electricity_pair_status",
    "linked_payment_basis_refs",
    "requires_owner_review",
    "raw_evidence",
    "source_file",
    "upload_id",
    "message_metadata",
)

PAYMENT_READY_FIELDS = (
    "counterparty_name",
    "inn",
    "bank_bik",
    "bank_account",
    "amount",
    "payment_purpose_suggestion",
)

LOW_CONFIDENCE_THRESHOLD = 0.65

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

KNOWN_COUNTERPARTIES = [
    {
        "canonical": "Амай",
        "aliases": ["амай", "тора"],
        "dds": "Оплата поставщикам",
        "pnl": "Себестоимость продуктов через iiko",
        "review": False,
    },
    {
        "canonical": "Альянс Юг",
        "aliases": ["альянс юг"],
        "dds": "Оплата поставщикам",
        "pnl": "Себестоимость продуктов через iiko",
        "review": False,
    },
    {
        "canonical": "Мяснов",
        "aliases": ["мяснофф-дон", "мяснов"],
        "dds": "Оплата поставщикам",
        "pnl": "Себестоимость продуктов через iiko",
        "review": False,
    },
    {
        "canonical": "Мистерия",
        "aliases": ["мистерия"],
        "dds": "Оплата поставщикам",
        "pnl": "Себестоимость продуктов через iiko",
        "review": False,
    },
    {
        "canonical": "Метро",
        "aliases": ["метро кэш энд керри", "metro"],
        "dds": "Оплата поставщикам",
        "pnl": "Себестоимость продуктов через iiko",
        "review": False,
    },
    {
        "canonical": "Суши Принт",
        "aliases": ["суши принт"],
        "dds": "Оплата поставщикам",
        "pnl": "Себестоимость продуктов через iiko",
        "review": False,
    },
    {
        "canonical": "ООО Айко",
        "aliases": ["айко", "iiko", "iikooffice"],
        "dds": "Оплаты систем автоматизации",
        "pnl": "Оплаты систем автоматизации",
        "review": False,
    },
    {
        "canonical": "Ревви / Greevy",
        "aliases": ["ревви", "реви", "greevy", "гриви"],
        "dds": "Оплаты систем автоматизации",
        "pnl": "Оплаты систем автоматизации",
        "review": False,
    },
    {
        "canonical": "Докс ин бокс",
        "aliases": ["доксинбокс", "докс ин бокс", "docsinbox", "docs in box"],
        "dds": "Оплаты систем автоматизации",
        "pnl": "Оплаты систем автоматизации",
        "review": False,
    },
    {
        "canonical": "StarterApp / ООО Назад в будущее",
        "aliases": ["starterapp", "starter app", "назад в будущее", "стартер"],
        "dds": "Оплаты систем автоматизации",
        "pnl": "Оплаты систем автоматизации",
        "review": True,
        "review_reason": "starterapp_split_payment_commission_platform_cost",
    },
    {
        "canonical": "Синапс / Синапсис",
        "aliases": ["синапс", "синапсис", "синопсис", "synapse", "о. о"],
        "dds": "Контекстная реклама",
        "pnl": "Маркетинг",
        "review": False,
    },
    {
        "canonical": "Экоцентр",
        "aliases": ["экоцентр"],
        "dds": "Аренда торговых точек",
        "pnl": "Объектные сервисы",
        "review": True,
        "review_reason": "ecocenter_article_needs_normalization",
    },
    {
        "canonical": "Лемма",
        "aliases": ["лемма", "lemma"],
        "dds": "",
        "pnl": "",
        "review": True,
        "review_reason": "lemma_article_not_confirmed_in_project_docs",
    },
]


@dataclass(frozen=True)
class DetectionResult:
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


class PaymentParser(Protocol):
    name: str
    source_type: str

    def detect(self, text: str, metadata: dict[str, Any]) -> DetectionResult:
        ...

    def parse(self, text: str, metadata: dict[str, Any]) -> dict[str, Any]:
        ...


def clean_digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def normalize_text(value: str) -> str:
    text = value.replace("\xa0", " ")
    text = text.replace("№", "N")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def normalize_key(value: str) -> str:
    text = normalize_text(value).casefold()
    text = text.replace("ё", "е")
    return re.sub(r"\s+", " ", text)


def money(value: Any) -> Decimal:
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


def first_match(text: str, patterns: list[str], flags: int = re.IGNORECASE | re.MULTILINE) -> str:
    for pattern in patterns:
        match = re.search(pattern, text, flags)
        if match:
            for group in match.groups():
                if group:
                    return group.strip()
    return ""


def find_labeled_digits(text: str, labels: list[str], lengths: tuple[int, ...]) -> str:
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
    """If the digit run is exactly one digit longer than a valid length, treat
    the leading digit as an OCR artifact from a table separator and strip it.

    Tesseract sometimes glues a vertical bar `|` from a table border onto the
    start of a number as `1`. This helper recovers the real value while
    refusing to act on ambiguous lengths.
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


def find_amount(text: str, source_type: str = "") -> str:
    patterns = [
        r"(?:итого\s+к\s+оплате|всего\s+к\s+оплате|к\s+оплате|сумма\s+к\s+оплате)\D{0,50}([0-9][0-9\s]*[,.]\d{2}|[0-9][0-9\s]*)",
        r"(?:сумма\s+платежа|сумма\s+документа|сумма\s+ордера)\D{0,50}([0-9][0-9\s]*[,.]\d{2}|[0-9][0-9\s]*)",
        r"(?:итог|итого|всего|total)\D{0,50}([0-9][0-9\s]*[,.]\d{2})",
    ]
    if source_type == "receipt":
        patterns.insert(0, r"(?:итог|итого|итого\s+к\s+оплате)\D{0,30}([0-9][0-9\s]*[,.]\d{2})")

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


def find_counterparty_name(text: str, source_type: str = "") -> str:
    label_groups = [
        r"Получатель",
        r"Поставщик",
        r"Исполнитель",
        r"Продавец",
    ]
    if source_type == "receipt":
        label_groups = [r"Пользователь", r"Продавец", r"Организация", r"Кассир"] + label_groups
    if source_type == "payment_order":
        label_groups = [r"Получатель", r"Банк получателя", r"Плательщик"] + label_groups

    for label in label_groups:
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


def find_document(text: str, source_type: str = "") -> tuple[str, str, str]:
    patterns = {
        "invoice": [
            rf"(Сч[её]т(?:\s+на\s+оплату)?|Сч[её]т-договор)\s*(?:N|№|No)?\s*([A-Za-zА-Яа-я0-9/_\-]+)(?:\s*от\s*({DATE_PATTERN}))?",
        ],
        "upd": [
            rf"(УПД|Универсальный\s+передаточный\s+документ)\s*(?:N|№|No)?\s*([A-Za-zА-Яа-я0-9/_\-]+)(?:\s*от\s*({DATE_PATTERN}))?",
        ],
        "waybill": [
            rf"(Товарная\s+накладная|Накладная|ТОРГ-?12)\s*(?:N|№|No)?\s*([A-Za-zА-Яа-я0-9/_\-]+)(?:\s*от\s*({DATE_PATTERN}))?",
        ],
        "receipt": [
            rf"(Кассовый\s+чек|Чек)\s*(?:N|№|No)?\s*([A-Za-zА-Яа-я0-9/_\-]+)?(?:\s*от\s*({DATE_PATTERN}))?",
            rf"(?:ФД|fd)\D{{0,10}}([0-9]{{3,16}}).{{0,60}}?({DATE_PATTERN})",
        ],
        "payment_order": [
            rf"(Плат[её]жн(?:ое|ого)\s+поручени[ея]|Плат[её]жка)\s*(?:N|№|No)?\s*([A-Za-zА-Яа-я0-9/_\-]+)(?:\s*от\s*({DATE_PATTERN}))?",
        ],
    }
    source_patterns = patterns.get(source_type, [])
    generic_patterns = [
        rf"(Сч[её]т(?:\s+на\s+оплату)?|Сч[её]т-договор|УПД|Универсальный\s+передаточный\s+документ|Товарная\s+накладная|Накладная|ТОРГ-?12|Плат[её]жн(?:ое|ого)\s+поручени[ея]|Плат[её]жка)\s*(?:N|№|No)?\s*([A-Za-zА-Яа-я0-9/_\-]+)(?:\s*от\s*({DATE_PATTERN}))?",
    ]
    for pattern in source_patterns + generic_patterns:
        match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
        if not match:
            continue
        groups = [group.strip() if group else "" for group in match.groups()]
        if source_type == "receipt" and len(groups) == 2:
            return "Кассовый чек", groups[0], normalize_date(groups[1])
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


def month_bounds(year: int, month: int) -> tuple[str, str]:
    start = dt.date(year, month, 1)
    if month == 12:
        end = dt.date(year, 12, 31)
    else:
        end = dt.date(year, month + 1, 1) - dt.timedelta(days=1)
    return start.isoformat(), end.isoformat()


def find_service_period(text: str) -> tuple[str, str, str]:
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


def mask_digits(value: str) -> str:
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
            evidence.append({"field": field, "kind": "regex", "pattern": pattern, "snippet": snippet})
    return evidence[:12]


def build_default_purpose(basis: dict[str, Any]) -> str:
    doc_type = str(basis.get("document_type") or "документу")
    doc_number = str(basis.get("document_number") or "")
    doc_date = str(basis.get("document_date") or "")
    period_label = str(basis.get("service_period_label") or "")

    if doc_number:
        if doc_date:
            purpose = f"Оплата по {doc_type.lower()} N {doc_number} от {doc_date}"
        else:
            purpose = f"Оплата по {doc_type.lower()} N {doc_number}"
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


WATER_UTILITY_SELECTED_ROWS = (4, 5, 6, 7)
MONEY_TOKEN_RE = re.compile(r"(?<!\d)(?:\d{1,3}(?:[ \xa0]\d{3})+|\d+)[,.:_]\d{2}(?!\d)")


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
    """Find the lowest unfilled row index 1..7 whose expected label matches."""
    if not label:
        return None
    for index, expected_label in enumerate(WATER_ROW_LABEL_SEQUENCE, start=1):
        if expected_label == label and index not in rows:
            return index
    return None


def collect_numbered_table_rows(text: str) -> dict[int, str]:
    rows: dict[int, str] = {}
    current: int | None = None
    stop_pattern = re.compile(
        r"^(?:итого|сумма\s+ндс|всего\s+с\s+ндс|задолженность|начислено|перерасч[её]ты|оплачено|всего\s+к\s+оплате)\b",
        re.IGNORECASE,
    )
    header_pattern = re.compile(r"^(?:N|№|товары|кол-во|ед\.?|цена|сумма)\b", re.IGNORECASE)
    # A row-label-only line is a strong signal that OCR garbled the leading
    # number (`6` → `©`, `1` → `т_`, ...). Such lines must not be folded into
    # the previous row as a continuation.
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
            # Some OCRs misread `6` as `5` (the previous row's marker). When
            # row 5 is already taken and the line clearly belongs to row 6 by
            # its label, push it to row 6.
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

        # Heuristic: line has no leading row digit but otherwise looks like a
        # water-utility data row (label + ≥3 money tokens). Slot it into the
        # next unfilled row whose expected label matches. Catches OCR misreads
        # of the row index (`6` → `©`/`o`, `1` → `т_`, ...).
        candidate_label = water_row_label(line)
        if candidate_label and len(water_money_values(line)) >= 3:
            inferred_row = _expected_water_row_for_label(rows, candidate_label)
            # Don't recover row 1 unless we're at the start of the table —
            # otherwise a stray label-bearing footnote could be mis-slotted.
            if inferred_row == 1 and current not in (None, 1):
                inferred_row = None
            if inferred_row is not None:
                stripped = re.sub(r"^[^\wа-яёА-ЯЁ]+", "", line).strip()
                rows[inferred_row] = stripped or line
                current = inferred_row
                continue

        if current is not None and not header_pattern.search(line) and not row_marker_pattern.search(line):
            rows[current] = f"{rows[current]} {line}".strip()

    return rows


def money_tokens(text: str) -> list[Decimal]:
    return [money(token) for token in MONEY_TOKEN_RE.findall(text)]


WATER_MONEY_TOKEN_RE = re.compile(
    r"(?<!\d)(?:\d{1,3}(?:[ \xa0]\d{3})+|\d+)[,.:_]\d{2}(?!\d)|(?<!\d)\d{4,7}(?!\d)"
)


def water_money_token(value: str) -> Decimal:
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


def money_close(left: Decimal, right: Decimal, tolerance: Decimal = Decimal("0.02")) -> bool:
    return abs(left - right) <= tolerance


def plausible_vat_ratio(base: Decimal, vat: Decimal) -> bool:
    if base <= 0 or vat < 0:
        return False
    ratio = vat / base
    return Decimal("0.05") <= ratio <= Decimal("0.35")


def shift_by_thousands(value: Decimal, target: Decimal, tolerance: Decimal = Decimal("0.20")) -> Decimal | None:
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


def water_row_amounts(text: str) -> tuple[str, str, str]:
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
    """In a column-major OCR layout, return the first `count` positive money
    tokens that appear after the given header line (which must be alone on
    its own line). Bails out on the next column header or invoice summary.
    """
    header_match = re.search(
        rf"^[ \t]*{header_pattern}[ \t]*$",
        text,
        re.IGNORECASE | re.MULTILINE,
    )
    if not header_match:
        return []
    tail = text[header_match.end():]
    stop = re.search(
        r"^[ \t]*(?:Кол-во|Ед\.?|Цена|Сумма(?:\s+(?:НДС|с\s+НДС))?|Всего|Итого|Задолженность|Начислено|Оплачено|Перерасч|по\s+счету)\b",
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
    """Reconstruct rows 1-7 of a water utility invoice from column-major OCR
    (typically macOS Vision text). Each money column is laid out as a
    single-line header followed by exactly 7 money tokens — one per row.

    Returns an empty dict when the column-major signature is absent, so this
    can be safely chained as a fallback to the row-major parser.
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


def water_selected_row_amounts(text: str) -> tuple[str, str, list[int]]:
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
        if reference and reference is not row and row["total"] > reference["total"] * Decimal("3"):
            row = reference
        elif (
            reference
            and reference is not row
            and money_close(row["total"], reference["total"], Decimal("1.00"))
            and plausible_vat_ratio(reference["base"], reference["vat"])
        ):
            row = reference
        selected_rows.append(row_number)
        total += row["total"]
        vat_total += row["vat"]

    if selected_rows != list(WATER_UTILITY_SELECTED_ROWS):
        return "", "", selected_rows
    if all(row_number in parsed for row_number in range(1, 8)):
        invoice_total = money(water_invoice_total_with_vat(text))
        parsed_invoice_total = sum(parsed[row_number]["total"] for row_number in range(1, 8))
        invoice_total_matches_rows = invoice_total > 0 and abs(parsed_invoice_total - invoice_total) <= Decimal("5.00")
        if invoice_total_matches_rows:
            selected_by_invoice_total = invoice_total - sum(parsed[row_number]["total"] for row_number in (1, 2, 3))
            if selected_by_invoice_total > 0 and abs(selected_by_invoice_total - total) <= Decimal("100.00"):
                total = selected_by_invoice_total

        invoice_vat = money(water_invoice_vat_total(text))
        if invoice_total_matches_rows and invoice_vat > 0:
            selected_vat_by_invoice_total = invoice_vat - sum(parsed[row_number]["vat"] for row_number in (1, 2, 3))
            if selected_vat_by_invoice_total > 0 and abs(selected_vat_by_invoice_total - vat_total) <= Decimal("20.00"):
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
    """Cross-check fallback: if the invoice's "Всего с НДС" line is present
    and rows 1-3 (the previous-period adjustment block) are all parsed, the
    selected rows 4-7 total equals invoice_total − Σ(rows 1-3). The same
    relation holds for VAT.

    Returns ("", "", []) when the cross-check is not applicable so the caller
    can keep trying other strategies.
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
    # Sanity-check: any rows 4-7 we DID parse should fit under derived_total.
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
    """If the OCR is column-major (one money column per line block),
    reconstruct rows 1-7 and return totals for rows 4-7. Returns empty when
    the column-major signature is absent."""
    parsed = parse_water_column_major_rows(text)
    if not parsed:
        return "", "", []
    total = sum((parsed[rn]["total"] for rn in WATER_UTILITY_SELECTED_ROWS), Decimal("0"))
    vat_total = sum((parsed[rn]["vat"] for rn in WATER_UTILITY_SELECTED_ROWS), Decimal("0"))
    if total <= 0:
        return "", "", []
    return fmt_money(total), fmt_money(vat_total), list(WATER_UTILITY_SELECTED_ROWS)


def water_utility_selected_row_totals(text: str) -> tuple[str, str, list[int]]:
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
    match = re.search(
        r"^\s*Поставщик\s*:?\s*(.+?)(?=\n\s*(?:Покупатель|Договор\s*/|Договор|N\s+Товары|№\s+Товары)|\Z)",
        text,
        re.IGNORECASE | re.DOTALL | re.MULTILINE,
    )
    if match:
        candidate = clean_name(match.group(1))
        # Vision OCR sometimes outputs "Поставщик:" / "Покупатель:" as bare
        # labels followed by the supplier+buyer names interleaved. Strip stray
        # leading label prefixes so the result is just the supplier name.
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
    # `N` can be followed by an OCR-introduced ordinal indicator (`º`, `°`,
    # `o`) that Vision often emits in place of `№`. Treat them as part of
    # the number-sign token rather than as the start of the document number.
    match = re.search(
        rf"\b(Сч[еёe]т)\s*(?:N[º°o]?|No|№)\s*([0-9][A-Za-zА-Яа-я0-9/_\-]*)\s*от\s*({DATE_PATTERN})",
        text,
        re.IGNORECASE,
    )
    if not match:
        return "", "", ""
    return "Счет", match.group(2), normalize_date(match.group(3))


def build_water_utility_purpose(basis: dict[str, Any]) -> str:
    doc_number = str(basis.get("document_number") or "")
    doc_date = str(basis.get("document_date") or "")
    period_label = str(basis.get("service_period_label") or "")

    if doc_number and doc_date:
        purpose = f"Оплата услуг водоснабжения и водоотведения по счету N {doc_number} от {doc_date}"
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


def require_service_period(basis: dict[str, Any], reason: str = "service_period_not_found") -> None:
    if basis.get("service_period_start") and basis.get("service_period_end"):
        return
    basis["requires_owner_review"] = True
    basis.setdefault("owner_review_reasons", []).append(reason)


ELECTRICITY_DOC_TYPE = "Акт приема-передачи электроэнергии"
ELECTRICITY_PAIR_RELATION = "electricity_actual_plus_advance"
ELECTRICITY_EXPENSE_RELATION = "electricity_actual_to_expense_accrual"
ELECTRICITY_WAITING_REASONS = {
    "electricity_pair_waiting_for_advance",
    "electricity_pair_waiting_for_actual",
}


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
    year = dt.date.today().year
    if re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", document_date):
        _, doc_month_text, doc_year_text = document_date.split(".")
        doc_month = int(doc_month_text)
        year = int(doc_year_text)
        if kind == "actual" and period_month > doc_month:
            year -= 1
        elif kind == "advance" and period_month < doc_month:
            year += 1
    return year


def find_electricity_service_period(text: str, kind: str, document_date: str) -> tuple[str, str, str]:
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


def line_amount(text: str, pattern: str, *, min_amount: Decimal = Decimal("0")) -> str:
    for raw_line in text.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not re.search(pattern, line, re.IGNORECASE):
            continue
        values = [value for value in money_tokens(line) if value >= min_amount]
        if values:
            return fmt_money(values[-1])
    return ""


def find_electricity_period_total(text: str) -> str:
    for pattern in (r"\bИТОГО\b", r"\bИтого\b", r"\bВсего\b"):
        amount = first_money_after(text, pattern)
        if amount:
            return amount
    return ""


def find_electricity_paid_advance_amount(text: str) -> str:
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
    row_total_floor = Decimal("100")
    energy_amount = line_amount(text, r"Электро\w*нерг\w*\s+за\s+[а-яё]+", min_amount=row_total_floor)
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


def build_electricity_merged_purpose(actual_basis: dict[str, Any], advance_basis: dict[str, Any]) -> str:
    actual_period = str(actual_basis.get("service_period_label") or "")
    advance_period = str(advance_basis.get("service_period_label") or "")
    doc_date = str(actual_basis.get("document_date") or "")
    purpose = "Оплата электроэнергии и потерь"
    if actual_period:
        purpose = f"{purpose} за {actual_period}"
    if advance_period:
        purpose = f"{purpose} и аванс за {advance_period}"
    if doc_date:
        purpose = f"{purpose} по акту от {doc_date}"
    return re.sub(r"\s+", " ", purpose).strip()[:210]


def next_month_start(value: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value or ""):
        return ""
    year, month, _ = (int(part) for part in value.split("-"))
    if month == 12:
        return f"{year + 1}-01-01"
    return f"{year}-{month + 1:02d}-01"


def electricity_periods_pair(actual_basis: dict[str, Any], advance_basis: dict[str, Any]) -> bool:
    actual_start = str(actual_basis.get("service_period_start") or "")
    advance_start = str(advance_basis.get("service_period_start") or "")
    return bool(actual_start and advance_start and next_month_start(actual_start) == advance_start)


def requisites_score(text: str) -> tuple[float, list[str]]:
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
        "dds_article_candidate": "",
        "pnl_article_candidate": "",
        "requires_owner_review": False,
        "raw_evidence": [],
        "source_file": str(metadata.get("source_file") or ""),
        "upload_id": str(metadata.get("upload_id") or ""),
        "message_metadata": {
            key: value
            for key, value in metadata.items()
            if key in {"source_channel", "sender", "chat_id", "message_id", "caption"}
            and value not in (None, "")
        },
    }


def common_basis(text: str, metadata: dict[str, Any], parser_name: str, source_type: str) -> dict[str, Any]:
    basis = empty_basis(parser_name, source_type, metadata)
    doc_type, doc_number, doc_date = find_document(text, source_type)
    period_start, period_end, period_label = find_service_period(text)
    vat_mode, vat_amount = find_vat(text)

    basis.update(
        {
            "counterparty_name": find_counterparty_name(text, source_type),
            "inn": find_labeled_digits(text, [r"ИНН"], (10, 12)),
            "kpp": find_labeled_digits(text, [r"КПП"], (9,)),
            "bank_bik": find_labeled_digits(text, [r"БИК"], (9,)),
            "bank_account": find_bank_account(text),
            "corr_account": find_corr_account(text),
            "bank_name": find_bank_name(text),
            "amount": find_amount(text, source_type),
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
    if not basis["kpp"] and len(basis["inn"]) == 12:
        basis["kpp"] = "0"
    basis["payment_purpose_suggestion"] = build_default_purpose(basis)
    enrich_known_counterparty(basis, text)
    basis["raw_evidence"] = build_evidence(text, basis)
    return basis


def enrich_known_counterparty(basis: dict[str, Any], text: str) -> None:
    haystack = normalize_key(" ".join([text, str(basis.get("counterparty_name") or "")]))
    for rule in KNOWN_COUNTERPARTIES:
        for alias in rule["aliases"]:
            if normalize_key(alias) not in haystack:
                continue
            basis["counterparty_alias"] = rule["canonical"]
            if not basis.get("counterparty_name"):
                basis["counterparty_name"] = rule["canonical"]
            basis["dds_article_candidate"] = rule.get("dds", "")
            basis["pnl_article_candidate"] = rule.get("pnl", "")
            if rule.get("review"):
                basis["requires_owner_review"] = True
                basis.setdefault("owner_review_reasons", []).append(rule.get("review_reason", "known_counterparty_review"))
            return

    if not basis.get("dds_article_candidate") and basis.get("source_type") in {"invoice", "upd", "waybill"}:
        lower = haystack
        if any(word in lower for word in ("товар", "продукт", "поставка", "накладная")):
            basis["dds_article_candidate"] = "Оплата поставщикам"
            basis["pnl_article_candidate"] = "Себестоимость продуктов через iiko"


class BaseRegexParser:
    name = "base"
    source_type = "generic"
    keywords: tuple[tuple[str, float, str], ...] = ()

    def detect(self, text: str, metadata: dict[str, Any]) -> DetectionResult:
        lower = normalize_key(text)
        score = 0.0
        reasons: list[str] = []
        for pattern, points, reason in self.keywords:
            if re.search(pattern, lower, re.IGNORECASE):
                score += points
                reasons.append(reason)
        req_score, req_reasons = requisites_score(text)
        score += req_score
        reasons.extend(req_reasons)
        return DetectionResult(self.name, self.source_type, min(score, 0.99), reasons)

    def parse(self, text: str, metadata: dict[str, Any]) -> dict[str, Any]:
        return common_basis(text, metadata, self.name, self.source_type)


class WaterUtilityInvoiceParser(BaseRegexParser):
    name = "water_utility_invoice_v1"
    source_type = "invoice"

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
            basis["amount"] = ""
            basis["vat_amount"] = ""
            basis["payment_purpose_suggestion"] = build_water_utility_purpose(basis)
            basis["raw_evidence"] = build_evidence(text, basis)
            basis["requires_owner_review"] = True
            basis.setdefault("owner_review_reasons", []).append("water_utility_rows_4_7_not_found")
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


class ElectricityActParser(BaseRegexParser):
    name = "electricity_act_v1"
    source_type = "utility_act"

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
        energy_amount, losses_amount, period_amount, paid_advance, amount_due = find_electricity_actual_amounts(text)
        advance_amount = find_electricity_advance_amount(text)
        if not kind:
            if period_amount and (losses_amount or amount_due or paid_advance):
                kind = "actual"
            elif advance_amount:
                kind = "advance"
        period_start, period_end, period_label = find_electricity_service_period(text, kind, document_date)
        supplier_name = find_electricity_supplier_name(text) or find_counterparty_name(text, self.source_type)
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
                "electricity_pair_status": "unpaired",
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
            basis.setdefault("owner_review_reasons", []).append("electricity_pair_waiting_for_actual")
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
            basis.setdefault("owner_review_reasons", []).append("electricity_pair_waiting_for_advance")

        if not kind:
            basis.setdefault("owner_review_reasons", []).append("electricity_act_kind_not_detected")
        require_service_period(basis)
        if kind == "actual" and not basis.get("electricity_amount_due"):
            basis.setdefault("owner_review_reasons", []).append("electricity_amount_due_not_found")
        if kind == "advance" and not basis.get("electricity_advance_amount"):
            basis.setdefault("owner_review_reasons", []).append("electricity_advance_amount_not_found")

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


class InvoiceParser(BaseRegexParser):
    name = "invoice_ru_v1"
    source_type = "invoice"
    keywords = (
        (r"сч[еe]т\s+N?\s*\d+", 0.22, "invoice_number"),
        (r"сч[еe]т\s+на\s+оплату", 0.35, "invoice_title"),
        (r"сч[еe]т[-\s]?договор", 0.25, "invoice_contract"),
        (r"поставщик|исполнитель", 0.08, "supplier_label"),
        (r"покупатель|заказчик", 0.05, "buyer_label"),
    )


class UPDParser(BaseRegexParser):
    name = "upd_ru_v1"
    source_type = "upd"
    keywords = (
        (r"универсальн\w+\s+передаточн\w+\s+документ", 0.4, "upd_title"),
        (r"\bупд\b", 0.3, "upd_abbreviation"),
        (r"статус\s*[:\-]?\s*[12]", 0.08, "upd_status"),
        (r"продавец|покупатель", 0.06, "seller_buyer_labels"),
    )


class WaybillParser(BaseRegexParser):
    name = "waybill_ru_v1"
    source_type = "waybill"
    keywords = (
        (r"товарн\w+\s+накладн", 0.35, "waybill_title"),
        (r"торг[-\s]?12", 0.3, "torg12"),
        (r"\bнакладная\b", 0.18, "nakladnaya"),
        (r"грузополучатель|грузоотправитель", 0.06, "cargo_labels"),
    )


class ReceiptParser(BaseRegexParser):
    name = "cash_receipt_ru_v1"
    source_type = "receipt"
    keywords = (
        (r"кассов\w+\s+чек", 0.35, "cash_receipt_title"),
        (r"фискальн|фн\b|фд\b|фп\b", 0.22, "fiscal_markers"),
        (r"\bитог\b|итого", 0.08, "receipt_total"),
        (r"признак\s+расч[еe]та|приход", 0.06, "receipt_operation_marker"),
    )

    def parse(self, text: str, metadata: dict[str, Any]) -> dict[str, Any]:
        basis = super().parse(text, metadata)
        basis["requires_owner_review"] = True
        basis.setdefault("owner_review_reasons", []).append("receipt_requires_payment_context")
        return basis


class PaymentOrderParser(BaseRegexParser):
    name = "payment_order_ru_v1"
    source_type = "payment_order"
    keywords = (
        (r"плат[еe]жн\w+\s+поручени", 0.4, "payment_order_title"),
        (r"вид\s+оп\.|очер\w+\s+плат", 0.08, "payment_order_fields"),
        (r"сумма\s+прописью", 0.1, "amount_words"),
        (r"получатель|плательщик", 0.08, "payer_payee_labels"),
    )


class KnownCounterpartyParser(BaseRegexParser):
    name = "known_counterparty_v1"
    source_type = "known_counterparty"

    def detect(self, text: str, metadata: dict[str, Any]) -> DetectionResult:
        lower = normalize_key(text)
        for rule in KNOWN_COUNTERPARTIES:
            for alias in rule["aliases"]:
                if normalize_key(alias) in lower:
                    req_score, req_reasons = requisites_score(text)
                    reasons = [f"known_counterparty:{rule['canonical']}"] + req_reasons
                    return DetectionResult(self.name, self.source_type, min(0.48 + req_score, 0.94), reasons)
        return DetectionResult(self.name, self.source_type, 0.0, [])


class GenericRequisitesParser(BaseRegexParser):
    name = "generic_requisites_v1"
    source_type = "generic"
    keywords = (
        (r"инн|кпп|бик|р\/с|к\/с", 0.08, "requisites_labels"),
    )


DEFAULT_REGISTRY: list[PaymentParser] = [
    WaterUtilityInvoiceParser(),
    ElectricityActParser(),
    InvoiceParser(),
    UPDParser(),
    WaybillParser(),
    ReceiptParser(),
    PaymentOrderParser(),
    KnownCounterpartyParser(),
    GenericRequisitesParser(),
]


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


def missing_ready_fields(basis: dict[str, Any]) -> list[str]:
    missing = [field for field in PAYMENT_READY_FIELDS if basis.get(field) in (None, "")]
    if basis.get("inn") and len(clean_digits(basis.get("inn"))) == 10 and not basis.get("kpp"):
        missing.append("kpp")
    return missing


def candidate_hash(basis: dict[str, Any]) -> str:
    parts = [
        clean_digits(basis.get("inn")),
        clean_digits(basis.get("bank_account")),
        fmt_money(basis.get("amount")) if basis.get("amount") else "",
        str(basis.get("document_type") or "").casefold(),
        str(basis.get("document_number") or "").casefold(),
        str(basis.get("document_date") or ""),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def expense_accrual_hash(basis: dict[str, Any]) -> str:
    parts = [
        clean_digits(basis.get("inn")),
        str(basis.get("document_type") or "").casefold(),
        str(basis.get("document_number") or "").casefold(),
        str(basis.get("document_date") or ""),
        str(basis.get("service_period_start") or ""),
        str(basis.get("service_period_end") or ""),
        fmt_money(basis.get("electricity_period_amount") or basis.get("amount")),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def review_state_for_basis(basis: dict[str, Any]) -> tuple[str, bool, list[str]]:
    missing = missing_ready_fields(basis)
    reasons = {
        str(reason)
        for reason in (basis.get("owner_review_reasons") or [])
        if reason and str(reason) not in ELECTRICITY_WAITING_REASONS
    }
    reasons = {reason for reason in reasons if not reason.startswith("missing_required_fields:")}

    confidence = float(basis.get("recognition_confidence") or 0)
    if confidence and confidence < LOW_CONFIDENCE_THRESHOLD:
        reasons.add("low_confidence")
    if missing:
        reasons.add("missing_required_fields:" + ",".join(missing))

    if missing:
        status = "owner_review"
    elif "low_confidence" in reasons:
        status = "low_confidence"
    elif reasons:
        status = "owner_review"
    else:
        status = "parsed_ready"
    return status, bool(reasons), sorted(reasons)


def expense_state_for_basis(basis: dict[str, Any]) -> tuple[str, bool, list[str]]:
    missing: list[str] = []
    if not basis.get("counterparty_name"):
        missing.append("counterparty_name")
    if not basis.get("inn"):
        missing.append("inn")
    if not basis.get("electricity_period_amount") and not basis.get("amount"):
        missing.append("amount")
    if not basis.get("service_period_start") or not basis.get("service_period_end"):
        missing.append("service_period")

    reasons = [f"missing_expense_fields:{','.join(missing)}"] if missing else []
    return ("owner_review" if missing else "parsed_ready", bool(missing), reasons)


def electricity_expense_accrual_basis(basis: dict[str, Any], *, payment_candidate_id: str) -> dict[str, Any] | None:
    if basis.get("parser_name") != "electricity_act_v1":
        return None
    if basis.get("electricity_act_kind") != "actual":
        return None
    amount = str(basis.get("electricity_period_amount") or "")
    if not amount:
        return None

    accrual = {
        "source_type": "expense_accrual",
        "parser_name": basis.get("parser_name", ""),
        "counterparty_name": basis.get("counterparty_name", ""),
        "inn": basis.get("inn", ""),
        "document_type": basis.get("document_type", ""),
        "document_number": basis.get("document_number", ""),
        "document_date": basis.get("document_date", ""),
        "service_period_start": basis.get("service_period_start", ""),
        "service_period_end": basis.get("service_period_end", ""),
        "service_period_label": basis.get("service_period_label", ""),
        "amount": amount,
        "pnl_article_candidate": basis.get("pnl_article_candidate", ""),
        "dds_article_candidate": basis.get("dds_article_candidate", ""),
        "expense_kind": "electricity_period_expense",
        "expense_source": "electricity_actual_act",
        "payment_candidate_id": payment_candidate_id,
        "payment_amount_candidate": basis.get("amount", ""),
        "raw_evidence": [
            {
                "field": "amount",
                "kind": "source_rule",
                "pattern": "electricity_energy_plus_losses",
                "snippet": "period_amount",
            }
        ],
    }
    status, requires_review, reasons = expense_state_for_basis(accrual)
    accrual["status"] = status
    accrual["requires_owner_review"] = requires_review
    accrual["owner_review_reasons"] = reasons
    accrual["accrual_hash"] = expense_accrual_hash(accrual)
    return accrual


def merge_electricity_pair_basis(
    actual_basis: dict[str, Any],
    advance_basis: dict[str, Any],
    *,
    actual_candidate_id: str,
    advance_candidate_id: str,
) -> tuple[dict[str, Any], str, bool]:
    merged = dict(actual_basis)
    amount_due = money(merged.get("electricity_amount_due") or merged.get("amount"))
    advance_amount = money(advance_basis.get("electricity_advance_amount") or advance_basis.get("amount"))
    payment_amount = fmt_money(amount_due + advance_amount)
    merged.update(
        {
            "amount": payment_amount,
            "electricity_advance_amount": fmt_money(advance_amount),
            "electricity_payment_amount": payment_amount,
            "electricity_pair_status": "merged",
            "linked_payment_basis_refs": [
                f"actual_candidate:{actual_candidate_id}",
                f"advance_candidate:{advance_candidate_id}",
            ],
            "payment_purpose_suggestion": build_electricity_merged_purpose(merged, advance_basis),
        }
    )
    merged["raw_evidence"] = list(merged.get("raw_evidence") or [])
    merged["raw_evidence"].append(
        {
            "field": "amount",
            "kind": "source_rule",
            "pattern": "electricity_amount_due_plus_advance",
            "snippet": "amount_due_plus_advance",
        }
    )
    status, requires_review, reasons = review_state_for_basis(merged)
    merged["requires_owner_review"] = requires_review
    merged["owner_review_reasons"] = reasons
    merged["candidate_hash"] = candidate_hash(merged)
    return merged, status, requires_review


def parse_text(
    text: str,
    metadata: dict[str, Any] | None = None,
    registry: list[PaymentParser] | None = None,
) -> dict[str, Any]:
    metadata = dict(metadata or {})
    normalized = normalize_text(text)
    parsers = registry or DEFAULT_REGISTRY
    detections = [parser.detect(normalized, metadata) for parser in parsers]
    detections.sort(key=lambda item: item.confidence, reverse=True)
    best_detection = detections[0] if detections else DetectionResult("none", "unknown", 0.0, [])
    parser_by_name = {parser.name: parser for parser in parsers}
    best_parser = parser_by_name.get(best_detection.parser_name, GenericRequisitesParser())
    if best_detection.confidence <= 0:
        best_parser = GenericRequisitesParser()
        best_detection = best_parser.detect(normalized, metadata)

    basis = best_parser.parse(normalized, metadata)
    field_score = completeness_score(basis)
    confidence = round(min(1.0, best_detection.confidence * 0.65 + field_score * 0.35), 3)
    basis["recognition_confidence"] = confidence

    missing = missing_ready_fields(basis)
    review_reasons = list(basis.get("owner_review_reasons") or [])
    if confidence < LOW_CONFIDENCE_THRESHOLD:
        review_reasons.append("low_confidence")
    if missing:
        review_reasons.append("missing_required_fields:" + ",".join(missing))

    requires_review = bool(review_reasons or basis.get("requires_owner_review"))
    if missing:
        status = "owner_review"
    elif confidence < LOW_CONFIDENCE_THRESHOLD:
        status = "low_confidence"
    elif requires_review:
        status = "owner_review"
    else:
        status = "parsed_ready"

    basis["requires_owner_review"] = requires_review
    basis["owner_review_reasons"] = sorted(set(review_reasons))
    basis["candidate_hash"] = candidate_hash(basis)
    basis["raw_evidence"].extend(
        [
            {
                "field": "parser",
                "kind": "detection",
                "pattern": best_detection.parser_name,
                "snippet": ",".join(best_detection.reasons[:5]),
            }
        ]
    )

    return {
        "payment_basis": {field: basis.get(field, "") for field in PAYMENT_BASIS_FIELDS if field in basis}
        | {
            "counterparty_alias": basis.get("counterparty_alias", ""),
            "owner_review_reasons": basis.get("owner_review_reasons", []),
            "candidate_hash": basis.get("candidate_hash", ""),
        },
        "detections": [detection.as_dict() for detection in detections if detection.confidence > 0],
        "best_detection": best_detection.as_dict(),
        "missing_required_fields": missing,
        "requires_owner_review": requires_review,
        "owner_review_reasons": sorted(set(review_reasons)),
        "status": status,
        "text_sha256": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
    }


def payment_basis_to_raw_payment(basis: dict[str, Any]) -> dict[str, Any]:
    raw = {
        "recipientName": basis.get("counterparty_name", ""),
        "inn": basis.get("inn", ""),
        "kpp": basis.get("kpp", "") or "0",
        "bankBik": basis.get("bank_bik", ""),
        "bankAcnt": basis.get("bank_account", ""),
        "recipientBankName": basis.get("bank_name", ""),
        "recipientCorrAccountNumber": basis.get("corr_account", ""),
        "amount": basis.get("amount", ""),
        "paymentPurpose": basis.get("payment_purpose_suggestion", ""),
        "sourceDocumentType": basis.get("document_type", ""),
        "sourceDocumentNumber": basis.get("document_number", ""),
        "sourceDocumentDate": basis.get("document_date", ""),
        "recognition": {
            "parser_name": basis.get("parser_name", ""),
            "source_type": basis.get("source_type", ""),
            "confidence": basis.get("recognition_confidence", 0),
            "requires_owner_review": bool(basis.get("requires_owner_review")),
            "owner_review_reasons": basis.get("owner_review_reasons", []),
        },
        "intake_requires_owner_review": bool(basis.get("requires_owner_review")),
        "intake_status": "owner_review" if basis.get("requires_owner_review") else "parsed_ready",
        "payment_basis": basis,
    }
    return {key: value for key, value in raw.items() if value not in (None, "")}


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def json_loads(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


class PaymentIntakeStore:
    """Private SQLite storage for document intake and parser output."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def init_db(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS uploads (
                    upload_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    source_channel TEXT,
                    sender TEXT,
                    chat_id TEXT,
                    message_id TEXT,
                    caption TEXT,
                    original_file_name TEXT,
                    stored_file TEXT,
                    mime_type TEXT,
                    source_file_sha256 TEXT,
                    metadata_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS parsed_documents (
                    parsed_id TEXT PRIMARY KEY,
                    upload_id TEXT,
                    source_file TEXT,
                    text_sha256 TEXT,
                    parser_name TEXT,
                    source_type TEXT,
                    confidence REAL,
                    status TEXT,
                    requires_owner_review INTEGER NOT NULL DEFAULT 0,
                    payment_basis_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS parser_runs (
                    run_id TEXT PRIMARY KEY,
                    parsed_id TEXT NOT NULL,
                    upload_id TEXT,
                    parser_name TEXT NOT NULL,
                    source_type TEXT,
                    confidence REAL,
                    reasons_json TEXT NOT NULL,
                    status TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS payment_order_candidates (
                    candidate_id TEXT PRIMARY KEY,
                    parsed_id TEXT NOT NULL,
                    upload_id TEXT,
                    status TEXT NOT NULL,
                    request_file TEXT,
                    counterparty_name TEXT,
                    inn TEXT,
                    kpp TEXT,
                    bank_bik TEXT,
                    bank_account TEXT,
                    amount TEXT,
                    document_type TEXT,
                    document_number TEXT,
                    document_date TEXT,
                    dds_article_candidate TEXT,
                    pnl_article_candidate TEXT,
                    requires_owner_review INTEGER NOT NULL DEFAULT 0,
                    candidate_hash TEXT,
                    payment_basis_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS expense_accrual_candidates (
                    accrual_id TEXT PRIMARY KEY,
                    parsed_id TEXT NOT NULL,
                    upload_id TEXT,
                    payment_candidate_id TEXT,
                    status TEXT NOT NULL,
                    counterparty_name TEXT,
                    inn TEXT,
                    amount TEXT,
                    document_type TEXT,
                    document_number TEXT,
                    document_date TEXT,
                    service_period_start TEXT,
                    service_period_end TEXT,
                    service_period_label TEXT,
                    pnl_article_candidate TEXT,
                    dds_article_candidate TEXT,
                    requires_owner_review INTEGER NOT NULL DEFAULT 0,
                    accrual_hash TEXT,
                    accrual_basis_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS document_links (
                    link_id TEXT PRIMARY KEY,
                    upload_id TEXT,
                    parsed_id TEXT,
                    candidate_id TEXT,
                    relation_type TEXT NOT NULL,
                    linked_id TEXT,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_parsed_upload ON parsed_documents(upload_id);
                CREATE INDEX IF NOT EXISTS idx_candidates_status ON payment_order_candidates(status);
                CREATE INDEX IF NOT EXISTS idx_candidates_hash ON payment_order_candidates(candidate_hash);
                CREATE INDEX IF NOT EXISTS idx_expense_accruals_status ON expense_accrual_candidates(status);
                CREATE INDEX IF NOT EXISTS idx_expense_accruals_hash ON expense_accrual_candidates(accrual_hash);
                CREATE INDEX IF NOT EXISTS idx_expense_accruals_period ON expense_accrual_candidates(service_period_start, service_period_end);
                CREATE INDEX IF NOT EXISTS idx_uploads_file_hash ON uploads(source_file_sha256);
                """
            )

    def save_upload(self, metadata: dict[str, Any]) -> None:
        upload_id = str(metadata.get("upload_id") or "")
        if not upload_id:
            raise ValueError("upload_id is required")
        with self.connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO uploads (
                    upload_id, created_at, source_channel, sender, chat_id, message_id,
                    caption, original_file_name, stored_file, mime_type, source_file_sha256,
                    metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    upload_id,
                    str(metadata.get("created_at") or dt.datetime.now().replace(microsecond=0).isoformat()),
                    str(metadata.get("source_channel") or ""),
                    str(metadata.get("sender") or ""),
                    str(metadata.get("chat_id") or ""),
                    str(metadata.get("message_id") or ""),
                    str(metadata.get("caption") or ""),
                    str(metadata.get("original_file_name") or ""),
                    str(metadata.get("stored_file") or ""),
                    str(metadata.get("mime_type") or ""),
                    str(metadata.get("source_file_sha256") or ""),
                    json_dumps(metadata),
                ),
            )

    def duplicate_candidate_exists(self, candidate_key: str) -> bool:
        if not candidate_key:
            return False
        with self.connect() as connection:
            row = connection.execute(
                "SELECT candidate_id FROM payment_order_candidates WHERE candidate_hash = ? LIMIT 1",
                (candidate_key,),
            ).fetchone()
        return row is not None

    def _insert_expense_accrual(
        self,
        connection: sqlite3.Connection,
        *,
        basis: dict[str, Any],
        parsed_id: str,
        upload_id: str,
        payment_candidate_id: str,
        created_at: str,
    ) -> str:
        accrual = electricity_expense_accrual_basis(basis, payment_candidate_id=payment_candidate_id)
        if not accrual:
            return ""
        accrual_id = f"accrual_{uuid.uuid4().hex[:12]}"
        connection.execute(
            """
            INSERT INTO expense_accrual_candidates (
                accrual_id, parsed_id, upload_id, payment_candidate_id, status,
                counterparty_name, inn, amount, document_type, document_number,
                document_date, service_period_start, service_period_end,
                service_period_label, pnl_article_candidate, dds_article_candidate,
                requires_owner_review, accrual_hash, accrual_basis_json,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                accrual_id,
                parsed_id,
                upload_id,
                payment_candidate_id,
                str(accrual.get("status") or "owner_review"),
                str(accrual.get("counterparty_name") or ""),
                clean_digits(accrual.get("inn")),
                str(accrual.get("amount") or ""),
                str(accrual.get("document_type") or ""),
                str(accrual.get("document_number") or ""),
                str(accrual.get("document_date") or ""),
                str(accrual.get("service_period_start") or ""),
                str(accrual.get("service_period_end") or ""),
                str(accrual.get("service_period_label") or ""),
                str(accrual.get("pnl_article_candidate") or ""),
                str(accrual.get("dds_article_candidate") or ""),
                1 if accrual.get("requires_owner_review") else 0,
                str(accrual.get("accrual_hash") or ""),
                json_dumps(accrual),
                created_at,
                created_at,
            ),
        )
        connection.execute(
            """
            INSERT INTO document_links (
                link_id, upload_id, parsed_id, candidate_id, relation_type,
                linked_id, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"link_{uuid.uuid4().hex[:12]}",
                upload_id,
                parsed_id,
                payment_candidate_id,
                ELECTRICITY_EXPENSE_RELATION,
                accrual_id,
                json_dumps({"rule": "electricity_energy_plus_losses"}),
                created_at,
            ),
        )
        return accrual_id

    def _sync_expense_accrual_payment_amount(
        self,
        connection: sqlite3.Connection,
        *,
        payment_candidate_id: str,
        payment_amount: str,
        updated_at: str,
    ) -> None:
        rows = connection.execute(
            """
            SELECT accrual_id, accrual_basis_json
            FROM expense_accrual_candidates
            WHERE payment_candidate_id = ?
            """,
            (payment_candidate_id,),
        ).fetchall()
        for row in rows:
            accrual = json_loads(row["accrual_basis_json"], {})
            accrual["payment_amount_candidate"] = payment_amount
            connection.execute(
                """
                UPDATE expense_accrual_candidates
                SET accrual_basis_json = ?, updated_at = ?
                WHERE accrual_id = ?
                """,
                (json_dumps(accrual), updated_at, row["accrual_id"]),
            )

    def _find_electricity_partner(
        self,
        connection: sqlite3.Connection,
        *,
        current_candidate_id: str,
        basis: dict[str, Any],
    ) -> dict[str, Any] | None:
        if basis.get("parser_name") != "electricity_act_v1":
            return None
        kind = str(basis.get("electricity_act_kind") or "")
        if kind not in {"actual", "advance"}:
            return None
        opposite = "advance" if kind == "actual" else "actual"
        rows = connection.execute(
            """
            SELECT * FROM payment_order_candidates
            WHERE candidate_id != ?
              AND inn = ?
              AND document_date = ?
              AND document_type = ?
            ORDER BY created_at DESC
            """,
            (
                current_candidate_id,
                clean_digits(basis.get("inn")),
                str(basis.get("document_date") or ""),
                ELECTRICITY_DOC_TYPE,
            ),
        ).fetchall()
        for row in rows:
            partner_basis = json_loads(row["payment_basis_json"], {})
            if partner_basis.get("parser_name") != "electricity_act_v1":
                continue
            if partner_basis.get("electricity_act_kind") != opposite:
                continue
            if partner_basis.get("electricity_pair_status") == "merged":
                continue
            actual_basis = basis if kind == "actual" else partner_basis
            advance_basis = partner_basis if kind == "actual" else basis
            if not actual_basis.get("electricity_amount_due") or not advance_basis.get("electricity_advance_amount"):
                continue
            if not electricity_periods_pair(actual_basis, advance_basis):
                continue
            payload = dict(row)
            payload["payment_basis"] = partner_basis
            return payload
        return None

    def _mark_electricity_advance_linked(
        self,
        connection: sqlite3.Connection,
        *,
        advance_candidate_id: str,
        advance_parsed_id: str,
        advance_basis: dict[str, Any],
        actual_candidate_id: str,
        updated_at: str,
    ) -> None:
        linked = dict(advance_basis)
        linked["electricity_pair_status"] = "linked"
        linked["linked_payment_basis_refs"] = [
            f"actual_candidate:{actual_candidate_id}",
            f"advance_candidate:{advance_candidate_id}",
        ]
        reasons = {
            str(reason)
            for reason in (linked.get("owner_review_reasons") or [])
            if reason and str(reason) != "electricity_pair_waiting_for_actual"
        }
        reasons.add("electricity_advance_linked_to_actual_candidate")
        linked["requires_owner_review"] = True
        linked["owner_review_reasons"] = sorted(reasons)
        connection.execute(
            """
            UPDATE payment_order_candidates
            SET status = ?, requires_owner_review = ?, payment_basis_json = ?, updated_at = ?
            WHERE candidate_id = ?
            """,
            ("owner_review", 1, json_dumps(linked), updated_at, advance_candidate_id),
        )
        connection.execute(
            """
            UPDATE parsed_documents
            SET status = ?, requires_owner_review = ?, payment_basis_json = ?
            WHERE parsed_id = ?
            """,
            ("owner_review", 1, json_dumps(linked), advance_parsed_id),
        )

    def _insert_electricity_pair_link(
        self,
        connection: sqlite3.Connection,
        *,
        actual_candidate_id: str,
        actual_parsed_id: str,
        upload_id: str,
        advance_candidate_id: str,
        created_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO document_links (
                link_id, upload_id, parsed_id, candidate_id, relation_type,
                linked_id, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"link_{uuid.uuid4().hex[:12]}",
                upload_id,
                actual_parsed_id,
                actual_candidate_id,
                ELECTRICITY_PAIR_RELATION,
                advance_candidate_id,
                json_dumps({"rule": "amount_due_plus_advance"}),
                created_at,
            ),
        )

    def _maybe_merge_electricity_pair(
        self,
        connection: sqlite3.Connection,
        *,
        current_candidate_id: str,
        current_parsed_id: str,
        current_upload_id: str,
        basis: dict[str, Any],
        created_at: str,
    ) -> str | None:
        partner = self._find_electricity_partner(
            connection,
            current_candidate_id=current_candidate_id,
            basis=basis,
        )
        if not partner:
            return None

        current_is_actual = basis.get("electricity_act_kind") == "actual"
        if current_is_actual:
            actual_basis = basis
            actual_candidate_id = current_candidate_id
            actual_parsed_id = current_parsed_id
            actual_upload_id = current_upload_id
            advance_basis = dict(partner["payment_basis"])
            advance_candidate_id = str(partner["candidate_id"])
            advance_parsed_id = str(partner["parsed_id"])
        else:
            actual_basis = dict(partner["payment_basis"])
            actual_candidate_id = str(partner["candidate_id"])
            actual_parsed_id = str(partner["parsed_id"])
            actual_upload_id = str(partner["upload_id"] or current_upload_id)
            advance_basis = basis
            advance_candidate_id = current_candidate_id
            advance_parsed_id = current_parsed_id

        merged_basis, merged_status, merged_requires_review = merge_electricity_pair_basis(
            actual_basis,
            advance_basis,
            actual_candidate_id=actual_candidate_id,
            advance_candidate_id=advance_candidate_id,
        )
        connection.execute(
            """
            UPDATE payment_order_candidates
            SET status = ?, amount = ?, requires_owner_review = ?, candidate_hash = ?,
                payment_basis_json = ?, updated_at = ?
            WHERE candidate_id = ?
            """,
            (
                merged_status,
                str(merged_basis.get("amount") or ""),
                1 if merged_requires_review else 0,
                str(merged_basis.get("candidate_hash") or ""),
                json_dumps(merged_basis),
                created_at,
                actual_candidate_id,
            ),
        )
        connection.execute(
            """
            UPDATE parsed_documents
            SET status = ?, requires_owner_review = ?, payment_basis_json = ?
            WHERE parsed_id = ?
            """,
            (
                merged_status,
                1 if merged_requires_review else 0,
                json_dumps(merged_basis),
                actual_parsed_id,
            ),
        )
        self._mark_electricity_advance_linked(
            connection,
            advance_candidate_id=advance_candidate_id,
            advance_parsed_id=advance_parsed_id,
            advance_basis=advance_basis,
            actual_candidate_id=actual_candidate_id,
            updated_at=created_at,
        )
        self._insert_electricity_pair_link(
            connection,
            actual_candidate_id=actual_candidate_id,
            actual_parsed_id=actual_parsed_id,
            upload_id=actual_upload_id,
            advance_candidate_id=advance_candidate_id,
            created_at=created_at,
        )
        self._sync_expense_accrual_payment_amount(
            connection,
            payment_candidate_id=actual_candidate_id,
            payment_amount=str(merged_basis.get("amount") or ""),
            updated_at=created_at,
        )
        return merged_status if current_is_actual else None

    def save_parse_result(
        self,
        parse_result: dict[str, Any],
        *,
        text: str,
        metadata: dict[str, Any],
    ) -> dict[str, str]:
        basis = dict(parse_result["payment_basis"])
        parsed_id = f"parsed_{uuid.uuid4().hex[:12]}"
        candidate_id = f"cand_{uuid.uuid4().hex[:12]}"
        upload_id = str(basis.get("upload_id") or metadata.get("upload_id") or "")
        created_at = dt.datetime.now().replace(microsecond=0).isoformat()
        text_sha = parse_result.get("text_sha256") or hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()
        status = str(parse_result.get("status") or "owner_review")
        if self.duplicate_candidate_exists(str(basis.get("candidate_hash") or "")):
            status = "duplicate"
            basis["requires_owner_review"] = True
            reasons = set(basis.get("owner_review_reasons") or [])
            reasons.add("duplicate_candidate_hash")
            basis["owner_review_reasons"] = sorted(reasons)

        basis["upload_id"] = upload_id
        basis["source_file"] = str(metadata.get("source_file") or basis.get("source_file") or "")
        requires_review = bool(basis.get("requires_owner_review") or status in {"owner_review", "low_confidence", "duplicate"})
        if status == "parsed_ready" and requires_review:
            status = "owner_review"

        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO parsed_documents (
                    parsed_id, upload_id, source_file, text_sha256, parser_name, source_type,
                    confidence, status, requires_owner_review, payment_basis_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    parsed_id,
                    upload_id,
                    str(basis.get("source_file") or ""),
                    text_sha,
                    str(basis.get("parser_name") or ""),
                    str(basis.get("source_type") or ""),
                    float(basis.get("recognition_confidence") or 0),
                    status,
                    1 if requires_review else 0,
                    json_dumps(basis),
                    created_at,
                ),
            )
            for detection in parse_result.get("detections", []):
                connection.execute(
                    """
                    INSERT INTO parser_runs (
                        run_id, parsed_id, upload_id, parser_name, source_type, confidence,
                        reasons_json, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"run_{uuid.uuid4().hex[:12]}",
                        parsed_id,
                        upload_id,
                        str(detection.get("parser_name") or ""),
                        str(detection.get("source_type") or ""),
                        float(detection.get("confidence") or 0),
                        json_dumps(detection.get("reasons") or []),
                        status,
                        created_at,
                    ),
                )
            connection.execute(
                """
                INSERT INTO payment_order_candidates (
                    candidate_id, parsed_id, upload_id, status, request_file,
                    counterparty_name, inn, kpp, bank_bik, bank_account, amount,
                    document_type, document_number, document_date, dds_article_candidate,
                    pnl_article_candidate, requires_owner_review, candidate_hash,
                    payment_basis_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate_id,
                    parsed_id,
                    upload_id,
                    status,
                    "",
                    str(basis.get("counterparty_name") or ""),
                    clean_digits(basis.get("inn")),
                    clean_digits(basis.get("kpp")),
                    clean_digits(basis.get("bank_bik")),
                    clean_digits(basis.get("bank_account")),
                    str(basis.get("amount") or ""),
                    str(basis.get("document_type") or ""),
                    str(basis.get("document_number") or ""),
                    str(basis.get("document_date") or ""),
                    str(basis.get("dds_article_candidate") or ""),
                    str(basis.get("pnl_article_candidate") or ""),
                    1 if requires_review else 0,
                    str(basis.get("candidate_hash") or ""),
                    json_dumps(basis),
                    created_at,
                    created_at,
                ),
            )
            self._insert_expense_accrual(
                connection,
                basis=basis,
                parsed_id=parsed_id,
                upload_id=upload_id,
                payment_candidate_id=candidate_id,
                created_at=created_at,
            )
            merged_status = self._maybe_merge_electricity_pair(
                connection,
                current_candidate_id=candidate_id,
                current_parsed_id=parsed_id,
                current_upload_id=upload_id,
                basis=basis,
                created_at=created_at,
            )
            if merged_status:
                status = merged_status
        return {"parsed_id": parsed_id, "candidate_id": candidate_id, "status": status}

    def get_parsed(self, parsed_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM parsed_documents WHERE parsed_id = ?",
                (parsed_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"parsed_id not found: {parsed_id}")
        payload = dict(row)
        payload["payment_basis"] = json_loads(payload.pop("payment_basis_json"), {})
        return payload

    def get_candidate(self, candidate_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM payment_order_candidates WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"candidate_id not found: {candidate_id}")
        payload = dict(row)
        payload["payment_basis"] = json_loads(payload.pop("payment_basis_json"), {})
        return payload

    def get_candidate_by_parsed_id(self, parsed_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM payment_order_candidates WHERE parsed_id = ? ORDER BY created_at DESC LIMIT 1",
                (parsed_id,),
            ).fetchone()
        if row is None:
            return None
        payload = dict(row)
        payload["payment_basis"] = json_loads(payload.pop("payment_basis_json"), {})
        return payload

    def get_expense_accrual_by_parsed_id(self, parsed_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM expense_accrual_candidates WHERE parsed_id = ? ORDER BY created_at DESC LIMIT 1",
                (parsed_id,),
            ).fetchone()
        if row is None:
            return None
        payload = dict(row)
        payload["accrual_basis"] = json_loads(payload.pop("accrual_basis_json"), {})
        return payload

    def list_expense_accruals(self, status: str = "", limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as connection:
            if status:
                rows = connection.execute(
                    """
                    SELECT * FROM expense_accrual_candidates
                    WHERE status = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (status, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM expense_accrual_candidates
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        payloads = []
        for row in rows:
            payload = dict(row)
            payload["accrual_basis"] = json_loads(payload.pop("accrual_basis_json"), {})
            payloads.append(payload)
        return payloads

    def get_upload(self, upload_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM uploads WHERE upload_id = ?", (upload_id,)).fetchone()
        if row is None:
            return None
        payload = dict(row)
        payload["metadata"] = json_loads(payload.pop("metadata_json"), {})
        return payload

    def list_candidates(self, status: str = "", limit: int = 50) -> list[dict[str, Any]]:
        statuses: tuple[str, ...] = ()
        if status == "ready":
            statuses = ("parsed_ready", "ready_for_bank_signature")
        elif status == "owner_review":
            statuses = ("owner_review", "low_confidence")
        elif status:
            statuses = (status,)

        with self.connect() as connection:
            if statuses:
                placeholders = ",".join("?" for _ in statuses)
                rows = connection.execute(
                    f"""
                    SELECT * FROM payment_order_candidates
                    WHERE status IN ({placeholders})
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (*statuses, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM payment_order_candidates
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        payloads = []
        for row in rows:
            payload = dict(row)
            payload["payment_basis"] = json_loads(payload.pop("payment_basis_json"), {})
            payloads.append(payload)
        return payloads

    def mark_candidate_request(
        self,
        *,
        candidate_id: str | None = None,
        parsed_id: str | None = None,
        request_file: str,
        status: str,
        requires_owner_review: bool,
    ) -> None:
        updated_at = dt.datetime.now().replace(microsecond=0).isoformat()
        with self.connect() as connection:
            if candidate_id:
                connection.execute(
                    """
                    UPDATE payment_order_candidates
                    SET request_file = ?, status = ?, requires_owner_review = ?, updated_at = ?
                    WHERE candidate_id = ?
                    """,
                    (request_file, status, 1 if requires_owner_review else 0, updated_at, candidate_id),
                )
            elif parsed_id:
                connection.execute(
                    """
                    UPDATE payment_order_candidates
                    SET request_file = ?, status = ?, requires_owner_review = ?, updated_at = ?
                    WHERE parsed_id = ?
                    """,
                    (request_file, status, 1 if requires_owner_review else 0, updated_at, parsed_id),
                )
