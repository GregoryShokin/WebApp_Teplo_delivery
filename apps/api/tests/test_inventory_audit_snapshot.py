"""Закрытая ревизия не пересчитывается, распределение остаётся снимком.

Решение владельца 31.10.2026: у закрытой ревизии распределение — снимок, уволенные из него не
пропадают. Раньше открытие ревизии пересчитывало её заново, круг людей брался по статусу на
сегодня: уволенный выпадал, его доля раскладывалась на оставшихся, а штрафы в ведомости
оставались прежними. Экран расходился с деньгами, хотя деньги были верны.

Здесь настоящая ревизия на настоящей базе: смены в табеле, расчёт, применение со штрафами в
``payroll_adjustment``, двери ``preview_audit`` и ``compute_audit``. Черновик считается как
раньше: этим же расчётом и проверяется, что снимок закрытой ревизии живёт отдельно.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import CurrentActor
from app.api.v1.routes import inventory as inventory_routes
from app.models import (
    Employee,
    EmployeePositionAssignment,
    InventoryAudit,
    InventoryAuditItem,
    InventoryAuditPosition,
    PayrollAdjustment,
    ShiftLedgerEntry,
)
from app.services.inventory_audit_service import (
    InventoryAuditConflictError,
    adjustment_comment,
    apply_audit_penalties,
    cancel_audit,
    compute_penalties,
)

BUSINESS_DATE = date(2026, 8, 3)
PREVIOUS_AUDIT_DATE = date(2026, 7, 27)
SHIFT_DATE = date(2026, 7, 29)
# Недостача поварской группы 10 000: ставка 50 %, штраф 5 000 на всех.
SHORTAGE = Decimal("10000.00")
PENALTY = Decimal("5000.00")


def actor_stub() -> CurrentActor:
    """Сервисный вызов без пользователя: применение берёт только подпись."""
    return CurrentActor(roles=frozenset({"finance_manager"}), user_id=None)


async def make_chef(session: AsyncSession, name: str) -> Employee:
    """Повар со сменой в периоде ревизии: участие считается по факту смены."""
    employee = Employee(
        id=uuid.uuid4(),
        full_name=name,
        iiko_id=f"iiko-{uuid.uuid4()}",
        position="Повар",
        category="category_2",
        status="active",
        hire_date=date(2026, 1, 1),
    )
    session.add(employee)
    await session.flush()
    session.add(
        EmployeePositionAssignment(
            id=uuid.uuid4(),
            employee_id=employee.id,
            position="Повар",
            effective_from=date(2026, 1, 1),
        )
    )
    session.add(
        ShiftLedgerEntry(
            id=uuid.uuid4(),
            employee_id=employee.id,
            work_date=SHIFT_DATE,
            opened_at=datetime(2026, 7, 29, 7, tzinfo=UTC),
            closed_at=datetime(2026, 7, 29, 19, tzinfo=UTC),
            payroll_role="sushi",
            category="category_2",
            source="manual_correction",
            is_resolved=True,
        )
    )
    await session.commit()
    return employee


async def make_draft(session: AsyncSession, business_date: date = BUSINESS_DATE) -> InventoryAudit:
    """Черновик ревизии с одной недостачей поварской группы."""
    position = InventoryAuditPosition(
        id=uuid.uuid4(),
        code=f"test-{uuid.uuid4()}",
        display_name="Лосось",
        allocation_group="chefs",
        is_active=True,
    )
    audit = InventoryAudit(
        id=uuid.uuid4(),
        business_date=business_date,
        previous_audit_date=PREVIOUS_AUDIT_DATE,
        source="manual",
        status="draft",
    )
    session.add_all([position, audit])
    await session.flush()
    session.add(
        InventoryAuditItem(
            id=uuid.uuid4(),
            audit_id=audit.id,
            position_id=position.id,
            product_name_snapshot="Лосось",
            shortage_amount=SHORTAGE,
            amount=-SHORTAGE,
        )
    )
    await session.commit()
    return audit


async def applied_audit_of_two(
    session: AsyncSession,
) -> tuple[InventoryAudit, Employee, Employee]:
    """Применённая ревизия на двух поваров, штрафы лежат в ведомостных записях."""
    stays = await make_chef(session, "Остался")
    leaves = await make_chef(session, "Ушёл")
    audit = await make_draft(session)
    await compute_penalties(session, audit.id)
    await apply_audit_penalties(session, audit.id, actor=actor_stub())
    return audit, stays, leaves


async def dismiss(session: AsyncSession, employee: Employee) -> None:
    """Уволить: именно этот статус выбрасывал человека из пересчёта."""
    employee.status = "inactive"
    employee.fire_date = BUSINESS_DATE
    await session.commit()


async def stored_state(session: AsyncSession, audit_id: uuid.UUID) -> dict[str, Any]:
    """Что лежит в базе про ревизию, свежим чтением мимо кэша сессии."""
    audit = await session.scalar(
        select(InventoryAudit)
        .where(InventoryAudit.id == audit_id)
        .execution_options(populate_existing=True)
    )
    assert audit is not None
    return {
        "snapshot": audit.computation_snapshot,
        "total_penalty": audit.total_penalty_amount,
        "total_shortage": audit.total_shortage_amount,
        "updated_at": audit.updated_at,
    }


async def open_through_doors(session: AsyncSession, audit_id: uuid.UUID) -> dict[str, Any]:
    """Открыть ревизию и нажать перерасчёт: что отдали двери."""
    preview = await inventory_routes.preview_audit(audit_id, session, actor_stub())
    computed = await inventory_routes.compute_audit(audit_id, session, actor_stub())
    return {"preview": preview, "compute": computed}


async def test_opening_an_applied_audit_changes_nothing_after_a_dismissal(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        audit, stays, leaves = await applied_audit_of_two(session)
        await dismiss(session, leaves)
        before = await stored_state(session, audit.id)

        doors = await open_through_doors(session, audit.id)

        assert await stored_state(session, audit.id) == before
        assert before["total_penalty"] == PENALTY
        people = before["snapshot"]["employee_penalties_by_id"]
        assert set(people) == {str(stays.id), str(leaves.id)}
        assert doors["preview"]["status"] == "applied"
        assert doors["preview"]["employee_count"] == 2
        assert doors["compute"]["total_penalty_amount"] == "5000.00"
        assert doors["compute"]["employee_penalties"] == before["snapshot"]["employee_penalties"]
        assert doors["compute"]["groups"] == before["snapshot"]["groups"]


async def test_opening_a_cancelled_audit_changes_nothing(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        audit, _stays, leaves = await applied_audit_of_two(session)
        await cancel_audit(session, audit.id, actor=actor_stub())
        await dismiss(session, leaves)
        before = await stored_state(session, audit.id)

        await open_through_doors(session, audit.id)

        assert await stored_state(session, audit.id) == before


async def test_cancelled_audit_without_a_snapshot_opens_empty(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Черновик отменили, не считая: у него нет снимка, и дверь не падает."""
    async with async_session_factory() as session:
        audit = await make_draft(session)
        await cancel_audit(session, audit.id, actor=actor_stub())
        before = await stored_state(session, audit.id)
        assert before["snapshot"] is None

        computed = await inventory_routes.compute_audit(audit.id, session, actor_stub())

        assert computed["groups"] == {}
        assert computed["employee_penalties"] == []
        assert computed["total_penalty_amount"] == "0.00"
        assert await stored_state(session, audit.id) == before


async def test_opening_a_draft_recalculates_as_before(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        first = await make_chef(session, "Первый")
        audit = await make_draft(session)
        await compute_penalties(session, audit.id)
        counted = await stored_state(session, audit.id)
        assert set(counted["snapshot"]["employee_penalties_by_id"]) == {str(first.id)}

        second = await make_chef(session, "Второй")
        preview = await inventory_routes.preview_audit(audit.id, session, actor_stub())

        recounted = await stored_state(session, audit.id)
        assert set(recounted["snapshot"]["employee_penalties_by_id"]) == {
            str(first.id),
            str(second.id),
        }
        assert recounted["updated_at"] > counted["updated_at"]
        assert preview["status"] == "draft"

        await dismiss(session, second)
        await inventory_routes.compute_audit(audit.id, session, actor_stub())

        # Черновик по-прежнему не берёт уволенных: снимком он ещё не стал.
        final = await stored_state(session, audit.id)
        assert set(final["snapshot"]["employee_penalties_by_id"]) == {str(first.id)}


@pytest.mark.parametrize("status", ["applied", "cancelled"])
async def test_compute_penalties_refuses_anything_but_a_draft(
    async_session_factory: async_sessionmaker[AsyncSession], status: str
) -> None:
    async with async_session_factory() as session:
        audit, _stays, _leaves = await applied_audit_of_two(session)
        if status == "cancelled":
            await cancel_audit(session, audit.id, actor=actor_stub())
        before = await stored_state(session, audit.id)

        with pytest.raises(InventoryAuditConflictError, match="только черновик"):
            await compute_penalties(session, audit.id)

        assert await stored_state(session, audit.id) == before


async def test_doors_answer_404_for_an_unknown_audit(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        for door in (inventory_routes.preview_audit, inventory_routes.compute_audit):
            with pytest.raises(HTTPException) as missing:
                await door(uuid.uuid4(), session, actor_stub())
            assert missing.value.status_code == 404


async def test_applied_audit_penalties_stay_in_the_payroll_records(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Снимок и деньги сходятся человек в человека: на этом стоит починка снимков."""
    async with async_session_factory() as session:
        audit, _stays, leaves = await applied_audit_of_two(session)
        await dismiss(session, leaves)
        await open_through_doors(session, audit.id)

        rows = await session.execute(
            select(PayrollAdjustment.employee_id, PayrollAdjustment.amount).where(
                PayrollAdjustment.comment == adjustment_comment(BUSINESS_DATE)
            )
        )
        stored = await stored_state(session, audit.id)
        assert {str(employee_id): str(amount) for employee_id, amount in rows.all()} == {
            key: f"{Decimal(str(value)):.2f}"
            for key, value in stored["snapshot"]["employee_penalties_by_id"].items()
        }
