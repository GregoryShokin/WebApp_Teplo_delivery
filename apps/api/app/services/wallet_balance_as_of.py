"""Остаток денег на произвольную дату — единственный расчёт денежного остатка в проекте.

ЗАЧЕМ ОТДЕЛЬНЫЙ МОДУЛЬ. Формула жила в модуле роутов (``routes/dds.py``), и оттого её нельзя
было переиспользовать, не импортируя роут: каждый следующий потребитель писал свою копию.
Копий стало четыре — витрина ДДС, касса, выплаты ведомости и расчёт платёжеспособности, — и
одна из них уже разошлась по смыслу с остальными (считала только активные кошельки). Балансу
на дату нужна одна функция, а не четыре похожие.

ЧЕМ ОТСЕКАЕМ ПЕРИОД. Даты у двух контуров разные, и это не небрежность:

* банковский кошелёк — ``BankOperation.operation_date``. Остаток банка ПО КОНСТРУКЦИИ есть
  выписка, а датой операции банк называет именно её. ``posted_at`` не годится: он nullable и у
  Сбера часто пуст. Дата проводки ДДС не годится принципиально — у части операций деньги несёт
  prebooked-проводка чужого контура, и её дату ставит домен в момент отправки в банк, а не банк
  в момент списания.
* наличный кошелёк — ``CashflowTransaction.operation_date``. Другого носителя даты у наличных
  нет: выписки не существует, остаток собирается из проводок, заведённых руками.

``as_of=None`` означает «без верхней границы», а НЕ «сегодня». Разница видна на проводке,
датированной вперёд: «сегодня» её отрежет, «без границы» — нет. Прежнее поведение всех
потребителей — именно «без границы», и подмена его на «сегодня» тихо сдвинула бы живые цифры.

ПОЧЕМУ НИЖНЯЯ ГРАНИЦА СТРОГАЯ. ``opening_balance`` — снимок на ``opening_balance_date``, он уже
включает всё по этот день включительно. Поэтому движения считаются в интервале
(opening_balance_date, as_of]: иначе день опорной точки посчитался бы дважды.

ЧЕГО ЭТОТ РАСЧЁТ НЕ ЗНАЕТ. Дата у исключения операции не хранится: ``classification_status``
меняется на месте, и операция, исключённая сегодня, исчезает из остатка любого прошлого месяца
задним числом. Пока это лечится только замком закрытого месяца, а не расчётом.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Account, BankOperation, CashflowTransaction, Wallet
from app.services.banking.cashflow_classify import EXCLUDED_QUALITY

# Типы кошельков, чей остаток берётся из банковской выписки, а не из проводок.
BANK_TYPES = ("bank", "bank_account")

# Статус операции выписки, исключённой из учёта вручную.
EXCLUDED_OPERATION = "excluded"


def _money(value: object) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"))


@dataclass(frozen=True)
class WalletBalanceAsOf:
    """Остаток одного кошелька на дату вместе с тем, из чего он сложился."""

    wallet_id: uuid.UUID
    code: str
    name: str
    type: str
    status: str
    is_bank: bool
    opening_balance: Decimal
    opening_balance_date: date | None
    movement: Decimal
    balance: Decimal
    # ``as_of`` раньше опорной точки. Остаток в этом случае равен входящему — и это ЛОЖЬ:
    # движений до опорной точки в системе нет вовсе, они свёрнуты в неё. Балансовый снимок
    # обязан отказаться от такой цифры, а не показать её уверенно.
    before_opening: bool


@dataclass(frozen=True)
class MoneyBalanceAsOf:
    """Денежный блок баланса: строки по кошелькам плюс признаки неполноты."""

    as_of: date | None
    rows: list[WalletBalanceAsOf]
    # Движение по счетам, за которыми не закреплён ни один кошелёк: деньги в банке есть,
    # в балансе их нет. Не ошибка расчёта, а дыра в справочнике — и её надо видеть.
    unmapped_bank_movement: Decimal
    # Операции выписки без счёта вовсе — их не отнести даже к чужому кошельку.
    orphan_bank_operations: int

    @property
    def total(self) -> Decimal:
        return _money(sum((row.balance for row in self.rows), Decimal("0.00")))


async def wallet_movement_deltas(
    session: AsyncSession, *, as_of: date | None = None
) -> dict[uuid.UUID, Decimal]:
    """Чистое движение денег по каждому кошельку — по ФАКТУ движения, а не по разметке.

    Баланс — это банковская реальность, а не классификация ДДС:

    * банковские кошельки идут за ВЫПИСКОЙ. Каждая импортированная операция двигает остаток
      своим направлением и суммой в момент появления, независимо от того, разнесена она по
      статье или ещё нет: разметка решает, в какой статье движение показать, но никогда не
      решает, было ли оно. Из остатка выпадают только явно исключённые операции. Внутренние
      переводы отдельной обработки не требуют — каждая нога приходит своей строкой выписки на
      своём счёте. Собственный баланс банка (Т-Банк ``otb``) не используется: он раздут
      лимитом овердрафта;
    * у наличных кошельков выписки нет, поэтому их остаток собирается из проводок ДДС. Мягко
      исключённые (ручной разбор) выпадают так же, как ``excluded`` у банка.
    """
    deltas: dict[uuid.UUID, Decimal] = defaultdict(lambda: Decimal("0"))

    after_opening_bank = or_(
        Wallet.opening_balance_date.is_(None),
        BankOperation.operation_date > Wallet.opening_balance_date,
    )
    bank_query = (
        select(
            Wallet.id,
            BankOperation.direction,
            func.coalesce(func.sum(BankOperation.amount), 0),
        )
        .join(Account, Account.id == Wallet.account_id)
        .join(BankOperation, BankOperation.account_id == Account.id)
        .where(
            Wallet.type.in_(BANK_TYPES),
            BankOperation.classification_status != EXCLUDED_OPERATION,
            after_opening_bank,
        )
        .group_by(Wallet.id, BankOperation.direction)
    )
    if as_of is not None:
        bank_query = bank_query.where(BankOperation.operation_date <= as_of)

    for wallet_id, direction, total in await session.execute(bank_query):
        amount = Decimal(total)
        deltas[wallet_id] += amount if direction == "in" else -amount

    after_opening_cash = or_(
        Wallet.opening_balance_date.is_(None),
        CashflowTransaction.operation_date > Wallet.opening_balance_date,
    )
    cash_query = (
        select(
            CashflowTransaction.wallet_id,
            CashflowTransaction.direction,
            func.coalesce(func.sum(CashflowTransaction.amount), 0),
        )
        .join(Wallet, Wallet.id == CashflowTransaction.wallet_id)
        .where(
            Wallet.type.not_in(BANK_TYPES),
            CashflowTransaction.quality_status != EXCLUDED_QUALITY,
            after_opening_cash,
        )
        .group_by(CashflowTransaction.wallet_id, CashflowTransaction.direction)
    )
    if as_of is not None:
        cash_query = cash_query.where(CashflowTransaction.operation_date <= as_of)

    for wallet_id, direction, total in await session.execute(cash_query):
        amount = Decimal(total)
        deltas[wallet_id] += amount if direction == "in" else -amount

    return deltas


async def wallet_balance_as_of(
    session: AsyncSession, wallet: Wallet, *, as_of: date | None = None
) -> Decimal:
    """Остаток одного кошелька: входящий плюс движение по дату включительно."""
    if wallet.type in BANK_TYPES and wallet.account_id is None:
        raise ValueError(
            f"Кошелёк «{wallet.name}» банковский, но за ним не закреплён счёт — "
            f"остаток по выписке считать не из чего"
        )
    deltas = await wallet_movement_deltas(session, as_of=as_of)
    return _money(Decimal(str(wallet.opening_balance or 0)) + deltas.get(wallet.id, Decimal("0")))


async def build_money_balance_as_of(
    session: AsyncSession, *, as_of: date | None = None, include_inactive: bool = True
) -> MoneyBalanceAsOf:
    """Денежный блок целиком: строка на кошелёк, итог и признаки неполноты.

    ``include_inactive`` разводит два разных вопроса, которые раньше решались одной формулой.
    Балансу нужны ВСЕ кошельки: счёт, закрытый в сентябре, 31 августа деньги ещё держал.
    Расчёту платёжеспособности — только активные: платить с закрытого счёта нельзя.
    """
    deltas = await wallet_movement_deltas(session, as_of=as_of)

    query = select(Wallet).order_by(Wallet.code)
    if not include_inactive:
        query = query.where(Wallet.status == "active")
    wallets = (await session.scalars(query)).all()

    rows = [
        WalletBalanceAsOf(
            wallet_id=wallet.id,
            code=wallet.code,
            name=wallet.name,
            type=wallet.type,
            status=wallet.status,
            is_bank=wallet.type in BANK_TYPES,
            opening_balance=_money(wallet.opening_balance),
            opening_balance_date=wallet.opening_balance_date,
            movement=_money(deltas.get(wallet.id, Decimal("0"))),
            balance=_money(
                Decimal(str(wallet.opening_balance or 0)) + deltas.get(wallet.id, Decimal("0"))
            ),
            before_opening=(
                as_of is not None
                and wallet.opening_balance_date is not None
                and as_of < wallet.opening_balance_date
            ),
        )
        for wallet in wallets
    ]

    return MoneyBalanceAsOf(
        as_of=as_of,
        rows=rows,
        unmapped_bank_movement=await _unmapped_bank_movement(session, as_of=as_of),
        orphan_bank_operations=await _orphan_bank_operations(session, as_of=as_of),
    )


async def _unmapped_bank_movement(session: AsyncSession, *, as_of: date | None) -> Decimal:
    """Движение по счетам, за которыми не закреплён кошелёк.

    Деньги в банке есть, а в балансе их нет: справочник кошельков не покрывает счёт. Молчать
    об этом нельзя — расхождение уедет в капитал и там растворится.
    """
    linked = select(Wallet.account_id).where(Wallet.account_id.is_not(None))
    query = (
        select(BankOperation.direction, func.coalesce(func.sum(BankOperation.amount), 0))
        .where(
            BankOperation.classification_status != EXCLUDED_OPERATION,
            BankOperation.account_id.is_not(None),
            BankOperation.account_id.not_in(linked),
        )
        .group_by(BankOperation.direction)
    )
    if as_of is not None:
        query = query.where(BankOperation.operation_date <= as_of)

    total = Decimal("0")
    for direction, amount in await session.execute(query):
        total += Decimal(amount) if direction == "in" else -Decimal(amount)
    return _money(total)


async def _orphan_bank_operations(session: AsyncSession, *, as_of: date | None) -> int:
    """Операции выписки без счёта: их не отнести даже к чужому кошельку."""
    query = (
        select(func.count()).select_from(BankOperation).where(BankOperation.account_id.is_(None))
    )
    if as_of is not None:
        query = query.where(BankOperation.operation_date <= as_of)
    return int(await session.scalar(query) or 0)
