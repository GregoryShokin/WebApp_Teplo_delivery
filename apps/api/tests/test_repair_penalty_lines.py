"""Скрипты починки: штрафы без строки, излишки и разошедшиеся снимки ревизий.

Решение владельца 31.10.2026: прежние неудержанные долги списываются, сотрудникам их не
объяснить; у закрытой ревизии распределение — снимок, уволенные из него не пропадают.

Запускается настоящая точка входа ``async_main``: без ``--apply`` ничего не пишется, с ним
всё пишется одной транзакцией, повторный запуск ничего не меняет. Прежнее состояние базы
собирается так, как оно получалось до правки: неделю закрыли, а штраф в неё в расчёт не попал;
снимок ревизии испорчен тем пересчётом, который раньше шёл при каждом открытии.
"""

from __future__ import annotations

import sys
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_inventory_audit_snapshot import (
    BUSINESS_DATE,
    PENALTY,
    applied_audit_of_two,
    dismiss,
    stored_state,
)
from test_payroll_penalty_debt import (
    ZERO,
    add_adjustment,
    add_shift,
    assert_week_equality,
    balance_row,
    calculate,
    close_week,
    cut_outside_world,
    lines_of,
    make_actor,
    make_cook,
    only_line,
    stamp_closing_day,
    week_with_colleague,
)

from app.models import (
    AttendanceEntry,
    Employee,
    InventoryAudit,
    PayrollAdjustment,
    PayrollLine,
    PayrollPayment,
    PayrollPeriod,
    PayrollRun,
    PayrollRunEvent,
    ShiftLedgerEntry,
)
from app.scripts import repair_audit_snapshots, repair_penalty_lines
from app.services import inventory_audit_service
from app.services.inventory_audit_service import adjustment_comment
from app.services.payroll_calculator import decimal
from app.services.payroll_rounding import round_payable_down
from app.services.payroll_runner import run_payroll, unfinalize_payroll_run
from app.services.staff_balance_as_of import build_staff_balance_as_of

WRITE_OFF_NOTE = "Списано: решение владельца 31.10.2026"


@pytest.fixture(autouse=True)
def isolate_stand(monkeypatch: pytest.MonkeyPatch) -> None:
    """Весь файл идёт на стенде расчёта: выручка и «сегодня» табеля подменены."""
    cut_outside_world(monkeypatch)


@pytest.fixture
def run_script(
    monkeypatch: pytest.MonkeyPatch,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> Any:
    """Запустить скрипт его точкой входа: своя сессия, свои ключи командной строки."""

    async def run(module: Any, *args: str) -> None:
        monkeypatch.setattr(module, "AsyncSessionLocal", async_session_factory)
        monkeypatch.setattr(sys, "argv", ["repair", *args])
        await module.async_main()

    return run


async def add_excess_line(
    session: AsyncSession, run: PayrollRun, employee: Employee
) -> PayrollLine:
    """Строка закрытой недели, где штраф больше начисленного: разница потерялась на обрезке."""
    line = PayrollLine(
        id=uuid.uuid4(),
        run_id=run.id,
        employee_id=employee.id,
        role="sushi",
        base_pay=Decimal("1000.00"),
        deduction=Decimal("1299.76"),
        total_payable=ZERO,
        components={"days": [], "adjustments": {"penalties": []}},
    )
    session.add(line)
    await session.commit()
    return line


async def closed_week_with_damage(
    session: AsyncSession,
) -> tuple[PayrollPeriod, PayrollRun, Employee, Employee, list[PayrollAdjustment], PayrollLine]:
    """Закрытая неделя: у одного два штрафа без строки, у другого излишек на строке."""
    actor = await make_actor(session)
    colleague = await make_cook(session, "Коллега")
    no_line = await make_cook(session, "Штраф без строки")
    excess = await make_cook(session, "С излишком")
    week = await week_with_colleague(session, 0, colleague)
    run = await close_week(session, week, actor)
    penalties = [
        await add_adjustment(session, no_line, week.start_date, "penalty", "64.41"),
        await add_adjustment(session, no_line, week.end_date, "penalty", "775.01"),
    ]
    line = await add_excess_line(session, run, excess)
    return week, run, no_line, excess, penalties, line


async def counts(session: AsyncSession) -> dict[str, Any]:
    """Всё, что мог бы записать скрипт."""
    written_off = await session.scalar(select(func.sum(PayrollLine.penalty_debt_written_off)))
    return {
        "lines": await session.scalar(select(func.count()).select_from(PayrollLine)),
        "payments": await session.scalar(select(func.count()).select_from(PayrollPayment)),
        "events": await session.scalar(
            select(func.count())
            .select_from(PayrollRunEvent)
            .where(PayrollRunEvent.action == repair_penalty_lines.REPAIR_ACTION)
        ),
        "written_off": written_off,
    }


async def test_without_apply_nothing_is_written(
    async_session_factory: async_sessionmaker[AsyncSession],
    run_script: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async with async_session_factory() as session:
        await closed_week_with_damage(session)
        before = await counts(session)

    await run_script(repair_penalty_lines)

    async with async_session_factory() as session:
        assert await counts(session) == before
    printed = capsys.readouterr().out
    assert "строк 1, штрафов 2 на 839.42" in printed
    assert "отметок 1 на 299.76" in printed
    assert "ничего не записано" in printed


async def test_apply_writes_the_line_the_zero_payment_the_mark_and_the_event(
    async_session_factory: async_sessionmaker[AsyncSession], run_script: Any
) -> None:
    async with async_session_factory() as session:
        week, run, no_line, excess, penalties, line = await closed_week_with_damage(session)
        summary_before = dict(run.summary)
        payable_before = sum(row.total_payable for row in await lines_of(session, run))

    await run_script(repair_penalty_lines, "--apply")

    async with async_session_factory() as session:
        run = await session.get(PayrollRun, run.id)
        new_line = await only_line(session, run, no_line)
        assert new_line.role == "sushi"
        assert new_line.total_payable == ZERO
        assert new_line.deduction == Decimal("839.42")
        assert new_line.penalty_debt_written_off == Decimal("839.42")
        assert new_line.penalty_debt_out == ZERO
        assert new_line.components["days"] == []
        assert new_line.components["penalty_debt_note"] == WRITE_OFF_NOTE
        assert [item["id"] for item in new_line.components["adjustments"]["penalties"]] == [
            str(penalty.id) for penalty in penalties
        ]
        assert_week_equality([new_line])

        payment = await session.scalar(
            select(PayrollPayment).where(
                PayrollPayment.run_id == run.id,
                PayrollPayment.employee_id == no_line.id,
            )
        )
        assert payment is not None
        assert payment.amount == ZERO
        assert payment.status == "paid"
        assert payment.paid_at == week.payroll_date
        assert payment.method is None

        marked = await session.get(PayrollLine, line.id)
        assert marked.penalty_debt_written_off == Decimal("299.76")
        assert marked.total_payable == ZERO
        assert marked.deduction == Decimal("1299.76")
        assert marked.components["penalty_debt_note"] == WRITE_OFF_NOTE
        assert_week_equality([marked])

        event = await session.scalar(
            select(PayrollRunEvent).where(
                PayrollRunEvent.run_id == run.id,
                PayrollRunEvent.action == repair_penalty_lines.REPAIR_ACTION,
            )
        )
        assert event is not None
        assert "решение владельца 31.10.2026" in event.reason
        assert event.payload["new_lines"][0]["employee_id"] == str(no_line.id)
        assert event.payload["new_lines"][0]["amount"] == "839.42"
        assert event.payload["write_offs"] == [
            {"line_id": str(line.id), "employee_id": str(excess.id), "amount": "299.76"}
        ]

        # Закрытые деньги не сдвинулись: только нулевая строка и отметки.
        assert sum(row.total_payable for row in await lines_of(session, run)) == payable_before
        assert run.summary["total_payable"] == summary_before["total_payable"]
        assert run.summary["deduction"] == summary_before["deduction"] + 839.42
        assert run.summary["line_count"] == summary_before["line_count"] + 1


async def test_second_run_changes_nothing(
    async_session_factory: async_sessionmaker[AsyncSession],
    run_script: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async with async_session_factory() as session:
        await closed_week_with_damage(session)

    await run_script(repair_penalty_lines, "--apply")
    async with async_session_factory() as session:
        after_first = await counts(session)
    capsys.readouterr()

    await run_script(repair_penalty_lines, "--apply")

    async with async_session_factory() as session:
        assert await counts(session) == after_first
    assert "строк 0, штрафов 0 на 0.00; отметок 0 на 0.00" in capsys.readouterr().out


async def test_written_off_penalty_is_not_a_debt_and_not_withheld_later(
    async_session_factory: async_sessionmaker[AsyncSession], run_script: Any
) -> None:
    async with async_session_factory() as session:
        week, _run, no_line, _excess, _penalties, _line = await closed_week_with_damage(session)
        await stamp_closing_day(session, week, date(2026, 7, 15))

    await run_script(repair_penalty_lines, "--apply")

    async with async_session_factory() as session:
        report = await build_staff_balance_as_of(session, as_of=date(2026, 7, 15))
        row = balance_row(report, no_line)
        assert row is None or row.receivable == ZERO
        assert report.penalty_debt_total == ZERO

        colleague = await session.scalar(select(Employee).where(Employee.full_name == "Коллега"))
        next_week = await week_with_colleague(session, 1, colleague)
        await add_shift(session, no_line, next_week.start_date)
        run = await calculate(session, next_week)
        line = await only_line(session, run, no_line)
        assert line.penalty_debt_in == ZERO
        assert line.total_payable == Decimal("2400.00")


async def test_penalty_of_a_person_with_a_line_goes_to_review_and_is_not_written(
    async_session_factory: async_sessionmaker[AsyncSession],
    run_script: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Штраф не попал в расчёт по другой причине: это не долг без строки, а случай на глаз."""
    async with async_session_factory() as session:
        actor = await make_actor(session)
        colleague = await make_cook(session, "Коллега")
        employee = await make_cook(session, "Со сменой")
        week = await week_with_colleague(session, 0, colleague)
        await add_shift(session, employee, week.start_date)
        await close_week(session, week, actor)
        await add_adjustment(session, employee, week.start_date, "penalty", "150")
        before = await counts(session)

    await run_script(repair_penalty_lines, "--apply")

    async with async_session_factory() as session:
        assert await counts(session) == before
    printed = capsys.readouterr().out
    assert "ТРЕБУЕТ ГЛАЗ" in printed
    assert "требуют глаз 1" in printed


async def test_open_week_and_imported_records_are_left_alone(
    async_session_factory: async_sessionmaker[AsyncSession], run_script: Any
) -> None:
    async with async_session_factory() as session:
        actor = await make_actor(session)
        colleague = await make_cook(session, "Коллега")
        employee = await make_cook(session, "Без строки")
        closed = await week_with_colleague(session, 0, colleague)
        await close_week(session, closed, actor)
        open_week = await week_with_colleague(session, 1, colleague)
        await calculate(session, open_week)
        # Открытую неделю заберёт обычный расчёт.
        await add_adjustment(session, employee, open_week.start_date, "penalty", "100")
        # Заливка из старой таблицы: её штрафы в расчёт и не должны были попасть.
        imported = await add_adjustment(session, employee, closed.start_date, "penalty", "200")
        imported.created_by_label = repair_penalty_lines.LEGACY_IMPORT_LABEL
        await session.commit()
        before = await counts(session)

    await run_script(repair_penalty_lines, "--apply")

    async with async_session_factory() as session:
        assert await counts(session) == before


async def test_new_debt_and_a_covered_excess_are_not_marked(
    async_session_factory: async_sessionmaker[AsyncSession], run_script: Any
) -> None:
    """После новых закрытых недель скрипт не должен списать настоящий, идущий дальше долг."""
    async with async_session_factory() as session:
        actor = await make_actor(session)
        colleague = await make_cook(session, "Коллега")
        debtor = await make_cook(session, "Должник")
        two_roles = await make_cook(session, "Две роли", roles=("sushi", "pizza"))
        week = await week_with_colleague(session, 0, colleague)
        await add_shift(session, debtor, week.start_date)
        await add_adjustment(session, debtor, week.start_date, "penalty", "4000")
        await add_shift(session, two_roles, week.start_date, role="sushi")
        await add_shift(session, two_roles, week.end_date, role="pizza")
        await add_adjustment(session, two_roles, week.start_date, "penalty", "3000")
        run = await close_week(session, week, actor)
        before = await counts(session)

    await run_script(repair_penalty_lines, "--apply")

    async with async_session_factory() as session:
        assert await counts(session) == before
        run = await session.get(PayrollRun, run.id)
        assert (await only_line(session, run, debtor)).penalty_debt_out == Decimal("1600.00")


async def test_imported_run_with_an_excess_is_not_marked(
    async_session_factory: async_sessionmaker[AsyncSession], run_script: Any
) -> None:
    async with async_session_factory() as session:
        actor = await make_actor(session)
        colleague = await make_cook(session, "Коллега")
        employee = await make_cook(session, "Из старой таблицы")
        week = await week_with_colleague(session, 0, colleague)
        run = await close_week(session, week, actor)
        run.is_imported_legacy = True
        await session.commit()
        line = await add_excess_line(session, run, employee)

    await run_script(repair_penalty_lines, "--apply")

    async with async_session_factory() as session:
        assert (await session.get(PayrollLine, line.id)).penalty_debt_written_off == ZERO


async def repaired_week_rolled_back_and_recounted(
    session: AsyncSession, run: PayrollRun, week: PayrollPeriod
) -> PayrollRun:
    """Откатить починенную неделю и посчитать её заново тем же настоящим расчётом."""
    actor = await make_actor(session)
    await unfinalize_payroll_run(session, run.id, reason="Правка смены", actor_user_id=actor.id)
    entries = (
        await session.scalars(select(AttendanceEntry).where(AttendanceEntry.period_id == week.id))
    ).all()
    assert entries
    return await run_payroll(session, week.id)


async def test_repaired_week_keeps_the_write_off_after_a_rollback(
    async_session_factory: async_sessionmaker[AsyncSession], run_script: Any
) -> None:
    async with async_session_factory() as session:
        week, run, no_line, _excess, _penalties, _line = await closed_week_with_damage(session)

    await run_script(repair_penalty_lines, "--apply")

    async with async_session_factory() as session:
        run = await session.get(PayrollRun, run.id)
        week = await session.get(PayrollPeriod, week.id)
        recounted = await repaired_week_rolled_back_and_recounted(session, run, week)
        line = await only_line(session, recounted, no_line)
        assert line.penalty_debt_written_off == Decimal("839.42")
        assert line.penalty_debt_out == ZERO


async def test_repair_mark_keeps_the_written_off_sum(
    async_session_factory: async_sessionmaker[AsyncSession], run_script: Any
) -> None:
    async with async_session_factory() as session:
        _week, run, no_line, excess, _penalties, _line = await closed_week_with_damage(session)

    await run_script(repair_penalty_lines, "--apply")

    async with async_session_factory() as session:
        run = await session.get(PayrollRun, run.id)
        line = await only_line(session, run, no_line)
        assert line.components["repair"]["amount"] == "839.42"
        assert (await only_line(session, run, excess)).components["repair"]["amount"]


async def test_shift_added_to_a_repaired_week_leaves_the_fine_forgiven(
    async_session_factory: async_sessionmaker[AsyncSession], run_script: Any
) -> None:
    """Списано насовсем: заработок недели штраф не гасит, а без него штраф не становится долгом."""
    async with async_session_factory() as session:
        week, run, no_line, _excess, _penalties, _line = await closed_week_with_damage(session)

    await run_script(repair_penalty_lines, "--apply")

    async with async_session_factory() as session:
        actor = await make_actor(session)
        run = await session.get(PayrollRun, run.id)
        week = await session.get(PayrollPeriod, week.id)
        await unfinalize_payroll_run(session, run.id, reason="Смена", actor_user_id=actor.id)
        await add_shift(session, no_line, week.start_date)
        run = await calculate(session, week)
        line = await only_line(session, run, no_line)
        wage = decimal(line.base_pay) + decimal(line.percent_pay)
        assert line.penalty_debt_written_off == Decimal("839.42")
        assert line.penalty_debt_out == ZERO
        assert decimal(line.total_payable) == round_payable_down(wage)
        assert_week_equality(await lines_of(session, run, no_line))

        await session.execute(
            delete(ShiftLedgerEntry).where(ShiftLedgerEntry.employee_id == no_line.id)
        )
        await session.execute(
            delete(AttendanceEntry).where(AttendanceEntry.employee_id == no_line.id)
        )
        await session.commit()
        run = await calculate(session, week)
        line = await only_line(session, run, no_line)
        assert line.penalty_debt_written_off == Decimal("839.42")
        assert line.penalty_debt_out == ZERO
        assert line.total_payable == ZERO


async def test_repeated_recounts_do_not_grow_the_write_off(
    async_session_factory: async_sessionmaker[AsyncSession], run_script: Any
) -> None:
    async with async_session_factory() as session:
        week, run, no_line, _excess, _penalties, _line = await closed_week_with_damage(session)

    await run_script(repair_penalty_lines, "--apply")

    async with async_session_factory() as session:
        run = await session.get(PayrollRun, run.id)
        week = await session.get(PayrollPeriod, week.id)
        for _ in range(3):
            recounted = await repaired_week_rolled_back_and_recounted(session, run, week)
            line = await only_line(session, recounted, no_line)
            assert line.penalty_debt_written_off == Decimal("839.42")
            actor = await make_actor(session)
            await session.refresh(week)
            run = await close_week(session, week, actor)


async def test_debt_of_the_past_week_does_not_eat_the_forgiven_fine(
    async_session_factory: async_sessionmaker[AsyncSession], run_script: Any
) -> None:
    async with async_session_factory() as session:
        actor = await make_actor(session)
        colleague = await make_cook(session, "Коллега")
        debtor = await make_cook(session, "Должник")
        past = await week_with_colleague(session, 0, colleague)
        await add_adjustment(session, debtor, past.start_date, "penalty", "1600")
        await close_week(session, past, actor)
        week = await week_with_colleague(session, 1, colleague)
        run = await close_week(session, week, actor)
        await add_adjustment(session, debtor, week.start_date, "penalty", "839.42")

    await run_script(repair_penalty_lines, "--apply")

    async with async_session_factory() as session:
        actor = await make_actor(session)
        run = await session.get(PayrollRun, run.id)
        week = await session.get(PayrollPeriod, week.id)
        await unfinalize_payroll_run(session, run.id, reason="Смена", actor_user_id=actor.id)
        await add_shift(session, debtor, week.start_date)
        run = await calculate(session, week)
        line = await only_line(session, run, debtor)
        wage = decimal(line.base_pay) + decimal(line.percent_pay)
        assert line.penalty_debt_written_off == Decimal("839.42")
        assert line.penalty_debt_in == Decimal("1600.00")
        assert decimal(line.total_payable) == round_payable_down(max(wage - Decimal("1600"), ZERO))
        assert_week_equality(await lines_of(session, run, debtor))


# --- снимки применённых ревизий ------------------------------------------------------


async def spoiled_audit(
    session: AsyncSession,
) -> tuple[InventoryAudit, Employee, Employee]:
    """Снимок после увольнения пересчитан прежним расчётом: доля ушедшего легла на оставшегося."""
    audit, stays, leaves = await applied_audit_of_two(session)
    await dismiss(session, leaves)
    await inventory_audit_service._compute_penalties(session, audit.id, commit=True)
    state = await stored_state(session, audit.id)
    assert set(state["snapshot"]["employee_penalties_by_id"]) == {str(stays.id)}
    return audit, stays, leaves


async def test_audit_repair_without_apply_writes_nothing(
    async_session_factory: async_sessionmaker[AsyncSession],
    run_script: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async with async_session_factory() as session:
        audit, _stays, _leaves = await spoiled_audit(session)
        before = await stored_state(session, audit.id)

    await run_script(repair_audit_snapshots)

    async with async_session_factory() as session:
        assert await stored_state(session, audit.id) == before
    assert "будет починено 1" in capsys.readouterr().out


async def test_audit_repair_returns_the_dismissed_person_and_the_sum_equals_the_fact(
    async_session_factory: async_sessionmaker[AsyncSession],
    run_script: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async with async_session_factory() as session:
        audit, stays, leaves = await spoiled_audit(session)
        applied_at = audit.applied_at
        rows = await session.execute(
            select(PayrollAdjustment.employee_id, PayrollAdjustment.amount).where(
                PayrollAdjustment.comment == adjustment_comment(BUSINESS_DATE)
            )
        )
        fact = {str(employee_id): amount for employee_id, amount in rows.all()}
        assert set(fact) == {str(stays.id), str(leaves.id)}

    await run_script(repair_audit_snapshots, "--apply")

    async with async_session_factory() as session:
        state = await stored_state(session, audit.id)
        people = state["snapshot"]["employee_penalties_by_id"]
        assert {key: Decimal(str(value)) for key, value in people.items()} == fact
        assert state["total_penalty"] == PENALTY
        assert sum(fact.values()) == PENALTY
        assert (await session.get(InventoryAudit, audit.id)).applied_at == applied_at
        rows = await session.scalars(
            select(PayrollAdjustment.amount).where(
                PayrollAdjustment.comment == adjustment_comment(BUSINESS_DATE)
            )
        )
        assert sum(rows.all()) == PENALTY
    assert "починено 1, оставлено на глаз 0" in capsys.readouterr().out

    async with async_session_factory() as session:
        repaired = await stored_state(session, audit.id)

    await run_script(repair_audit_snapshots, "--apply")

    async with async_session_factory() as session:
        assert await stored_state(session, audit.id) == repaired
    assert "совпало 1, починено 0" in capsys.readouterr().out


async def test_audit_repair_leaves_a_mismatch_for_the_eye_and_writes_nothing(
    async_session_factory: async_sessionmaker[AsyncSession],
    run_script: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Сумма расчёта не сошлась с фактом до копейки: такую ревизию смотрит человек."""
    async with async_session_factory() as session:
        audit, stays, _leaves = await spoiled_audit(session)
        penalty = await session.scalar(
            select(PayrollAdjustment).where(
                PayrollAdjustment.employee_id == stays.id,
                PayrollAdjustment.comment == adjustment_comment(BUSINESS_DATE),
            )
        )
        penalty.amount += Decimal("0.01")
        await session.commit()
        before = await stored_state(session, audit.id)

    await run_script(repair_audit_snapshots, "--apply")

    async with async_session_factory() as session:
        assert await stored_state(session, audit.id) == before
    printed = capsys.readouterr().out
    assert "НА ГЛАЗ" in printed
    assert "оставлено на глаз 1" in printed


async def test_audit_repair_does_not_touch_a_snapshot_that_matches(
    async_session_factory: async_sessionmaker[AsyncSession],
    run_script: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async with async_session_factory() as session:
        audit, _stays, leaves = await applied_audit_of_two(session)
        await dismiss(session, leaves)
        before = await stored_state(session, audit.id)

    await run_script(repair_audit_snapshots, "--apply")

    async with async_session_factory() as session:
        assert await stored_state(session, audit.id) == before
    assert "Проверено 1, совпало 1" in capsys.readouterr().out
