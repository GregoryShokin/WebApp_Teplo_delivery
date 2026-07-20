"""Полный разбор РУЧНОЙ проводки ДДС (`CashflowTransaction` без операции выписки).

В отличие от банковской операции (её баланс идёт из выписки, а классификация — лишь ярлык
сверху), готовая ручная проводка САМА двигает баланс наличного кошелька (баланс = Σ проводок,
см. ``_wallet_movement_deltas``). Поэтому любое действие обязано СОХРАНЯТЬ баланс по всем
кошелькам, а не просто переклеить статус:

* ``split`` — одну строку заменяем на N строк той же суммой (кликнутую мутируем в первую,
  остальные добавляем). Σ = исходная сумма → баланс не меняется. Провенанс исходной строки
  (``source_id``) сохраняется — важно для идемпотентного импортёра сведения касс.
  ВНУТРЕННИЙ ПЕРЕВОД — это свойство СТРОКИ: если у строки статья «перевод между счетами» и
  указан счёт-получатель, то помимо строки заводится встречная нога (для наличного получателя;
  для банковского — нет, её принесёт выписка) + ``TransferGroup``. Так «перевод между счетами»
  живёт в том же разборе, что и обычные статьи (в т.ч. смешанный разбор перевод+расход).
* ``exclude`` — мягкое исключение (``quality_status='excluded'``): проводка уходит из ДДС и из
  баланса кошелька (``_wallet_movement_deltas`` фильтрует excluded). Обратимо — повторный
  разбор снова назначает статью.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, NamedTuple
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CashflowTransaction, DdsArticle, EmployeePayout, TransferGroup, Wallet
from app.services.banking.classifier import (
    EMPLOYEE_PAYOUT_ARTICLE_CODES,
    SUPPLIER_PAYMENT_ARTICLE_CODE,
    TRANSFER_IN_ARTICLE_CODE,
    TRANSFER_OUT_ARTICLE_CODE,
)

# Наличные (не банковские) кошельки не имеют выписки — их баланс = Σ cashflow-проводок; для
# них при переводе нужно дорисовать встречную ногу. Банковские получают in-ногу из выписки.
BANK_WALLET_TYPES = ("bank", "bank_account")

# Строки, производные ОТ ручной проводки: доли сплита и встречная нога перевода. По ним
# отличаем «пиров»-доли (трогать нельзя) от зависимой встречной ноги (снимаем при переразборе).
SPLIT_SOURCE_KIND = "manual_split"
TRANSFER_SOURCE_KIND = "manual_transfer"

EXCLUDED_QUALITY = "excluded"
MANUAL_QUALITY = "manual_override"
PAYROLL_PROTECTED_SOURCE_KINDS = frozenset({"payroll_payout", "payroll_bank_to_safe"})


class CashflowClassificationConflictError(ValueError):
    """Проводка принадлежит доменному контуру и не может правиться общим классификатором."""


def ensure_cashflow_reclassifiable(txn: CashflowTransaction) -> None:
    """Не дать общему ДДС-разбору рассинхронизировать зарплату и её проводки.

    Зарплатные расходы и транзит банк→Сейф создаются из ведомости. Их исключение/сплит
    меняет баланс, но не ``PayrollPayment.booked_amount`` и не резерв ведомости, поэтому
    исправлять такие строки можно только доменной корректировкой внутри ведомости.
    """
    if txn.source_kind in PAYROLL_PROTECTED_SOURCE_KINDS:
        raise CashflowClassificationConflictError(
            "Зарплатную проводку нельзя исправлять через разбор ДДС — "
            "скорректируйте выплату в ведомости"
        )


async def _transfer_article_ids(session: AsyncSession) -> tuple[UUID | None, UUID | None]:
    out_id = await session.scalar(
        select(DdsArticle.id).where(DdsArticle.code == TRANSFER_OUT_ARTICLE_CODE)
    )
    in_id = await session.scalar(
        select(DdsArticle.id).where(DdsArticle.code == TRANSFER_IN_ARTICLE_CODE)
    )
    return out_id, in_id


async def _clear_transfer_counter_leg(session: AsyncSession, txn: CashflowTransaction) -> None:
    """Снять встречную ногу, ранее заведённую переводом ЭТОЙ проводки (идемпотентность переразбора).

    Доли сплита (``manual_split``) — независимые части исходной суммы (пиры), их НЕ трогаем:
    удаление сломало бы баланс. Снимаем только зависимую встречную ногу перевода
    (``manual_transfer``), иначе повторный перевод задвоил бы приход получателю.
    """
    if txn.transfer_group_id is None:
        return
    counter_legs = await session.scalars(
        select(CashflowTransaction).where(
            CashflowTransaction.source_id == txn.id,
            CashflowTransaction.source_kind == TRANSFER_SOURCE_KIND,
        )
    )
    for leg in counter_legs.all():
        await session.delete(leg)
    txn.transfer_group_id = None
    await session.flush()


async def _book_transfer_counter_leg(
    session: AsyncSession,
    leg: CashflowTransaction,
    *,
    destination: Wallet,
    out_article: UUID | None,
    in_article: UUID | None,
) -> None:
    """Завести встречную ногу перевода для строки разбора со статьёй «перевод между счетами».

    Направление встречной ноги — противоположно направлению строки (out↔in). ``TransferGroup``
    связывает обе ноги. Наличному получателю нога нужна (нет выписки), банковскому — нет (его
    приход придёт из выписки, иначе задвоение).
    """
    this_is_out = leg.direction == "out"
    group = TransferGroup(
        amount=leg.amount,
        from_wallet_id=leg.wallet_id if this_is_out else destination.id,
        to_wallet_id=destination.id if this_is_out else leg.wallet_id,
        initiated_at=leg.operation_date,
        completed_at=leg.operation_date,
        status="matched",
    )
    session.add(group)
    await session.flush()
    leg.transfer_group_id = group.id
    if destination.type not in BANK_WALLET_TYPES:
        counter_leg = CashflowTransaction(
            wallet_id=destination.id,
            direction="in" if this_is_out else "out",
            amount=leg.amount,
            operation_date=leg.operation_date,
            article_id=in_article if this_is_out else out_article,
            source_kind=TRANSFER_SOURCE_KIND,
            source_id=leg.id,
            payment_purpose=leg.payment_purpose,
            transfer_group_id=group.id,
            quality_status=MANUAL_QUALITY,
        )
        session.add(counter_leg)
        await session.flush()


class CashflowSplitLine(NamedTuple):
    """Одна доля разбора ручной проводки ДДС.

    ``counterparty_id`` — контрагент ИМЕННО этой доли (один платёж часто покрывает расходы
    разных контрагентов). Пусто — берётся общий ``counterparty_id`` вызова.
    """

    article_id: UUID
    amount: Decimal
    comment: str | None = None
    transfer_wallet_id: UUID | None = None
    employee_id: UUID | None = None
    counterparty_id: UUID | None = None


async def apply_cashflow_split(
    session: AsyncSession,
    txn: CashflowTransaction,
    *,
    splits: list[CashflowSplitLine | tuple[Any, ...]],
    counterparty_id: UUID | None = None,
) -> list[UUID]:
    """Разнести ручную проводку по нескольким статьям ДДС, сохранив баланс.

    Каждая доля — ``CashflowSplitLine`` (голые кортежи той же формы тоже принимаются). Кликнутую
    строку мутируем в первую долю (её ``id``/``source_id``/``source_kind`` сохраняются — провенанс
    и идемпотентность импортёра), остальные доли добавляем новыми строками того же
    кошелька/направления/даты. Σ долей = сумма проводки, поэтому баланс не меняется.

    Контрагент — свойство доли: общий ``counterparty_id`` служит лишь дефолтом для долей, где он
    не задан.

    Доля со статьёй «перевод между счетами» и указанным ``transfer_wallet_id`` дополнительно
    заводит встречную ногу на счёт-получатель (наличный) + ``TransferGroup`` — тогда деньги не
    «теряются». Счёт-получатель допустим ТОЛЬКО у строки с транзитной статьёй.

    Доля с зарплатной статьёй (``EMPLOYEE_PAYOUT_ARTICLE_CODES``) и ``employee_id`` заводит
    ``EmployeePayout`` («выплачено») на эту долю — расчёт ЗП вычтет её из «к выдаче» (как и при
    разборе операции выписки). ``employee_id`` на не-зарплатной статье — ошибка.
    """
    ensure_cashflow_reclassifiable(txn)
    if not splits:
        raise ValueError("Нужна хотя бы одна статья")
    # Совместимость: доля — (article, amount, comment, transfer_wallet_id[, employee_id
    # [, counterparty_id]]). Старые кортежи дополняются дефолтами NamedTuple.
    splits = [
        line if isinstance(line, CashflowSplitLine) else CashflowSplitLine(*line) for line in splits
    ]
    salary_article_ids = set(
        (
            await session.scalars(
                select(DdsArticle.id).where(DdsArticle.code.in_(EMPLOYEE_PAYOUT_ARTICLE_CODES))
            )
        ).all()
    )
    for line in splits:
        if line.employee_id is not None and line.article_id not in salary_article_ids:
            raise ValueError("Сотрудника можно указать только для зарплатной статьи")

    # «Оплата поставщикам» без контрагента — расход в никуда: платёж не попадает в карточку и в
    # ДЗ/КЗ (правило 1 считает от контрагента проводки). Решение владельца 20.07.2026.
    supplier_payment_article_id = await session.scalar(
        select(DdsArticle.id).where(DdsArticle.code == SUPPLIER_PAYMENT_ARTICLE_CODE)
    )
    if supplier_payment_article_id is not None and any(
        line.article_id == supplier_payment_article_id
        and (line.counterparty_id or counterparty_id) is None
        for line in splits
    ):
        raise ValueError("Для статьи «Оплата поставщикам» укажите контрагента")

    original_amount = Decimal(txn.amount)
    total = sum((line.amount for line in splits), Decimal("0"))
    if total != original_amount:
        raise ValueError(f"Сумма по статьям ({total}) не равна сумме проводки ({original_amount})")

    out_article, in_article = await _transfer_article_ids(session)
    transfer_article_ids = {a for a in (out_article, in_article) if a is not None}
    destinations: dict[UUID, Wallet] = {}
    for line in splits:
        article_id, transfer_wallet_id = line.article_id, line.transfer_wallet_id
        if await session.get(DdsArticle, article_id) is None:
            raise ValueError("Статья не найдена")
        if transfer_wallet_id is None:
            continue
        if article_id not in transfer_article_ids:
            raise ValueError(
                "Счёт-получатель можно указать только для строки «перевод между счетами»"
            )
        if transfer_wallet_id == txn.wallet_id:
            raise ValueError("Счёт-получатель должен отличаться от счёта проводки")
        destination = await session.get(Wallet, transfer_wallet_id)
        if destination is None:
            raise ValueError("Счёт-получатель не найден")
        destinations[transfer_wallet_id] = destination

    # Переразбор: снять прежнюю встречную ногу этой проводки (была переводом → стала расходом).
    await _clear_transfer_counter_leg(session, txn)

    # Переразбор: снять EmployeePayout, заведённые прежним разбором ЭТОЙ проводки (сама строка +
    # её доли manual_split). Уже зачтённую в проведённой ведомости выплату не трогаем — блокируем.
    prior_split_ids = select(CashflowTransaction.id).where(
        CashflowTransaction.source_kind == SPLIT_SOURCE_KIND,
        CashflowTransaction.source_id == txn.id,
    )
    prior_payouts = (
        await session.scalars(
            select(EmployeePayout).where(
                or_(
                    EmployeePayout.cashflow_transaction_id == txn.id,
                    EmployeePayout.cashflow_transaction_id.in_(prior_split_ids),
                )
            )
        )
    ).all()
    for payout in prior_payouts:
        if Decimal(payout.offset_amount) > 0:
            raise ValueError(
                "Проводка уже зачтена в проведённой ведомости сотрудника — "
                "сначала откатите ведомость (дефинализация)"
            )
    for payout in prior_payouts:
        await session.delete(payout)
    if prior_payouts:
        await session.flush()

    created: list[UUID] = []
    for index, line in enumerate(splits):
        article_id, amount, employee_id = line.article_id, line.amount, line.employee_id
        transfer_wallet_id = line.transfer_wallet_id
        line_counterparty_id = line.counterparty_id or counterparty_id
        if index == 0:
            txn.article_id = article_id
            txn.amount = amount
            txn.comment = line.comment
            txn.counterparty_id = line_counterparty_id
            txn.quality_status = MANUAL_QUALITY
            leg = txn
        else:
            leg = CashflowTransaction(
                wallet_id=txn.wallet_id,
                direction=txn.direction,
                amount=amount,
                operation_date=txn.operation_date,
                article_id=article_id,
                counterparty_id=line_counterparty_id,
                source_kind=SPLIT_SOURCE_KIND,
                source_id=txn.id,
                payment_purpose=txn.payment_purpose,
                comment=line.comment,
                quality_status=MANUAL_QUALITY,
            )
            session.add(leg)
            await session.flush()
        created.append(leg.id)
        if transfer_wallet_id is not None:
            await _book_transfer_counter_leg(
                session,
                leg,
                destination=destinations[transfer_wallet_id],
                out_article=out_article,
                in_article=in_article,
            )
        # Зарплатная статья + сотрудник → леджер «выплачено» (EmployeePayout) на эту долю.
        # Денежный факт несёт сама проводка (баланс = Σ проводок), поэтому второй проводки нет.
        if employee_id is not None and article_id in salary_article_ids:
            from app.services.payroll_admin import resolve_employee_payout_kind

            payout_kind = await resolve_employee_payout_kind(
                session, employee_id, as_of=txn.operation_date
            )
            session.add(
                EmployeePayout(
                    employee_id=employee_id,
                    kind=payout_kind,
                    amount=amount,
                    payout_date=txn.operation_date,
                    wallet_id=leg.wallet_id,
                    article_id=article_id,
                    cashflow_transaction_id=leg.id,
                    status="paid",
                )
            )
            await session.flush()
    return created


async def apply_cashflow_exclude(session: AsyncSession, txn: CashflowTransaction) -> list[UUID]:
    """Мягко исключить ручную проводку из ДДС и из баланса кошелька (обратимо).

    ``quality_status='excluded'`` убирает проводку из витрины баланса (фильтр в
    ``_wallet_movement_deltas``) — баланс кошелька изменится на её сумму. Повторный разбор
    (split) снова назначит статью и вернёт проводку в баланс.
    """
    ensure_cashflow_reclassifiable(txn)
    await _clear_transfer_counter_leg(session, txn)
    txn.quality_status = EXCLUDED_QUALITY
    return [txn.id]
