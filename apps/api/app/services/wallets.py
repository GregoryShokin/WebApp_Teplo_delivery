"""Общие константы и резолверы кошельков ДДС.

Вынесены из ``payroll_payouts`` в отдельный модуль без зависимостей на payroll,
чтобы депозитные сервисы (курьеры/производственный персонал) переиспользовали
их без цикл-импорта (deposit ↔ payroll). ``payroll_payouts`` реэкспортирует эти
имена для обратной совместимости.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Wallet

# Подотчётный Сейф (личная карта владельца) — наличный кошелёк ДДС.
SAFE_WALLET_CODE = "cash_safe"
# Типы наличных кошельков, с которых допустима выдача наличной части (Сейф, кассы).
CASH_WALLET_TYPES = frozenset({"cash", "cash_register", "cash_safe", "store_cash"})
# Статьи ДДС для внутренних переводов между счетами (банк↔Сейф и т.п.).
DDS_ARTICLE_TRANSFER_IN_CODE = "postuplenie_perevod_mezhdu_schetami"
DDS_ARTICLE_TRANSFER_OUT_CODE = "vybytie_perevod_mezhdu_schetami"


class CashWalletError(ValueError):
    """Наличный счёт не найден, неактивен или не является наличным."""


async def resolve_cash_wallet(session: AsyncSession, code: str) -> Wallet:
    """Найти активный наличный кошелёк по коду (Сейф / Торговая касса Черникова и т.п.)."""
    wallet = await session.scalar(select(Wallet).where(Wallet.code == code))
    if wallet is None or wallet.status != "active":
        raise CashWalletError("Наличный счёт не найден или неактивен")
    if wallet.type not in CASH_WALLET_TYPES:
        raise CashWalletError("Выбранный счёт не является наличным")
    return wallet
