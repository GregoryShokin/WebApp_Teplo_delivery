"""Этап 3 кассового контура: передача целёвок в кассу, «К выдаче», разрешения.

- «Передать в кассу»: двухногое перемещение всего остатка Сейф → ТК Черникова,
  резерв меняет локацию/кошелёк, уходит из резервов Сейфа и появляется в
  «целевых в кассе»; запреты — повторная передача, неактивный резерв,
  резерв банковской выдачи аванса.
- «Выдано»: расход целёвки наличными из кассы (полный/частичный) по статье
  целёвки с контрагентом, той же семантикой оплат, что у Сейфа.
- Разрешения на авансы/займы: pending-выдача без проводок и удержаний,
  исполнение админом (проводка + активация датой подтверждения), двусторонняя
  отмена, гонки → конфликты.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    CashflowTransaction,
    Counterparty,
    DdsArticle,
    Employee,
    EmployeePositionAssignment,
    PayrollRate,
    SafeAllocation,
    SalaryAdvance,
    SalaryAdvanceBankDraft,
    Wallet,
)
from app.services.banking.classifier import SAFE_WALLET_CODE
from app.services.banking.safe_allocations import (
    ALLOCATION_TO_KASSA_SOURCE_KIND,
    KASSA_TARGET_PAYOUT_SOURCE_KIND,
    allocation_advance_draft_id,
    cancel_allocation,
    create_allocation,
    kassa_targets_count,
    kassa_targets_total,
    pay_allocation,
    safe_reserved_total,
    transfer_allocation_to_kassa,
)
from app.services.kassa.payouts import (
    KASSA_WALLET_CODE,
    KassaPayoutError,
    kassa_journal,
    kassa_pending_payload,
    kassa_today,
    pay_kassa_target,
)
from app.services.payroll_advance_service import (
    advance_payout_status,
    cancel_kassa_advance,
    disburse_kassa_advance,
    issue_advance,
    list_kassa_pending_advances,
)
from app.services.payroll_runner import PayrollConflictError

MOSCOW_TZ = ZoneInfo("Europe/Moscow")


async def _wallet(session: AsyncSession, code: str) -> Wallet:
    wallet = await session.scalar(select(Wallet).where(Wallet.code == code))
    assert wallet is not None, f"кошелёк {code} должен быть в сидах"
    return wallet


async def _expense_article(session: AsyncSession) -> DdsArticle:
    article = DdsArticle(
        code=f"target_test_{uuid.uuid4().hex[:6]}",
        name="Хозрасходы (тест целёвок)",
        movement_type="outflow",
        activity_type="operating",
    )
    session.add(article)
    await session.flush()
    return article


async def _counterparty(session: AsyncSession) -> Counterparty:
    counterparty = Counterparty(
        name=f"Поставщик целёвки {uuid.uuid4().hex[:6]}",
        type="legal_entity",
        status="active",
    )
    session.add(counterparty)
    await session.flush()
    return counterparty


async def _safe_target(
    session: AsyncSession,
    *,
    amount: str = "3000.00",
    paid: str | None = None,
) -> SafeAllocation:
    """Целёвка на Сейфе (опционально частично оплаченная)."""
    safe = await _wallet(session, SAFE_WALLET_CODE)
    article = await _expense_article(session)
    counterparty = await _counterparty(session)
    allocation = await create_allocation(
        session,
        wallet_id=safe.id,
        amount=Decimal(amount),
        free_amount=None,
        article_id=article.id,
        counterparty_id=counterparty.id,
        purpose="Накладные №7, №9 — тестовый закуп",
    )
    if paid is not None:
        await pay_allocation(
            session, allocation, amount=Decimal(paid), operation_date=date(2026, 7, 1)
        )
    return allocation


async def _tx_count(session: AsyncSession, wallet_id: uuid.UUID) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(CashflowTransaction)
            .where(CashflowTransaction.wallet_id == wallet_id)
        )
        or 0
    )


async def _make_okladnik(session: AsyncSession) -> Employee:
    employee = Employee(
        id=uuid.uuid4(),
        full_name="Окладник Целевой",
        iiko_id=f"iiko-{uuid.uuid4()}",
        status="active",
        is_senior=False,
        is_deputy_senior=False,
        pin_hash="hashed-pin",
        pin_set_at=datetime(2026, 1, 1, tzinfo=UTC),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    session.add(employee)
    await session.flush()
    session.add(
        EmployeePositionAssignment(
            id=uuid.uuid4(),
            employee_id=employee.id,
            position="Управляющий",
            effective_from=date(2026, 1, 1),
            effective_to=None,
        )
    )
    session.add(
        PayrollRate(
            id=uuid.uuid4(),
            employee_id=None,
            position_group="Управляющий",
            category="admin",
            station=None,
            rate_type="monthly",
            amount=Decimal("90000"),
            is_active=True,
            effective_from=date(2026, 1, 1),
        )
    )
    await session.flush()
    return employee


async def _kassa_pending_advance(
    session: AsyncSession, *, amount: str = "1000"
) -> SalaryAdvance:
    employee = await _make_okladnik(session)
    tk = await _wallet(session, KASSA_WALLET_CODE)
    return await issue_advance(
        session,
        employee_id=employee.id,
        amount=Decimal(amount),
        allow_loan=False,
        requested_kind="advance",
        payout_method="cash",
        wallet_id=tk.id,
        kassa_pending=True,
    )


# --------------------------------------------------------------------------- #
# «Передать в кассу»
# --------------------------------------------------------------------------- #


async def test_transfer_moves_outstanding_and_relocates(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Передача частично оплаченного резерва: переезжает остаток, инварианты сходятся."""
    async with async_session_factory() as session:
        safe = await _wallet(session, SAFE_WALLET_CODE)
        tk = await _wallet(session, KASSA_WALLET_CODE)
        allocation = await _safe_target(session, amount="3000.00", paid="1000.00")
        await session.commit()

        reserved_before = await safe_reserved_total(session, safe.id)
        targets_before = await kassa_targets_total(session, tk.id)

        legs = await transfer_allocation_to_kassa(
            session, allocation, operation_date=date(2026, 7, 6)
        )
        await session.commit()
        await session.refresh(allocation)

        # Резерв переехал: локация и кошелёк — касса, суммы не тронуты.
        assert allocation.location == "kassa"
        assert allocation.wallet_id == tk.id
        assert allocation.status == "partially_paid"
        assert Decimal(allocation.amount) == Decimal("3000.00")
        assert Decimal(allocation.amount_paid) == Decimal("1000.00")

        # Две ноги перемещения ровно на остаток 2000: out Сейф, in касса.
        rows = (
            await session.execute(
                select(CashflowTransaction).where(
                    CashflowTransaction.source_kind == ALLOCATION_TO_KASSA_SOURCE_KIND,
                    CashflowTransaction.source_id == allocation.id,
                )
            )
        ).scalars()
        by_direction = {leg.direction: leg for leg in rows}
        assert set(by_direction) == {"out", "in"}
        assert by_direction["out"].wallet_id == safe.id
        assert by_direction["in"].wallet_id == tk.id
        assert Decimal(by_direction["out"].amount) == Decimal("2000.00")
        assert Decimal(by_direction["in"].amount) == Decimal("2000.00")
        assert len(legs) == 2
        assert "Передача целёвки в кассу" in (by_direction["in"].payment_purpose or "")

        # Сейф: резервы уменьшились на остаток; касса: целевые выросли на него же.
        assert reserved_before - await safe_reserved_total(session, safe.id) == Decimal(
            "2000.00"
        )
        assert await kassa_targets_total(session, tk.id) - targets_before == Decimal(
            "2000.00"
        )
        assert await kassa_targets_count(session, tk.id) >= 1


async def test_transfer_rejects_repeat_partial_and_inactive(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Повторная передача, отменённый резерв — конфликт; частичной передачи нет by design."""
    async with async_session_factory() as session:
        allocation = await _safe_target(session, amount="500.00")
        await transfer_allocation_to_kassa(
            session, allocation, operation_date=date(2026, 7, 6)
        )
        await session.commit()

        with pytest.raises(ValueError, match="уже передан"):
            await transfer_allocation_to_kassa(
                session, allocation, operation_date=date(2026, 7, 6)
            )

        cancelled = await _safe_target(session, amount="700.00")
        await cancel_allocation(session, cancelled)
        with pytest.raises(ValueError, match="активный"):
            await transfer_allocation_to_kassa(
                session, cancelled, operation_date=date(2026, 7, 6)
            )


async def test_transfer_guard_detects_advance_linked_reserve(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Резерв банк-выдачи аванса определяется по черновику — роут отдаёт 409."""
    async with async_session_factory() as session:
        employee = await _make_okladnik(session)
        allocation = await _safe_target(session, amount="4000.00")
        advance = SalaryAdvance(
            employee_id=employee.id,
            role="Управляющий",
            kind="advance",
            amount=Decimal("4000.00"),
            per_installment_amount=Decimal("4000.00"),
            installments_count=1,
            recovered_amount=Decimal("0"),
            status="awaiting_payout",
            issued_on=date(2026, 7, 1),
        )
        session.add(advance)
        await session.flush()
        session.add(
            SalaryAdvanceBankDraft(
                advance_id=advance.id,
                document_id=f"teplo-advance-{advance.id}"[:64],
                amount=advance.amount,
                status="paid",
                safe_allocation_id=allocation.id,
                payload={},
            )
        )
        await session.commit()

        assert await allocation_advance_draft_id(session, allocation.id) is not None
        plain = await _safe_target(session, amount="100.00")
        assert await allocation_advance_draft_id(session, plain.id) is None


# --------------------------------------------------------------------------- #
# «Выдано» — выдача целёвки из кассы
# --------------------------------------------------------------------------- #


async def test_pay_kassa_target_partial_then_full(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        tk = await _wallet(session, KASSA_WALLET_CODE)
        allocation = await _safe_target(session, amount="3000.00")
        await transfer_allocation_to_kassa(
            session, allocation, operation_date=date(2026, 7, 6)
        )
        await session.commit()

        tx_id = await pay_kassa_target(
            session, allocation_id=allocation.id, amount=Decimal("1200.00")
        )
        await session.refresh(allocation)
        assert allocation.status == "partially_paid"
        assert Decimal(allocation.amount_paid) == Decimal("1200.00")

        transaction = await session.get(CashflowTransaction, tx_id)
        assert transaction is not None
        assert transaction.wallet_id == tk.id
        assert transaction.direction == "out"
        assert transaction.source_kind == KASSA_TARGET_PAYOUT_SOURCE_KIND
        assert transaction.source_id == allocation.id
        assert transaction.article_id == allocation.article_id
        assert transaction.counterparty_id == allocation.counterparty_id
        assert transaction.operation_date == kassa_today()

        # Строка сразу видна в кассовом журнале.
        journal = await kassa_journal(
            session, date_from=kassa_today(), date_to=kassa_today()
        )
        assert any(
            str(item["id"]) == str(tx_id) and item["direction"] == "out"
            for item in journal["items"]
        )
        # Шапка журнала несёт разбивку «из них целевые» и счётчик «К выдаче».
        assert journal["targets_total"] == pytest.approx(1800.0)
        assert journal["pending_count"] >= 1

        # Добор остатка — целёвка полностью выдана и уходит из «К выдаче».
        await pay_kassa_target(
            session, allocation_id=allocation.id, amount=Decimal("1800.00")
        )
        await session.refresh(allocation)
        assert allocation.status == "paid"
        assert await kassa_targets_total(session, tk.id) == Decimal("0")


async def test_pay_kassa_target_rejects_over_outstanding_and_safe_location(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        allocation = await _safe_target(session, amount="1000.00")
        await session.commit()

        # Целёвка ещё на Сейфе — из кассы не выдаётся.
        with pytest.raises(KassaPayoutError, match="не передана"):
            await pay_kassa_target(
                session, allocation_id=allocation.id, amount=Decimal("100.00")
            )

        await transfer_allocation_to_kassa(
            session, allocation, operation_date=date(2026, 7, 6)
        )
        await session.commit()
        # Больше остатка — ошибка, остаток висит нетронутым.
        with pytest.raises(KassaPayoutError, match="больше остатка"):
            await pay_kassa_target(
                session, allocation_id=allocation.id, amount=Decimal("1000.01")
            )
        await session.rollback()
        await session.refresh(allocation)
        assert Decimal(allocation.amount_paid) == Decimal("0")


async def test_cancel_target_in_kassa_releases_without_postings(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Отмена целёвки «в кассе»: резерв снят, деньги остаются свободными, проводок нет."""
    async with async_session_factory() as session:
        tk = await _wallet(session, KASSA_WALLET_CODE)
        allocation = await _safe_target(session, amount="900.00")
        await transfer_allocation_to_kassa(
            session, allocation, operation_date=date(2026, 7, 6)
        )
        await session.commit()

        tx_before = await _tx_count(session, tk.id)
        targets_before = await kassa_targets_total(session, tk.id)

        await cancel_allocation(session, allocation)
        await session.commit()
        await session.refresh(allocation)

        assert allocation.status == "cancelled"
        assert await _tx_count(session, tk.id) == tx_before  # ни одной новой проводки
        assert targets_before - await kassa_targets_total(session, tk.id) == Decimal(
            "900.00"
        )


# --------------------------------------------------------------------------- #
# Разрешения на авансы/займы через кассу
# --------------------------------------------------------------------------- #


async def test_kassa_pending_advance_moves_no_money(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Разрешение: ни проводки, ни удержаний, ни iiko — обязательство не начато."""
    async with async_session_factory() as session:
        advance = await _kassa_pending_advance(session, amount="1000")

        assert advance.status == "awaiting_payout"
        assert advance.payout_method == "cash"
        booked = await session.scalar(
            select(CashflowTransaction.id).where(
                CashflowTransaction.source_kind == "salary_advance",
                CashflowTransaction.source_id == advance.id,
            )
        )
        assert booked is None
        assert advance_payout_status(advance, None) == "awaiting_kassa"

        pending = await list_kassa_pending_advances(session)
        assert [item[0].id for item in pending] == [advance.id]

        payload = await kassa_pending_payload(session)
        assert payload["pending_count"] == 1
        assert payload["permissions"][0]["id"] == advance.id
        assert payload["permissions"][0]["employee_name"] == "Окладник Целевой"


async def test_kassa_pending_requires_tk_wallet(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        employee = await _make_okladnik(session)
        safe = await _wallet(session, SAFE_WALLET_CODE)
        with pytest.raises(PayrollConflictError, match="Торговая касса"):
            await issue_advance(
                session,
                employee_id=employee.id,
                amount=Decimal("500"),
                allow_loan=False,
                requested_kind="advance",
                wallet_id=safe.id,
                kassa_pending=True,
            )


async def test_disburse_kassa_advance_books_and_activates_today(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """«Выплачено»: проводка из ТК + активация датой подтверждения; повтор — конфликт."""
    async with async_session_factory() as session:
        tk = await _wallet(session, KASSA_WALLET_CODE)
        advance = await _kassa_pending_advance(session, amount="1000")

        disbursed = await disburse_kassa_advance(session, advance_id=advance.id)
        today_msk = datetime.now(MOSCOW_TZ).date()
        assert disbursed.status == "issued"
        assert disbursed.issued_on == today_msk  # удержания пойдут от даты выдачи

        transaction = await session.scalar(
            select(CashflowTransaction).where(
                CashflowTransaction.source_kind == "salary_advance",
                CashflowTransaction.source_id == advance.id,
            )
        )
        assert transaction is not None
        assert transaction.wallet_id == tk.id
        assert transaction.direction == "out"
        assert Decimal(transaction.amount) == Decimal("1000.00")
        assert transaction.operation_date == today_msk

        # Исполненное разрешение исчезает из «К выдаче», повторное исполнение — 409.
        assert await list_kassa_pending_advances(session) == []
        with pytest.raises(PayrollConflictError, match="уже исполнено"):
            await disburse_kassa_advance(session, advance_id=advance.id)


async def test_disburse_rejects_bank_awaiting(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Банковская awaiting-выдача не исполняется кассой — свой контур «Выплачено»."""
    async with async_session_factory() as session:
        employee = await _make_okladnik(session)
        tk = await _wallet(session, KASSA_WALLET_CODE)
        advance = SalaryAdvance(
            employee_id=employee.id,
            role="Управляющий",
            kind="advance",
            amount=Decimal("2000.00"),
            per_installment_amount=Decimal("2000.00"),
            installments_count=1,
            recovered_amount=Decimal("0"),
            status="awaiting_payout",
            issued_on=date(2026, 7, 1),
            wallet_id=tk.id,
        )
        session.add(advance)
        await session.flush()
        session.add(
            SalaryAdvanceBankDraft(
                advance_id=advance.id,
                document_id=f"teplo-advance-{advance.id}"[:64],
                amount=advance.amount,
                status="paid",
                payload={},
            )
        )
        await session.commit()

        with pytest.raises(PayrollConflictError, match="банковская"):
            await disburse_kassa_advance(session, advance_id=advance.id)
        # И в списке разрешений её нет.
        assert await list_kassa_pending_advances(session) == []


async def test_cancel_kassa_advance_two_sided(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Отмена админом ставит «отменено кассой», отзыв создателем — обычная отмена."""
    async with async_session_factory() as session:
        by_admin = await _kassa_pending_advance(session)
        cancelled = await cancel_kassa_advance(
            session, advance_id=by_admin.id, by_kassa_admin=True
        )
        assert cancelled.status == "cancelled"
        assert cancelled.kassa_cancelled_at is not None
        assert advance_payout_status(cancelled, None) == "cancelled_by_kassa"

        by_creator = await _kassa_pending_advance(session)
        revoked = await cancel_kassa_advance(
            session, advance_id=by_creator.id, by_kassa_admin=False
        )
        assert revoked.status == "cancelled"
        assert revoked.kassa_cancelled_at is None
        assert advance_payout_status(revoked, None) == "cancelled"

        # Отменённые разрешения не видны в «К выдаче».
        assert await list_kassa_pending_advances(session) == []
        # Повторная отмена (гонка двух сторон) видима как конфликт.
        with pytest.raises(PayrollConflictError, match="уже отменено"):
            await cancel_kassa_advance(session, advance_id=by_admin.id, by_kassa_admin=False)


async def test_revoke_after_disburse_conflicts(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Отзыв создателем после исполнения админом → 409 «уже выдано»."""
    async with async_session_factory() as session:
        advance = await _kassa_pending_advance(session)
        await disburse_kassa_advance(session, advance_id=advance.id)
        with pytest.raises(PayrollConflictError, match="уже исполнено"):
            await cancel_kassa_advance(
                session, advance_id=advance.id, by_kassa_admin=False
            )


async def test_pending_payload_mixes_targets_and_permissions(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """«К выдаче» = целёвки в кассе + активные разрешения; чужое не попадает."""
    async with async_session_factory() as session:
        in_kassa = await _safe_target(session, amount="2500.00")
        await transfer_allocation_to_kassa(
            session, in_kassa, operation_date=date(2026, 7, 6)
        )
        await _safe_target(session, amount="999.00")  # осталась на Сейфе — не в кассе
        advance = await _kassa_pending_advance(session)

        payload = await kassa_pending_payload(session)
        assert payload["pending_count"] == 2
        assert [target["id"] for target in payload["targets"]] == [in_kassa.id]
        target = payload["targets"][0]
        assert target["outstanding"] == pytest.approx(2500.0)
        assert target["from_bank_payout"] is False
        assert target["article_name"] is not None
        assert target["counterparty_name"] is not None
        assert [perm["id"] for perm in payload["permissions"]] == [advance.id]
        assert payload["targets_total"] == pytest.approx(2500.0)
