"""Выбор банковского клиента-плательщика по провайдеру (Т-Банк / Сбер) для payout-черновиков.

Оба клиента реализуют Protocol ``BankClient`` (метод ``create_payment_draft``), поэтому
payout-сервисы (ЗП-ведомости, депозиты, авансы/займы) не зависят от конкретного банка —
они получают и клиент, и счёт-плательщик через эти фабрики по коду банка из выбранного
пользователем канала выплаты (``bank_draft`` = Т-Банк, ``bank_draft_sber`` = Сбер).
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.services.banking.base import BankClient
from app.services.banking.sber import MOCK_PAYER_ACCOUNT, SberClient
from app.services.banking.tbank import TbankClient

# Провайдеры, умеющие создавать платёжный черновик (плательщики выплат).
PAYOUT_PROVIDERS = ("tbank", "sber")

# Значения канала выплаты (account_choice / payout_method) → банк-плательщик. Старый `bank_draft`
# = Т-Банк (обратная совместимость), новый `bank_draft_sber` = Сбер. Оба книжат транзит на Сейф.
BANK_DRAFT_CHANNELS = {"bank_draft": "tbank", "bank_draft_sber": "sber"}


def channel_provider(channel: str | None) -> str | None:
    """Банк-провайдер по значению канала выплаты, или None если канал не банк-черновик."""
    return BANK_DRAFT_CHANNELS.get(channel or "")


def payout_client_for(provider: str, session: AsyncSession | None) -> BankClient:
    """Клиент банка-плательщика по коду провайдера (по умолчанию — Т-Банк)."""
    if provider == "sber":
        return SberClient(session)
    return TbankClient(session)


def payer_account_for(settings: Settings, provider: str) -> str | None:
    """Номер расчётного счёта плательщика для провайдера (в mock — заглушка)."""
    number = (
        settings.sber_api_account_number
        if provider == "sber"
        else settings.tbank_api_account_number
    )
    if number:
        return number
    if settings.teplo_bank_client_mode == "mock":
        return MOCK_PAYER_ACCOUNT
    return None
