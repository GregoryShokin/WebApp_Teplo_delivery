"""Сторож задвоенного возврата: один возврат поставщика, проведённый двумя каналами.

ОТКУДА ДЫРА. Возврат переплаты гасит дебиторку контрагента без аллокации — пересборка
``resync_counterparty_refunds`` берёт ВСЕ его приходы с возвратной статьёй и списывает ими
авансы по FIFO. Если те же деньги провести дважды — «Новым платежом» в Сейф/Кассу и разбором
выписки, — пересборка честно видит два прихода: аванс 1 000, возврат 300 наличными и та же
выписка на 300 дают погашение 600. С 25.09 (78770ec1) пересборка стоит на всех дверях разбора
выписки, поэтому ошибка оператора, раньше тихо лежавшая, сразу бьёт по дебиторке.

ПОЧЕМУ НЕ В РАСЧЁТЕ. Код расчёта прав: в ДДС действительно два прихода, и отличить «те же
деньги» от «два возврата одной суммы» по данным нельзя. Ошибка — задвоенный приход в ДДС, её
исправляет человек (исключает лишнюю проводку). Здесь мы только показываем ему совпадение ДО
того, как он её совершит.

ЧТО СЧИТАЕМ ДВОЙНИКОМ. Приход того же контрагента с возвратной статьёй на ту же сумму в пределах
``REFUND_TWIN_WINDOW_DAYS`` дней, проведённый ДРУГИМ каналом: наличные против банка. Внутри
одного канала совпадение — не наша дыра: две операции выписки — это два реальных поступления,
а своя же проводка при переразметке иначе нашла бы саму себя. Исключённые проводки и
гашения бартерного займа деньгами (``not_barter_money_return``) дебиторку не гасят — и
двойниками не считаются.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CashflowTransaction, DdsArticle, Wallet
from app.services.banking.cashflow_classify import EXCLUDED_QUALITY
from app.services.supplier_prepayments import (
    SUPPLIER_REFUND_ARTICLE_CODE,
    not_barter_money_return,
)
from app.services.supplier_service_periods import money
from app.services.wallets import CASH_WALLET_TYPES

# «Несколько дней»: наличные вносят в день передачи, а выписка приходит датой зачисления —
# между ними бывают выходные и задержка банка. Шире недели совпадение суммы уже ни о чём не
# говорит, а окно поуже пропустит возврат, отражённый после выходных.
REFUND_TWIN_WINDOW_DAYS = 7

RefundChannel = Literal["cash", "bank"]


@dataclass(frozen=True)
class RefundTwin:
    transaction_id: uuid.UUID
    operation_date: date
    amount: Decimal
    wallet_name: str
    channel: RefundChannel
    source_kind: str


def wallet_channel(wallet_type: str) -> RefundChannel:
    """Канал денег по виду кошелька: наличные (Сейф, Касса) или всё безналичное."""
    return "cash" if wallet_type in CASH_WALLET_TYPES else "bank"


async def find_refund_twins(
    session: AsyncSession,
    *,
    counterparty_id: uuid.UUID,
    amount: Decimal,
    on_date: date,
    channel: RefundChannel,
) -> list[RefundTwin]:
    """Возвраты контрагента на ту же сумму рядом по дате, уже проведённые другим каналом."""
    amount = money(amount)
    if amount <= 0:
        return []
    cash_types = tuple(sorted(CASH_WALLET_TYPES))
    other_channel = (
        Wallet.type.not_in(cash_types) if channel == "cash" else Wallet.type.in_(cash_types)
    )
    rows = (
        await session.execute(
            select(CashflowTransaction, Wallet.name, Wallet.type)
            .join(DdsArticle, DdsArticle.id == CashflowTransaction.article_id)
            .join(Wallet, Wallet.id == CashflowTransaction.wallet_id)
            .where(
                CashflowTransaction.counterparty_id == counterparty_id,
                CashflowTransaction.direction == "in",
                CashflowTransaction.amount == amount,
                CashflowTransaction.quality_status != EXCLUDED_QUALITY,
                CashflowTransaction.operation_date
                >= on_date - timedelta(days=REFUND_TWIN_WINDOW_DAYS),
                CashflowTransaction.operation_date
                <= on_date + timedelta(days=REFUND_TWIN_WINDOW_DAYS),
                DdsArticle.code == SUPPLIER_REFUND_ARTICLE_CODE,
                not_barter_money_return(),
                other_channel,
            )
            .order_by(CashflowTransaction.operation_date, CashflowTransaction.created_at)
        )
    ).all()
    return [
        RefundTwin(
            transaction_id=transaction.id,
            operation_date=transaction.operation_date,
            amount=money(transaction.amount),
            wallet_name=wallet_name,
            channel=wallet_channel(wallet_type),
            source_kind=transaction.source_kind,
        )
        for transaction, wallet_name, wallet_type in rows
    ]
