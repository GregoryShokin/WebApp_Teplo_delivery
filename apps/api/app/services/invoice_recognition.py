"""Гибридное распознавание счёта/УПД из PDF: контрагент, сумма, реквизиты.

Два слоя (решение владельца 2026-06-25):
1. Детерминированный — текст из PDF (``pypdf``) + регексы. Быстро, бесплатно, проверяемо;
   хорош для цифровых счетов с обычной разметкой.
2. LLM-фолбэк (Claude по самому PDF) — для незнакомых макетов и сканов, где регексы не
   добрали обязательные поля. Включается только если задан ``ANTHROPIC_API_KEY``.

Результат — :class:`RecognizedInvoice` с полями ПОЛУЧАТЕЛЯ платежа (поставщика), суммой к
оплате (с НДС) и оценкой уверенности. Решение «материализовать в накладную или в ручную
проверку» принимает вызывающий ingest по порогу ``invoice_recognition_min_confidence``.

Никаких системных зависимостей (poppler/tesseract) — цифровой текст берёт ``pypdf``, а всё
остальное (включая сканы) читает Claude из PDF напрямую.
"""

from __future__ import annotations

import base64
import logging
import re
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from app.core.config import Settings

logger = logging.getLogger(__name__)

# ИНН наших собственных юрлиц — исключаем из кандидатов на контрагента (мы — покупатель).
OWN_INNS = frozenset({"890307589201"})
# Наши расчётные счета — исключаем из реквизитов получателя (в двусторонних документах —
# актах, счёт-договорах — печатаются реквизиты и поставщика, и наши как плательщика).
OWN_ACCOUNTS = frozenset({"40802810100002438573"})

_RU_MONTHS = {
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
_RU_MONTH_PATTERN = "|".join(sorted(_RU_MONTHS, key=len, reverse=True))

# Деньги: «1 234,56» / «1 234.56» / «12 000,00» (тысячные — пробел/неразрывный пробел).
# Границы обязательны: без них шаблон жадно склеивал соседние числа через пробел —
# «от 19.07.2026 7 984,90» читалось как 20 267 984,90, а «Итого 40702810430000013829 1 000,00»
# утаскивало номер счёта в сумму. Тысячные группы строго по три цифры, дробная часть не должна
# продолжаться цифрой или точкой (иначе «19.07» из даты 19.07.2026 сходит за деньги).
_MONEY = r"(?<![\d,.])\d{1,3}(?:[   ]?\d{3})*[.,]\d{2}(?![\d,.])"
_MONEY_RE = re.compile(_MONEY)
# Маркеры итоговой суммы — от самых однозначных к общим. Ищем деньги ПОСЛЕ маркера, а не
# «первое число, перед которым нет цифр»: прежний строгий шаблон спотыкался о служебные номера
# («Всего к оплате (9) …») и заголовки колонок («ИТОГО к оплате: № Наименование услуг НДС
# Сумма, руб …»), проваливался на общий фолбэк «итого» и хватал первую строку реестра услуг
# (кейс СДЭК: 7 893,40 вместо 7 984,90).
_AMOUNT_MARKERS = (
    r"итого\s+с\s+ндс",
    r"итого\s+к\s+оплате",
    r"всего\s+к\s+оплате",
    r"сумма\s+к\s+оплате",
    r"\bк\s+оплате",
    r"на\s+сумму",
    r"\bитого\b",
    r"\bвсего\b",
)
_ORG_RE = re.compile(
    r"(?:ООО|ОАО|ЗАО|ПАО|НАО|АО|ИП)\s+[«\"’']?[^»\"’'\n;:|/]{2,80}",
    re.IGNORECASE,
)


@dataclass
class RecognizedInvoice:
    recipient_name: str | None = None
    inn: str | None = None
    kpp: str | None = None
    bank_acnt: str | None = None
    bank_bik: str | None = None
    corr_account: str | None = None
    amount: Decimal | None = None
    invoice_number: str | None = None
    invoice_date: date | None = None
    service_period_start: date | None = None
    service_period_end: date | None = None
    service_period_source: str | None = None
    service_period_confidence: float | None = None
    # Несколько различных периодов в одном счёте не угадываем: весь счёт уходит оператору.
    service_period_ambiguous: bool = False
    service_period_candidates: list[tuple[date, date]] = field(default_factory=list)
    confidence: float = 0.0
    engine: str = "none"
    raw_text_excerpt: str = ""
    notes: list[str] = field(default_factory=list)
    # Тип документа: invoice — счёт на оплату (материализуем); upd/reconciliation/act —
    # закрывающие/сверочные документы (НЕ счёт, ingest уводит в ignored); unknown — формат не
    # опознан (оператор разберёт в needs_review).
    document_kind: str = "invoice"
    # Подсказка по продукту для счетов iiko (courierica — ПО курьеров; iiko_license — лицензия
    # iiko/iikoCloud). Нужна, чтобы развести два регулярных счёта одного поставщика по статьям ДДС.
    product_hint: str | None = None
    # Второй документ ИЗ ТОГО ЖЕ вложения: поставщик шлёт пакет «счёт + УПД» одним PDF (СДЭК).
    # Основным остаётся счёт (он идёт в очередь оплат), спутник — закрывающий УПД/акт, который
    # ingest проводит отдельным документом (кредиторка / гашение дебиторки).
    companion: RecognizedInvoice | None = None

    @property
    def is_payment_invoice(self) -> bool:
        return self.document_kind == "invoice"

    def requisites(self) -> dict[str, str]:
        """Реквизиты в форме, понятной ``build_payment_draft_api_payload`` (для Фазы 2/3)."""
        out: dict[str, str] = {}
        if self.recipient_name:
            out["recipientName"] = self.recipient_name
        if self.inn:
            out["inn"] = self.inn
        if self.kpp:
            out["kpp"] = self.kpp
        if self.bank_acnt:
            out["bankAcnt"] = self.bank_acnt
        if self.bank_bik:
            out["bankBik"] = self.bank_bik
        if self.corr_account:
            out["recipientCorrAccountNumber"] = self.corr_account
        return out

    def to_json(self) -> dict[str, object]:
        return {
            "recipient_name": self.recipient_name,
            "inn": self.inn,
            "kpp": self.kpp,
            "bank_acnt": self.bank_acnt,
            "bank_bik": self.bank_bik,
            "corr_account": self.corr_account,
            "amount": str(self.amount) if self.amount is not None else None,
            "invoice_number": self.invoice_number,
            "invoice_date": self.invoice_date.isoformat() if self.invoice_date else None,
            "service_period_start": (
                self.service_period_start.isoformat() if self.service_period_start else None
            ),
            "service_period_end": (
                self.service_period_end.isoformat() if self.service_period_end else None
            ),
            "service_period_source": self.service_period_source,
            "service_period_confidence": self.service_period_confidence,
            "service_period_ambiguous": self.service_period_ambiguous,
            "service_period_candidates": [
                {"start": start.isoformat(), "end": end.isoformat()}
                for start, end in self.service_period_candidates
            ],
            "confidence": round(self.confidence, 3),
            "engine": self.engine,
            "document_kind": self.document_kind,
            "product_hint": self.product_hint,
            "requisites": self.requisites(),
            "notes": self.notes,
            "text_excerpt": self.raw_text_excerpt[:2000],
            "companion": self.companion.to_json() if self.companion is not None else None,
        }


def _money(value: str | None) -> Decimal | None:
    if not value:
        return None
    text = re.sub(r"[\s  ]", "", str(value)).replace(",", ".")
    text = re.sub(r"[^0-9.\-]", "", text)
    if text.count(".") > 1:  # «1.234.56» — последняя точка десятичная
        head, _, tail = text.rpartition(".")
        text = head.replace(".", "") + "." + tail
    try:
        amount = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    return amount if amount > 0 else None


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    text = value.strip().lower()
    m = re.search(r"(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{2,4})", text)
    if m:
        day, month, year = (int(p) for p in m.groups())
        if year < 100:
            year += 2000
        try:
            return date(year, month, day)
        except ValueError:
            return None
    m = re.search(r"(\d{1,2})\s+([а-яё]+)\s+(\d{4})", text)
    if m and m.group(2) in _RU_MONTHS:
        try:
            return date(int(m.group(3)), _RU_MONTHS[m.group(2)], int(m.group(1)))
        except ValueError:
            return None
    return None


# «от <дата>» — так подписана дата счёта. Но в унифицированных бланках эта же конструкция
# стоит в шапке («Утверждена Постановлением Госкомстата России от 05.01.2004 № 1»), и первое
# совпадение уводило дату документа в 2004 год (ведомость Т-53, 27.07.2026). Поэтому идём по
# всем совпадениям и берём первое правдоподобное.
_DATE_AFTER_OT_RE = re.compile(
    r"от\s+(\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4}|\d{1,2}\s+[а-яё]+\s+\d{4})",
    re.IGNORECASE,
)
# Слева от даты — признаки нормативной шапки бланка, а не даты документа.
_BOILERPLATE_RE = re.compile(r"утвержд|постановлен|госкомстат|окуд|приказ[а-я]*\s+минфин", re.I)
# Электронный документооборот моложе этого года; всё раньше — реквизит бланка, не счёт.
_EARLIEST_INVOICE_YEAR = 2015


def _pick_invoice_date(text: str) -> date | None:
    for m in _DATE_AFTER_OT_RE.finditer(text):
        context = text[max(0, m.start() - 80) : m.start()]
        if _BOILERPLATE_RE.search(context):
            continue
        parsed = _parse_date(m.group(1))
        if parsed is None or parsed.year < _EARLIEST_INVOICE_YEAR:
            continue
        return parsed
    return None


def _month_period(year: int, month: int) -> tuple[date, date]:
    return date(year, month, 1), date(year, month, monthrange(year, month)[1])


def _extract_service_periods(text: str) -> list[tuple[date, date, str, float]]:
    """Извлечь все различные периоды с детерминированным приоритетом.

    Поддерживает явные диапазоны, словесные диапазоны и формулировки «за август 2026» /
    «за период: апрель 2026». Дата самого счёта без маркера периода не используется.
    """
    found: list[tuple[date, date, str, float]] = []

    # [01.08.2026—31.08.2026], 01.08.2026 - 31.08.2026, с 01.08 по 31.08.2026.
    numeric = re.compile(
        r"(?:\bс\s+|\[)?(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{2,4}))?\s*"
        r"(?:—|–|-|по)\s*(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{2,4})(?:\])?",
        re.IGNORECASE,
    )
    for match in numeric.finditer(text):
        d1, m1, y1_raw, d2, m2, y2_raw = match.groups()
        y2 = int(y2_raw)
        if y2 < 100:
            y2 += 2000
        y1 = int(y1_raw) if y1_raw else y2
        if y1 < 100:
            y1 += 2000
        try:
            start, end = date(y1, int(m1), int(d1)), date(y2, int(m2), int(d2))
        except ValueError:
            continue
        if end >= start:
            found.append((start, end, "document_range", 0.99))

    # «с 1 августа 2026 по 31 августа 2026» (год в первой дате может отсутствовать).
    words = re.compile(
        rf"\bс\s+(\d{{1,2}})\s+({_RU_MONTH_PATTERN})(?:\s+(\d{{4}}))?\s+"
        rf"по\s+(\d{{1,2}})\s+({_RU_MONTH_PATTERN})\s+(\d{{4}})",
        re.IGNORECASE,
    )
    for match in words.finditer(text):
        d1, month1, y1_raw, d2, month2, y2_raw = match.groups()
        y2 = int(y2_raw)
        y1 = int(y1_raw) if y1_raw else y2
        try:
            start = date(y1, _RU_MONTHS[month1.casefold()], int(d1))
            end = date(y2, _RU_MONTHS[month2.casefold()], int(d2))
        except (KeyError, ValueError):
            continue
        if end >= start:
            found.append((start, end, "document_range", 0.98))

    # «вознаграждение … за июнь 2026», «за период: апрель 2026».
    month_phrase = re.compile(
        rf"(?:\bза\s+(?:период\s*[:\-]?\s*)?|\bпериод\s*[:\-]\s*)"
        rf"({_RU_MONTH_PATTERN})\s+(\d{{4}})",
        re.IGNORECASE,
    )
    for match in month_phrase.finditer(text):
        month_name, year_raw = match.groups()
        month = _RU_MONTHS.get(month_name.casefold())
        if month is not None:
            start, end = _month_period(int(year_raw), month)
            found.append((start, end, "document_month", 0.94))

    # Дедуп: одна строка может одновременно совпасть с несколькими формулировками.
    unique: dict[tuple[date, date], tuple[date, date, str, float]] = {}
    for candidate in found:
        key = candidate[0], candidate[1]
        current = unique.get(key)
        if current is None or candidate[3] > current[3]:
            unique[key] = candidate
    return list(unique.values())


def _apply_service_period(rec: RecognizedInvoice, text: str, context_text: str | None) -> None:
    candidates = _extract_service_periods(text)
    source_prefix = "document"
    if not candidates and (context_text or "").strip():
        candidates = _extract_service_periods(context_text or "")
        source_prefix = "subject"
    rec.service_period_candidates = [(item[0], item[1]) for item in candidates]
    if len(candidates) > 1:
        rec.service_period_ambiguous = True
        rec.notes.append("обнаружено несколько периодов оказания услуг — требуется ручной разбор")
        return
    if len(candidates) == 1:
        start, end, source, confidence = candidates[0]
        rec.service_period_start = start
        rec.service_period_end = end
        rec.service_period_source = source.replace("document", source_prefix)
        rec.service_period_confidence = (
            confidence if source_prefix == "document" else min(confidence, 0.85)
        )


def extract_pdf_pages(pdf: bytes) -> list[str]:
    """Тексты страниц цифрового PDF (``pypdf``). Для сканов вернёт пусто → решит LLM-фолбэк.

    Постранично, а не одной строкой, потому что в одном вложении может лежать ПАКЕТ документов
    (СДЭК: стр. 1 — счёт на оплату, стр. 2 — УПД, стр. 3 — приложение к УПД). По склеенному
    тексту тип документа определить нельзя: маркеры разных документов перебивают друг друга."""
    try:
        from io import BytesIO

        from pypdf import PdfReader
    except Exception:  # noqa: BLE001 - до пересборки образа пакета может не быть
        logger.warning("pypdf недоступен — пропускаю детерминированный слой", exc_info=True)
        return []
    try:
        reader = PdfReader(BytesIO(pdf))
        return [page.extract_text() or "" for page in reader.pages]
    except Exception:  # noqa: BLE001 - битый/зашифрованный PDF
        logger.warning("не удалось извлечь текст из PDF", exc_info=True)
        return []


def extract_pdf_text(pdf: bytes) -> str:
    """Весь текст PDF одной строкой (совместимость с вызывающими, которым страницы не нужны)."""
    return "\n".join(extract_pdf_pages(pdf))


def _pick_inn(text: str) -> str | None:
    seen: list[str] = []
    for m in re.finditer(r"ИНН[\s:№]*?(\d{12}|\d{10})", text, re.IGNORECASE):
        inn = m.group(1)
        if inn not in seen:
            seen.append(inn)
    for inn in seen:
        if inn not in OWN_INNS:
            return inn
    return None


def _labelled_20(text: str, *labels: str) -> str | None:
    for label in labels:
        m = re.search(label + r"[\s:№]*?(\d{20})", text, re.IGNORECASE)
        if m:
            return m.group(1)
    return None


def _amount_near_marker(text: str, marker: str, *, window: int = 200) -> Decimal | None:
    """Итоговая сумма после маркера: МАКСИМУМ денежных значений в его строке (или в окне).

    Максимум, а не первое число, потому что итоговая строка УПД/счёта печатается колонками
    «без НДС | НДС | с НДС» («Всего к оплате (9) 6 545,00 Х 1 439,90 7 984,90») либо
    «услуга | доп.сбор | … | итого» («Итого: 7 893,40 91,50 0,00 0,00 7 984,90»). Сумма к
    оплате — всегда наибольшая из них, поэтому колонка «без НДС» больше не выигрывает."""
    for m in re.finditer(marker, text, re.IGNORECASE):
        tail = text[m.end() : m.end() + window]
        # Сначала — своя строка маркера (там итог и печатают); если в ней денег нет, документ
        # переносит сумму на следующую строку — расширяем до окна.
        for scope in (tail.split("\n", 1)[0], tail):
            values = [v for v in (_money(raw) for raw in _MONEY_RE.findall(scope)) if v is not None]
            if values:
                return max(values)
    return None


def _pick_amount(text: str) -> Decimal | None:
    for marker in _AMOUNT_MARKERS:
        amount = _amount_near_marker(text, marker)
        if amount is not None:
            return amount
    return None


def _pick_recipient(text: str) -> str | None:
    for m in _ORG_RE.finditer(text):
        name = re.sub(r"\s+", " ", m.group(0)).strip(" .,;:")
        low = name.lower()
        if "шокин" in low:  # это мы (покупатель)
            continue
        if "банк" in low or "сбербанк" in low or "точка" in low:  # банк получателя, не поставщик
            continue
        if name.count("«") > name.count("»"):  # регекс остановился перед закрывающей «»»
            name += "»"
        if name.count('"') % 2 == 1:  # «АО "АЙКО» — добрать закрывающую прямую кавычку
            name += '"'
        return name
    return None


# Позитивные маркеры «это счёт на оплату» (его материализуем). Один поставщик может звать
# документ по-разному: iiko — «счёт на оплату», ЛЕММА — «счёт-договор», Стартер печатает
# «образец заполнения платёжного поручения».
_INVOICE_MARKERS = (
    "счет на оплату",
    "счёт на оплату",
    "счет-договор",
    "счёт-договор",
    "счет договор",
    "счёт договор",
    "счет-оферта",
    "счёт-оферта",
    "образец заполнения платежного поручения",
    "образец заполнения платёжного поручения",
)
_PAY_DUE_RE = re.compile(r"(всего|итого|сумма)\s+к\s+оплате", re.IGNORECASE)

# Маркеры закрывающих актов (НЕ счёт на оплату): акт выполненных работ/услуг (Спецавто Юг),
# акт передачи прав / приёма-передачи (DocsInbox и пр.).
_ACT_MARKERS = (
    "о приемке выполненных работ",
    "о приёмке выполненных работ",
    "акт сдачи-приемки",
    "акт сдачи-приёмки",
    "акт приема-передачи",
    "акт приёма-передачи",
    "акт передачи прав",
)


def _classify_document(text: str) -> str:
    """invoice | upd | reconciliation | act | unknown.

    Только ``invoice`` материализуется в накладную к оплате. Классификация по содержанию, а не
    по отправителю: «Лицензиар/Лицензиат» сами по себе НЕ признак акта (так оформлены и счета
    ЛЕММА/iiko), поэтому решает наличие позитивного маркера счёта.
    """
    # Нормализуем пробелы: pypdf переносит слова, поэтому многословные маркеры ищем по тексту
    # со схлопнутыми пробельными последовательностями («Универсальный\nпередаточный\nдокумент»).
    low = re.sub(r"\s+", " ", text.casefold())
    # Тип определяем по ЗАГОЛОВКУ документа, а не по упоминаниям в теле: акт передачи прав
    # ссылается на «счёт-оферту», а счёт-оферта в условиях упоминает «акт сверки» — и то и другое
    # сбивает поиск по подстроке. Надёжные заголовочные сигналы (начинается с «Акт…», УПД)
    # проверяем РАНЬШЕ маркеров счёта; неоднозначные body-маркеры — только как фолбэк после них.
    if re.match(r"\s*акт\s+сверки\b", low):
        return "reconciliation"
    if re.match(r"\s*акт\b", low):  # «Акт …» в начале — закрывающий акт (счета так не начинаются)
        return "act"
    if "универсальный передаточный документ" in low:  # УПД декларирует себя в шапке
        return "upd"
    if any(m in low for m in _INVOICE_MARKERS):
        return "invoice"
    # Фолбэки для документов без явного заголовка-«Акт» и без маркера счёта:
    if "акт сверки" in low:
        return "reconciliation"
    if any(m in low for m in _ACT_MARKERS):
        return "act"
    # Передаточный акт iiko («Документы для…»): шаблонная подпись сторон без маркера счёта.
    if "организация, адрес, телефон, факс" in low:
        return "act"
    # Общий счёт нестандартного макета: есть слово «счёт» и явная сумма «к оплате».
    if re.search(r"сч[ёе]т", low) and _PAY_DUE_RE.search(low):
        return "invoice"
    return "unknown"


# Страница-продолжение объявляет себя в первой строке: приложение, спецификация, реестр,
# «лист 2». Такую страницу нельзя считать началом нового документа, даже если в её теле есть
# ссылка вида «Основание: Счет на оплату № 55» — иначе приложение к УПД становится счётом и
# встаёт в очередь оплат (риск оплатить уже закрытую поставку второй раз).
_CONTINUATION_RE = re.compile(
    r"\s*(приложение|спецификаци|реестр|расшифровка|продолжение|лист\s*\d)", re.IGNORECASE
)
# Заголовок документа печатается в шапке страницы; ниже — тело со ссылками на другие документы.
_HEADER_CHARS = 400


def _page_section_kind(page: str) -> str:
    """Тип документа, НАЧИНАЮЩЕГОСЯ на этой странице, либо 'unknown' (страница-продолжение).

    Отличается от ``_classify_document`` тем, что смотрит только ШАПКУ страницы: упоминание
    другого документа в теле («Основание: Счет на оплату №…», «Расшифровка счета») не должно
    открывать новую секцию. Это регресс, найденный предделойным аудитом: двухстраничный УПД
    разваливался на «счёт + спутник», и закрывающий документ уходил в очередь оплат."""
    if not page.strip() or _CONTINUATION_RE.match(page):
        return "unknown"
    return _classify_document(page[:_HEADER_CHARS])


def split_document_sections(pages: list[str]) -> list[tuple[str, str]]:
    """Разбить страницы PDF на секции «один документ» → ``[(тип, текст), …]``.

    Поставщик может слать ПАКЕТ одним файлом (СДЭК: счёт + УПД + приложение к УПД). Страница
    без собственного заголовка (``unknown``) — продолжение предыдущего документа (реестр
    накладных, вторая страница спецификации), поэтому прилипает к текущей секции; страница
    того же типа — тоже (многостраничный УПД). Новый ИЗВЕСТНЫЙ тип открывает новую секцию.
    """
    sections: list[tuple[str, list[str]]] = []
    for page in pages:
        kind = _page_section_kind(page)
        if sections and (kind == "unknown" or kind == sections[-1][0]):
            sections[-1][1].append(page)
            continue
        sections.append((kind, [page]))
    return [(kind, "\n".join(chunk)) for kind, chunk in sections]


_ACC20_RE = re.compile(r"\d{20}")


def _extract_party_accounts(text: str, bik: str | None = None) -> tuple[str | None, str | None]:
    """Расчётный (р/с) и корреспондентский (к/с) счёт ПОЛУЧАТЕЛЯ по структуре счетов ЦБ РФ:
    р/с начинается с ``40`` (счета клиентов), к/с — с ``301``. В счёте реквизиты получателя идут
    первыми, поэтому р/с — первый «чужой» 40-счёт (наш отбрасываем по ``OWN_ACCOUNTS``). к/с
    выбираем по БИК: последние 3 цифры к/с совпадают с последними 3 цифрами БИК банка — так к/с
    поставщика надёжно отделяется от нашего в двусторонних документах (счёт-договор, акт), где
    эвристика «по близости» промахивается из-за вёрстки.

    Решает кейс iiko/Стартер, где счёт получателя помечен просто «Сч. №», без метки «р/с»."""
    rs = None
    ks_candidates: list[str] = []
    for m in _ACC20_RE.finditer(text):
        acc = m.group(0)
        if acc in OWN_ACCOUNTS:
            continue
        if acc.startswith("40") and rs is None:
            rs = acc
        elif acc.startswith("301"):
            ks_candidates.append(acc)
    ks: str | None = None
    if bik and len(bik) == 9:
        ks = next((a for a in ks_candidates if a[-3:] == bik[-3:]), None)
    if ks is None and ks_candidates:
        ks = ks_candidates[0]
    return rs, ks


# Слова, которые встречаются между «№» и настоящим номером: у СДЭК шапка звучит
# «Счет № Счет на оплату № СКБ-0437096» — без пропуска этих слов номером становится «Счет».
_NUMBER_SKIP_WORDS = frozenset({"счет", "счёт", "на", "оплату", "фактура", "договор"})


def _number_after_hash(tail: str) -> str | None:
    """Первый осмысленный номер после «№»: служебные слова пропускаем, номер обязан иметь цифру.

    На чужом слове без цифр останавливаемся — значит номер не здесь, и брать соседний текст
    (дату, наименование) нельзя."""
    for token in re.findall(r"[\w/№-]+", tail[:60]):
        cleaned = token.strip("-/№")
        if not cleaned:
            continue
        if cleaned.casefold() in _NUMBER_SKIP_WORDS:
            continue
        return cleaned if any(ch.isdigit() for ch in cleaned) else None
    return None


def _pick_number(text: str) -> str | None:
    # 1) «Счёт [на оплату|-фактура|-договор] № <номер>» (iiko, ЛЕММА, СДЭК).
    for m in re.finditer(
        r"сч[ёе]т(?:[-\s]*фактура|[-\s]*договор)?(?:\s+на\s+оплату)?\s*№",
        text,
        re.IGNORECASE,
    ):
        candidate = _number_after_hash(text[m.end() :])
        if candidate:
            return candidate
    # 2) «Счёт <номер> от <дата>» без № (Стартер: «Счет 0000-001175 от 31 марта 2026 г.»).
    m = re.search(r"сч[ёе]т\s+([0-9][\w\-/]*)\s+от\s+\d", text, re.IGNORECASE)
    if m:
        return m.group(1).strip("-/")
    return None


def _iiko_product(text: str, number: str | None) -> str | None:
    """Развести два регулярных счёта iiko: courierica (ПО курьеров, суффикс -лсп / is-175038 /
    позиции «Курьерика») vs iiko_license (лицензия iikoCloud, суффикс -лк / ic-109122)."""
    low = (text + " " + (number or "")).casefold()
    if "курьерик" in low or "-лсп" in low or "is-175038" in low:
        return "courierica"
    if "iikocloud" in low or "-лк" in low or "ic-109122" in low:
        return "iiko_license"
    return None


def _confidence(rec: RecognizedInvoice) -> float:
    score = 0.0
    if rec.amount is not None:
        score += 0.35
    if rec.inn:
        score += 0.30
    if rec.recipient_name:
        score += 0.10
    if rec.bank_acnt:
        score += 0.10
    if rec.bank_bik:
        score += 0.075
    if rec.corr_account:
        score += 0.075
    return min(score, 1.0)


def deterministic_recognize(text: str, *, context_text: str | None = None) -> RecognizedInvoice:
    rec = RecognizedInvoice(engine="deterministic", raw_text_excerpt=text[:2000])
    if not text.strip():
        rec.document_kind = "unknown"  # пустой текст (скан) — пусть решает LLM/оператор
        _apply_service_period(rec, text, context_text)
        return rec
    rec.document_kind = _classify_document(text)
    rec.inn = _pick_inn(text)
    rec.amount = _pick_amount(text)
    rec.recipient_name = _pick_recipient(text)
    bik = re.search(r"БИК[\s:№]*?(\d{9})", text, re.IGNORECASE)
    rec.bank_bik = bik.group(1) if bik else None
    # Реквизиты получателя по структуре (исключая наши), с откатом на явные метки. к/с разводим
    # по БИК, поэтому БИК извлекаем раньше счетов.
    rs, ks = _extract_party_accounts(text, rec.bank_bik)
    rec.bank_acnt = rs or _labelled_20(
        text, r"р[\s/.]*с", r"расч[её]тный\s+сч[её]т", r"сч[её]т\s+получателя"
    )
    rec.corr_account = ks or _labelled_20(text, r"к[\s/.]*с", r"корр[\w.\s]*сч[её]т")
    kpp = re.search(r"КПП[\s:№]*?(\d{9})", text, re.IGNORECASE)
    rec.kpp = kpp.group(1) if kpp else None
    rec.invoice_number = _pick_number(text)
    rec.invoice_date = _pick_invoice_date(text)
    _apply_service_period(rec, text, context_text)
    rec.product_hint = _iiko_product(text, rec.invoice_number)
    if rec.product_hint == "courierica":
        rec.notes.append("iiko: Курьерика (ПО курьеров)")
    elif rec.product_hint == "iiko_license":
        rec.notes.append("iiko: лицензия iiko/iikoCloud")
    rec.confidence = _confidence(rec)
    return rec


def deterministic_recognize_pages(
    pages: list[str], *, context_text: str | None = None
) -> RecognizedInvoice:
    """Распознать вложение постранично, разведя пакет «счёт + УПД» на документ и спутника.

    Пакет одним файлом (СДЭК) прежде схлопывался в ОДИН документ, и побеждал УПД: маркер
    «универсальный передаточный документ» со второй страницы проверяется раньше маркеров счёта
    и ищется по всему склеенному тексту. Итог — счёт исчезал из очереди оплат, а кредиторка
    вставала на сумму из приложения к УПД. Теперь: счёт — основной документ (его платят),
    закрывающий УПД/акт из того же файла — ``companion`` (его проводит ingest отдельно).
    """
    text = "\n".join(pages)
    sections = split_document_sections(pages)
    bill = next((body for kind, body in sections if kind == "invoice"), None)
    closing = next((body for kind, body in sections if kind in ("upd", "act")), None)
    if bill is None or closing is None:
        # Обычный случай — один документ на файл: классифицируем целиком, как прежде.
        return deterministic_recognize(text, context_text=context_text)

    rec = deterministic_recognize(bill, context_text=context_text)
    companion = deterministic_recognize(closing, context_text=context_text)
    # Реквизиты получателя и ИНН печатаются на странице счёта; на листе УПД их может не быть
    # («ИНН/КПП продавца: …» под общий регекс не подходит). Спутник — не платёжный документ,
    # поэтому недостающее наследуем от счёта: контрагент у пакета один.
    #
    # НО собственные реквизиты документа — сумму, номер и дату — не наследуем НИКОГДА:
    # у закрывающего они свои, и подстановка чужих ломает и учёт, и дедуп. Закрывающий без
    # распознанного итога получил бы сумму счёта и создал кредиторку из воздуха, а с чужим
    # номером не схлопнулся бы с тем же УПД, пришедшим отдельным письмом (двойная кредиторка).
    _merge_identity(companion, rec)
    companion.notes.append("закрывающий документ из пакета вместе со счётом")
    rec.companion = companion
    rec.notes.append("в файле пакет документов: счёт + закрывающий")
    return rec


_LLM_TOOL = {
    "name": "record_invoice",
    "description": "Записать распознанные реквизиты счёта/УПД на оплату.",
    "input_schema": {
        "type": "object",
        "properties": {
            "is_invoice": {
                "type": "boolean",
                "description": "true только если документ — счёт на оплату, счёт-фактура или УПД.",
            },
            "recipient_name": {
                "type": "string",
                "description": "Наименование ПОЛУЧАТЕЛЯ платежа (поставщика).",
            },
            "inn": {"type": "string", "description": "ИНН получателя (10 или 12 цифр)."},
            "kpp": {"type": "string"},
            "bank_acnt": {"type": "string", "description": "Расчётный счёт получателя (20 цифр)."},
            "bank_bik": {"type": "string", "description": "БИК банка получателя (9 цифр)."},
            "corr_account": {
                "type": "string",
                "description": "Корреспондентский счёт банка (20 цифр).",
            },
            "amount": {"type": "string", "description": "Итоговая сумма К ОПЛАТЕ с НДС, число."},
            "invoice_number": {"type": "string"},
            "invoice_date": {"type": "string", "description": "Дата счёта в формате YYYY-MM-DD."},
            "service_period_start": {
                "type": "string",
                "description": "Начало периода оказания услуги, YYYY-MM-DD.",
            },
            "service_period_end": {
                "type": "string",
                "description": "Окончание периода оказания услуги, YYYY-MM-DD.",
            },
            "confidence": {"type": "number", "description": "Уверенность 0..1."},
        },
        "required": ["is_invoice", "confidence"],
    },
}

_LLM_PROMPT = (
    "Это документ из почты поставщика. Покупатель — наш ИП «Тепло» (ИП Шокина Е.А., ИНН "
    "890307589201). Если это счёт на оплату / счёт-фактура / УПД — извлеки реквизиты "
    "ПОЛУЧАТЕЛЯ платежа (поставщика), НЕ покупателя, итоговую сумму К ОПЛАТЕ с НДС и "
    "период оказания услуги, только если он явно указан. "
    "Если документ НЕ является счётом/УПД (письмо, акт сверки, договор, реклама) — верни "
    "is_invoice=false и confidence=0. Вызови инструмент record_invoice."
)


async def llm_recognize(pdf: bytes, *, settings: Settings) -> RecognizedInvoice | None:
    """Распознать счёт по самому PDF через Claude (structured tool-output). None — если ключа
    нет, пакет недоступен или модель не сочла документ счётом."""
    if not settings.anthropic_api_key:
        return None
    try:
        from anthropic import AsyncAnthropic
    except Exception:  # noqa: BLE001 - до пересборки образа пакета может не быть
        logger.warning("anthropic SDK недоступен — LLM-фолбэк выключен", exc_info=True)
        return None
    # Через тот же релей, что и ИИ-ревьюер налогов: с прод-IP Anthropic отдаёт 403 по региону.
    client = AsyncAnthropic(
        api_key=settings.anthropic_api_key,
        timeout=settings.anthropic_timeout_seconds,
        max_retries=settings.anthropic_max_retries,
        default_headers=(
            {"x-relay-secret": settings.anthropic_relay_secret}
            if settings.anthropic_relay_secret
            else None
        ),
    )
    b64 = base64.standard_b64encode(pdf).decode("ascii")
    try:
        message = await client.messages.create(
            model=settings.invoice_recognition_model,
            max_tokens=1024,
            tools=[_LLM_TOOL],
            tool_choice={"type": "tool", "name": "record_invoice"},
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": "application/pdf",
                                "data": b64,
                            },
                        },
                        {"type": "text", "text": _LLM_PROMPT},
                    ],
                }
            ],
        )
    except Exception:  # noqa: BLE001 - сетевая/лимитная ошибка не должна валить весь проход
        logger.warning("LLM-распознавание не удалось", exc_info=True)
        return None

    payload: dict[str, object] | None = None
    for block in message.content:
        if getattr(block, "type", None) == "tool_use":
            payload = dict(block.input)  # type: ignore[arg-type]
            break
    if not payload or not payload.get("is_invoice"):
        return None

    def _digits(value: object, length: tuple[int, ...]) -> str | None:
        s = re.sub(r"\D", "", str(value or ""))
        return s if len(s) in length else None

    def _text(key: str) -> str | None:
        raw = payload.get(key)
        return (str(raw).strip() or None) if raw else None

    rec = RecognizedInvoice(engine="llm")
    rec.recipient_name = _text("recipient_name")
    rec.inn = _digits(payload.get("inn"), (10, 12))
    rec.kpp = _digits(payload.get("kpp"), (9,))
    rec.bank_acnt = _digits(payload.get("bank_acnt"), (20,))
    rec.bank_bik = _digits(payload.get("bank_bik"), (9,))
    rec.corr_account = _digits(payload.get("corr_account"), (20,))
    rec.amount = _money(str(payload.get("amount"))) if payload.get("amount") is not None else None
    rec.invoice_number = _text("invoice_number")
    rec.invoice_date = _parse_date(_text("invoice_date") or "")
    start_text = _text("service_period_start")
    end_text = _text("service_period_end")
    try:
        if start_text and end_text:
            rec.service_period_start = date.fromisoformat(start_text)
            rec.service_period_end = date.fromisoformat(end_text)
            if rec.service_period_end >= rec.service_period_start:
                rec.service_period_source = "llm"
                rec.service_period_confidence = float(payload.get("confidence") or 0.0)
                rec.service_period_candidates = [(rec.service_period_start, rec.service_period_end)]
            else:
                rec.service_period_start = None
                rec.service_period_end = None
    except (TypeError, ValueError):
        rec.service_period_start = None
        rec.service_period_end = None
    try:
        rec.confidence = float(payload.get("confidence") or 0.0)
    except (TypeError, ValueError):
        rec.confidence = 0.0
    if rec.inn in OWN_INNS:  # модель спутала покупателя и поставщика
        rec.inn = None
        rec.notes.append("llm вернул наш ИНН как получателя — отброшено")
    return rec


def _merge_identity(base: RecognizedInvoice, extra: RecognizedInvoice) -> RecognizedInvoice:
    """Дополнить ``base`` только тем, что описывает КОНТРАГЕНТА, не сам документ.

    Для спутника из пакета: имя, ИНН/КПП и банковские реквизиты у счёта и его закрывающего
    общие, а сумма, номер и дата — свои у каждого. Наследование последних создавало кредиторку
    из суммы счёта и ломало дедуп закрывающего по номеру."""
    for attr in ("recipient_name", "inn", "kpp", "bank_acnt", "bank_bik", "corr_account"):
        if getattr(base, attr) in (None, "") and getattr(extra, attr) not in (None, ""):
            setattr(base, attr, getattr(extra, attr))
    return base


def _merge(base: RecognizedInvoice, extra: RecognizedInvoice) -> RecognizedInvoice:
    """Дополнить ``base`` недостающими полями из ``extra`` (детерминированное в приоритете)."""
    for attr in (
        "recipient_name",
        "inn",
        "kpp",
        "bank_acnt",
        "bank_bik",
        "corr_account",
        "amount",
        "invoice_number",
        "invoice_date",
        "service_period_start",
        "service_period_end",
        "service_period_source",
        "service_period_confidence",
    ):
        if getattr(base, attr) in (None, "") and getattr(extra, attr) not in (None, ""):
            setattr(base, attr, getattr(extra, attr))
    return base


async def recognize(
    pdf: bytes, *, settings: Settings, context_text: str | None = None
) -> RecognizedInvoice:
    """Распознать счёт: детерминированный слой, при нехватке — LLM-фолбэк, затем слияние."""
    pages = extract_pdf_pages(pdf)
    text = "\n".join(pages)
    det = deterministic_recognize_pages(pages, context_text=context_text)

    # Закрывающие/сверочные документы (УПД, акт сверки, передаточный акт) опознаются по тексту
    # надёжно — не зовём LLM, чтобы он ошибочно не «спас» не-счёт в счёт на оплату.
    if det.document_kind in ("upd", "reconciliation", "act"):
        return det

    sufficient = (
        det.amount is not None
        and det.inn is not None
        and det.confidence >= settings.invoice_recognition_min_confidence
    )
    if sufficient or not settings.anthropic_api_key:
        return det

    llm = await llm_recognize(pdf, settings=settings)
    if llm is None:
        return det

    if det.amount is None and det.inn is None and not text.strip():
        # Скан без текста — доверяем LLM целиком.
        llm.raw_text_excerpt = ""
        llm.confidence = max(llm.confidence, _confidence(llm))
        return llm

    merged = _merge(det, llm)
    # Детерминированный слой мог пометить период спорным (>1 кандидата) и оставить даты
    # пустыми. Если LLM затем дал ОДИН однозначный период — снимаем «спорно», иначе счёт с
    # уже определённым периодом зря уйдёт оператору. Спорным оставляем, только если и LLM
    # видит несколько кандидатов.
    if (
        merged.service_period_ambiguous
        and merged.service_period_start
        and merged.service_period_end
        and len(llm.service_period_candidates) <= 1
    ):
        merged.service_period_ambiguous = False
        merged.notes = [n for n in merged.notes if "несколько периодов" not in n]
    merged.engine = "deterministic+llm"
    merged.raw_text_excerpt = text[:2000]
    merged.confidence = max(det.confidence, llm.confidence, _confidence(merged))
    return merged
