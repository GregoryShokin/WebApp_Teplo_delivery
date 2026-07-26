"""Черновик платёжки в банк на уплату налога (единый налоговый платёж).

Черновик ≠ оплата: он лишь готовит платёжку в Т-Банке. Деньги уходят только после того, как
владелец подтвердит её в банк-клиенте, а факт списания придёт из выписки (``source_kind=
'bank_statement'``) и сойдётся в сверке. Получатель — фиксированная константа реквизитов ФНС,
не контрагент. Переиспользует общий контур черновика (``build_payment_draft_api_payload`` +
``TbankClient.create_payment_draft``), как это делают зарплатные и депозитные выплаты.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.tax import TaxBankDraft
from app.services.banking.base import PaymentDraftResult
from app.services.banking.exceptions import BankFetchError
from app.services.banking.fns_enp_requisites import treasury_enp_requisites
from app.services.banking.payout import payer_account_for, payout_client_for
from app.services.banking.sfr_injury_requisites import treasury_injury_requisites


class TaxDraftError(RuntimeError):
    """Черновик налоговой платёжки создать нельзя — причина в сообщении."""


def requisites_for_kind(tax_kind: str | None) -> dict:
    """Реквизиты получателя по виду налога.

    Всё, кроме травматизма, уходит единым налоговым платежом в ФНС. Травматизм — вне ЕНС:
    отдельная платёжка в СФР со своим КБК, счётом и статусом плательщика «08».
    """
    if tax_kind == "contrib_injury":
        return treasury_injury_requisites()
    return treasury_enp_requisites()


async def create_tax_bank_draft(
    session: AsyncSession,
    *,
    settings: Settings,
    amount: Decimal,
    purpose: str | None = None,
    tax_kind: str | None = None,
) -> PaymentDraftResult:
    """Создать черновик налоговой платёжки в Т-Банк (по умолчанию — ЕНП в ФНС)."""
    if amount is None or amount <= 0:
        raise TaxDraftError("Сумма платежа должна быть больше нуля.")
    payer_account = payer_account_for(settings, "tbank")
    if not payer_account:
        raise TaxDraftError("Не настроен счёт плательщика в Т-Банке.")

    requisites = requisites_for_kind(tax_kind)
    client = payout_client_for("tbank", session)
    return await client.create_payment_draft(
        document_id=uuid.uuid4().hex,
        amount=amount,
        purpose=purpose or str(requisites["paymentPurpose"]),
        requisites=requisites,
        payer_account=payer_account,
    )


async def create_tax_payment_draft(
    session: AsyncSession,
    *,
    tax_kind: str,
    amount: Decimal,
    purpose: str | None = None,
    for_year: int | None = None,
    for_period: str | None = None,
    due_date: date | None = None,
    title: str | None = None,
    created_by: uuid.UUID | None = None,
) -> TaxBankDraft:
    """Подготовить налоговый платёж в очередь «Активные платежи» (``status='ready_to_send'``).

    Платёж становится видимым и проверяемым до отправки в банк. Повторный клик по тому же
    обязательству (вид налога + период) НЕ плодит вторую строку: существующий неотправленный
    черновик обновляется (сумма/назначение/срок), а не дублируется.
    """
    if amount is None or amount <= 0:
        raise TaxDraftError("Сумма платежа должна быть больше нуля.")
    reqs = requisites_for_kind(tax_kind)
    resolved_purpose = (purpose or "").strip() or str(reqs["paymentPurpose"])

    # Идемпотентность по (вид налога, год, период), сравнение в Python — null-безопасно.
    # Неотправленный черновик обновляется; УЖЕ ОТПРАВЛЕННЫЙ — не дублируется: повторный
    # клик «Отправить в банк» не должен плодить вторую платёжку в Т-Банке.
    candidates = (
        await session.scalars(
            select(TaxBankDraft).where(
                TaxBankDraft.status.in_(("ready_to_send", "in_bank")),
                TaxBankDraft.tax_kind == tax_kind,
            )
        )
    ).all()
    existing = next(
        (d for d in candidates if d.for_year == for_year and d.for_period == for_period),
        None,
    )
    if existing is not None:
        if existing.status == "in_bank":
            raise TaxDraftError(
                "Платёж по этому обязательству уже отправлен в банк — подтвердите его "
                "в банк-клиенте или удалите черновик там."
            )
        existing.amount = amount
        existing.purpose = resolved_purpose
        existing.due_date = due_date
        if title:
            existing.title = title
        await session.flush()
        return existing

    draft = TaxBankDraft(
        tax_kind=tax_kind,
        for_year=for_year,
        for_period=for_period,
        title=title,
        amount=amount,
        purpose=resolved_purpose,
        due_date=due_date,
        kbk=str(reqs["kbk"]) if reqs.get("kbk") else None,
        recipient_name=str(reqs["recipientName"]) if reqs.get("recipientName") else None,
        status="ready_to_send",
        bank_provider="tbank",
        created_by=created_by,
    )
    session.add(draft)
    await session.flush()
    return draft


async def send_tax_draft_to_bank(
    session: AsyncSession,
    draft: TaxBankDraft,
    *,
    settings: Settings,
    amount: Decimal | None = None,
    purpose: str | None = None,
) -> TaxBankDraft:
    """Отправить подготовленный налоговый платёж в банк.

    Создаёт черновик платёжки ЕНП в Т-Банке и переводит строку в ``in_bank``. Сумму и
    назначение можно поправить перед отправкой (реквизиты получателя ФНС фиксированы,
    менять их нельзя). Отказ банка оставляет строку в очереди со статусом ``failed`` и текстом
    ошибки — платёж не теряется и его можно отправить повторно.
    """
    if draft.status not in ("ready_to_send", "failed"):
        raise TaxDraftError("Этот платёж уже отправлен в банк или закрыт.")
    if amount is not None:
        if amount <= 0:
            raise TaxDraftError("Сумма платежа должна быть больше нуля.")
        draft.amount = amount
    if purpose is not None and purpose.strip():
        draft.purpose = purpose.strip()

    try:
        result = await create_tax_bank_draft(
            session,
            settings=settings,
            amount=draft.amount,
            purpose=draft.purpose,
            tax_kind=draft.tax_kind,
        )
    except BankFetchError as exc:
        draft.status = "failed"
        draft.last_error = str(exc)[:500]
        await session.flush()
        raise

    draft.status = "in_bank"
    draft.document_id = result.document_id
    draft.provider_ref = result.provider_ref
    draft.last_error = None
    await session.flush()
    return draft


async def cancel_tax_draft(session: AsyncSession, draft: TaxBankDraft) -> TaxBankDraft:
    """Отменить подготовленный налоговый платёж (убрать из очереди активных платежей)."""
    if draft.status in ("paid", "cancelled"):
        raise TaxDraftError("Платёж уже закрыт.")
    draft.status = "cancelled"
    await session.flush()
    return draft
