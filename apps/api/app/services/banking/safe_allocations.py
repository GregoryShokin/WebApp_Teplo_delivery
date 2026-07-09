"""Резервы (аллокации) подотчётного счёта «Сейф» — фаза 2.

Резерв (``SafeAllocation``) — намерение потратить часть остатка Сейфа: деньги ещё
на карте, поэтому резерв НЕ создаёт cashflow и НЕ двигает баланс, лишь уменьшает
«свободно» = баланс − Σ(непогашенных резервов). Расход признаётся при оплате:
каждая оплата (в т.ч. частичная) — отдельная out-``CashflowTransaction`` на Сейфе
(``source_kind='safe_payout'``, ``source_id`` = резерв), а ``amount_paid`` копит их
сумму. Инвариант: баланс = свободно + Σ(резерв.amount − резерв.amount_paid).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    BankOperation,
    CashflowTransaction,
    DdsArticle,
    EmployeePayout,
    SafeAllocation,
    SalaryAdvanceBankDraft,
    Wallet,
)
from app.services.banking.classifier import (
    SAFE_WALLET_CODE,
    TRANSFER_IN_ARTICLE_CODE,
    TRANSFER_OUT_ARTICLE_CODE,
    book_safe_topup,
)

SAFE_PAYOUT_SOURCE_KIND = "safe_payout"
SAFE_CASH_WITHDRAWAL_SOURCE_KIND = "safe_cash_withdrawal"
SAFE_RECONCILE_ADJUST_SOURCE_KIND = "safe_reconcile_adjust"
# Двухногое перемещение при передаче целёвки Сейф → касса (source_id = резерв).
ALLOCATION_TO_KASSA_SOURCE_KIND = "safe_allocation_to_kassa"
# Выдача целёвки наличными из кассы, вкладка «К выдаче» (source_id = резерв).
KASSA_TARGET_PAYOUT_SOURCE_KIND = "kassa_target_payout"
ACTIVE_RESERVE_STATUSES = ("reserved", "partially_paid")
# Куда снимаются наличные с карты «Сейф» (параллельный наличный счёт).
CASH_WITHDRAWAL_WALLET_CODE = "tk_chernikova"
# Корректировка дрейфа при сверке: недостача → расход, излишек → поступление.
DRIFT_SHORTAGE_ARTICLE_CODE = "prochie_rashody"
DRIFT_SURPLUS_ARTICLE_CODE = "prochie_postupleniya"


async def _article_id(session: AsyncSession, code: str) -> UUID:
    article_id = await session.scalar(select(DdsArticle.id).where(DdsArticle.code == code))
    if article_id is None:
        raise ValueError(f"Не найдена статья ДДС «{code}»")
    return article_id


async def safe_reserved_total(session: AsyncSession, wallet_id: UUID) -> Decimal:
    """Σ непогашенных остатков активных резервов кошелька (для «зарезервировано»)."""
    total = await session.scalar(
        select(func.coalesce(func.sum(SafeAllocation.amount - SafeAllocation.amount_paid), 0)).where(
            SafeAllocation.wallet_id == wallet_id,
            SafeAllocation.status.in_(ACTIVE_RESERVE_STATUSES),
        )
    )
    return Decimal(total or 0)


async def safe_active_allocations_count(session: AsyncSession, wallet_id: UUID) -> int:
    """Число активных резервов кошелька — для бейджа «N целевых» на плитке Сейфа."""
    count = await session.scalar(
        select(func.count())
        .select_from(SafeAllocation)
        .where(
            SafeAllocation.wallet_id == wallet_id,
            SafeAllocation.status.in_(ACTIVE_RESERVE_STATUSES),
        )
    )
    return int(count or 0)


async def create_allocation(
    session: AsyncSession,
    *,
    wallet_id: UUID,
    amount: Decimal,
    free_amount: Decimal | None,
    article_id: UUID | None = None,
    counterparty_id: UUID | None = None,
    employee_id: UUID | None = None,
    purpose: str | None = None,
    source_draft_id: UUID | None = None,
    source_draft_line_id: UUID | None = None,
    source_operation_id: UUID | None = None,
    created_by_user_id: UUID | None = None,
) -> SafeAllocation:
    """Создать резерв. Запрет перерезервирования: ``amount`` ≤ свободно (``free_amount``).

    ``free_amount=None`` — без проверки: авто-резерв под оплаченный банковский черновик
    (``source_draft_id``), где перевод р/с→Сейф уже пополнил баланс ровно на эту сумму.
    ``employee_id`` — зарплатная целёвка «через Сейф»: при оплате резерва заведётся EmployeePayout.
    """
    if amount <= 0:
        raise ValueError("Сумма резерва должна быть больше нуля")
    if free_amount is not None and amount > free_amount:
        raise ValueError(
            f"Недостаточно свободных средств на Сейфе: свободно {free_amount}, запрошено {amount}"
        )
    allocation = SafeAllocation(
        wallet_id=wallet_id,
        amount=amount,
        amount_paid=Decimal("0"),
        article_id=article_id,
        counterparty_id=counterparty_id,
        employee_id=employee_id,
        purpose=purpose,
        source_draft_id=source_draft_id,
        source_draft_line_id=source_draft_line_id,
        source_operation_id=source_operation_id,
        status="reserved",
        created_by_user_id=created_by_user_id,
    )
    session.add(allocation)
    await session.flush()
    return allocation


async def book_safe_topup_reserves(
    session: AsyncSession,
    operation: BankOperation,
    *,
    reserves: list[tuple[UUID, Decimal, UUID | None]],
    counterparty_id: UUID | None = None,
    created_by_user_id: UUID | None = None,
) -> list[SafeAllocation]:
    """Разбор операции «через Сейф»: транзит р/с→Сейф + целёвки-резервы по разметке.

    Топ-ап (``book_safe_topup``) пополняет Сейф на всю сумму операции; на каждую размеченную
    строку (``reserves``: article_id, amount, employee_id) создаётся резерв ``SafeAllocation``
    со статьёй/получателем. Фактический расход — позже, по «Выплачено» (``pay_allocation``):
    out-проводка ``safe_payout``, а у зарплатного резерва (``employee_id``) ещё и
    ``EmployeePayout`` под сотрудника — тогда расчёт ЗП учтёт «уже выплачено».

    Идемпотентно: прежние НЕоплаченные резервы этой операции снимаем; если по любому уже была
    оплата — блокируем (деньги с Сейфа уже выданы)."""
    prior = (
        await session.scalars(
            select(SafeAllocation).where(SafeAllocation.source_operation_id == operation.id)
        )
    ).all()
    for allocation in prior:
        if Decimal(allocation.amount_paid) > 0 or allocation.status in ("paid", "partially_paid"):
            raise ValueError(
                "По этой операции уже была выплата с Сейфа — повторный разбор невозможен"
            )
    for allocation in prior:
        await session.delete(allocation)
    if prior:
        await session.flush()

    await book_safe_topup(session, operation)  # транзит р/с→Сейф (идемпотентно чистит свои ноги)
    safe_wallet = await session.scalar(
        select(Wallet).where(Wallet.code == SAFE_WALLET_CODE, Wallet.status == "active")
    )
    if safe_wallet is None:
        raise ValueError("Не найден кошелёк «Сейф»")
    created: list[SafeAllocation] = []
    for article_id, amount, employee_id in reserves:
        created.append(
            await create_allocation(
                session,
                wallet_id=safe_wallet.id,
                amount=amount,
                free_amount=None,  # транзит только что пополнил Сейф на всю сумму операции
                article_id=article_id,
                counterparty_id=counterparty_id,
                employee_id=employee_id,
                purpose=operation.payment_purpose,
                source_operation_id=operation.id,
                created_by_user_id=created_by_user_id,
            )
        )
    return created


async def pay_allocation(
    session: AsyncSession,
    allocation: SafeAllocation,
    *,
    amount: Decimal,
    operation_date: date,
    source_kind: str = SAFE_PAYOUT_SOURCE_KIND,
    created_by_user_id: UUID | None = None,
) -> UUID:
    """Провести оплату резерва (полную или частичную): out-проводка + обновить статус.

    Кошелёк ноги — текущий ``allocation.wallet_id``: у резерва на Сейфе это Сейф,
    у переданного в кассу — касса (``source_kind='kassa_target_payout'``).
    """
    if allocation.status == "cancelled":
        raise ValueError("Резерв отменён — оплата невозможна")
    if allocation.status == "paid":
        raise ValueError("Резерв уже полностью оплачен")
    if amount <= 0:
        raise ValueError("Сумма оплаты должна быть больше нуля")
    outstanding = Decimal(allocation.amount) - Decimal(allocation.amount_paid)
    if amount > outstanding:
        raise ValueError(f"Сумма оплаты ({amount}) больше остатка резерва ({outstanding})")

    leg = CashflowTransaction(
        wallet_id=allocation.wallet_id,
        direction="out",
        amount=amount,
        operation_date=operation_date,
        article_id=allocation.article_id,
        counterparty_id=allocation.counterparty_id,
        source_kind=source_kind,
        source_id=allocation.id,
        payment_purpose=allocation.purpose,
        quality_status="final",
        created_by_user_id=created_by_user_id,
    )
    session.add(leg)
    allocation.amount_paid = Decimal(allocation.amount_paid) + amount
    allocation.status = (
        "paid" if allocation.amount_paid >= Decimal(allocation.amount) else "partially_paid"
    )
    await session.flush()

    # Зарплатная целёвка «через Сейф»: фактическая выплата сотруднику — здесь (не при разборе).
    # Заводим EmployeePayout(«выплачено») на эту (в т.ч. частичную) оплату → расчёт ЗП учтёт.
    if allocation.employee_id is not None:
        from app.services.payroll_admin import resolve_employee_payout_kind

        payout_kind = await resolve_employee_payout_kind(
            session, allocation.employee_id, as_of=operation_date
        )
        session.add(
            EmployeePayout(
                employee_id=allocation.employee_id,
                kind=payout_kind,
                amount=amount,
                payout_date=operation_date,
                wallet_id=allocation.wallet_id,
                article_id=allocation.article_id,
                cashflow_transaction_id=leg.id,
                safe_allocation_id=allocation.id,
                status="paid",
                created_by_user_id=created_by_user_id,
            )
        )
        await session.flush()
    return leg.id


async def cancel_allocation(session: AsyncSession, allocation: SafeAllocation) -> None:
    """Отменить резерв. Оплаченные ноги остаются, неоплаченный остаток освобождается.

    Для целёвки, переданной в кассу, отмена работает так же: резерв снимается,
    деньги остаются в кассе свободными наличными, обратных проводок нет
    (возврат на Сейф — руками обычным переводом, вне этого контура).
    """
    if allocation.status == "paid":
        raise ValueError("Нельзя отменить полностью оплаченный резерв")
    if allocation.status == "cancelled":
        return
    allocation.status = "cancelled"
    await session.flush()


async def kassa_targets_total(session: AsyncSession, kassa_wallet_id: UUID) -> Decimal:
    """Σ непогашенных остатков целёвок, переданных в кассу («целевые в кассе»)."""
    outstanding = func.sum(SafeAllocation.amount - SafeAllocation.amount_paid)
    total = await session.scalar(
        select(func.coalesce(outstanding, 0)).where(
            SafeAllocation.wallet_id == kassa_wallet_id,
            SafeAllocation.location == "kassa",
            SafeAllocation.status.in_(ACTIVE_RESERVE_STATUSES),
        )
    )
    return Decimal(total or 0)


async def kassa_targets_count(session: AsyncSession, kassa_wallet_id: UUID) -> int:
    """Число активных целёвок в кассе — для бейджа «К выдаче» и плитки кассы."""
    count = await session.scalar(
        select(func.count())
        .select_from(SafeAllocation)
        .where(
            SafeAllocation.wallet_id == kassa_wallet_id,
            SafeAllocation.location == "kassa",
            SafeAllocation.status.in_(ACTIVE_RESERVE_STATUSES),
        )
    )
    return int(count or 0)


async def allocation_advance_draft_id(
    session: AsyncSession, allocation_id: UUID
) -> UUID | None:
    """Черновик банк-выдачи аванса, привязанный к резерву (таким передача запрещена)."""
    return await session.scalar(
        select(SalaryAdvanceBankDraft.id).where(
            SalaryAdvanceBankDraft.safe_allocation_id == allocation_id
        )
    )


async def transfer_allocation_to_kassa(
    session: AsyncSession,
    allocation: SafeAllocation,
    *,
    operation_date: date,
) -> list[UUID]:
    """Передать целёвку в кассу: перемещение ВСЕГО остатка Сейф → касса + смена локации.

    Двухногий перевод (по механике снятия наличных) на непогашенный остаток резерва —
    частичная передача запрещена by design (частичной бывает только выдача из кассы).
    Резерв меняет ``wallet_id`` на кассу и ``location`` на ``kassa``: он перестаёт
    считаться в резервах Сейфа (инвариант баланса Сейфа сходится, свободно не меняется)
    и появляется в «целевых в кассе». Вызывающий обязан держать row-lock резерва
    (повторная передача отсекается проверкой локации).
    """
    if allocation.status not in ACTIVE_RESERVE_STATUSES:
        raise ValueError("Передать в кассу можно только активный резерв")
    if allocation.location != "safe":
        raise ValueError("Резерв уже передан в кассу")
    outstanding = Decimal(allocation.amount) - Decimal(allocation.amount_paid)
    if outstanding <= 0:
        raise ValueError("У резерва нет непогашенного остатка")
    # Страховка идемпотентности поверх row-lock: одна целёвка — одно перемещение.
    existing = await session.scalar(
        select(CashflowTransaction.id).where(
            CashflowTransaction.source_kind == ALLOCATION_TO_KASSA_SOURCE_KIND,
            CashflowTransaction.source_id == allocation.id,
        )
    )
    if existing is not None:
        raise ValueError("Резерв уже передан в кассу")
    dest_wallet = await session.scalar(
        select(Wallet).where(
            Wallet.code == CASH_WITHDRAWAL_WALLET_CODE, Wallet.status == "active"
        )
    )
    if dest_wallet is None:
        raise ValueError("Не найдена наличная касса для передачи")
    out_article = await _article_id(session, TRANSFER_OUT_ARTICLE_CODE)
    in_article = await _article_id(session, TRANSFER_IN_ARTICLE_CODE)
    label = allocation.purpose or "целевой резерв"
    purpose = f"Передача целёвки в кассу: {label}"
    out_leg = CashflowTransaction(
        wallet_id=allocation.wallet_id,
        direction="out",
        amount=outstanding,
        operation_date=operation_date,
        article_id=out_article,
        source_kind=ALLOCATION_TO_KASSA_SOURCE_KIND,
        source_id=allocation.id,
        payment_purpose=purpose,
        quality_status="final",
    )
    in_leg = CashflowTransaction(
        wallet_id=dest_wallet.id,
        direction="in",
        amount=outstanding,
        operation_date=operation_date,
        article_id=in_article,
        source_kind=ALLOCATION_TO_KASSA_SOURCE_KIND,
        source_id=allocation.id,
        payment_purpose=purpose,
        quality_status="final",
    )
    session.add(out_leg)
    session.add(in_leg)
    allocation.wallet_id = dest_wallet.id
    allocation.location = "kassa"
    await session.flush()
    return [out_leg.id, in_leg.id]


async def book_safe_cash_withdrawal(
    session: AsyncSession,
    *,
    safe_wallet: Wallet,
    amount: Decimal,
    operation_date: date,
) -> list[UUID]:
    """Снятие наличных с карты «Сейф» → касса Черникова (двухногий перевод между счетами).

    Не расход, а перемещение: out с Сейфа + in в наличную кассу. Деньги остаются в
    учётном контуре до фактической траты и не искажают сверку остатка карты.
    """
    if amount <= 0:
        raise ValueError("Сумма снятия должна быть больше нуля")
    dest_wallet = await session.scalar(
        select(Wallet).where(
            Wallet.code == CASH_WITHDRAWAL_WALLET_CODE, Wallet.status == "active"
        )
    )
    if dest_wallet is None:
        raise ValueError("Не найдена наличная касса для снятия")
    out_article = await _article_id(session, TRANSFER_OUT_ARTICLE_CODE)
    in_article = await _article_id(session, TRANSFER_IN_ARTICLE_CODE)
    source_id = uuid4()
    purpose = "Снятие наличных с Сейфа в кассу"
    out_leg = CashflowTransaction(
        wallet_id=safe_wallet.id,
        direction="out",
        amount=amount,
        operation_date=operation_date,
        article_id=out_article,
        source_kind=SAFE_CASH_WITHDRAWAL_SOURCE_KIND,
        source_id=source_id,
        payment_purpose=purpose,
        quality_status="final",
    )
    in_leg = CashflowTransaction(
        wallet_id=dest_wallet.id,
        direction="in",
        amount=amount,
        operation_date=operation_date,
        article_id=in_article,
        source_kind=SAFE_CASH_WITHDRAWAL_SOURCE_KIND,
        source_id=source_id,
        payment_purpose=purpose,
        quality_status="final",
    )
    session.add(out_leg)
    session.add(in_leg)
    await session.flush()
    return [out_leg.id, in_leg.id]


async def book_safe_drift_adjustment(
    session: AsyncSession,
    *,
    safe_wallet: Wallet,
    delta: Decimal,
    operation_date: date,
) -> UUID:
    """Выровнять учётный баланс Сейфа на реальный при сверке (``delta`` = реальный − учётный).

    ``delta`` > 0 — излишек (нераспознанное поступление) → in «Прочие поступления»;
    ``delta`` < 0 — недостача (трата мимо учёта) → out «Прочие расходы».
    """
    if delta == 0:
        raise ValueError("Нет расхождения для корректировки")
    if delta > 0:
        direction = "in"
        article = await _article_id(session, DRIFT_SURPLUS_ARTICLE_CODE)
    else:
        direction = "out"
        article = await _article_id(session, DRIFT_SHORTAGE_ARTICLE_CODE)
    leg = CashflowTransaction(
        wallet_id=safe_wallet.id,
        direction=direction,
        amount=abs(delta),
        operation_date=operation_date,
        article_id=article,
        source_kind=SAFE_RECONCILE_ADJUST_SOURCE_KIND,
        source_id=uuid4(),
        payment_purpose="Корректировка остатка Сейфа по сверке с картой",
        quality_status="final",
    )
    session.add(leg)
    await session.flush()
    return leg.id
