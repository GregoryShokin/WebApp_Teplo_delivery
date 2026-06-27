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
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from app.core.config import Settings

logger = logging.getLogger(__name__)

# ИНН наших собственных юрлиц — исключаем из кандидатов на контрагента (мы — покупатель).
OWN_INNS = frozenset({"890307589201"})

_RU_MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}

# Деньги: «1 234,56» / «1 234.56» / «12 000,00» (тысячные — пробел/неразрывный пробел).
_MONEY = r"\d[\d   ]*[.,]\d{2}"
_AMOUNT_PATTERNS = (
    rf"итого\s+к\s+оплате[^0-9\-]{{0,40}}?({_MONEY})",
    rf"всего\s+к\s+оплате[^0-9\-]{{0,40}}?({_MONEY})",
    rf"\bк\s+оплате[^0-9\-]{{0,40}}?({_MONEY})",
    rf"сумма\s+к\s+оплате[^0-9\-]{{0,40}}?({_MONEY})",
    rf"\bитого[^0-9\-]{{0,20}}?({_MONEY})",
    rf"\bвсего[^0-9\-]{{0,20}}?({_MONEY})",
)
_ORG_RE = re.compile(
    r"(?:ООО|ОАО|ЗАО|ПАО|НАО|АО|ИП)\s+[«\"’']?[^»\"’'\n;:|]{2,80}",
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
    confidence: float = 0.0
    engine: str = "none"
    raw_text_excerpt: str = ""
    notes: list[str] = field(default_factory=list)

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
            "confidence": round(self.confidence, 3),
            "engine": self.engine,
            "requisites": self.requisites(),
            "notes": self.notes,
            "text_excerpt": self.raw_text_excerpt[:2000],
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


def extract_pdf_text(pdf: bytes) -> str:
    """Текст из цифрового PDF (``pypdf``). Для сканов вернёт пусто → решит LLM-фолбэк."""
    try:
        from io import BytesIO

        from pypdf import PdfReader
    except Exception:  # noqa: BLE001 - до пересборки образа пакета может не быть
        logger.warning("pypdf недоступен — пропускаю детерминированный слой", exc_info=True)
        return ""
    try:
        reader = PdfReader(BytesIO(pdf))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception:  # noqa: BLE001 - битый/зашифрованный PDF
        logger.warning("не удалось извлечь текст из PDF", exc_info=True)
        return ""


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


def _pick_amount(text: str) -> Decimal | None:
    for pattern in _AMOUNT_PATTERNS:
        m = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
        if m:
            amount = _money(m.group(1))
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
        return name
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


def deterministic_recognize(text: str) -> RecognizedInvoice:
    rec = RecognizedInvoice(engine="deterministic", raw_text_excerpt=text[:2000])
    if not text.strip():
        return rec
    rec.inn = _pick_inn(text)
    rec.amount = _pick_amount(text)
    rec.recipient_name = _pick_recipient(text)
    rec.bank_acnt = _labelled_20(
        text, r"р[\s/.]*с", r"расч[её]тный\s+сч[её]т", r"сч[её]т\s+получателя"
    )
    rec.corr_account = _labelled_20(text, r"к[\s/.]*с", r"корр[\w.\s]*сч[её]т")
    bik = re.search(r"БИК[\s:№]*?(\d{9})", text, re.IGNORECASE)
    rec.bank_bik = bik.group(1) if bik else None
    kpp = re.search(r"КПП[\s:№]*?(\d{9})", text, re.IGNORECASE)
    rec.kpp = kpp.group(1) if kpp else None
    num = re.search(
        r"сч[ёе]т(?:[\s\-]*фактура)?(?:\s+на\s+оплату)?\s*№\s*([\w\-/]+)", text, re.IGNORECASE
    )
    rec.invoice_number = num.group(1) if num else None
    dm = re.search(
        r"от\s+(\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4}|\d{1,2}\s+[а-яё]+\s+\d{4})",
        text,
        re.IGNORECASE,
    )
    rec.invoice_date = _parse_date(dm.group(1)) if dm else None
    rec.confidence = _confidence(rec)
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
            "confidence": {"type": "number", "description": "Уверенность 0..1."},
        },
        "required": ["is_invoice", "confidence"],
    },
}

_LLM_PROMPT = (
    "Это документ из почты поставщика. Покупатель — наш ИП «Тепло» (ИП Шокина Е.А., ИНН "
    "890307589201). Если это счёт на оплату / счёт-фактура / УПД — извлеки реквизиты "
    "ПОЛУЧАТЕЛЯ платежа (поставщика), НЕ покупателя, и итоговую сумму К ОПЛАТЕ с НДС. "
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
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
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
    try:
        rec.confidence = float(payload.get("confidence") or 0.0)
    except (TypeError, ValueError):
        rec.confidence = 0.0
    if rec.inn in OWN_INNS:  # модель спутала покупателя и поставщика
        rec.inn = None
        rec.notes.append("llm вернул наш ИНН как получателя — отброшено")
    return rec


def _merge(base: RecognizedInvoice, extra: RecognizedInvoice) -> RecognizedInvoice:
    """Дополнить ``base`` недостающими полями из ``extra`` (детерминированное в приоритете)."""
    for attr in (
        "recipient_name", "inn", "kpp", "bank_acnt",
        "bank_bik", "corr_account", "amount", "invoice_number", "invoice_date",
    ):
        if getattr(base, attr) in (None, "") and getattr(extra, attr) not in (None, ""):
            setattr(base, attr, getattr(extra, attr))
    return base


async def recognize(pdf: bytes, *, settings: Settings) -> RecognizedInvoice:
    """Распознать счёт: детерминированный слой, при нехватке — LLM-фолбэк, затем слияние."""
    text = extract_pdf_text(pdf)
    det = deterministic_recognize(text)

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
    merged.engine = "deterministic+llm"
    merged.raw_text_excerpt = text[:2000]
    merged.confidence = max(det.confidence, llm.confidence, _confidence(merged))
    return merged
