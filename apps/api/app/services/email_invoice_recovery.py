"""Восстановление почтового документа после сверки распознавания с исходником.

Сервис не распознаёт PDF повторно и не решает, какая дата правильная: вызывающий передаёт
проверенные поля и точный снимок прежнего распознавания. Деньги и банковские черновики он
не создаёт. План применения можно проверить во внешней транзакции с последующим rollback.
"""

from __future__ import annotations

import copy
import uuid
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Counterparty,
    CounterpartyPayableProfile,
    EmailInvoiceIntake,
    SupplierInvoice,
)
from app.services import email_invoice_ingest as ingest
from app.services import supplier_service_periods as service_periods
from app.services.invoice_review import validate_review_fields


def _checked_recognition(value: dict[str, Any]) -> dict[str, Any]:
    rec = copy.deepcopy(value)
    if rec.get("package_ambiguous"):
        raise ValueError("Состав пакета документов не подтверждён")
    if ingest._doc_kind_from_recognition(rec.get("document_kind")) is None:
        raise ValueError("Не подтверждён тип документа")
    validate_review_fields(rec)
    try:
        amount = Decimal(str(rec.get("amount")))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("Не подтверждена сумма документа") from None
    if not amount.is_finite() or amount <= 0:
        raise ValueError("Не подтверждена положительная сумма документа")
    # Для восстановления собственного номера/даты нельзя подставлять реквизиты соседа.
    if not rec.get("invoice_number") or ingest._date_or_none(rec.get("invoice_date")) is None:
        raise ValueError("Не подтверждены номер и дата документа")
    start = ingest._date_or_none(rec.get("service_period_start"))
    end = ingest._date_or_none(rec.get("service_period_end"))
    if bool(start) != bool(end):
        raise ValueError("Укажите обе границы периода услуги")
    if start is not None and end is not None:
        service_periods.validate_period(start, end)
    if rec.get("service_period_ambiguous"):
        raise ValueError("Период услуги остаётся неоднозначным")
    return rec


async def _lock_invoice(session: AsyncSession, invoice_id: uuid.UUID) -> SupplierInvoice:
    invoice = await session.scalar(
        select(SupplierInvoice)
        .where(SupplierInvoice.id == invoice_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if invoice is None:
        raise ValueError("Связанный документ не найден")
    if invoice.draft_id is not None or invoice.payment_status != "unpaid":
        raise ValueError("Документ уже в банке, оплачен или аннулирован — восстановление запрещено")
    if invoice.source != "email":
        raise ValueError("Связанный документ принадлежит другому источнику")
    return invoice


async def _set_invoice_fields(
    session: AsyncSession, invoice: SupplierInvoice, recognition: dict[str, Any]
) -> None:
    invoice.amount = Decimal(str(recognition["amount"]))
    invoice.number = str(recognition["invoice_number"])
    invoice.invoice_date = ingest._date_or_none(recognition["invoice_date"])
    invoice.service_period_start = ingest._date_or_none(recognition.get("service_period_start"))
    invoice.service_period_end = ingest._date_or_none(recognition.get("service_period_end"))
    invoice.service_period_source = recognition.get("service_period_source") or None
    invoice.service_period_confidence = recognition.get("service_period_confidence")
    invoice.service_period_status = (
        "ready" if invoice.service_period_start and invoice.service_period_end else "not_required"
    )
    invoice.vat_total, invoice.vat_breakdown = ingest._recognized_vat(recognition, invoice.amount)
    invoice.raw_payload = {**(invoice.raw_payload or {}), "recognition": recognition}
    # Сначала решаем, информационный ли внешний акт при действующем договоре; только
    # затем синхронизируем расход. Иначе исправление могло бы задвоить договорное начисление.
    await ingest._resync_closing_after_edit(session, invoice)
    await service_periods.sync_invoice_accrual(session, invoice)


async def recover_intake_recognition(
    session: AsyncSession,
    intake: EmailInvoiceIntake,
    *,
    recognition: dict[str, Any],
    expected_recognition: dict[str, Any],
    reason: str,
    actor_user_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Применить сверенные поля, сохранив документы и их связи. Не коммитит.

    Потерянный счёт из пакета восстанавливается отдельным bill. Уже заведённый акт не
    переименовывается в счёт: его ID остаётся у закрывающего-спутника. Изменённый другим
    процессом документ, оплаченный документ и ручное исключение автоматически не трогаются.
    """
    if not reason.strip():
        raise ValueError("Укажите основание восстановления")
    rec = _checked_recognition(recognition)
    companion_rec = rec.get("companion")
    if companion_rec is not None:
        if not isinstance(companion_rec, dict):
            raise ValueError("Некорректный закрывающий документ пакета")
        companion_rec = _checked_recognition(companion_rec)
        if ingest._doc_kind_from_recognition(companion_rec.get("document_kind")) != "closing":
            raise ValueError("Спутник пакета должен быть актом или УПД")
        if ingest._doc_kind_from_recognition(rec.get("document_kind")) != "bill":
            raise ValueError("Основным документом пакета должен быть счёт")
        rec["companion"] = companion_rec

    async with session.begin_nested():
        await session.refresh(intake, with_for_update=True)
        if intake.recognition != expected_recognition:
            raise ValueError("Распознавание изменилось после проверки — повторите сверку")
        if intake.status not in ("linked", "closing", "needs_review", "recognized", "failed"):
            raise ValueError(
                "Исключённый, проигнорированный или дублирующий документ не восстанавливается"
            )
        if intake.counterparty_id is None:
            raise ValueError("Не выбран контрагент")
        cp = await session.get(Counterparty, intake.counterparty_id)
        if cp is None:
            raise ValueError("Контрагент не найден")
        profile = await session.scalar(
            select(CounterpartyPayableProfile).where(
                CounterpartyPayableProfile.counterparty_id == intake.counterparty_id
            )
        )
        if (
            ingest._doc_kind_from_recognition(rec.get("document_kind")) == "bill"
            and ingest.service_period_is_required(
                rec, bool(profile and profile.service_period_required)
            )
            and not (rec.get("service_period_start") and rec.get("service_period_end"))
        ):
            raise ValueError("Для счёта за период укажите начало и окончание услуги")
        for doc_rec in (rec, companion_rec):
            if doc_rec and doc_rec.get("inn") and cp.inn and str(doc_rec["inn"]) != cp.inn:
                raise ValueError("ИНН проверенного документа не совпадает с контрагентом")

        primary = await _lock_invoice(session, intake.invoice_id) if intake.invoice_id else None
        companion = (
            await _lock_invoice(session, intake.companion_invoice_id)
            if intake.companion_invoice_id
            else None
        )
        for document in (primary, companion):
            if document and document.counterparty_id != intake.counterparty_id:
                raise ValueError("Связанный документ относится к другому контрагенту")
        before = {
            "recognition": copy.deepcopy(intake.recognition),
            "status": intake.status,
            "invoice_id": str(intake.invoice_id) if intake.invoice_id else None,
            "companion_invoice_id": str(intake.companion_invoice_id)
            if intake.companion_invoice_id
            else None,
        }
        before["documents"] = [
            {
                "id": str(document.id),
                "kind": document.doc_kind,
                "number": document.number,
                "invoice_date": str(document.invoice_date) if document.invoice_date else None,
                "amount": str(document.amount),
                "payment_status": document.payment_status,
                "activation_status": document.activation_status,
                "informational": document.informational,
                "service_period_start": str(document.service_period_start)
                if document.service_period_start
                else None,
                "service_period_end": str(document.service_period_end)
                if document.service_period_end
                else None,
            }
            for document in (primary, companion)
            if document is not None
        ]
        old_history = copy.deepcopy((intake.recognition or {}).get("recovery_history") or [])
        history_item = {
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "actor_user_id": str(actor_user_id) if actor_user_id else None,
            "reason": reason.strip(),
            "before": {
                **before,
                "recognition": {
                    k: v for k, v in before["recognition"].items() if k != "recovery_history"
                },
            },
        }
        rec["recovery_history"] = [*old_history, history_item]
        new_kind = ingest._doc_kind_from_recognition(rec.get("document_kind"))
        if primary is not None and primary.doc_kind != new_kind:
            if (
                primary.doc_kind != "closing"
                or new_kind != "bill"
                or companion_rec is None
                or companion is not None
            ):
                raise ValueError("Изменение роли связанного документа требует отдельного разбора")
            # Это собственный акт из того же вложения, а не чужая найденная накладная.
            # Платёжные аллокации не переносятся: ID акта и его учётная история сохраняются.
            primary.external_id = f"{intake.attachment_sha256}:closing"[:128]
            await _set_invoice_fields(session, primary, companion_rec)
            intake.companion_invoice_id = primary.id
            intake.invoice_id = None
            primary = None
        elif primary is not None:
            await _set_invoice_fields(session, primary, rec)

        if companion is not None:
            if companion_rec is None:
                raise ValueError("Нельзя потерять уже созданный закрывающий документ пакета")
            await _set_invoice_fields(session, companion, companion_rec)
        intake.recognition = rec
        if primary is None:
            intake.status = "needs_review"
            await ingest.materialize_from_intake(session, intake)
        else:
            intake.status = "linked" if new_kind == "bill" else "closing"
            await ingest._materialize_package_companion(
                session, intake, cp_id=intake.counterparty_id, recognition=rec
            )
        intake.error = None
        await session.flush()
        return {
            "intake_id": str(intake.id),
            "before": before,
            "status": intake.status,
            "invoice_id": str(intake.invoice_id) if intake.invoice_id else None,
            "companion_invoice_id": str(intake.companion_invoice_id)
            if intake.companion_invoice_id
            else None,
        }
