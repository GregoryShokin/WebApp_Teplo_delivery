"""Банк-черновик выдачи депозита (курьеры + производственный персонал) — этап 3.

Выдача депозита безналичным каналом идёт «как зарплата»: деньги переводятся с
расчётного счёта на карту «Сейф» (ИП Шокина), откуда раздаются сотрудникам. У самих
сотрудников банковских реквизитов в системе НЕТ, поэтому черновик платежа в Т-Банк
всегда выписывается на ИП Шокину (те же реквизиты, что у зарплатного черновика,
``payroll.bank_payout_requisites``).

Модуль отвечает за ДВЕ вещи:
1. Пара ДДС-проводок перевода банк→Сейф (как ``payroll_payouts.book_bank_to_safe_transfer``):
   банк-нога (out, «Выбытие — перевод между счетами») — prebooked-цель для исходящей
   операции выписки; Сейф-нога (in, «Поступление — перевод между счетами») — двигает баланс
   Сейфа, откуда депозит раздаётся. Сам расход «Выдача депозита» списывается с Сейфа
   отдельной проводкой в ``deposit_service``/``couriers.deposit_service`` (wallet=Сейф для
   банк-черновика) — здесь только транзит.
2. Черновик платежа в Т-Банк на ИП Шокину (``build_payment_draft_api_payload`` +
   ``TbankClient.create_payment_draft``). Сетевой вызов — ПОСЛЕ commit, устойчиво: ошибка
   банка логируется и НЕ откатывает уже проведённую выдачу (как iiko-изъятие). iiko здесь
   НЕ вызывается (деньги идут не через «Главную кассу»).

Идемпотентность транзита — по ``(source_kind, source_id)``. У производственной выдачи
``source_id = DepositTransaction.id`` (UUID); у курьерской ``CourierDepositTransaction.id``
целочисленный и в UUID-поле ``source_id`` не помещается → ``source_id=None`` (операция
создаётся один раз, как в ``_book_deposit_return_cashflow``).
"""

from __future__ import annotations

import logging
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import Account, CashflowTransaction, DdsArticle, Wallet
from app.services.banking import BankClient
from app.services.banking.ip_card_requisites import (
    load_owner_approved_ip_card_requisites,
)
from app.services.banking.payout import payer_account_for, payout_client_for
from app.services.banking.tbank import build_payment_draft_api_payload
from app.services.wallets import (
    DDS_ARTICLE_TRANSFER_IN_CODE,
    DDS_ARTICLE_TRANSFER_OUT_CODE,
    SAFE_WALLET_CODE,
)

logger = logging.getLogger(__name__)

MOCK_PAYER_ACCOUNT = "00000000000000000000"

# source_kind транзитной пары банк→Сейф для выдачи депозита.
COURIER_DEPOSIT_RETURN_DRAFT_SOURCE_KIND = "courier_deposit_return_draft"
PRODUCTION_DEPOSIT_PAYOUT_DRAFT_SOURCE_KIND = "production_deposit_payout_draft"

MONEY = Decimal("0.01")


def _money(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(MONEY)


async def book_deposit_bank_to_safe_transfer(
    session: AsyncSession,
    *,
    source_kind: str,
    source_id: uuid.UUID | None,
    amount: Decimal,
    operation_date: date,
    purpose: str,
    provider: str = "tbank",
) -> bool:
    """Завести транзит банк→Сейф под безналичную выдачу депозита.

    ``provider`` задаёт банк-плательщика (``tbank`` / ``sber``): банк-нога перевода садится на
    кошелёк расчётного счёта этого банка. Идемпотентно по ``(source_kind, source_id)`` (когда
    ``source_id`` задан). Устойчиво: нет расчётного/Сейф-кошелька или статей переводов — no-op
    (возврат False), выдачу не валим. Возвращает True, если пара проводок создана.
    """
    amount = _money(amount)
    if amount <= 0:
        return False
    if source_id is not None:
        existing = await session.scalar(
            select(CashflowTransaction.id).where(
                CashflowTransaction.source_kind == source_kind,
                CashflowTransaction.source_id == source_id,
            )
        )
        if existing is not None:
            return False

    settings = get_settings()
    payer_account = payer_account_for(settings, provider)
    if not payer_account:
        return False
    bank_wallet = await session.scalar(
        select(Wallet)
        .join(Account, Account.id == Wallet.account_id)
        .where(Account.account_number == payer_account, Wallet.status == "active")
    )
    safe_wallet = await session.scalar(
        select(Wallet).where(Wallet.code == SAFE_WALLET_CODE, Wallet.status == "active")
    )
    if bank_wallet is None or safe_wallet is None:
        return False
    transfer_out_article = await session.scalar(
        select(DdsArticle.id).where(DdsArticle.code == DDS_ARTICLE_TRANSFER_OUT_CODE)
    )
    transfer_in_article = await session.scalar(
        select(DdsArticle.id).where(DdsArticle.code == DDS_ARTICLE_TRANSFER_IN_CODE)
    )
    session.add(
        CashflowTransaction(
            wallet_id=bank_wallet.id,
            direction="out",
            amount=amount,
            operation_date=operation_date,
            article_id=transfer_out_article,
            source_kind=source_kind,
            source_id=source_id,
            payment_purpose=purpose,
            quality_status="final",
        )
    )
    session.add(
        CashflowTransaction(
            wallet_id=safe_wallet.id,
            direction="in",
            amount=amount,
            operation_date=operation_date,
            article_id=transfer_in_article,
            source_kind=source_kind,
            source_id=source_id,
            payment_purpose=purpose,
            quality_status="final",
        )
    )
    await session.flush()
    return True


async def send_deposit_payout_bank_draft(
    session: AsyncSession,
    *,
    document_id: str,
    amount: Decimal,
    purpose: str,
    bank_client: BankClient | None = None,
    provider: str = "tbank",
) -> bool:
    """Выписать банк-черновик на Сейф (Шокину) под выдачу депозита.

    ``provider`` (``tbank`` / ``sber``) выбирает банк-плательщика: Т-Банк — черновик через
    Open API, Сбер — рублёвый РПП без подписи (черновик, подписывается в СберБизнес).
    Получатель-Сейф общий и зафиксирован в коде; одноимённая настройка БД используется
    только для контроля дрейфа. Вызывать ПОСЛЕ commit (БД — источник истины). Устойчиво:
    нет расчётного счёта или ошибка банка — логируется, исключение НЕ поднимается.
    Возвращает True, если черновик отправлен (или сэмулирован в mock-режиме).
    """
    try:
        amount = _money(amount)
        if amount <= 0:
            return False
        requisites = await _bank_payout_requisites(session)
        settings = get_settings()
        payer_account = payer_account_for(settings, provider)
        if not payer_account:
            logger.warning(
                "Банк-черновик выдачи депозита %s: не настроен расчётный счёт плательщика (%s)",
                document_id,
                provider,
            )
            return False
        # Валидация полей платежа (выкинет ValueError при нехватке реквизитов) — до сетевого вызова.
        build_payment_draft_api_payload(
            document_id=document_id,
            amount=amount,
            purpose=purpose,
            requisites=requisites,
            payer_account=payer_account,
        )
        client = bank_client or payout_client_for(provider, session)
        result = await client.create_payment_draft(
            document_id=document_id,
            amount=amount,
            purpose=purpose,
            requisites=dict(requisites),
            payer_account=payer_account,
        )
        logger.info(
            "Банк-черновик выдачи депозита %s отправлен: status=%s ref=%s",
            document_id,
            getattr(result, "status", None),
            getattr(result, "provider_ref", None),
        )
        return True
    except Exception as exc:  # noqa: BLE001 — банк не должен валить уже проведённую выдачу
        logger.warning(
            "Банк-черновик выдачи депозита %s не отправлен: %s", document_id, exc
        )
        return False


async def _bank_payout_requisites(session: AsyncSession) -> dict[str, Any]:
    return await load_owner_approved_ip_card_requisites(session)


def _payer_account(settings: Any) -> str | None:
    if settings.tbank_api_account_number:
        return settings.tbank_api_account_number
    if settings.teplo_bank_client_mode == "mock":
        return MOCK_PAYER_ACCOUNT
    return None
