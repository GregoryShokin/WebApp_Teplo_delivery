from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    CashflowTransaction,
    DdsArticle,
    PayrollLine,
    PayrollPayment,
    PayrollPayoutBooking,
    PayrollRun,
    PayrollRunEvent,
)
from app.services.payroll_runner import PayrollConflictError, PayrollNotFoundError, money_text

PAYROLL_PAYMENT_METHODS = frozenset({"business_card", "cash", "transfer", "other"})


async def _payroll_cash_wallet_id(session: AsyncSession, code: str) -> uuid.UUID:
    from app.services.payroll_payouts import PAYROLL_CASH_WALLET_CODES
    from app.services.wallets import CashWalletError, resolve_cash_wallet

    if code not in PAYROLL_CASH_WALLET_CODES:
        raise PayrollConflictError("Для выплаты можно выбрать только Сейф или торговую кассу")
    try:
        return (await resolve_cash_wallet(session, code)).id
    except CashWalletError as exc:
        raise PayrollConflictError(str(exc)) from exc


async def mark_payment(
    session: AsyncSession,
    run_id: uuid.UUID,
    employee_id: uuid.UUID,
    *,
    paid_at: date,
    method: str,
    actor_user_id: uuid.UUID | None,
    cash_wallet_code: str = "cash_safe",
) -> PayrollPayment:
    run = await _get_payment_run(session, run_id)
    _validate_method(method)
    pay_wallet_id = await _payroll_cash_wallet_id(session, cash_wallet_code)
    amount = await _employee_payable_amount(session, run_id, employee_id)

    payment = await session.scalar(
        select(PayrollPayment).where(
            PayrollPayment.run_id == run_id,
            PayrollPayment.employee_id == employee_id,
        )
    )
    if payment is None:
        payment = PayrollPayment(
            id=uuid.uuid4(),
            run_id=run_id,
            employee_id=employee_id,
            amount=amount,
            paid_at=paid_at,
            method=method,
            paid_by_user_id=actor_user_id,
            status="paid",
            **_initial_split_for_method(amount, method),
        )
        session.add(payment)
    else:
        payment.amount = amount
        _reconcile_split_for_paid(payment, amount, method)
        payment.paid_at = paid_at
        payment.method = method
        payment.paid_by_user_id = actor_user_id
        payment.status = "paid"
    await session.flush()
    from app.services.payroll_payouts import book_payout_expense_for_employees

    payout_result = await book_payout_expense_for_employees(
        session, run, [employee_id], pay_wallet_id=pay_wallet_id
    )
    await _reconcile_pool_reserves(session, run_id)
    _add_payment_event(
        session,
        run=run,
        action="payment_marked",
        actor_user_id=actor_user_id,
        payload={
            "employee_id": str(employee_id),
            "amount": money_text(amount),
            "method": method,
            "paid_at": paid_at.isoformat(),
            "cash_wallet_code": cash_wallet_code,
        },
    )
    await session.commit()
    await session.refresh(payment)
    _post_deposit_payout_iiko(payout_result, run, paid_at)
    return payment


async def unmark_payment(
    session: AsyncSession,
    run_id: uuid.UUID,
    employee_id: uuid.UUID,
    *,
    actor_user_id: uuid.UUID | None,
) -> None:
    run = await _get_payment_run(session, run_id)
    payment = await session.scalar(
        select(PayrollPayment).where(
            PayrollPayment.run_id == run_id,
            PayrollPayment.employee_id == employee_id,
        )
    )
    if payment is None or payment.status != "paid":
        raise PayrollNotFoundError("Payroll payment not found")

    bookings = (
        await session.scalars(
            select(PayrollPayoutBooking).where(
                PayrollPayoutBooking.payment_id == payment.id,
                PayrollPayoutBooking.reversal_transaction_id.is_(None),
            )
        )
    ).all()
    if Decimal(payment.booked_amount or 0) > 0 and not bookings:
        bookings = await _link_legacy_payout(session, run, payment)

    reversal_ids: list[str] = []
    for booking in bookings:
        original = await session.get(CashflowTransaction, booking.cashflow_transaction_id)
        if original is None:
            raise PayrollConflictError("Не найдена исходная проводка выплаты в ДДС")
        reversal = CashflowTransaction(
            wallet_id=original.wallet_id,
            direction="in",
            amount=booking.amount,
            operation_date=datetime.now(UTC).date(),
            article_id=original.article_id,
            source_kind=original.source_kind,
            source_id=run.id,
            payment_purpose="Финансовый откат выплаты зарплаты",
            comment=f"Отмена отметки выплаты сотруднику {employee_id}",
            quality_status="final",
            created_by_user_id=actor_user_id,
        )
        session.add(reversal)
        await session.flush()
        booking.reversal_transaction_id = reversal.id
        booking.reversed_at = datetime.now(UTC)
        reversal_ids.append(str(reversal.id))

    await session.execute(delete(PayrollPayment).where(PayrollPayment.id == payment.id))
    await _reconcile_pool_reserves(session, run_id)
    _add_payment_event(
        session,
        run=run,
        action="payment_unmarked",
        actor_user_id=actor_user_id,
        payload={
            "employee_id": str(employee_id),
            "amount": money_text(payment.amount),
            "method": payment.method,
            "paid_at": payment.paid_at.isoformat(),
            "reversal_transaction_ids": reversal_ids,
        },
    )
    await session.commit()


async def _link_legacy_payout(
    session: AsyncSession,
    run: PayrollRun,
    payment: PayrollPayment,
) -> list[PayrollPayoutBooking]:
    """Восстановить связи для расходов, созданных до появления payout-booking.

    Старый bulk объединял сотрудников по статье в одну проводку. Долю конкретного сотрудника
    можно однозначно восстановить из строк ведомости; занятая другими сотрудниками часть
    общей проводки учитывается через уже созданные booking-записи.
    """
    from app.services.payroll_payout_allocation import (
        DDS_ARTICLE_ADMIN_PAYROLL,
        DDS_ARTICLE_PRODUCTION_PAYROLL,
        build_payout_buckets,
    )
    from app.services.payroll_payouts import DDS_ARTICLE_DEPOSIT_PAYOUT

    lines = (
        await session.scalars(
            select(PayrollLine).where(
                PayrollLine.run_id == run.id,
                PayrollLine.employee_id == payment.employee_id,
            )
        )
    ).all()
    if not lines:
        raise PayrollConflictError("Не найдены строки сотрудника в ведомости")
    target = Decimal(payment.booked_amount or 0).quantize(Decimal("0.01"))
    distributed = _distribute_legacy_amount(target, lines)
    is_admin = isinstance(run.summary, dict) and run.summary.get("kind") == "admin"
    default_article = DDS_ARTICLE_ADMIN_PAYROLL if is_admin else DDS_ARTICLE_PRODUCTION_PAYROLL
    buckets = build_payout_buckets(distributed, default_article_code=default_article)
    accrued = sum((Decimal(line.total_payable or 0) for line in lines), Decimal("0"))
    if Decimal(payment.amount or 0) >= accrued:
        deposit = sum(
            (Decimal(getattr(line, "deposit_payout_scheduled", 0) or 0) for line in lines),
            Decimal("0"),
        )
        if deposit > 0:
            from app.services.payroll_payout_allocation import PayoutBucket

            buckets.append(PayoutBucket(DDS_ARTICLE_DEPOSIT_PAYOUT, deposit))

    codes = {bucket.article_code for bucket in buckets}
    articles = dict(
        (
            await session.execute(
                select(DdsArticle.code, DdsArticle.id).where(DdsArticle.code.in_(codes))
            )
        ).all()
    )
    created_at = payment.created_at or datetime.now(UTC)
    candidates = (
        await session.scalars(
            select(CashflowTransaction)
            .where(
                CashflowTransaction.source_kind == "payroll_payout",
                CashflowTransaction.source_id == run.id,
                CashflowTransaction.direction == "out",
                CashflowTransaction.quality_status != "excluded",
                CashflowTransaction.created_at >= created_at - timedelta(minutes=5),
                CashflowTransaction.created_at <= created_at + timedelta(minutes=15),
            )
            .order_by(CashflowTransaction.created_at, CashflowTransaction.id)
        )
    ).all()
    linked_rows = (
        (
            await session.execute(
                select(
                    PayrollPayoutBooking.cashflow_transaction_id,
                    func.coalesce(func.sum(PayrollPayoutBooking.amount), 0),
                )
                .where(
                    PayrollPayoutBooking.cashflow_transaction_id.in_([row.id for row in candidates])
                )
                .group_by(PayrollPayoutBooking.cashflow_transaction_id)
            )
        ).all()
        if candidates
        else []
    )
    linked = {transaction_id: Decimal(amount) for transaction_id, amount in linked_rows}
    result: list[PayrollPayoutBooking] = []
    for bucket in buckets:
        remaining = Decimal(bucket.total)
        article_id = articles.get(bucket.article_code)
        for transaction in candidates:
            if transaction.article_id != article_id or remaining <= 0:
                continue
            capacity = Decimal(transaction.amount) - linked.get(transaction.id, Decimal("0"))
            if capacity <= 0:
                continue
            share = min(remaining, capacity)
            booking = PayrollPayoutBooking(
                run_id=run.id,
                employee_id=payment.employee_id,
                payment_id=payment.id,
                cashflow_transaction_id=transaction.id,
                amount=share,
            )
            session.add(booking)
            result.append(booking)
            linked[transaction.id] = linked.get(transaction.id, Decimal("0")) + share
            remaining -= share
        if remaining > 0:
            raise PayrollConflictError(
                "Не удалось сопоставить старую выплату с проводкой ДДС; откат не выполнен"
            )
    await session.flush()
    return result


def _distribute_legacy_amount(
    amount: Decimal, lines: list[PayrollLine]
) -> list[tuple[str, Decimal]]:
    if len(lines) == 1:
        return [(lines[0].role, amount)]
    total = sum((Decimal(line.total_payable or 0) for line in lines), Decimal("0"))
    if total <= 0:
        return [(lines[0].role, amount)]
    result: list[tuple[str, Decimal]] = []
    allocated = Decimal("0")
    for index, line in enumerate(lines):
        share = (
            amount - allocated
            if index == len(lines) - 1
            else (Decimal(line.total_payable or 0) / total * amount).quantize(Decimal("0.01"))
        )
        allocated += share
        if share > 0:
            result.append((line.role, share))
    return result


async def mark_all_payments(
    session: AsyncSession,
    run_id: uuid.UUID,
    *,
    paid_at: date,
    method: str,
    actor_user_id: uuid.UUID | None,
    cash_wallet_code: str = "cash_safe",
) -> int:
    run = await _get_payment_run(session, run_id)
    _validate_method(method)
    pay_wallet_id = await _payroll_cash_wallet_id(session, cash_wallet_code)
    rows = await _unpaid_employee_payment_rows(session, run_id)
    if not rows:
        return 0

    for employee_id, amount, payment in rows:
        if payment is None:
            session.add(
                PayrollPayment(
                    id=uuid.uuid4(),
                    run_id=run_id,
                    employee_id=employee_id,
                    amount=amount,
                    paid_at=paid_at,
                    method=method,
                    paid_by_user_id=actor_user_id,
                    status="paid",
                    **_initial_split_for_method(amount, method),
                )
            )
            continue
        payment.amount = amount
        _reconcile_split_for_paid(payment, amount, method)
        payment.paid_at = paid_at
        payment.method = method
        payment.paid_by_user_id = actor_user_id
        payment.status = "paid"

    # Расход ЗП + выдача депозита в ДДС из Сейфа по статьям — по ВНОВЬ выплаченным сотрудникам.
    from app.services.payroll_payouts import book_payout_expense_for_employees

    payout_result = await book_payout_expense_for_employees(
        session,
        run,
        [employee_id for employee_id, _amount, _payment in rows],
        pay_wallet_id=pay_wallet_id,
    )
    await _reconcile_pool_reserves(session, run_id)
    _add_payment_event(
        session,
        run=run,
        action="payment_marked",
        actor_user_id=actor_user_id,
        payload={
            "employee_ids": [str(employee_id) for employee_id, _amount, _payment in rows],
            "count": len(rows),
            "amount_total": money_text(
                sum((amount for _employee_id, amount, _payment in rows), Decimal("0"))
            ),
            "method": method,
            "paid_at": paid_at.isoformat(),
            "cash_wallet_code": cash_wallet_code,
        },
    )
    await session.commit()
    _post_deposit_payout_iiko(payout_result, run, paid_at)
    return len(rows)


async def mark_payments_selected(
    session: AsyncSession,
    run_id: uuid.UUID,
    employee_ids: list[uuid.UUID],
    *,
    paid_at: date,
    actor_user_id: uuid.UUID | None,
    cash_wallet_code: str = "cash_safe",
) -> int:
    """Mark the given employees as paid in one shot, without a payment method.

    Used by the bulk "Выплатить" action: the owner only confirms that the
    selected employees received their pay; the channel (cash/card/transfer) is
    not recorded.
    """
    run = await _get_payment_run(session, run_id)
    pay_wallet_id = await _payroll_cash_wallet_id(session, cash_wallet_code)
    wanted = set(employee_ids)
    if not wanted:
        return 0
    rows = [row for row in await _unpaid_employee_payment_rows(session, run_id) if row[0] in wanted]
    if not rows:
        return 0

    for employee_id, amount, payment in rows:
        if payment is None:
            session.add(
                PayrollPayment(
                    id=uuid.uuid4(),
                    run_id=run_id,
                    employee_id=employee_id,
                    amount=amount,
                    paid_at=paid_at,
                    method=None,
                    paid_by_user_id=actor_user_id,
                    status="paid",
                    **_initial_split_for_method(amount, None),
                )
            )
            continue
        payment.amount = amount
        _reconcile_split_for_paid(payment, amount, None)
        payment.paid_at = paid_at
        payment.method = None
        payment.paid_by_user_id = actor_user_id
        payment.status = "paid"

    # Расход ЗП + выдача депозита в ДДС из Сейфа по статьям — по ВНОВЬ выплаченным сотрудникам.
    from app.services.payroll_payouts import book_payout_expense_for_employees

    payout_result = await book_payout_expense_for_employees(
        session,
        run,
        [employee_id for employee_id, _amount, _payment in rows],
        pay_wallet_id=pay_wallet_id,
    )
    await _reconcile_pool_reserves(session, run_id)
    _add_payment_event(
        session,
        run=run,
        action="payment_marked",
        actor_user_id=actor_user_id,
        payload={
            "employee_ids": [str(employee_id) for employee_id, _amount, _payment in rows],
            "count": len(rows),
            "amount_total": money_text(
                sum((amount for _employee_id, amount, _payment in rows), Decimal("0"))
            ),
            "method": None,
            "paid_at": paid_at.isoformat(),
            "cash_wallet_code": cash_wallet_code,
        },
    )
    await session.commit()
    _post_deposit_payout_iiko(payout_result, run, paid_at)
    return len(rows)


async def mark_partial_payment(
    session: AsyncSession,
    run_id: uuid.UUID,
    employee_id: uuid.UUID,
    *,
    amount: Decimal | None,
    paid_at: date,
    method: str | None = None,
    comment: str | None = None,
    actor_user_id: uuid.UUID | None,
    cash_wallet_code: str = "cash_safe",
) -> PayrollPayment:
    """Выплатить сотруднику ЧАСТЬ начисленного (или доплатить остаток) — снятие атомарности.

    ``amount`` — сумма транша (None = весь остаток). ``PayrollPayment.amount`` — бегущий итог
    выплаченного, статус ``paid`` (закрыт) / ``partially_paid`` (остаток > 0). Транш книжится в
    ДДС инкрементально (Сейф-модель, ``book_payout_expense_for_employees`` по дельте).
    """
    run = await _get_payment_run(session, run_id)
    pay_wallet_id = await _payroll_cash_wallet_id(session, cash_wallet_code)
    if method is not None:
        _validate_method(method)
    accrued = await _employee_payable_amount(session, run_id, employee_id)
    # Депозит книжится (ДДС «Выдача депозита» + iiko-изъятие) только на ПОЛНОМ пути «Выплатить».
    # Частичная выплата закрывает сотрудника по ФОТ (accrued=Σtotal_payable, без депозита) и
    # исключает его из bulk — тогда выдача депозита потерялась бы. Гейтим такого сотрудника.
    scheduled_deposit = await session.scalar(
        select(func.coalesce(func.sum(PayrollLine.deposit_payout_scheduled), 0)).where(
            PayrollLine.run_id == run_id,
            PayrollLine.employee_id == employee_id,
        )
    )
    if scheduled_deposit and Decimal(scheduled_deposit) > 0:
        raise PayrollConflictError(
            "У сотрудника запланирована выдача депозита — выплатите полностью через «Выплатить»"
        )
    payment = await session.scalar(
        select(PayrollPayment).where(
            PayrollPayment.run_id == run_id,
            PayrollPayment.employee_id == employee_id,
        )
    )
    already = Decimal(payment.amount) if payment is not None else Decimal("0")
    remaining = accrued - already
    if remaining <= 0:
        raise PayrollConflictError("Сотруднику уже выплачена вся сумма")
    tranche = (remaining if amount is None else Decimal(amount)).quantize(Decimal("0.01"))
    if tranche <= 0:
        raise PayrollConflictError("Сумма выплаты должна быть больше нуля")
    if tranche > remaining:
        raise PayrollConflictError("Сумма превышает остаток к выплате")
    new_total = already + tranche
    status = "paid" if new_total >= accrued else "partially_paid"
    if payment is None:
        payment = PayrollPayment(
            id=uuid.uuid4(),
            run_id=run_id,
            employee_id=employee_id,
            amount=new_total,
            booked_amount=Decimal("0"),
            paid_at=paid_at,
            method=method,
            comment=comment,
            paid_by_user_id=actor_user_id,
            status=status,
            **_initial_split_for_method(new_total, method),
        )
        session.add(payment)
    else:
        payment.amount = new_total
        _reconcile_split_for_paid(payment, new_total, method)
        payment.paid_at = paid_at
        payment.method = method
        if comment is not None:
            payment.comment = comment
        payment.paid_by_user_id = actor_user_id
        payment.status = status
    await session.flush()

    from app.services.payroll_payouts import book_payout_expense_for_employees

    payout_result = await book_payout_expense_for_employees(
        session,
        run,
        [employee_id],
        amount_by_employee={employee_id: new_total},
        pay_wallet_id=pay_wallet_id,
    )
    await _reconcile_pool_reserves(session, run_id)
    _add_payment_event(
        session,
        run=run,
        action="payment_marked",
        actor_user_id=actor_user_id,
        payload={
            "employee_id": str(employee_id),
            "amount": money_text(tranche),
            "amount_total": money_text(new_total),
            "accrued": money_text(accrued),
            "partial": status == "partially_paid",
            "method": method,
            "paid_at": paid_at.isoformat(),
            "cash_wallet_code": cash_wallet_code,
        },
    )
    await session.commit()
    await session.refresh(payment)
    _post_deposit_payout_iiko(payout_result, run, paid_at)
    return payment


async def apply_pool_tranche(
    session: AsyncSession,
    run: PayrollRun,
    employee_id: uuid.UUID,
    *,
    tranche: Decimal,
    pay_wallet_id: uuid.UUID,
    is_cash: bool,
    paid_at: date,
    actor_user_id: uuid.UUID | None,
) -> Decimal:
    """Провести ОДИН транш выплаты ЗП сотруднику из пула-резерва — БЕЗ commit.

    Отличия от ``mark_partial_payment``: (1) кошелёк выплаты явный (``pay_wallet_id`` —
    Сейф ЛИБО касса, без run-level каскада нал/безнал); (2) не коммитит — пул проводится
    атомарно оркестратором ``payroll_reserves.pay_run_from_pool``; (3) не пишет событие
    (оркестратор пишет одно сводное); (4) возвращает забронированную дельту — на неё
    оркестратор наращивает ``amount_paid`` резерва.

    Депозит-гейт и учёт ``booked_amount`` (защита от задвоения ДДС) — те же, что в частичной
    выплате. ``tranche`` приходит из ``allocate_pool`` (уже ≤ остатка); дополнительно клампится
    остатком на случай гонки. Возвращает 0, если проводить нечего.
    """
    scheduled_deposit = await session.scalar(
        select(func.coalesce(func.sum(PayrollLine.deposit_payout_scheduled), 0)).where(
            PayrollLine.run_id == run.id,
            PayrollLine.employee_id == employee_id,
        )
    )
    if scheduled_deposit and Decimal(scheduled_deposit) > 0:
        raise PayrollConflictError(
            "У сотрудника запланирована выдача депозита — выплатите полностью через «Выплатить»"
        )
    accrued = await _employee_payable_amount(session, run.id, employee_id)
    payment = await session.scalar(
        select(PayrollPayment).where(
            PayrollPayment.run_id == run.id,
            PayrollPayment.employee_id == employee_id,
        )
    )
    already = Decimal(payment.amount) if payment is not None else Decimal("0")
    remaining = accrued - already
    tranche = min(Decimal(tranche), remaining).quantize(Decimal("0.01"))
    if tranche <= 0:
        return Decimal("0")
    method = "cash" if is_cash else None
    new_total = already + tranche
    status = "paid" if new_total >= accrued else "partially_paid"
    if payment is None:
        payment = PayrollPayment(
            id=uuid.uuid4(),
            run_id=run.id,
            employee_id=employee_id,
            amount=new_total,
            booked_amount=Decimal("0"),
            paid_at=paid_at,
            method=method,
            paid_by_user_id=actor_user_id,
            status=status,
            **_initial_split_for_method(new_total, method),
        )
        session.add(payment)
    else:
        payment.amount = new_total
        _reconcile_split_for_paid(payment, new_total, method)
        payment.paid_at = paid_at
        payment.method = method
        payment.paid_by_user_id = actor_user_id
        payment.status = status
    await session.flush()

    from app.services.payroll_payouts import book_payout_expense_for_employees

    result = await book_payout_expense_for_employees(
        session,
        run,
        [employee_id],
        amount_by_employee={employee_id: new_total},
        pay_wallet_id=pay_wallet_id,
    )
    return result.booked_total


async def _reconcile_pool_reserves(session: AsyncSession, run_id: uuid.UUID) -> None:
    """Сверить пул-резервы ЗП после ручной/bulk выплаты — их amount_paid должен учесть расход
    на том же Сейфе/кассе (иначе резерв застрянет в partially_paid с фантомным earmark'ом).
    Ленивый импорт — разрыв цикла payments↔reserves."""
    from app.services.payroll_reserves import reconcile_run_reserves

    await reconcile_run_reserves(session, run_id)


def _post_deposit_payout_iiko(payout_result: Any, run: PayrollRun, paid_at: date) -> None:
    """iiko-изъятие «Выдача депозита» на наличную часть выдачи с ТК Черникова.

    После commit (БД — источник истины). Ошибка iiko не валит выплату (логируется внутри).
    No-op, если наличной выдачи депозита с ТК Черникова не было (deposit_iiko_amount == 0).
    """
    amount = getattr(payout_result, "deposit_iiko_amount", Decimal("0"))
    if amount and amount > 0:
        from app.services.deposit_iiko_payout_production import (
            post_production_deposit_payout_to_iiko,
        )

        post_production_deposit_payout_to_iiko(amount=amount, payout_date=paid_at, source_id=run.id)


async def _get_payment_run(session: AsyncSession, run_id: uuid.UUID) -> PayrollRun:
    run = await session.get(PayrollRun, run_id)
    if run is None:
        raise PayrollNotFoundError("Payroll run not found")
    if run.is_imported_legacy:
        raise PayrollConflictError("Импортированная ведомость — выплаты не отмечаются")
    if run.status != "finalized":
        raise PayrollConflictError("Сначала финализируйте ведомость")
    return run


def _validate_method(method: str) -> None:
    if method not in PAYROLL_PAYMENT_METHODS:
        raise PayrollConflictError("Некорректный способ выплаты")


async def _employee_payable_amount(
    session: AsyncSession,
    run_id: uuid.UUID,
    employee_id: uuid.UUID,
) -> Decimal:
    amount = await session.scalar(
        select(func.sum(PayrollLine.total_payable)).where(
            PayrollLine.run_id == run_id,
            PayrollLine.employee_id == employee_id,
        )
    )
    if amount is None:
        raise PayrollConflictError("У сотрудника нет начислений в этой ведомости")
    return Decimal(amount)


async def _unpaid_employee_payment_rows(
    session: AsyncSession,
    run_id: uuid.UUID,
) -> list[tuple[uuid.UUID, Decimal, PayrollPayment | None]]:
    result = await session.execute(
        select(
            PayrollLine.employee_id,
            func.sum(PayrollLine.total_payable),
            PayrollPayment,
        )
        .outerjoin(
            PayrollPayment,
            (PayrollPayment.run_id == PayrollLine.run_id)
            & (PayrollPayment.employee_id == PayrollLine.employee_id),
        )
        .where(
            PayrollLine.run_id == run_id,
            (PayrollPayment.id.is_(None)) | (PayrollPayment.status != "paid"),
        )
        .group_by(PayrollLine.employee_id, PayrollPayment.id)
        .order_by(PayrollLine.employee_id)
    )
    return [
        (employee_id, Decimal(amount), payment) for employee_id, amount, payment in result.all()
    ]


def _initial_split_for_method(amount: Decimal, method: str | None) -> dict[str, Decimal]:
    if method == "cash":
        return {"amount_cash": amount, "amount_account": Decimal("0")}
    return {"amount_cash": Decimal("0"), "amount_account": amount}


def _reconcile_split_for_paid(payment: PayrollPayment, amount: Decimal, method: str | None) -> None:
    amount_cash = Decimal(payment.amount_cash or 0)
    amount_account = Decimal(payment.amount_account or 0)
    if amount_cash == 0 and amount_account == 0:
        split = _initial_split_for_method(amount, method)
        payment.amount_cash = split["amount_cash"]
        payment.amount_account = split["amount_account"]
        return
    if amount_cash == 0 and method == "cash":
        payment.amount_cash = amount
        payment.amount_account = Decimal("0")
        return
    payment.amount_cash = min(amount_cash, amount)
    payment.amount_account = amount - payment.amount_cash


def _add_payment_event(
    session: AsyncSession,
    *,
    run: PayrollRun,
    action: str,
    actor_user_id: uuid.UUID | None,
    payload: dict[str, Any],
) -> None:
    session.add(
        PayrollRunEvent(
            run_id=run.id,
            period_id=run.period_id,
            action=action,
            actor_user_id=actor_user_id,
            payload=payload,
        )
    )
