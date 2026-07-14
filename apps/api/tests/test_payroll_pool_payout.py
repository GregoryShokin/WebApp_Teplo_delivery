"""Выплата ЗП из пулов-резервов, привязанных к ведомости: раскладка, переток, ДДС, solvency.

Money-path: резерв-контейнер (Сейф/касса), проводки через apply_pool_tranche → ДДС из
кошелька пула, переток Сейф↔касса, отсутствие задвоения ДДС через booked_amount.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_payroll_payments import create_actor_user, create_payroll_run

from app.models import Account, CashflowTransaction, PayrollPayment, SafeAllocation, Wallet
from app.services.payroll_payouts import (
    MOCK_PAYER_ACCOUNT,
    PAYROLL_PAYOUT_SOURCE_KIND,
    book_bank_to_safe_transfer,
    set_run_payout_cash,
)
from app.services.payroll_reserves import (
    pay_run_from_pool,
    run_solvency,
)

pytestmark = pytest.mark.asyncio

PAID_AT = date(2026, 5, 27)
OP_DATE = date(2026, 5, 26)
# Три сотрудника 1000/2000/3000 — базовый набор для раскладки пула.
THREE = [[Decimal("1000")], [Decimal("2000")], [Decimal("3000")]]


async def _seed_bank_payer(session: AsyncSession) -> None:
    """Расчётный счёт ИП + банковский кошелёк-плательщик (нужен для транзита банк→Сейф)."""
    account = Account(
        id=uuid.uuid4(),
        bank_code="tbank",
        account_number=MOCK_PAYER_ACCOUNT,
        legal_entity="ИП Шокина Е.А.",
        status="active",
    )
    session.add(account)
    await session.flush()
    session.add(
        Wallet(
            id=uuid.uuid4(),
            code=f"bank-pp-{uuid.uuid4().hex[:8]}",
            name="Тест банк (плательщик)",
            type="bank",
            status="active",
            account_id=account.id,
            opening_balance=Decimal("0"),
        )
    )
    await session.flush()


async def _wallet_id(session: AsyncSession, code: str) -> uuid.UUID:
    return await session.scalar(
        select(Wallet.id).where(Wallet.code == code, Wallet.status == "active")
    )


async def _reserve(
    session: AsyncSession, run_id: uuid.UUID, location: str
) -> SafeAllocation | None:
    return await session.scalar(
        select(SafeAllocation).where(
            SafeAllocation.source_run_id == run_id,
            SafeAllocation.location == location,
            SafeAllocation.status != "cancelled",
        )
    )


async def _dds_out(
    session: AsyncSession, run_id: uuid.UUID, wallet_id: uuid.UUID | None = None
) -> Decimal:
    stmt = select(func.coalesce(func.sum(CashflowTransaction.amount), 0)).where(
        CashflowTransaction.source_kind == PAYROLL_PAYOUT_SOURCE_KIND,
        CashflowTransaction.source_id == run_id,
        CashflowTransaction.direction == "out",
    )
    if wallet_id is not None:
        stmt = stmt.where(CashflowTransaction.wallet_id == wallet_id)
    return Decimal(await session.scalar(stmt) or 0)


async def _payment(session: AsyncSession, run_id: uuid.UUID, emp_id: uuid.UUID) -> PayrollPayment:
    return await session.scalar(
        select(PayrollPayment).where(
            PayrollPayment.run_id == run_id, PayrollPayment.employee_id == emp_id
        )
    )


async def _setup_run_with_reserves(
    session: AsyncSession,
    *,
    totals: list[list[Decimal]],
    cash: Decimal,
) -> tuple[uuid.UUID, list[uuid.UUID], uuid.UUID]:
    """Финализированная ведомость + касса-резерв (нал) + Сейф-резерв (безнал через транзит).

    Возвращает (run_id, employee_ids, actor_id).
    """
    await _seed_bank_payer(session)
    actor = await create_actor_user(session)
    _period, run, employees = await create_payroll_run(session, employee_line_totals=totals)
    await set_run_payout_cash(
        session, run.id, amount_cash=cash, cash_wallet_code="tk_chernikova", actor_user_id=actor.id
    )
    await book_bank_to_safe_transfer(session, run, operation_date=OP_DATE)
    await session.commit()
    return run.id, [e.id for e in employees], actor.id


async def test_reserves_created_at_finalize_and_transit(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        run_id, _emps, _actor = await _setup_run_with_reserves(
            session, totals=THREE, cash=Decimal("2000")
        )
    async with async_session_factory() as session:
        safe = await _reserve(session, run_id, "safe")
        kassa = await _reserve(session, run_id, "kassa")
        assert safe is not None and safe.amount == Decimal("4000.00")  # 6000 − 2000 нал
        assert safe.employee_id is None and safe.source_run_id == run_id
        assert kassa is not None and kassa.amount == Decimal("2000.00")
        assert kassa.employee_id is None


async def test_pool_safe_then_kassa_overflow_pays_everyone(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Сейф-пул 4000 гасит меньших, переток на касса-пул 2000 добивает — все выплачены."""
    async with async_session_factory() as session:
        run_id, emps, actor = await _setup_run_with_reserves(
            session, totals=THREE, cash=Decimal("2000")
        )
        safe = await _reserve(session, run_id, "safe")
        result = await pay_run_from_pool(
            session, reserve_id=safe.id, paid_at=PAID_AT, actor_user_id=actor
        )
    assert result.primary_booked == Decimal("4000.00")
    assert result.overflow_booked == Decimal("2000.00")
    assert result.overflow_reserve_id is not None

    async with async_session_factory() as session:
        # Все трое полностью выплачены.
        expected_amounts = [Decimal("1000.00"), Decimal("2000.00"), Decimal("3000.00")]
        for emp, expected in zip(emps, expected_amounts, strict=True):
            p = await _payment(session, run_id, emp)
            assert p.amount == expected, (emp, p.amount)
            assert p.status == "paid"
            assert p.booked_amount == expected  # нет задвоения
        # ДДС: 4000 с Сейфа + 2000 с кассы = 6000, без задвоения.
        safe_wid = await _wallet_id(session, "cash_safe")
        kassa_wid = await _wallet_id(session, "tk_chernikova")
        assert await _dds_out(session, run_id, safe_wid) == Decimal("4000.00")
        assert await _dds_out(session, run_id, kassa_wid) == Decimal("2000.00")
        assert await _dds_out(session, run_id) == Decimal("6000.00")
        # Резервы полностью потрачены.
        safe = await _reserve(session, run_id, "safe")
        kassa = await _reserve(session, run_id, "kassa")
        assert safe.status == "paid" and safe.amount_paid == Decimal("4000.00")
        assert kassa.status == "paid" and kassa.amount_paid == Decimal("2000.00")


async def test_pool_boundary_leaves_debt_when_no_overflow(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Один Сейф-пул 4000, наличных нет: E3 недополучает (partially_paid), долг 2000."""
    async with async_session_factory() as session:
        run_id, emps, actor = await _setup_run_with_reserves(
            session, totals=THREE, cash=Decimal("0")
        )
        safe = await _reserve(session, run_id, "safe")
        assert safe.amount == Decimal("6000.00")  # всё безналом
        # Урежем пул искусственно до 4000, чтобы проверить долг (эмулируем нехватку).
        safe.amount = Decimal("4000.00")
        await session.commit()
        result = await pay_run_from_pool(
            session, reserve_id=safe.id, paid_at=PAID_AT, actor_user_id=actor
        )
    assert result.primary_booked == Decimal("4000.00")
    assert result.overflow_reserve_id is None

    async with async_session_factory() as session:
        p1 = await _payment(session, run_id, emps[0])
        p2 = await _payment(session, run_id, emps[1])
        p3 = await _payment(session, run_id, emps[2])
        assert p1.status == "paid" and p1.amount == Decimal("1000.00")
        assert p2.status == "paid" and p2.amount == Decimal("2000.00")
        assert p3.status == "partially_paid" and p3.amount == Decimal("1000.00")  # 3000 − 2000 долг
        assert await _dds_out(session, run_id) == Decimal("4000.00")


async def test_pool_selected_subset_only(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        run_id, emps, actor = await _setup_run_with_reserves(
            session, totals=THREE, cash=Decimal("0")
        )
        safe = await _reserve(session, run_id, "safe")
        # Платим только E1 и E3 из безналичного пула.
        await pay_run_from_pool(
            session,
            reserve_id=safe.id,
            selected_ids={emps[0], emps[2]},
            allow_overflow=False,
            paid_at=PAID_AT,
            actor_user_id=actor,
        )
    async with async_session_factory() as session:
        assert (await _payment(session, run_id, emps[0])).amount == Decimal("1000.00")
        assert await _payment(session, run_id, emps[1]) is None  # E2 не выбран
        assert (await _payment(session, run_id, emps[2])).amount == Decimal("3000.00")


async def test_pool_repeat_no_double_book(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Повторная выплата из уже опустошённого пула ничего не задваивает."""
    async with async_session_factory() as session:
        run_id, emps, actor = await _setup_run_with_reserves(
            session, totals=[[Decimal("1000")], [Decimal("2000")]], cash=Decimal("0")
        )
        safe = await _reserve(session, run_id, "safe")
        await pay_run_from_pool(session, reserve_id=safe.id, paid_at=PAID_AT, actor_user_id=actor)
    async with async_session_factory() as session:
        assert await _dds_out(session, run_id) == Decimal("3000.00")
        safe = await _reserve(session, run_id, "safe")
        assert safe.status == "paid"


async def test_pool_manual_boundary_override(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Ручной граничный E1 (маленькая ЗП) уходит в конец: E2,E3 гасятся, E1 получает остаток."""
    async with async_session_factory() as session:
        run_id, emps, actor = await _setup_run_with_reserves(
            session, totals=THREE, cash=Decimal("0")
        )
        safe = await _reserve(session, run_id, "safe")
        safe.amount = Decimal("5500.00")  # хватает на E2(2000)+E3(3000)=5000, E1 остаётся 500
        await session.commit()
        await pay_run_from_pool(
            session,
            reserve_id=safe.id,
            boundary_override=emps[0],
            allow_overflow=False,
            paid_at=PAID_AT,
            actor_user_id=actor,
        )
    async with async_session_factory() as session:
        assert (await _payment(session, run_id, emps[1])).status == "paid"
        assert (await _payment(session, run_id, emps[2])).status == "paid"
        p1 = await _payment(session, run_id, emps[0])
        assert p1.status == "partially_paid" and p1.amount == Decimal("500.00")


async def test_reserve_reconciles_when_paid_via_register(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Выплата через реестр (mark_partial, БЕЗ pay_wallet_id) тоже двигает пул-резерв через
    сверку — иначе он застрял бы в partially_paid с фантомным earmark'ом."""
    from app.services.payroll_payments import mark_partial_payment

    async with async_session_factory() as session:
        run_id, emps, actor = await _setup_run_with_reserves(
            session, totals=[[Decimal("1000")]], cash=Decimal("0")
        )
        safe = await _reserve(session, run_id, "safe")
        assert safe.amount == Decimal("1000.00")
        # Частичная выплата через реестр (не через пул).
        await mark_partial_payment(
            session, run_id, emps[0], amount=Decimal("600"), paid_at=PAID_AT, actor_user_id=actor
        )
    async with async_session_factory() as session:
        safe = await _reserve(session, run_id, "safe")
        assert safe.status == "partially_paid" and safe.amount_paid == Decimal("600.00")
    async with async_session_factory() as session:
        # Доплата остатка через реестр → резерв полностью закрыт (не застрял).
        await mark_partial_payment(
            session, run_id, emps[0], amount=None, paid_at=PAID_AT, actor_user_id=actor
        )
    async with async_session_factory() as session:
        safe = await _reserve(session, run_id, "safe")
        assert safe.status == "paid" and safe.amount_paid == Decimal("1000.00")


async def test_reserve_reconciles_across_register_then_pool(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Смешанный путь: реестр частично + пул остаток → резерв закрывается, без рассинхрона."""
    from app.services.payroll_payments import mark_partial_payment

    async with async_session_factory() as session:
        run_id, emps, actor = await _setup_run_with_reserves(
            session, totals=[[Decimal("1000")]], cash=Decimal("0")
        )
        await mark_partial_payment(
            session, run_id, emps[0], amount=Decimal("600"), paid_at=PAID_AT, actor_user_id=actor
        )
        safe = await _reserve(session, run_id, "safe")
        await pay_run_from_pool(session, reserve_id=safe.id, paid_at=PAID_AT, actor_user_id=actor)
    async with async_session_factory() as session:
        safe = await _reserve(session, run_id, "safe")
        assert safe.status == "paid" and safe.amount_paid == Decimal("1000.00")
        assert (await _payment(session, run_id, emps[0])).amount == Decimal("1000.00")
        assert await _dds_out(session, run_id) == Decimal("1000.00")  # ДДС не задвоился


async def test_ensure_reserve_after_paid_no_integrity_error(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Полностью оплаченный пул-резерв НЕ занимает слот уникума → пересборка не ловит 500."""
    from app.models import PayrollRun
    from app.services.payroll_reserves import ensure_run_kassa_reserve

    async with async_session_factory() as session:
        run_id, _emps, actor = await _setup_run_with_reserves(
            session, totals=[[Decimal("1000")]], cash=Decimal("1000")
        )
        kassa = await _reserve(session, run_id, "kassa")
        await pay_run_from_pool(session, reserve_id=kassa.id, paid_at=PAID_AT, actor_user_id=actor)
    async with async_session_factory() as session:
        paid = await _reserve(session, run_id, "kassa")
        assert paid.status == "paid"
        # Повторный ensure (смена сплита после оплаты) — новый активный резерв, без IntegrityError.
        run = await session.get(PayrollRun, run_id)
        again = await ensure_run_kassa_reserve(session, run, cash_amount=Decimal("500"))
        await session.commit()
        assert again is not None and again.id != paid.id and again.status == "reserved"


async def test_pay_employee_from_reserve_custom_amount(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Карандаш-контур: ручная сумма одному сотруднику, остаток лежит резервом для следующей."""
    from app.services.payroll_reserves import pay_employee_from_reserve

    async with async_session_factory() as session:
        # THREE = 1000/2000/3000 (ФОТ 6000); всё наличными → касса-резерв 6000.
        run_id, emps, actor = await _setup_run_with_reserves(
            session, totals=THREE, cash=Decimal("6000")
        )
        kassa = await _reserve(session, run_id, "kassa")
        # Повару 1 (начислено 1000) выплачиваем вручную 500.
        res = await pay_employee_from_reserve(
            session, reserve_id=kassa.id, employee_id=emps[0],
            amount=Decimal("500"), paid_at=PAID_AT, actor_user_id=actor,
        )
    assert res.booked == Decimal("500.00")
    assert res.employee_total_paid == Decimal("500.00")
    assert res.employee_remaining == Decimal("500.00")
    assert res.reserve_status == "partially_paid"
    assert res.reserve_outstanding == Decimal("5500.00")  # 6000 − 500

    async with async_session_factory() as session:
        p = await _payment(session, run_id, emps[0])
        assert p.status == "partially_paid" and p.amount == Decimal("500.00")
        assert await _dds_out(session, run_id) == Decimal("500.00")
        # Доплата остатка тем же карандашом.
        kassa = await _reserve(session, run_id, "kassa")
        res2 = await pay_employee_from_reserve(
            session, reserve_id=kassa.id, employee_id=emps[0],
            amount=Decimal("500"), paid_at=PAID_AT, actor_user_id=actor,
        )
    assert res2.employee_total_paid == Decimal("1000.00")
    assert res2.employee_remaining == Decimal("0.00")


async def test_pay_employee_from_reserve_rejects_over_reserve(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app.services.payroll_reserves import PayrollConflictError, pay_employee_from_reserve

    async with async_session_factory() as session:
        run_id, emps, actor = await _setup_run_with_reserves(
            session, totals=[[Decimal("50000")]], cash=Decimal("10000")
        )
        kassa = await _reserve(session, run_id, "kassa")  # пул 10000
        with pytest.raises(PayrollConflictError, match="В резерве осталось"):
            await pay_employee_from_reserve(
                session, reserve_id=kassa.id, employee_id=emps[0],
                amount=Decimal("15000"), paid_at=PAID_AT, actor_user_id=actor,
            )


async def test_kassa_target_payout_rejects_run_pool_reserve(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Пул-резерв ЗП нельзя выдать через «К выдаче» кассы (pay_allocation книжил бы лишний
    cashflow мимо PayrollPayment) — только через окно ведомости."""
    from app.services.kassa.payouts import KassaPayoutError, pay_kassa_target

    async with async_session_factory() as session:
        run_id, _emps, actor = await _setup_run_with_reserves(
            session, totals=[[Decimal("1000")]], cash=Decimal("1000")
        )
        kassa = await _reserve(session, run_id, "kassa")
        with pytest.raises(KassaPayoutError, match="через окно ведомости"):
            await pay_kassa_target(
                session, allocation_id=kassa.id, amount=Decimal("500"), actor_user_id=actor
            )


async def test_paid_run_bank_draft_not_active(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Черновик выплаты полностью оплаченной ведомости не висит «Отправлен в банк» (баг с
    застрявшим created/updated при оплате иным путём)."""
    import uuid as _uuid

    from app.models import PayrollBankDraft, PayrollPayment
    from app.services.payments_aggregator import _payroll_bank_draft_items

    async def _draft_item(run_id: uuid.UUID):
        async with async_session_factory() as s:
            items = await _payroll_bank_draft_items(s)
            return next(i for i in items if i.extra.get("run_id") == str(run_id))

    async with async_session_factory() as session:
        run_id, emps, _actor = await _setup_run_with_reserves(
            session, totals=THREE, cash=Decimal("0")
        )
        session.add(
            PayrollBankDraft(
                id=_uuid.uuid4(),
                run_id=run_id,
                document_id=f"doc-{run_id}",
                amount=Decimal("6000"),
                status="created",
                bank_provider="tbank",
            )
        )
        await session.commit()
    # Ещё не выплачено → «в банке».
    assert (await _draft_item(run_id)).state == "in_bank"

    async with async_session_factory() as session:
        draft = await session.scalar(
            select(PayrollBankDraft).where(PayrollBankDraft.run_id == run_id)
        )
        assert draft is not None
        draft.status = "deleted"
        draft.last_error = "Черновик удалён в банке"
        await session.commit()
    # Банк подтвердил удаление, деньги ещё не выдавались → можно отправить ведомость повторно.
    ready = await _draft_item(run_id)
    assert ready.state == "ready_to_send"
    assert ready.bucket == "bank_ready"
    assert ready.can_send_to_bank is True
    assert ready.extra["last_error"] == "Черновик удалён в банке"

    async with async_session_factory() as session:
        for emp, amt in zip(emps, [Decimal("1000"), Decimal("2000"), Decimal("3000")], strict=True):
            session.add(
                PayrollPayment(
                    id=_uuid.uuid4(),
                    run_id=run_id,
                    employee_id=emp,
                    amount=amt,
                    booked_amount=amt,
                    status="paid",
                )
            )
        await session.commit()
    # Полностью выплачено → черновик историчен (paid), из активных уходит.
    assert (await _draft_item(run_id)).state == "paid"


async def test_run_solvency_breakdown_is_consistent(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Формула available = Сейф+касса+банк+овердрафт − чужие резервы; shortfall = max(0, …)."""
    from app.models import PayrollRun

    async with async_session_factory() as session:
        _period, run, _emps = await create_payroll_run(
            session, employee_line_totals=[[Decimal("1000")], [Decimal("2000")]]
        )
        run = await session.get(PayrollRun, run.id)
        breakdown = await run_solvency(session, run)
    assert breakdown.required_total == Decimal("3000.00")
    assert breakdown.remaining == Decimal("3000.00")
    assert breakdown.available == (
        breakdown.safe_balance
        + breakdown.kassa_balance
        + breakdown.bank_total
        + breakdown.overdraft_limit
        - breakdown.reserved_other
    )
    expected_shortfall = max(Decimal("0"), breakdown.remaining - breakdown.available)
    assert breakdown.shortfall == expected_shortfall
    assert breakdown.solvent == (breakdown.shortfall <= 0)


async def test_run_solvency_flags_insolvent_when_required_exceeds_funds(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Ведомость крупнее доступных средств → not solvent, shortfall = required − available."""
    from app.models import PayrollRun

    async with async_session_factory() as session:
        _period, run, _emps = await create_payroll_run(
            session, employee_line_totals=[[Decimal("99000000")]]
        )
        run = await session.get(PayrollRun, run.id)
        breakdown = await run_solvency(session, run)
    assert breakdown.required_total == Decimal("99000000.00")
    assert breakdown.solvent is False
    assert breakdown.shortfall == breakdown.remaining - breakdown.available
