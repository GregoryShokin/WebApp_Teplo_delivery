"""Ручной файл → предложения для разбора. Не создаёт контрагентов и платежей.

Внешняя передача для этого нового пути по умолчанию выключена. Превью и тесты
работают с локальным текстовым разбором или подставным ответом модели.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models import Counterparty, EmailInvoiceIntake
from app.services import invoice_recognition as recognition
from app.services.utility_images import HEIC_MIME_TYPES, to_displayable


def add_duration_evidence(rec: recognition.RecognizedInvoice, text: str) -> None:
    """Явная длительность отдельно от дат. Пробная строка не меняет основную услугу."""
    if not rec.line_items:
        pattern = re.compile(
            r"^\s*(?:\d+[.)]?\s+)?(.+?)\s+(\d+(?:[.,]\d+)?)\s+"
            r"(мес\.?|месяц\w*|шт\.?)\s+([\d \u00a0]+[.,]\d{2})\s+"
            r"([\d \u00a0]+[.,]\d{2})\s*$",
            re.MULTILINE | re.IGNORECASE,
        )
        for match in pattern.finditer(text):
            description, quantity, unit, price, amount = match.groups()
            rec.line_items.append(
                {
                    "description": description,
                    "quantity": quantity,
                    "unit": unit,
                    "unit_price": price,
                    "amount": amount,
                }
            )
    monthly = []
    for item in rec.line_items:
        if not str(item.get("unit", "")).lower().startswith("мес"):
            continue
        try:
            quantity = Decimal(str(item.get("quantity", "")).replace(",", "."))
            amount = Decimal(re.sub(r"\s", "", str(item.get("amount", ""))).replace(",", "."))
            if not quantity.is_finite() or not amount.is_finite():
                continue
        except (InvalidOperation, ValueError):
            continue
        if quantity == int(quantity) and 1 <= quantity <= 120 and amount > 1:
            monthly.append((int(quantity), item))
    durations = {months for months, _ in monthly}
    if rec.service_duration_months is None and len(durations) == 1:
        rec.service_duration_months = durations.pop()
        item = monthly[0][1]
        rec.field_evidence["service_duration_months"] = (
            f"{item.get('description', '')}: {item.get('quantity')} {item.get('unit')}"
        )
    if rec.service_duration_months is None:
        matches = list(re.finditer(r"(?:на|за|сроком)\s+(\d{1,2})\s+месяц\w*", text, re.I))
        values = {int(m[1]) for m in matches if 1 <= int(m[1]) <= 120}
        if len(values) == 1:
            rec.service_duration_months = values.pop()
            rec.field_evidence["service_duration_months"] = matches[0][0]


async def recognize_intake(
    session: AsyncSession,
    intake: EmailInvoiceIntake,
    *,
    settings: Settings,
    extracted_text: str | None = None,
) -> None:
    previous = intake.recognition or {}
    if extracted_text is None and intake.attachment_mime != "application/pdf":
        extracted_text = previous.get("raw_text")
    content = bytes(intake.pdf_bytes or b"")
    mime = intake.attachment_mime or "application/pdf"
    if mime in HEIC_MIME_TYPES:
        content, mime = to_displayable(content, mime)
    rec = await recognition.recognize(
        content,
        settings=settings,
        mime=mime,
        context_text=extracted_text,
        force_llm=True,
        allow_llm=settings.invoice_review_external_llm_enabled,
    )
    text = extracted_text or rec.raw_text_excerpt
    add_duration_evidence(rec, text or "")
    result = rec.to_json()
    result["raw_text"] = text
    if previous.get("utility"):
        result["utility"] = dict(previous["utility"])
        if rec.amount is not None:
            result["utility"]["blocking"] = []
            result["utility"]["raw_reasons"] = []
    reviewed = previous.get("reviewed_fields") or {}
    result.update(reviewed)
    result["reviewed_fields"] = reviewed
    if "requisites_reviewed" in previous:
        result["requisites_reviewed"] = previous["requisites_reviewed"]
    intake.recognition = result
    intake.engine = rec.engine
    intake.confidence = Decimal(str(max(0, min(1, rec.confidence))))
    if intake.invoice_id is None:
        intake.status = "needs_review"
    if intake.counterparty_id is None and rec.inn:
        intake.counterparty_id = await session.scalar(
            select(Counterparty.id).where(Counterparty.inn == rec.inn)
        )
    await session.flush()


def service_period_is_required(
    recognition: dict[str, object], profile_required: bool = False
) -> bool:
    """Длительность в самом счёте требует дат независимо от настройки карточки."""
    return bool(
        profile_required
        or recognition.get("service_duration_months")
        or recognition.get("service_period_ambiguous")
    )


def validate_review_fields(fields: dict[str, object]) -> None:
    if fields.get("amount") is not None:
        try:
            amount = Decimal(str(fields["amount"]).replace(",", ".").replace(" ", ""))
            if not amount.is_finite() or amount <= 0:
                raise ValueError
        except (InvalidOperation, ValueError):
            raise ValueError("Сумма к оплате должна быть положительным числом") from None
    if fields.get("invoice_date"):
        try:
            date.fromisoformat(str(fields["invoice_date"]))
        except ValueError:
            raise ValueError("Укажите корректную дату счёта") from None
    start, end = fields.get("service_period_start"), fields.get("service_period_end")
    if bool(start) != bool(end):
        raise ValueError("Укажите обе даты периода оказания услуги")
    if start and end:
        try:
            if date.fromisoformat(str(end)) < date.fromisoformat(str(start)):
                raise ValueError
        except ValueError:
            raise ValueError("Проверьте начало и окончание периода услуги") from None
