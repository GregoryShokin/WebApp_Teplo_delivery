"""Черновик платёжки в банк на уплату налога (единый налоговый платёж).

Черновик ≠ оплата: он лишь готовит платёжку в Т-Банке. Деньги уходят только после того, как
владелец подтвердит её в банк-клиенте, а факт списания придёт из выписки (``source_kind=
'bank_statement'``) и сойдётся в сверке. Получатель — фиксированная константа реквизитов ФНС,
не контрагент. Переиспользует общий контур черновика (``build_payment_draft_api_payload`` +
``TbankClient.create_payment_draft``), как это делают зарплатные и депозитные выплаты.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.services.banking.base import PaymentDraftResult
from app.services.banking.fns_enp_requisites import treasury_enp_requisites
from app.services.banking.payout import payer_account_for, payout_client_for


class TaxDraftError(RuntimeError):
    """Черновик налоговой платёжки создать нельзя — причина в сообщении."""


async def create_tax_bank_draft(
    session: AsyncSession,
    *,
    settings: Settings,
    amount: Decimal,
    purpose: str | None = None,
) -> PaymentDraftResult:
    """Создать черновик платёжки ЕНП в Т-Банк на указанную сумму."""
    if amount is None or amount <= 0:
        raise TaxDraftError("Сумма платежа должна быть больше нуля.")
    payer_account = payer_account_for(settings, "tbank")
    if not payer_account:
        raise TaxDraftError("Не настроен счёт плательщика в Т-Банке.")

    requisites = treasury_enp_requisites()
    client = payout_client_for("tbank", session)
    return await client.create_payment_draft(
        document_id=uuid.uuid4().hex,
        amount=amount,
        purpose=purpose or str(requisites["paymentPurpose"]),
        requisites=requisites,
        payer_account=payer_account,
    )
