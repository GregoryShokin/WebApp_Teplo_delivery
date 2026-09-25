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

ЧТО СЧИТАЕМ ДВОЙНИКОМ. Возвратный платёж того же контрагента на ту же сумму в пределах
``REFUND_TWIN_WINDOW_DAYS`` дней, проведённый ДРУГИМ каналом: наличные против банка (платёж —
операция выписки целиком или отдельная проводка; либо все такие платежи окна вместе). Внутри
одного канала совпадение — не наша дыра: две операции выписки — это два реальных поступления,
а своя же проводка при переразметке иначе нашла бы саму себя. Исключённые проводки и
гашения бартерного займа деньгами (``not_barter_money_return``) дебиторку не гасят — и
двойниками не считаются.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
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
    """Один возвратный ПЛАТЁЖ другим каналом: операция выписки со всеми её возвратными долями
    этого контрагента или отдельная проводка."""

    transaction_id: uuid.UUID
    operation_date: date
    amount: Decimal
    wallet_name: str
    channel: RefundChannel
    source_kind: str


@dataclass(frozen=True)
class RefundTwins:
    items: list[RefundTwin]
    # Совпала не одна проводка, а СУММА возвратов другим каналом в окне (150 + 150 против 300).
    combined: bool = False


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
) -> RefundTwins:
    """Возвраты контрагента на ту же сумму рядом по дате, уже проведённые другим каналом.

    СРАВНИВАЕМ ПЛАТЕЖИ, А НЕ СТРОКИ. Выписку на 300 можно разнести двумя возвратными долями по
    150 — пересборка всё равно спишет аванс на 300, а построчное сравнение с наличными 300
    промолчало бы. Поэтому доли одной операции выписки складываются в один платёж, а окно
    разбора спрашивает суммой возвратных строк контрагента. Если отдельного платежа на эту сумму
    нет, сверяем ещё и сумму ВСЕХ возвратов другим каналом в окне: наличные 150 + 150 против
    выписки на 300 — та же ошибка, разнесённая на два приёма."""
    amount = money(amount)
    if amount <= 0:
        return RefundTwins(items=[])
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
    payments: dict[uuid.UUID, RefundTwin] = {}
    for transaction, wallet_name, wallet_type in rows:
        # Доли разбора одной операции выписки — один платёж: у всех source_id = операция.
        key = (
            transaction.source_id
            if transaction.source_kind == "bank_operation" and transaction.source_id is not None
            else transaction.id
        )
        known = payments.get(key)
        if known is None:
            payments[key] = RefundTwin(
                transaction_id=transaction.id,
                operation_date=transaction.operation_date,
                amount=money(transaction.amount),
                wallet_name=wallet_name,
                channel=wallet_channel(wallet_type),
                source_kind=transaction.source_kind,
            )
        else:
            payments[key] = replace(known, amount=known.amount + money(transaction.amount))
    same = [payment for payment in payments.values() if payment.amount == amount]
    if same:
        return RefundTwins(items=same)
    everything = list(payments.values())
    if len(everything) > 1 and sum((p.amount for p in everything), Decimal("0")) == amount:
        return RefundTwins(items=everything, combined=True)
    return RefundTwins(items=[])


# Правило классификации со статьёй возврата разносит выписку ФОНОМ — сторож выше спрашивает
# только человека в окне разбора, и авторазметка прошла бы мимо него. Воспроизведено 25.09:
# правило с возвратной статьёй провело следующую выписку само, и погашение аванса выросло
# 34 717,95 → 35 017,95 → 35 317,95. Решение владельца 25.09: сторож остаётся
# предупреждением, а правило с этой статьёй не заводится ни одной дверью.
REFUND_RULE_REFUSAL = (
    "Правило со статьёй «Возврат переплаты от поставщиков» не заводим: авторазметка гасила бы "
    "авансы поставщика фоном, мимо проверки на задвоенный возврат. Такие возвраты разбирайте "
    "вручную"
)


async def refund_rule_refusal(session: AsyncSession, article_id: uuid.UUID | None) -> str | None:
    """Текст отказа, если правило классификации ставило бы статью возврата переплаты."""
    if article_id is None:
        return None
    code = await session.scalar(select(DdsArticle.code).where(DdsArticle.id == article_id))
    return REFUND_RULE_REFUSAL if code == SUPPLIER_REFUND_ARTICLE_CODE else None
