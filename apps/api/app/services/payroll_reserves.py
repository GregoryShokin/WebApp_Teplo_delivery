"""Выплата ЗП из резервов-контейнеров, привязанных к ведомости («раскладка пула»).

Контур: финализация ведомости с разбивкой нал/безнал заводит резервы-контейнеры
(``SafeAllocation`` с ``source_run_id`` и ``employee_id IS NULL``): безналичный пул на
Сейфе (``location='safe'``, материализуется в момент транзита банк→Сейф) и наличный пул в
кассе (``location='kassa'``). Оба видны в «Активных платежах» как «Выплата зарплаты…».

Резерв-контейнер сам cashflow НЕ книжит — он лишь earmark'ит остаток кошелька (уменьшает
«свободно») и ограничивает сумму пула. Клик по резерву открывает список сотрудников ЭТОЙ
ведомости; ``allocate_pool`` раскладывает пул по сотрудникам (меньшие ЗП сначала → минимум
людей с долгом; граничный получает сплит), а оркестратор ``pay_run_from_pool`` проводит
каждый транш через существующий зарплатный путь (``book_payout_expense_for_employees`` с
маршрутизацией в кошелёк пула → инкрементальный ДДС по ``booked_amount``) и наращивает
``amount_paid`` резерва. Непокрытое обоими пулами остаётся долгом (``partially_paid``).

Этот модуль держит ЧИСТУЮ раскладку (``allocate_pool``) отдельно от БД-оркестрации, чтобы
money-path-алгоритм тестировался изолированно.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AppSetting,
    CashflowTransaction,
    DdsArticle,
    PayrollLine,
    PayrollPayment,
    PayrollRun,
    SafeAllocation,
    Wallet,
)
from app.services.banking.cashflow_classify import EXCLUDED_QUALITY
from app.services.banking.safe_allocations import (
    ACTIVE_RESERVE_STATUSES,
    book_internal_transfer,
    create_allocation,
)
from app.services.payroll_payout_allocation import (
    DDS_ARTICLE_ADMIN_PAYROLL,
    DDS_ARTICLE_PRODUCTION_PAYROLL,
)
from app.services.payroll_runner import PayrollConflictError, PayrollNotFoundError
from app.services.wallet_balance_as_of import build_money_balance_as_of
from app.services.wallets import SAFE_WALLET_CODE

KASSA_WALLET_CODE = "tk_chernikova"
OVERDRAFT_LIMIT_SETTING = "payroll.overdraft_limit"
# Название резерва-контейнера ЗП — попадает в purpose и в заголовок «Активных платежей».
PAYROLL_RESERVE_LABEL_PRODUCTION = "Выплата зарплаты производственному персоналу"
PAYROLL_RESERVE_LABEL_ADMIN = "Выплата зарплаты административного персонала"


def _q(value: Decimal | int | str) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@dataclass(frozen=True, slots=True)
class EmployeeShare:
    """Доля сотрудника в раскладке: сколько ему ещё осталось выплатить по ведомости."""

    employee_id: uuid.UUID
    remaining: Decimal  # accrued − already_paid; учитывается только > 0


@dataclass(frozen=True, slots=True)
class PoolAllocation:
    """Транш одному сотруднику из ОДНОГО пула (> 0)."""

    employee_id: uuid.UUID
    amount: Decimal


@dataclass(frozen=True, slots=True)
class RunPaymentSettlement:
    """Сводка закрытия обязательства по ФОТ ведомости."""

    required: Decimal
    paid: Decimal
    booked: Decimal
    settled: bool


def allocate_pool(
    pool: Decimal,
    shares: list[EmployeeShare],
    *,
    selected_ids: set[uuid.UUID] | None = None,
    boundary_override: uuid.UUID | None = None,
) -> list[PoolAllocation]:
    """Разложить ОДИН пул по сотрудникам: меньшие ЗП сначала, граничный получает сплит.

    Параметры
    ---------
    pool
        Ёмкость пула (остаток резерва = ``amount − amount_paid``). ≤ 0 → пустая раскладка.
    shares
        Доли всех сотрудников ведомости. Учитываются только с ``remaining > 0``.
    selected_ids
        Явно выбранные сотрудники (режим «выбрал конкретных»). ``None`` — все с долгом
        (режим «отметил всех»).
    boundary_override
        Сотрудник, которого пользователь назначил граничным (получит сплит/долг). Он
        сдвигается в КОНЕЦ очереди — остальных гасим первыми (меньшие суммы вперёд), а он
        забирает остаток пула (полностью, частично или 0, если пул до него не дошёл).
        Если не в кандидатах — игнорируется.

    Возвращает список траншей (только > 0), Σ ≤ ``pool``. Сотрудник с ``0 < транш <
    remaining`` — фактический граничный (получил сплит); идущие за ним получают 0 (не
    попадают в результат). Непокрытое = ``Σ remaining(кандидатов) − Σ транш`` — уйдёт в
    другой пул или в долг (считает вызывающий).
    """
    pool_left = _q(pool)
    if pool_left <= 0:
        return []

    candidates = [s for s in shares if _q(s.remaining) > 0]
    if selected_ids is not None:
        candidates = [s for s in candidates if s.employee_id in selected_ids]

    # Меньшие суммы вперёд; тай-брейк по id — детерминизм раскладки.
    candidates.sort(key=lambda s: (_q(s.remaining), str(s.employee_id)))

    # Ручной граничный: сдвигаем в конец, чтобы именно он абсорбировал остаток пула.
    if boundary_override is not None and any(
        s.employee_id == boundary_override for s in candidates
    ):
        candidates = [s for s in candidates if s.employee_id != boundary_override] + [
            s for s in candidates if s.employee_id == boundary_override
        ]

    allocations: list[PoolAllocation] = []
    for share in candidates:
        if pool_left <= 0:
            break
        take = min(_q(share.remaining), pool_left)
        if take > 0:
            allocations.append(PoolAllocation(share.employee_id, take))
            pool_left -= take
    return allocations


def allocated_total(allocations: list[PoolAllocation]) -> Decimal:
    """Σ траншей раскладки (сколько пул фактически покрыл)."""
    return _q(sum((a.amount for a in allocations), Decimal("0")))


# --- БД-слой: резервы-контейнеры, оркестрация выплаты, платёжеспособность ---------


@dataclass(frozen=True, slots=True)
class PoolPayoutResult:
    """Итог выплаты из пула: сколько проведено с основного резерва и с перетока."""

    reserve_id: uuid.UUID
    primary_booked: Decimal
    overflow_reserve_id: uuid.UUID | None
    overflow_booked: Decimal
    employees_paid: int


@dataclass(frozen=True, slots=True)
class ReserveTransferResult:
    """Итог переноса части зарплатного резерва между Сейфом и кассой."""

    source_reserve_id: uuid.UUID
    destination_reserve_id: uuid.UUID
    transfer_id: uuid.UUID
    amount: Decimal
    destination_location: str
    allocations: tuple[PoolAllocation, ...]


@dataclass(frozen=True, slots=True)
class ReserveCancelResult:
    """Итог снятия непогашенного остатка одного зарплатного резерва."""

    reserve_id: uuid.UUID
    released: Decimal
    status: str


@dataclass(frozen=True, slots=True)
class SolvencyBreakdown:
    """Разложение платёжеспособности под ведомость (advisory — банк по последней выписке)."""

    available: Decimal
    required_total: Decimal
    remaining: Decimal
    shortfall: Decimal
    overdraft_limit: Decimal
    safe_balance: Decimal
    kassa_balance: Decimal
    bank_total: Decimal
    reserved_other: Decimal
    solvent: bool = False


def _is_admin_run(run: PayrollRun) -> bool:
    return isinstance(run.summary, dict) and run.summary.get("kind") == "admin"


def _reserve_label(run: PayrollRun) -> str:
    return PAYROLL_RESERVE_LABEL_ADMIN if _is_admin_run(run) else PAYROLL_RESERVE_LABEL_PRODUCTION


async def _payroll_article_id(session: AsyncSession, run: PayrollRun) -> uuid.UUID | None:
    code = DDS_ARTICLE_ADMIN_PAYROLL if _is_admin_run(run) else DDS_ARTICLE_PRODUCTION_PAYROLL
    return await session.scalar(select(DdsArticle.id).where(DdsArticle.code == code))


async def _resolve_wallet(session: AsyncSession, code: str) -> Wallet:
    wallet = await session.scalar(
        select(Wallet).where(Wallet.code == code, Wallet.status == "active")
    )
    if wallet is None:
        raise PayrollConflictError(f"Не найден активный кошелёк «{code}»")
    return wallet


async def run_pool_shares(session: AsyncSession, run_id: uuid.UUID) -> list[EmployeeShare]:
    """Доли сотрудников ведомости для раскладки пула: ``remaining = ФОТ − уже выплачено``.

    Исключены сотрудники с запланированной выдачей депозита (``deposit_payout_scheduled>0``) —
    они идут только полным путём «Выплатить» (гейт в ``apply_pool_tranche``).
    """
    rows = (
        await session.execute(
            select(
                PayrollLine.employee_id,
                func.sum(PayrollLine.total_payable),
                func.coalesce(func.sum(PayrollLine.deposit_payout_scheduled), 0),
            )
            .where(PayrollLine.run_id == run_id)
            .group_by(PayrollLine.employee_id)
        )
    ).all()
    paid_rows = (
        await session.execute(
            select(PayrollPayment.employee_id, PayrollPayment.amount).where(
                PayrollPayment.run_id == run_id
            )
        )
    ).all()
    paid_by = {emp: Decimal(amount) for emp, amount in paid_rows}
    shares: list[EmployeeShare] = []
    for employee_id, accrued, deposit in rows:
        if Decimal(deposit or 0) > 0:
            continue
        remaining = Decimal(accrued or 0) - paid_by.get(employee_id, Decimal("0"))
        if remaining > 0:
            shares.append(EmployeeShare(employee_id, _q(remaining)))
    return shares


async def run_payment_settlement(session: AsyncSession, run_id: uuid.UUID) -> RunPaymentSettlement:
    """Проверить, что весь ФОТ и выплачен сотрудникам, и проведён в ДДС.

    ``PayrollPayment.amount`` отвечает за факт выплаты сотруднику, ``booked_amount`` — за
    проведённую сумму ДДС. Только совпадение обеих сумм с ФОТ позволяет освободить остатки
    резервов: это защищает от ситуации «отметили выплаченным, но расход ещё не создался».
    """
    required = _q(
        await session.scalar(
            select(func.coalesce(func.sum(PayrollLine.total_payable), 0)).where(
                PayrollLine.run_id == run_id
            )
        )
        or 0
    )
    paid, booked = (
        await session.execute(
            select(
                func.coalesce(func.sum(PayrollPayment.amount), 0),
                func.coalesce(func.sum(PayrollPayment.booked_amount), 0),
            ).where(PayrollPayment.run_id == run_id)
        )
    ).one()
    paid_q = _q(paid or 0)
    booked_q = _q(booked or 0)
    return RunPaymentSettlement(
        required=required,
        paid=paid_q,
        booked=booked_q,
        settled=required > 0 and paid_q >= required and booked_q >= required,
    )


async def _active_run_reserve(
    session: AsyncSession,
    run_id: uuid.UUID,
    location: str,
    *,
    for_update: bool = False,
) -> SafeAllocation | None:
    stmt = select(SafeAllocation).where(
        SafeAllocation.source_run_id == run_id,
        SafeAllocation.location == location,
        SafeAllocation.status.in_(ACTIVE_RESERVE_STATUSES),
    )
    if for_update:
        stmt = stmt.with_for_update()
    return await session.scalar(stmt)


async def ensure_run_kassa_reserve(
    session: AsyncSession,
    run: PayrollRun,
    *,
    cash_amount: Decimal,
    created_by_user_id: uuid.UUID | None = None,
) -> SafeAllocation | None:
    """Завести/обновить наличный пул-резерв ЗП в кассе (кликабелен сразу).

    Идемпотентно: не более одного активного касса-резерва на ведомость. ``cash_amount<=0``
    снимает неоплаченный резерв. Сумму не опускаем ниже уже выданного (``amount_paid``).
    """
    cash = _q(cash_amount)
    existing = await _active_run_reserve(session, run.id, "kassa")
    if cash <= 0:
        if existing is not None and Decimal(existing.amount_paid) == 0:
            existing.status = "cancelled"
            await session.flush()
        return None
    article_id = await _payroll_article_id(session, run)
    label = _reserve_label(run)
    if existing is not None:
        existing.amount = max(cash, _q(existing.amount_paid))
        existing.article_id = article_id
        existing.purpose = label
        await session.flush()
        return existing
    wallet = await _resolve_wallet(session, KASSA_WALLET_CODE)
    return await create_allocation(
        session,
        wallet_id=wallet.id,
        amount=cash,
        free_amount=None,
        article_id=article_id,
        purpose=label,
        source_run_id=run.id,
        location="kassa",
        created_by_user_id=created_by_user_id,
    )


async def ensure_run_safe_reserve(
    session: AsyncSession,
    run: PayrollRun,
    *,
    account_amount: Decimal,
    created_by_user_id: uuid.UUID | None = None,
) -> SafeAllocation | None:
    """Завести/обновить безналичный пул-резерв ЗП на Сейфе — вызывается В МОМЕНТ транзита
    банк→Сейф (деньги только что пришли на Сейф). До транзита резерва нет: в «Активных
    платежах» ЗП виден как банк-черновик «Отправлен в банк» (некликабелен).
    """
    account = _q(account_amount)
    existing = await _active_run_reserve(session, run.id, "safe")
    if account <= 0:
        return existing
    article_id = await _payroll_article_id(session, run)
    label = _reserve_label(run)
    if existing is not None:
        existing.amount = max(account, _q(existing.amount_paid))
        existing.article_id = article_id
        existing.purpose = label
        await session.flush()
        return existing
    wallet = await _resolve_wallet(session, SAFE_WALLET_CODE)
    return await create_allocation(
        session,
        wallet_id=wallet.id,
        amount=account,
        free_amount=None,  # транзит только что пополнил Сейф ровно на эту сумму
        article_id=article_id,
        purpose=label,
        source_run_id=run.id,
        location="safe",
        created_by_user_id=created_by_user_id,
    )


async def reconcile_run_reserves(
    session: AsyncSession,
    run_id: uuid.UUID,
    *,
    include_paid: bool = False,
) -> None:
    """Сверить пул-резервы с действующим ДДС и общим обязательством ведомости.

    ``amount_paid`` = min(amount, Σ ДЕЙСТВУЮЩИХ out-проводок ``payroll_payout`` на
    ``wallet_id`` этой ведомости). ``quality_status='excluded'`` не двигает баланс наличного
    кошелька и поэтому не считается расходом именно этого кошелька. Когда весь ФОТ уже
    выплачен И проведён в ДДС, непотреблённый остаток любого резерва снимается: зарплата могла
    быть фактически выдана из второго пула, и держать деньги первого пула целевыми больше не
    требуется. Ручной/bulk путь выплаты
    («Выплатить»/«Доплатить» в реестре) книжит расход на ТОТ ЖЕ Сейф/кассу БЕЗ
    ``pay_wallet_id`` и не трогает резерв — без сверки резерв застрял бы в
    ``partially_paid`` с фантомным earmark'ом. Вызывается в конце любой выплаты по ведомости
    (пул и реестр). ``include_paid=True`` используется доменной коррекцией кошелька, чтобы
    пересчитать ранее ошибочно закрытый резерв.
    """
    from app.services.payroll_payouts import PAYROLL_PAYOUT_SOURCE_KIND

    statuses = (*ACTIVE_RESERVE_STATUSES, "paid") if include_paid else ACTIVE_RESERVE_STATUSES
    reserves = (
        await session.scalars(
            select(SafeAllocation).where(
                SafeAllocation.source_run_id == run_id,
                SafeAllocation.status.in_(statuses),
            )
        )
    ).all()
    if not reserves:
        return
    settlement = await run_payment_settlement(session, run_id)
    for reserve in reserves:
        net_paid = await session.scalar(
            select(
                func.coalesce(
                    func.sum(
                        case(
                            (CashflowTransaction.direction == "out", CashflowTransaction.amount),
                            else_=-CashflowTransaction.amount,
                        )
                    ),
                    0,
                )
            ).where(
                CashflowTransaction.source_kind == PAYROLL_PAYOUT_SOURCE_KIND,
                CashflowTransaction.source_id == run_id,
                CashflowTransaction.direction.in_(("out", "in")),
                CashflowTransaction.wallet_id == reserve.wallet_id,
                CashflowTransaction.quality_status != EXCLUDED_QUALITY,
            )
        )
        paid = min(_q(reserve.amount), max(Decimal("0"), _q(net_paid or 0)))
        reserve.amount_paid = paid
        if settlement.settled and paid < _q(reserve.amount):
            # Обязательство ведомости закрыто из другого кошелька: фактически не потраченный
            # остаток этого пула освобождается, но amount_paid остаётся честным счётчиком ДДС.
            reserve.status = "cancelled"
        elif paid <= 0:
            reserve.status = "reserved"
        elif paid >= _q(reserve.amount):
            reserve.status = "paid"
        else:
            reserve.status = "partially_paid"
    await session.flush()


async def cancel_run_reserves(session: AsyncSession, run_id: uuid.UUID) -> int:
    """Снять активные пул-резервы ведомости (при дефинализации). Оплаченные ноги остаются,
    неоплаченный остаток освобождается. Возвращает число снятых резервов."""
    reserves = (
        await session.scalars(
            select(SafeAllocation).where(
                SafeAllocation.source_run_id == run_id,
                SafeAllocation.status.in_(ACTIVE_RESERVE_STATUSES),
            )
        )
    ).all()
    count = 0
    for reserve in reserves:
        reserve.status = "cancelled"
        count += 1
    if count:
        await session.flush()
    return count


async def transfer_run_reserve(
    session: AsyncSession,
    *,
    reserve_id: uuid.UUID,
    selected_ids: set[uuid.UUID],
    boundary_override: uuid.UUID | None = None,
    operation_date: date,
    actor_user_id: uuid.UUID | None,
) -> ReserveTransferResult:
    """Перенести выбранную часть зарплатного пула Сейф↔касса вместе с деньгами.

    Сумма берётся из той же раскладки, которую пользователь видит перед выплатой:
    выбранные сотрудники, меньшие остатки первыми, опциональный граничный сотрудник.
    Источник уменьшается ровно на перенесённую сумму, резерв получателя увеличивается
    на неё же, а двухногий внутренний перевод сохраняет свободные остатки обоих счетов.
    Выбранная раскладка пишется в событие ведомости для аудита.
    """
    if not selected_ids:
        raise PayrollConflictError("Выберите сотрудников для передачи резерва")

    source = await session.get(SafeAllocation, reserve_id, with_for_update=True)
    if source is None or source.source_run_id is None:
        raise PayrollNotFoundError("Резерв ведомости не найден")
    if source.employee_id is not None:
        raise PayrollConflictError("Это не пул-резерв ведомости")
    if source.status not in ACTIVE_RESERVE_STATUSES:
        raise PayrollConflictError("Резерв уже оплачен или отменён")

    run = await session.get(PayrollRun, source.source_run_id)
    if run is None:
        raise PayrollNotFoundError("Ведомость не найдена")
    if run.status != "finalized":
        raise PayrollConflictError("Сначала финализируйте ведомость")

    destination_location = "safe" if source.location == "kassa" else "kassa"
    source_outstanding = _q(Decimal(source.amount) - Decimal(source.amount_paid))
    allocations = allocate_pool(
        source_outstanding,
        await run_pool_shares(session, run.id),
        selected_ids=selected_ids,
        boundary_override=boundary_override,
    )
    transfer_amount = allocated_total(allocations)
    if transfer_amount <= 0:
        raise PayrollConflictError("У выбранных сотрудников нет остатка к выплате")

    destination = await _active_run_reserve(session, run.id, destination_location, for_update=True)
    destination_wallet = await _resolve_wallet(
        session,
        SAFE_WALLET_CODE if destination_location == "safe" else KASSA_WALLET_CODE,
    )
    source_wallet = await session.get(Wallet, source.wallet_id)
    if source_wallet is None or source_wallet.status != "active":
        raise PayrollConflictError("Счёт-источник резерва не найден или неактивен")

    try:
        transfer_id = await book_internal_transfer(
            session,
            source_wallet=source_wallet,
            dest_wallet=destination_wallet,
            amount=transfer_amount,
            purpose=f"Перенос резерва ЗП: {_reserve_label(run)}",
            operation_date=operation_date,
        )
    except ValueError as exc:
        raise PayrollConflictError(str(exc)) from exc

    source_left = _q(source_outstanding - transfer_amount)
    if source_left <= 0:
        # Выплаченная часть остаётся в истории, но непогашенного резерва на источнике
        # больше нет. cancelled здесь означает именно снятый/перенесённый остаток.
        source.status = "cancelled"
    else:
        source.amount = _q(Decimal(source.amount) - transfer_amount)
        source.status = "partially_paid" if Decimal(source.amount_paid) > 0 else "reserved"

    if destination is None:
        destination = await create_allocation(
            session,
            wallet_id=destination_wallet.id,
            amount=transfer_amount,
            free_amount=None,
            article_id=source.article_id,
            purpose=source.purpose or _reserve_label(run),
            source_run_id=run.id,
            location=destination_location,
            created_by_user_id=actor_user_id,
        )
    else:
        destination.amount = _q(Decimal(destination.amount) + transfer_amount)
        destination.status = (
            "partially_paid" if Decimal(destination.amount_paid) > 0 else "reserved"
        )
        await session.flush()

    _add_reserve_transfer_event(
        session,
        run=run,
        source=source,
        destination=destination,
        transfer_id=transfer_id,
        amount=transfer_amount,
        allocations=allocations,
        actor_user_id=actor_user_id,
    )
    await session.commit()
    return ReserveTransferResult(
        source_reserve_id=source.id,
        destination_reserve_id=destination.id,
        transfer_id=transfer_id,
        amount=transfer_amount,
        destination_location=destination_location,
        allocations=tuple(allocations),
    )


async def cancel_run_reserve(
    session: AsyncSession,
    *,
    reserve_id: uuid.UUID,
    actor_user_id: uuid.UUID | None,
) -> ReserveCancelResult:
    """Снять непогашенный остаток одного зарплатного резерва без обратной проводки."""
    reserve = await session.get(SafeAllocation, reserve_id, with_for_update=True)
    if reserve is None or reserve.source_run_id is None:
        raise PayrollNotFoundError("Резерв ведомости не найден")
    if reserve.employee_id is not None:
        raise PayrollConflictError("Это не пул-резерв ведомости")
    run = await session.get(PayrollRun, reserve.source_run_id)
    if run is None:
        raise PayrollNotFoundError("Ведомость не найдена")
    if reserve.status == "paid":
        raise PayrollConflictError("Полностью выплаченный резерв отменить нельзя")
    if reserve.status == "cancelled":
        return ReserveCancelResult(reserve.id, Decimal("0.00"), reserve.status)

    released = _q(Decimal(reserve.amount) - Decimal(reserve.amount_paid))
    reserve.status = "cancelled"
    _add_reserve_cancel_event(
        session,
        run=run,
        reserve=reserve,
        released=released,
        actor_user_id=actor_user_id,
    )
    await session.commit()
    return ReserveCancelResult(reserve.id, released, reserve.status)


async def pay_run_from_pool(
    session: AsyncSession,
    *,
    reserve_id: uuid.UUID,
    selected_ids: set[uuid.UUID] | None = None,
    boundary_override: uuid.UUID | None = None,
    allow_overflow: bool = True,
    expected_location: str | None = None,
    paid_at: date,
    actor_user_id: uuid.UUID | None,
) -> PoolPayoutResult:
    """Выплатить сотрудникам ведомости из пула-резерва (Сейф ИЛИ касса) с перетоком.

    Раскладывает остаток резерва по сотрудникам (``allocate_pool``), проводит каждый транш
    через ``apply_pool_tranche`` (ДДС из кошелька пула, учёт ``booked_amount``) и наращивает
    ``amount_paid`` резерва. Если ``allow_overflow`` и есть второй активный пул-резерв
    ведомости — непокрытое доводится с него (симметрия Сейф↔касса). Непокрытое обоими
    остаётся долгом (``partially_paid`` в ``PayrollPayment``). ``expected_location`` не даёт
    кассовому интерфейсу подставить резерв другого наличного счёта. Атомарно (один commit).
    """
    primary = await session.get(SafeAllocation, reserve_id, with_for_update=True)
    if primary is None or primary.source_run_id is None:
        raise PayrollNotFoundError("Резерв ведомости не найден")
    if primary.employee_id is not None:
        raise PayrollConflictError("Это не пул-резерв ведомости")
    if expected_location is not None and primary.location != expected_location:
        raise PayrollConflictError("Этот резерв относится к другому наличному счёту")
    if primary.status not in ACTIVE_RESERVE_STATUSES:
        raise PayrollConflictError("Резерв уже оплачен или отменён")
    run = await session.get(PayrollRun, primary.source_run_id)
    if run is None:
        raise PayrollNotFoundError("Ведомость не найдена")
    if run.status != "finalized":
        raise PayrollConflictError("Сначала финализируйте ведомость")

    # Лениво (разрыв цикла payments↔payouts↔reserves на загрузке модулей).
    from app.services.payroll_payments import apply_pool_tranche

    shares = await run_pool_shares(session, run.id)
    paid_by_emp: dict[uuid.UUID, Decimal] = {}

    async def _drain(reserve: SafeAllocation, share_list: list[EmployeeShare]) -> Decimal:
        pool = _q(Decimal(reserve.amount) - Decimal(reserve.amount_paid))
        allocs = allocate_pool(
            pool, share_list, selected_ids=selected_ids, boundary_override=boundary_override
        )
        is_cash = reserve.location == "kassa"
        booked = Decimal("0")
        for alloc in allocs:
            delta = await apply_pool_tranche(
                session,
                run,
                alloc.employee_id,
                tranche=alloc.amount,
                pay_wallet_id=reserve.wallet_id,
                is_cash=is_cash,
                paid_at=paid_at,
                actor_user_id=actor_user_id,
            )
            booked += delta
            paid_by_emp[alloc.employee_id] = (
                paid_by_emp.get(alloc.employee_id, Decimal("0")) + alloc.amount
            )
        return _q(booked)

    primary_booked = await _drain(primary, shares)

    overflow_reserve_id: uuid.UUID | None = None
    overflow_booked = Decimal("0")
    if allow_overflow:
        secondary = await session.scalar(
            select(SafeAllocation)
            .where(
                SafeAllocation.source_run_id == run.id,
                SafeAllocation.id != primary.id,
                SafeAllocation.status.in_(ACTIVE_RESERVE_STATUSES),
            )
            .with_for_update()
        )
        if secondary is not None and Decimal(secondary.amount) > Decimal(secondary.amount_paid):
            residual = [
                EmployeeShare(
                    s.employee_id,
                    _q(Decimal(s.remaining) - paid_by_emp.get(s.employee_id, Decimal("0"))),
                )
                for s in shares
            ]
            overflow_booked = await _drain(secondary, residual)
            if overflow_booked > 0:
                overflow_reserve_id = secondary.id

    # amount_paid/статус резервов — из фактических out-проводок (устойчиво к смешиванию с
    # ручным/bulk путём выплаты по этой же ведомости).
    await reconcile_run_reserves(session, run.id)

    _add_pool_payout_event(
        session,
        run=run,
        reserve=primary,
        primary_booked=primary_booked,
        overflow_booked=overflow_booked,
        actor_user_id=actor_user_id,
    )
    await session.commit()
    return PoolPayoutResult(
        reserve_id=primary.id,
        primary_booked=_q(primary_booked),
        overflow_reserve_id=overflow_reserve_id,
        overflow_booked=_q(overflow_booked),
        employees_paid=len(paid_by_emp),
    )


@dataclass(frozen=True, slots=True)
class EmployeeReservePayResult:
    """Итог ручной выплаты одному сотруднику из резерва (карандаш → сумма → ✓)."""

    booked: Decimal
    employee_total_paid: Decimal
    employee_remaining: Decimal
    reserve_status: str
    reserve_outstanding: Decimal


async def pay_employee_from_reserve(
    session: AsyncSession,
    *,
    reserve_id: uuid.UUID,
    employee_id: uuid.UUID,
    amount: Decimal,
    paid_at: date,
    actor_user_id: uuid.UUID | None,
) -> EmployeeReservePayResult:
    """Выплатить ОДНОМУ сотруднику ручную сумму из пула-резерва (Сейф/касса).

    Карандаш-контур: пользователь сам вводит сумму по сотруднику, остаток резерва остаётся
    earmark'ом для следующей выплаты. Сумма ограничена и остатком резерва, и остатком
    начисленного сотруднику. Проводка — через ``apply_pool_tranche`` (кошелёк резерва, ДДС по
    ``booked_amount``), затем сверка резерва. Атомарно.
    """
    reserve = await session.get(SafeAllocation, reserve_id, with_for_update=True)
    if reserve is None or reserve.source_run_id is None:
        raise PayrollNotFoundError("Резерв ведомости не найден")
    if reserve.employee_id is not None:
        raise PayrollConflictError("Это не пул-резерв ведомости")
    if reserve.status not in ACTIVE_RESERVE_STATUSES:
        raise PayrollConflictError("Резерв уже оплачен или отменён")
    run = await session.get(PayrollRun, reserve.source_run_id)
    if run is None:
        raise PayrollNotFoundError("Ведомость не найдена")
    if run.status != "finalized":
        raise PayrollConflictError("Сначала финализируйте ведомость")

    amount = _q(amount)
    if amount <= 0:
        raise PayrollConflictError("Сумма выплаты должна быть больше нуля")
    outstanding = _q(Decimal(reserve.amount) - Decimal(reserve.amount_paid))
    if amount > outstanding:
        raise PayrollConflictError(
            f"В резерве осталось {outstanding} — уменьшите сумму или пополните резерв"
        )

    from app.services.payroll_payments import apply_pool_tranche

    booked = await apply_pool_tranche(
        session,
        run,
        employee_id,
        tranche=amount,
        pay_wallet_id=reserve.wallet_id,
        is_cash=reserve.location == "kassa",
        paid_at=paid_at,
        actor_user_id=actor_user_id,
    )
    await reconcile_run_reserves(session, run.id)

    accrued = await session.scalar(
        select(func.coalesce(func.sum(PayrollLine.total_payable), 0)).where(
            PayrollLine.run_id == run.id, PayrollLine.employee_id == employee_id
        )
    )
    total_paid = await session.scalar(
        select(func.coalesce(PayrollPayment.amount, 0)).where(
            PayrollPayment.run_id == run.id, PayrollPayment.employee_id == employee_id
        )
    )
    _add_pool_payout_event(
        session,
        run=run,
        reserve=reserve,
        primary_booked=_q(booked),
        overflow_booked=Decimal("0"),
        actor_user_id=actor_user_id,
    )
    await session.commit()
    await session.refresh(reserve)
    total_paid_q = _q(total_paid or 0)
    return EmployeeReservePayResult(
        booked=_q(booked),
        employee_total_paid=total_paid_q,
        employee_remaining=_q(max(Decimal("0"), _q(accrued or 0) - total_paid_q)),
        reserve_status=reserve.status,
        reserve_outstanding=_q(Decimal(reserve.amount) - Decimal(reserve.amount_paid)),
    )


def _add_pool_payout_event(
    session: AsyncSession,
    *,
    run: PayrollRun,
    reserve: SafeAllocation,
    primary_booked: Decimal,
    overflow_booked: Decimal,
    actor_user_id: uuid.UUID | None,
) -> None:
    from app.models import PayrollRunEvent

    session.add(
        PayrollRunEvent(
            run_id=run.id,
            period_id=run.period_id,
            action="pool_payout",
            actor_user_id=actor_user_id,
            payload={
                "reserve_id": str(reserve.id),
                "reserve_location": reserve.location,
                "primary_booked": str(_q(primary_booked)),
                "overflow_booked": str(_q(overflow_booked)),
            },
        )
    )


def _add_reserve_transfer_event(
    session: AsyncSession,
    *,
    run: PayrollRun,
    source: SafeAllocation,
    destination: SafeAllocation,
    transfer_id: uuid.UUID,
    amount: Decimal,
    allocations: list[PoolAllocation],
    actor_user_id: uuid.UUID | None,
) -> None:
    from app.models import PayrollRunEvent

    session.add(
        PayrollRunEvent(
            run_id=run.id,
            period_id=run.period_id,
            action="reserve_transferred",
            actor_user_id=actor_user_id,
            payload={
                "source_reserve_id": str(source.id),
                "source_location": source.location,
                "destination_reserve_id": str(destination.id),
                "destination_location": destination.location,
                "transfer_id": str(transfer_id),
                "amount": str(_q(amount)),
                "employee_allocations": [
                    {"employee_id": str(item.employee_id), "amount": str(_q(item.amount))}
                    for item in allocations
                ],
            },
        )
    )


def _add_reserve_cancel_event(
    session: AsyncSession,
    *,
    run: PayrollRun,
    reserve: SafeAllocation,
    released: Decimal,
    actor_user_id: uuid.UUID | None,
) -> None:
    from app.models import PayrollRunEvent

    session.add(
        PayrollRunEvent(
            run_id=run.id,
            period_id=run.period_id,
            action="reserve_cancelled",
            actor_user_id=actor_user_id,
            payload={
                "reserve_id": str(reserve.id),
                "location": reserve.location,
                "released": str(_q(released)),
            },
        )
    )


# --- Платёжеспособность (solvency) ------------------------------------------------


async def _reserved_by_other_runs(session: AsyncSession, run_id: uuid.UUID) -> Decimal:
    """Σ непогашенных остатков активных резервов, НЕ относящихся к этой ведомости."""
    outstanding = func.sum(SafeAllocation.amount - SafeAllocation.amount_paid)
    total = await session.scalar(
        select(func.coalesce(outstanding, 0)).where(
            SafeAllocation.status.in_(ACTIVE_RESERVE_STATUSES),
            or_(
                SafeAllocation.source_run_id.is_(None),
                SafeAllocation.source_run_id != run_id,
            ),
        )
    )
    return _q(total or 0)


async def _bank_total(session: AsyncSession) -> Decimal:
    """Σ балансов банковских кошельков по выписке (advisory).

    ``include_inactive=False`` передаётся ЯВНО, и это единственное место, где так и надо:
    расчёт отвечает на вопрос «чем платить прямо сейчас», а с закрытого счёта не платят.
    Балансу на дату нужен противоположный ответ — счёт, закрытый в сентябре, 31 августа деньги
    ещё держал, — поэтому витрина и снимок зовут ту же функцию без этого флага. Раньше разница
    была не параметром, а четвёртой копией формулы, и заметить её можно было только сличением.
    """
    money = await build_money_balance_as_of(session, include_inactive=False)
    return _q(sum((row.balance for row in money.rows if row.is_bank), Decimal("0")))


async def _overdraft_limit(session: AsyncSession) -> Decimal:
    setting = await session.scalar(
        select(AppSetting).where(AppSetting.key == OVERDRAFT_LIMIT_SETTING)
    )
    if setting is None:
        return Decimal("0")
    value = setting.value
    if isinstance(value, dict):
        value = value.get("limit", 0)
    try:
        return _q(value)
    except (ArithmeticError, TypeError, ValueError):
        return Decimal("0")


async def _run_grand_total(session: AsyncSession, run_id: uuid.UUID) -> Decimal:
    total = await session.scalar(
        select(
            func.coalesce(func.sum(PayrollLine.total_payable), 0)
            + func.coalesce(func.sum(PayrollLine.deposit_payout_scheduled), 0)
        ).where(PayrollLine.run_id == run_id)
    )
    return _q(total or 0)


async def _run_paid_total(session: AsyncSession, run_id: uuid.UUID) -> Decimal:
    total = await session.scalar(
        select(func.coalesce(func.sum(PayrollPayment.amount), 0)).where(
            PayrollPayment.run_id == run_id
        )
    )
    return _q(total or 0)


async def run_solvency(session: AsyncSession, run: PayrollRun) -> SolvencyBreakdown:
    """Хватит ли денег на выплату ведомости: Σ(Сейф+касса точно) + банк + овердрафт − чужие
    резервы, против остатка к выплате. Advisory: банк/овердрафт по последней выписке."""
    from app.services.kassa.payouts import kassa_balance

    safe_wallet = await session.scalar(
        select(Wallet).where(Wallet.code == SAFE_WALLET_CODE, Wallet.status == "active")
    )
    kassa_wallet = await session.scalar(
        select(Wallet).where(Wallet.code == KASSA_WALLET_CODE, Wallet.status == "active")
    )
    safe_balance = await kassa_balance(session, safe_wallet) if safe_wallet else Decimal("0")
    kassa_bal = await kassa_balance(session, kassa_wallet) if kassa_wallet else Decimal("0")
    bank_total = await _bank_total(session)
    reserved_other = await _reserved_by_other_runs(session, run.id)
    overdraft = await _overdraft_limit(session)
    available = _q(safe_balance + kassa_bal + bank_total + overdraft - reserved_other)

    grand_total = await _run_grand_total(session, run.id)
    already = await _run_paid_total(session, run.id)
    remaining = _q(max(Decimal("0"), grand_total - already))
    shortfall = _q(max(Decimal("0"), remaining - available))
    return SolvencyBreakdown(
        available=available,
        required_total=grand_total,
        remaining=remaining,
        shortfall=shortfall,
        overdraft_limit=overdraft,
        safe_balance=_q(safe_balance),
        kassa_balance=_q(kassa_bal),
        bank_total=bank_total,
        reserved_other=reserved_other,
        solvent=shortfall <= 0,
    )
