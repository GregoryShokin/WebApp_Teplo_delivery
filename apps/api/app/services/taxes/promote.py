"""Продвижение распознанного документа из staging в плановое обязательство ``tax_payment``.

Мост между «бухгалтер прислала платёжку» и налоговым контуром: строка ``tax_document_intake``
со статусом ``parsed`` превращается в ``TaxPayment(status='planned')`` — то есть в обязательство,
которое видно в календаре и участвует в сверке ДО того, как деньги ушли из банка.

Четыре гарда, каждый закрывает реальный случай:

1. **Ноль не продвигается.** Нулевая платёжка-заглушка (реальный случай 22.07.2026) —
   это не обязательство на 0 ₽, а несформированный документ. Продвигать нечего.
2. **Смешанный ЕНП не продвигается.** ``enp_payroll`` содержит НДФЛ и взносы одной суммой;
   пока нет уведомления с разносом, вид платежа неизвестен, а гадать нельзя.
3. **Только ``parsed``.** ``needs_review`` ждёт владельца — автоматика его не трогает.
4. **Повторное продвижение обновляет план, а не плодит строки.** Агент прислал ноль,
   потом исправление — плановая строка должна стать одной, с последней суммой.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tax import TAX_PAYMENT_KINDS, TaxDocumentIntake, TaxPayment

logger = logging.getLogger(__name__)

# Вид, который нельзя продвинуть без разноса: НДФЛ и взносы в одной сумме.
UNSPLITTABLE_KIND = "enp_payroll"


class PromotionError(RuntimeError):
    """Документ нельзя продвинуть — причина в сообщении."""


@dataclass(frozen=True)
class PromotionResult:
    intake_id: uuid.UUID
    tax_payment_id: uuid.UUID | None
    action: str  # 'created' | 'updated' | 'skipped'
    reason: str | None = None


def _tax_year(period_hint: str | None, due: date | None) -> int:
    """Налоговый год обязательства.

    Годовой платёж по УСН уплачивается в АПРЕЛЕ СЛЕДУЮЩЕГО года — для него год берётся
    предыдущий, иначе платёж уедет не в тот период нарастающего итога.
    """
    if due is None:
        return date.today().year
    if period_hint == "year" and due.month <= 4:
        return due.year - 1
    return due.year


def _validate(intake: TaxDocumentIntake) -> tuple[str, Decimal, date | None, str | None]:
    """Проверить, что документ пригоден к продвижению. Вернуть (kind, amount, due, period)."""
    if intake.document_type != "payment_order":
        raise PromotionError(
            f"Продвигать можно только платёжки, а это {intake.document_type}."
        )
    if intake.status == "promoted":
        raise PromotionError("Документ уже продвинут.")
    if intake.status != "parsed":
        raise PromotionError(
            f"Продвигаются только уверенно распознанные документы (статус parsed), "
            f"текущий — {intake.status}."
        )

    rec = intake.recognition or {}
    kind = rec.get("tax_kind")
    if kind == UNSPLITTABLE_KIND:
        raise PromotionError(
            "Зарплатный ЕНП содержит НДФЛ и взносы одной суммой — нужен разнос "
            "по уведомлению об исчисленных суммах, продвигать нельзя."
        )
    if kind not in TAX_PAYMENT_KINDS:
        raise PromotionError(f"Неизвестный вид платежа: {kind!r}.")

    raw_amount = rec.get("amount")
    if raw_amount is None:
        raise PromotionError("В документе не распознана сумма.")
    amount = Decimal(str(raw_amount))
    if amount <= 0:
        raise PromotionError(
            "Сумма в документе нулевая — это несформированная платёжка, а не обязательство."
        )

    due = date.fromisoformat(rec["due_date"]) if rec.get("due_date") else None
    return kind, amount, due, rec.get("period_hint")


async def promote_intake(
    session: AsyncSession,
    intake: TaxDocumentIntake,
    *,
    actor_user_id: uuid.UUID | None = None,
) -> PromotionResult:
    """Превратить распознанную платёжку в плановое обязательство.

    Идемпотентно по слоту ``(for_year, kind, for_period)``: повторный документ того же
    обязательства обновляет плановую сумму и срок, а не создаёт вторую строку.
    """
    kind, amount, due, period = _validate(intake)
    year = _tax_year(period, due)

    existing = (
        await session.execute(
            select(TaxPayment).where(
                TaxPayment.status == "planned",
                TaxPayment.for_year == year,
                TaxPayment.kind == kind,
                TaxPayment.for_period == period,
            )
        )
    ).scalar_one_or_none()

    if existing is not None:
        existing.amount = amount
        if due is not None:
            existing.paid_on = due
        existing.source_kind = "tax_notice"
        existing.quality_status = "confirmed"
        intake.status = "promoted"
        intake.tax_payment_bundle_id = existing.bundle_id
        await session.flush()
        return PromotionResult(intake.id, existing.id, "updated")

    bundle_id = uuid.uuid4()
    payment = TaxPayment(
        id=uuid.uuid4(),
        bundle_id=bundle_id,
        # Для планового обязательства дата — это СРОК уплаты; при оплате она перезапишется
        # фактической датой списания (кассовый принцип вычета опирается на факт).
        paid_on=due or date.today(),
        kind=kind,
        amount=amount,
        recipient="sfr" if kind == "contrib_injury" else "fns",
        for_year=year,
        for_period=period,
        status="planned",
        purpose=(intake.recognition or {}).get("purpose"),
        source_kind="tax_notice",
        quality_status="confirmed",
        note=f"Из документа: {intake.filename}" if intake.filename else None,
        created_by_user_id=actor_user_id,
    )
    session.add(payment)
    intake.status = "promoted"
    intake.tax_payment_bundle_id = bundle_id
    await session.flush()
    return PromotionResult(intake.id, payment.id, "created")


async def promote_ready_intakes(
    session: AsyncSession, *, actor_user_id: uuid.UUID | None = None
) -> list[PromotionResult]:
    """Продвинуть все готовые документы. Непригодные пропускаются с причиной, не падают."""
    rows = (
        await session.execute(
            select(TaxDocumentIntake)
            .where(
                TaxDocumentIntake.status == "parsed",
                TaxDocumentIntake.document_type == "payment_order",
            )
            .order_by(TaxDocumentIntake.received_at.asc())
        )
    ).scalars().all()

    results: list[PromotionResult] = []
    for intake in rows:
        try:
            results.append(await promote_intake(session, intake, actor_user_id=actor_user_id))
        except PromotionError as exc:
            logger.info("promote: пропуск %s — %s", intake.filename, exc)
            results.append(PromotionResult(intake.id, None, "skipped", str(exc)))
    return results
