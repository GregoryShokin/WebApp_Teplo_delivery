from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import desc, exists, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AccumulationFundAccount,
    AccumulationFundTransaction,
    AppSetting,
    AppSettingHistory,
    AttendanceEntry,
    DepositAccount,
    DepositTransaction,
    Employee,
    PayrollLine,
    PayrollPayment,
    PayrollPeriod,
    PayrollRun,
    PayrollRunEvent,
)
from app.services import vacation_service
from app.services.accumulation_fund_service import (
    fund_outstanding,
    payout_fund_accounts_for_year,
)
from app.services.attendance_loader import load_attendance_entries
from app.services.deferred_audit_charge_service import (
    apply_pending_splits_for_run,
    collapse_deferred_charges_on_dismissal,
)
from app.services.deposit_schedule import (
    is_scheduled_payout_enabled,
    load_pending_schedules,
    load_period_schedules,
    mark_schedules_processed_for_run,
    revert_schedules_for_run,
)
from app.services.iiko_revenue import fetch_daily_revenue
from app.services.payroll_advance_recovery import (
    apply_advance_issuances,
    apply_advance_recoveries,
    apply_advance_recoveries_to_balances,
    run_has_unaccounted_advances,
)
from app.services.payroll_calculator import (
    DAILY_REVENUE_CONFIG_KEY,
    PAYROLL_RATE_SNAPSHOT_SUMMARY_KEY,
    calculate_payroll_lines,
    decimal,
    deduplicate_issues,
    load_deposit_balances_for_employees,
    load_payroll_settings,
    money,
    needs_setup_issue,
    payroll_rate_snapshot_payload,
    summarize_lines,
    vacation_role_for_employee_day,
)
from app.services.payroll_employee_payout_offset import (
    apply_employee_payout_offsets,
    apply_employee_payout_offsets_to_balances,
)
from app.services.payroll_freelancer_settlement import (
    apply_freelancer_cash_settlements,
    run_has_stale_freelancer_settlements,
)
from app.services.position_registry import production_payroll_positions


class PayrollNotFoundError(LookupError):
    pass


class PayrollConflictError(RuntimeError):
    pass


REVENUE_CACHE_DISPLAY_NAME = "Дневная выручка iiko"
MOSCOW_TZ = ZoneInfo("Europe/Moscow")

logger = logging.getLogger(__name__)


def compute_next_payroll_period_dates(today: date) -> tuple[date, date, date]:
    days_since_payday = (today.weekday() - 1) % 7
    payroll_date = today - timedelta(days=days_since_payday)
    start_date = payroll_date - timedelta(days=7)
    end_date = payroll_date - timedelta(days=1)
    return start_date, end_date, payroll_date


async def ensure_daily_revenue_cached(
    session: AsyncSession,
    date_from: date,
    date_to: date,
    *,
    force_refresh: bool = False,
) -> dict[date, Decimal]:
    setting = await session.scalar(
        select(AppSetting).where(AppSetting.key == DAILY_REVENUE_CONFIG_KEY)
    )
    cached_value = (
        setting.value if setting is not None and isinstance(setting.value, Mapping) else {}
    )
    cached = {str(key): str(value) for key, value in cached_value.items()}
    requested_dates = list(iter_dates(date_from, date_to))

    if not force_refresh and all(work_date.isoformat() in cached for work_date in requested_dates):
        return {work_date: decimal(cached[work_date.isoformat()]) for work_date in requested_dates}

    fetched = await fetch_daily_revenue(session, date_from, date_to)
    fetched_for_range = {
        work_date.isoformat(): str(fetched.get(work_date, Decimal("0")))
        for work_date in requested_dates
    }
    merged = cached | fetched_for_range
    await write_daily_revenue_cache(session, setting, merged)
    return {
        work_date: decimal(fetched_for_range[work_date.isoformat()])
        for work_date in requested_dates
    }


async def write_daily_revenue_cache(
    session: AsyncSession,
    setting: AppSetting | None,
    value: dict[str, str],
) -> None:
    old_value = setting.value if setting is not None else None
    if old_value == value:
        return

    if setting is None:
        setting = AppSetting(
            key=DAILY_REVENUE_CONFIG_KEY,
            value=value,
            value_type="object",
            category="payroll",
            display_name=REVENUE_CACHE_DISPLAY_NAME,
            description="Кэш дневной выручки из iiko OLAP для расчёта процента ЗП.",
            widget_type="json",
            widget_options=None,
            unit="₽",
        )
        session.add(setting)
        await session.flush()
    else:
        setting.value = value
        setting.updated_at = datetime.now(UTC)

    session.add(
        AppSettingHistory(
            setting_id=setting.id,
            old_value=old_value,
            new_value=value,
            changed_by_user_id=None,
        )
    )
    await session.flush()


def iter_dates(date_from: date, date_to: date) -> Iterable[date]:
    current = date_from
    while current <= date_to:
        yield current
        current += timedelta(days=1)


async def auto_create_next_period(
    session: AsyncSession,
    *,
    today: date | None = None,
) -> PayrollPeriod:
    # Сначала — самая ранняя нефинализированная неделя БЕЗ прогона. Такую заводит
    # ночной свип «окна авансов» (`refresh_current_week_advance_window`) до payday;
    # без этой ветки кнопка «рассчитать» (append-after-latest) создала бы следующую
    # неделю ПОВЕРХ пред-созданной и пропустила бы её. Когда пред-созданных недель
    # нет — ветка ничего не находит и поведение прежнее (append-after-latest).
    pending = await session.scalar(
        select(PayrollPeriod)
        .where(
            PayrollPeriod.period_type == "week",
            PayrollPeriod.status != "finalized",
            ~exists().where(PayrollRun.period_id == PayrollPeriod.id),
        )
        .order_by(PayrollPeriod.start_date)
        .limit(1)
    )
    if pending is not None:
        return pending

    existing = await session.scalar(select(PayrollPeriod).order_by(desc(PayrollPeriod.start_date)))
    if existing is None:
        start_date, end_date, payroll_date = compute_next_payroll_period_dates(
            today or datetime.now(UTC).date()
        )
    else:
        start_date = existing.end_date + timedelta(days=1)
        end_date = start_date + timedelta(days=6)
        payroll_date = end_date + timedelta(days=1)

    duplicate = await session.scalar(
        select(PayrollPeriod).where(
            PayrollPeriod.period_type == "week",
            PayrollPeriod.start_date == start_date,
            PayrollPeriod.end_date == end_date,
        )
    )
    if duplicate is not None:
        return duplicate

    period = PayrollPeriod(
        period_type="week",
        start_date=start_date,
        end_date=end_date,
        payroll_date=payroll_date,
        status="open",
    )
    session.add(period)
    await session.commit()
    await session.refresh(period)
    return period


def current_week_bounds(today: date) -> tuple[date, date, date]:
    """Границы ТЕКУЩЕЙ (идущей) недельной ЗП-недели, содержащей ``today``.

    Неделя вт→пн, выплата в следующий вторник. В отличие от
    ``compute_next_payroll_period_dates`` (даёт уже завершённую неделю К ВЫПЛАТЕ),
    здесь возвращается неделя, которая отрабатывается ПРЯМО СЕЙЧАС — её earned-to-date
    нужен для аванса среди недели. Пример: today=пт → неделя вт(этой)–пн(следующего).
    """
    days_since_tuesday = (today.weekday() - 1) % 7
    start_date = today - timedelta(days=days_since_tuesday)
    end_date = start_date + timedelta(days=6)
    payroll_date = end_date + timedelta(days=1)
    return start_date, end_date, payroll_date


async def ensure_weekly_period(
    session: AsyncSession, start_date: date, end_date: date, payroll_date: date
) -> PayrollPeriod:
    """Get-or-create недельного периода по точным границам (без commit; flush)."""
    existing = await session.scalar(
        select(PayrollPeriod).where(
            PayrollPeriod.period_type == "week",
            PayrollPeriod.start_date == start_date,
            PayrollPeriod.end_date == end_date,
        )
    )
    if existing is not None:
        return existing
    period = PayrollPeriod(
        period_type="week",
        start_date=start_date,
        end_date=end_date,
        payroll_date=payroll_date,
        status="open",
    )
    session.add(period)
    await session.flush()
    return period


async def refresh_current_week_advance_window(
    session: AsyncSession, *, today: date | None = None
) -> PayrollPeriod | None:
    """Ночной свип «окна заработанного» для авансов производственникам (повар/кассир).

    По календарю МСК определяет текущую (идущую) неделю вт→пн, гарантирует её
    недельный период и перечитывает явки iiko за уже отработанные дни. НЕ запускает
    полный расчёт (`run_payroll`) — только период + явки, из которых
    ``available_to_advance`` считает earned-to-date на лету. Депозит/фонд не
    задеваются (двигаются строго в finalize). Идемпотентно: при пустой выгрузке iiko
    загрузчик сохранённые явки не затирает; финализированную неделю не трогаем.
    """
    today = today or datetime.now(MOSCOW_TZ).date()
    start_date, end_date, payroll_date = current_week_bounds(today)
    period = await ensure_weekly_period(session, start_date, end_date, payroll_date)
    if period.status == "finalized":
        await session.commit()
        return period
    await load_attendance_entries(session, period, force_reload=True)
    # Процент за смену в earned-to-date аванса: кешируем дневную выручку iiko по
    # ЗАКРЫТЫМ (полностью прошедшим) дням недели. Сегодняшний день ещё идёт — его
    # выручку не берём (процент за него появится завтрашним свипом). Провал выгрузки
    # выручки НЕ должен потерять уже загруженные явки — глушим ошибку и логируем.
    revenue_to = min(today - timedelta(days=1), end_date)
    if start_date <= revenue_to:
        try:
            await ensure_daily_revenue_cached(session, start_date, revenue_to)
        except Exception:
            logger.exception(
                "advance-window: не удалось закешировать выручку %s–%s; явки сохраняем",
                start_date,
                revenue_to,
            )
    await session.commit()
    return period


async def run_payroll(
    session: AsyncSession,
    period_id: uuid.UUID,
    *,
    iiko_records: Iterable[Mapping[str, Any]] | None = None,
    force_refresh: bool = False,
    refresh_attendance: bool = False,
) -> PayrollRun:
    period = await session.get(PayrollPeriod, period_id)
    if period is None:
        raise PayrollNotFoundError("Payroll period not found")
    imported_run = await session.scalar(
        select(PayrollRun)
        .where(
            PayrollRun.period_id == period.id,
            PayrollRun.is_imported_legacy.is_(True),
        )
        .limit(1)
    )
    if imported_run is not None:
        raise PayrollConflictError(
            "Это импортированный период — пересчёт затрёт исторические данные"
        )
    if period.status == "finalized":
        raise PayrollConflictError("Payroll period is finalized")

    finalized_run = await session.scalar(
        select(PayrollRun).where(
            PayrollRun.period_id == period.id,
            PayrollRun.status == "finalized",
        )
    )
    if finalized_run is not None:
        raise PayrollConflictError("Payroll run is finalized")

    # Upsert: переиспользуем последний незафинализированный run для этого периода,
    # чтобы пересчёт не плодил дубликаты строк в payroll_run.
    existing_run = await session.scalar(
        select(PayrollRun)
        .where(PayrollRun.period_id == period.id)
        .order_by(PayrollRun.started_at.desc())
        .limit(1)
    )
    line_deposit_overrides: dict[tuple[uuid.UUID, str], dict[str, Any]] = {}
    frozen_rate_versions: list[Mapping[str, Any]] | None = None
    if existing_run is not None:
        frozen_rate_versions = locked_payroll_rate_versions_from_summary(existing_run.summary)
        line_deposit_overrides = await load_line_deposit_overrides(session, existing_run.id)
        # Очищаем линии прошлой попытки расчёта — они будут пересозданы.
        await session.execute(
            text("DELETE FROM payroll_line WHERE run_id = :run_id"),
            {"run_id": existing_run.id},
        )
        # Удаляем превью-транзакции прошлой попытки расчёта. Они НЕ влияли на balance
        # (см. update_deposits_and_fund — balance меняется только в finalize),
        # так что откат balance тут не нужен. Только если run уже был finalized —
        # но это блокируется выше (raise PayrollConflictError).
        await session.execute(
            text("DELETE FROM deposit_transaction WHERE run_id = :run_id"),
            {"run_id": existing_run.id},
        )
        await session.execute(
            text("DELETE FROM salary_advance_recovery WHERE run_id = :run_id"),
            {"run_id": existing_run.id},
        )
        # Превью-зачёты «уже выплачено банком» прошлой попытки — как и возвраты авансов,
        # баланс EmployeePayout.offset_amount они не трогали (двигается на финализации).
        await session.execute(
            text("DELETE FROM employee_payout_offset WHERE run_id = :run_id"),
            {"run_id": existing_run.id},
        )
        existing_run.started_at = datetime.now(UTC)
        existing_run.finished_at = None
        existing_run.status = "running"
        existing_run.blocking_issues = []
        existing_run.summary = {}
        run = existing_run
    else:
        run = PayrollRun(
            period_id=period.id,
            started_at=datetime.now(UTC),
            status="running",
            blocking_issues=[],
            summary={},
        )
        session.add(run)
    await session.flush()

    try:
        entries = await load_attendance_entries(
            session, period, iiko_records=iiko_records, force_reload=refresh_attendance
        )
        blocking_issues = await collect_blocking_issues(session, entries, period=period)
        if blocking_issues:
            run.status = "blocked"
            run.finished_at = datetime.now(UTC)
            run.blocking_issues = blocking_issues
            run.summary = payroll_summary_with_rate_snapshot(
                {"blocking_issue_count": len(blocking_issues)},
                frozen_rate_versions,
                locked=frozen_rate_versions is not None,
            )
            await session.commit()
            await session.refresh(run)
            return run
        employee_ids = {entry.employee_id for entry in entries}
        employees = (
            {
                employee.id: employee
                for employee in (
                    await session.scalars(select(Employee).where(Employee.id.in_(employee_ids)))
                ).all()
            }
            if employee_ids
            else {}
        )
        attendance_warnings = collect_attendance_warnings(entries, employees)
        attendance_warning_summary = {
            "attendance_warnings": attendance_warnings,
            "attendance_warning_count": len(attendance_warnings),
        }

        # Отложенная выдача депозита (за feature-флагом): pending-планы сотрудников прогона
        # попадут в столбец «Выдача депозита» и сбросят накопление (удержание копит заново).
        deposit_payout_schedules: dict[uuid.UUID, dict[str, Any]] = {}
        if await is_scheduled_payout_enabled(session):
            pending_schedules = await load_pending_schedules(session, employee_ids)
            deposit_payout_schedules = {
                emp_id: {"requested_amount": schedule.requested_amount}
                for emp_id, schedule in pending_schedules.items()
            }

        await ensure_daily_revenue_cached(
            session,
            period.start_date,
            period.end_date,
            force_refresh=force_refresh,
        )
        deferred_charge_summary: dict[str, int] = {}
        calculation = await calculate_payroll_lines(
            session,
            period,
            run.id,
            entries,
            line_deposit_overrides=line_deposit_overrides,
            rate_versions_override=frozen_rate_versions,
            deposit_payout_schedules=deposit_payout_schedules,
        )
        if calculation.blocking_issues:
            run.status = "blocked"
            run.finished_at = datetime.now(UTC)
            run.blocking_issues = calculation.blocking_issues
            run.summary = payroll_summary_with_rate_snapshot(
                calculation.summary | deferred_charge_summary,
                frozen_rate_versions,
                locked=frozen_rate_versions is not None,
            )
            await session.commit()
            await session.refresh(run)
            return run

        for line in calculation.lines:
            session.add(line)
        await session.flush()

        applied_splits = await apply_pending_splits_for_run(
            session,
            run=run,
            period_end=period.end_date,
            period_start=period.start_date,
        )
        collapsed_recipients = await collapse_dismissed_deferred_charge_recipients(
            session,
            period=period,
            run=run,
        )
        deferred_charge_summary = deferred_charge_run_summary(
            applied_splits_count=len(applied_splits),
            collapsed_recipients_count=len(collapsed_recipients),
        )
        if deferred_charge_summary:
            run.summary = {**(run.summary or {}), **deferred_charge_summary}
            await session.flush()
            await session.execute(
                text("DELETE FROM payroll_line WHERE run_id = :run_id"),
                {"run_id": run.id},
            )
            calculation = await calculate_payroll_lines(
                session,
                period,
                run.id,
                entries,
                line_deposit_overrides=line_deposit_overrides,
                rate_versions_override=frozen_rate_versions,
                deposit_payout_schedules=deposit_payout_schedules,
            )
            if calculation.blocking_issues:
                run.status = "blocked"
                run.finished_at = datetime.now(UTC)
                run.blocking_issues = calculation.blocking_issues
                run.summary = payroll_summary_with_rate_snapshot(
                    calculation.summary | deferred_charge_summary,
                    frozen_rate_versions,
                    locked=frozen_rate_versions is not None,
                )
                await session.commit()
                await session.refresh(run)
                return run
            for line in calculation.lines:
                session.add(line)
            await session.flush()

        # Депозит-выдача уволенным, привязанная к этому периоду (без явок → без обычной
        # строки): добираем «депозитные» строки, чтобы выплата прошла вместе с ведомостью.
        period_deposit_lines = await build_period_deposit_payout_lines(
            session, period, run.id, calculation.lines
        )
        if period_deposit_lines:
            calculation.lines.extend(period_deposit_lines)
        advance_summary = await apply_advance_recoveries(session, period, run, calculation.lines)
        advance_issue_summary = await apply_advance_issuances(
            session, period, run, calculation.lines
        )
        # Внештат: смены, уже выданные наличными из кассы (paid_cash), исключаем из
        # «к выплате» (в ФОТ остаются). Мутирует net строк по образцу возврата авансов.
        freelancer_cash_summary = await apply_freelancer_cash_settlements(
            session, period, run, calculation.lines
        )
        # «Уже выплачено банком»: выплаты сотруднику из журнала ДДС, привязанные к зарплатной
        # статье, вычитаем из «к выдаче» (остаток переносится на следующую ведомость).
        payout_offset_summary = await apply_employee_payout_offsets(
            session, period, run, calculation.lines
        )
        subledger_summary = await update_deposits_and_fund(session, period, run, calculation.lines)
        paid_vacations = await vacation_service.mark_vacations_paid_for_payroll_period(
            session,
            period_start=period.start_date,
            period_end=period.end_date,
            payroll_date=period.payroll_date,
        )
        run.status = "completed"
        run.finished_at = datetime.now(UTC)
        run.blocking_issues = []
        run.summary = payroll_summary_with_rate_snapshot(
            calculation.summary
            | deferred_charge_summary
            | subledger_summary
            | advance_summary
            | advance_issue_summary
            | freelancer_cash_summary
            | attendance_warning_summary
            | {"vacations_marked_paid": paid_vacations}
            # Итог к выплате — ПОСЛЕ удержаний авансов/займов (перекрывает начисленный).
            | {
                "total_payable": money(
                    sum((decimal(line.total_payable) for line in calculation.lines), decimal(0))
                )
            },
            frozen_rate_versions,
            locked=frozen_rate_versions is not None,
        )
        await session.commit()
        await session.refresh(run)
        return run
    except Exception as exc:
        run.status = "failed"
        run.finished_at = datetime.now(UTC)
        run.summary = {"error": str(exc)[:500]}
        await session.commit()
        raise


async def collapse_dismissed_deferred_charge_recipients(
    session: AsyncSession,
    *,
    period: PayrollPeriod,
    run: PayrollRun,
) -> list[Any]:
    dismissed_ids = await session.scalars(
        select(Employee.id).where(
            # `dismissing` — увольнение ещё идёт (расчёты не закрыты); их отложенные
            # штрафы тоже схлопываем, иначе сотрудник застрянет в dismissing.
            Employee.status.in_(("inactive", "dismissing")),
            Employee.fire_date.is_not(None),
            Employee.fire_date >= period.start_date,
            Employee.fire_date <= period.end_date,
        )
    )
    collapsed: list[Any] = []
    for employee_id in dismissed_ids.all():
        collapsed.extend(
            await collapse_deferred_charges_on_dismissal(
                session,
                employee_id=employee_id,
                run=run,
                period_end=period.end_date,
            )
        )
    return collapsed


def deferred_charge_run_summary(
    *,
    applied_splits_count: int,
    collapsed_recipients_count: int,
) -> dict[str, int]:
    summary: dict[str, int] = {}
    if applied_splits_count:
        summary["deferred_charges_applied"] = applied_splits_count
    if collapsed_recipients_count:
        summary["deferred_charge_recipients_collapsed"] = collapsed_recipients_count
    return summary


def locked_payroll_rate_versions_from_summary(
    summary: Mapping[str, Any] | None,
) -> list[Mapping[str, Any]] | None:
    if not isinstance(summary, Mapping):
        return None
    snapshot = summary.get(PAYROLL_RATE_SNAPSHOT_SUMMARY_KEY)
    if not isinstance(snapshot, Mapping) or not bool(snapshot.get("locked")):
        return None
    rates = snapshot.get("rates")
    if not isinstance(rates, list):
        return None
    return [dict(rate) for rate in rates if isinstance(rate, Mapping)]


def payroll_summary_with_rate_snapshot(
    summary: Mapping[str, Any],
    rate_versions: Iterable[Mapping[str, Any]] | None = None,
    *,
    locked: bool,
) -> dict[str, Any]:
    result = dict(summary)
    if rate_versions is not None:
        result[PAYROLL_RATE_SNAPSHOT_SUMMARY_KEY] = payroll_rate_snapshot_payload(
            rate_versions,
            locked=locked,
        )
        return result

    snapshot = result.get(PAYROLL_RATE_SNAPSHOT_SUMMARY_KEY)
    if isinstance(snapshot, Mapping):
        rates = snapshot.get("rates")
        if isinstance(rates, list):
            result[PAYROLL_RATE_SNAPSHOT_SUMMARY_KEY] = payroll_rate_snapshot_payload(
                (rate for rate in rates if isinstance(rate, Mapping)),
                locked=locked or bool(snapshot.get("locked")),
            )
            return result

    return result


async def load_line_deposit_overrides(
    session: AsyncSession,
    run_id: uuid.UUID,
) -> dict[tuple[uuid.UUID, str], dict[str, Any]]:
    result = await session.scalars(
        select(PayrollLine).where(
            PayrollLine.run_id == run_id,
            or_(
                PayrollLine.deposit_excluded_for_run.is_(True),
                PayrollLine.deposit_exclusion_reason.is_not(None),
            ),
        )
    )
    return line_deposit_overrides_from_lines(result.all())


def line_deposit_overrides_from_lines(
    lines: Iterable[PayrollLine],
) -> dict[tuple[uuid.UUID, str], dict[str, Any]]:
    overrides: dict[tuple[uuid.UUID, str], dict[str, Any]] = {}
    for line in lines:
        excluded = bool(getattr(line, "deposit_excluded_for_run", False))
        reason = clean_optional_text(getattr(line, "deposit_exclusion_reason", None))
        if not excluded and reason is None:
            continue
        overrides[(line.employee_id, line.role)] = {
            "deposit_excluded_for_run": excluded,
            "deposit_exclusion_reason": reason,
        }
    return overrides


async def collect_blocking_issues(
    session: AsyncSession,
    entries: Iterable[AttendanceEntry],
    *,
    period: PayrollPeriod | None = None,
) -> list[dict[str, Any]]:
    entries = list(entries)
    if not entries:
        if period is not None:
            vacation_days = await vacation_service.vacation_days_for_payroll_period(
                session,
                period_start=period.start_date,
                period_end=period.end_date,
            )
            if vacation_days:
                return []
        return [{"type": "missing_attendance", "message": "No attendance entries for period"}]
    employee_ids = {entry.employee_id for entry in entries}
    employees = {
        employee.id: employee
        for employee in (
            await session.scalars(select(Employee).where(Employee.id.in_(employee_ids)))
        ).all()
    }
    issues: list[dict[str, Any]] = []
    for entry in entries:
        employee = employees.get(entry.employee_id)
        if employee is None:
            issues.append(
                {
                    "type": "unknown_employee",
                    "employee_id": str(entry.employee_id),
                    "work_date": entry.work_date.isoformat(),
                }
            )
            continue
        if employee.status == "requires_setup":
            issues.append(needs_setup_issue(employee))
        if entry.quality_status == "quality_review":
            issues.append(
                {
                    "type": "attendance_quality_review",
                    "employee_id": str(employee.id),
                    "employee_name": employee.full_name,
                    "work_date": entry.work_date.isoformat(),
                    "quality_status": entry.quality_status,
                    "notes": entry.notes,
                }
            )
        if employee.fire_date and entry.work_date > employee.fire_date:
            issues.append(
                {
                    "type": "post_termination_attendance",
                    "employee_id": str(employee.id),
                    "employee_name": employee.full_name,
                    "work_date": entry.work_date.isoformat(),
                    "fire_date": employee.fire_date.isoformat(),
                }
            )
    return deduplicate_issues(issues)


def collect_attendance_warnings(
    entries: Iterable[AttendanceEntry],
    employees: Mapping[uuid.UUID, Employee],
) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    for entry in entries:
        if entry.quality_status != "review_warning":
            continue
        employee = employees.get(entry.employee_id)
        if employee is None:
            continue
        warnings.append(
            {
                "type": "attendance_review_warning",
                "employee_id": str(employee.id),
                "employee_name": employee.full_name,
                "work_date": entry.work_date.isoformat(),
                "quality_status": entry.quality_status,
                "notes": entry.notes,
            }
        )
    return warnings


async def build_period_deposit_payout_lines(
    session: AsyncSession,
    period: PayrollPeriod,
    run_id: uuid.UUID,
    existing_lines: list[PayrollLine],
) -> list[PayrollLine]:
    """«Депозитные» строки для сотрудников с выдачей депозита, привязанной к этому периоду.

    Нужны для уволенных «через ведомость»: у них нет явок в периоде, значит нет обычной
    строки — но выдачу депозита надо провести вместе с ведомостью (через Сейф-контур при
    финализации, как остальные выдачи). Сотрудники, у которых строка уже есть (с явками),
    здесь пропускаются — их выдача считается в основном расчёте.
    """
    if not await is_scheduled_payout_enabled(session):
        return []
    period_scheduled = await load_period_schedules(session, period.id)
    if not period_scheduled:
        return []
    existing_emp_ids = {line.employee_id for line in existing_lines}
    target_emp_ids = [emp_id for emp_id in period_scheduled if emp_id not in existing_emp_ids]
    if not target_emp_ids:
        return []
    employees = {
        employee.id: employee
        for employee in (
            await session.scalars(select(Employee).where(Employee.id.in_(target_emp_ids)))
        ).all()
    }
    balances = await load_deposit_balances_for_employees(session, target_emp_ids)
    settings = await load_payroll_settings(session)
    new_lines: list[PayrollLine] = []
    for emp_id in target_emp_ids:
        employee = employees.get(emp_id)
        if employee is None:
            continue
        schedule = period_scheduled[emp_id]
        available = decimal(balances.get(emp_id, 0))
        requested = schedule.requested_amount
        requested_dec = decimal(requested) if requested is not None else available
        payout = max(min(requested_dec, available), Decimal("0"))
        if payout <= 0:
            continue
        role = vacation_role_for_employee_day(settings, employee, period.payroll_date)
        line = PayrollLine(
            run_id=run_id,
            employee_id=emp_id,
            role=role,
            deposit_payout_scheduled=money(payout),
            total_payable=Decimal("0"),
            components={"days": [], "deposit_payout": money(payout)},
        )
        session.add(line)
        new_lines.append(line)
    if new_lines:
        await session.flush()
    return new_lines


async def update_deposits_and_fund(
    session: AsyncSession,
    period: PayrollPeriod,
    run: PayrollRun,
    lines: Iterable[PayrollLine],
) -> dict[str, Any]:
    lines = list(lines)
    now = datetime.now(UTC)
    employee_ids = {line.employee_id for line in lines}
    deposit_accounts = await get_deposit_accounts(session, employee_ids)
    deposit_transactions: list[DepositTransaction] = []

    for line in lines:
        amount = decimal((line.components or {}).get("deposit_withholding", 0))
        if amount <= 0:
            continue
        # Транзакция создаётся как «превью» расчёта (status = completed, не finalized).
        # Balance счёта НЕ меняем — это произойдёт только в finalize_payroll_run.
        # До финализации Исходные данные / API GET /deposits должны показывать «истинный»
        # баланс (initial + ручные операции), а не накопленные но ещё не выплаченные.
        transaction = DepositTransaction(
            employee_id=line.employee_id,
            run_id=run.id,
            transaction_type="accrual",
            amount=amount,
            created_at=now,
        )
        session.add(transaction)
        deposit_transactions.append(transaction)

    # Отложенная выдача депозита: превью payout-транзакции из столбца «Выдача депозита».
    # Как и накопление, баланс двигается только в finalize (apply_deposit_transactions_to_balances
    # вычитает payout). deposit_payout_scheduled заполнен на первой строке сотрудника — без дублей.
    for line in lines:
        payout = decimal(getattr(line, "deposit_payout_scheduled", 0))
        if payout <= 0:
            continue
        transaction = DepositTransaction(
            employee_id=line.employee_id,
            run_id=run.id,
            transaction_type="payout",
            amount=payout,
            created_at=now,
        )
        session.add(transaction)
        deposit_transactions.append(transaction)

    employees_by_id = await get_employees_by_id(session, employee_ids)
    write_off_transactions = await apply_configured_deposit_write_offs(
        session,
        run,
        period,
        deposit_accounts,
        employees_by_id,
        now,
    )
    deposit_transactions.extend(write_off_transactions)

    fund_accrual_total = await accrue_fund(session, period, lines, run)
    fund_payout_total = await payout_previous_year_fund_if_due(session, period, run)

    return {
        "deposit_transaction_count": len(deposit_transactions),
        "fund_accrual_total": money(fund_accrual_total),
        "fund_payout_total": money(fund_payout_total),
    }


async def get_deposit_accounts(
    session: AsyncSession,
    employee_ids: Iterable[uuid.UUID],
) -> dict[uuid.UUID, DepositAccount]:
    employee_ids = set(employee_ids)
    if not employee_ids:
        return {}
    result = await session.scalars(
        select(DepositAccount).where(DepositAccount.employee_id.in_(employee_ids))
    )
    return {account.employee_id: account for account in result.all()}


async def get_employees_by_id(
    session: AsyncSession,
    employee_ids: Iterable[uuid.UUID],
) -> dict[uuid.UUID, Employee]:
    employee_ids = set(employee_ids)
    if not employee_ids:
        return {}
    result = await session.scalars(select(Employee).where(Employee.id.in_(employee_ids)))
    return {employee.id: employee for employee in result.all()}


async def apply_configured_deposit_write_offs(
    session: AsyncSession,
    run: PayrollRun,
    period: PayrollPeriod,
    accounts_by_employee_id: dict[uuid.UUID, DepositAccount],
    employees_by_id: Mapping[uuid.UUID, Employee],
    now: datetime,
) -> list[DepositTransaction]:
    from app.services.payroll_calculator import load_payroll_settings

    settings = await load_payroll_settings(session)
    write_offs = matching_deposit_write_offs(settings, period, employees_by_id)
    return apply_deposit_write_offs_to_accounts(
        accounts_by_employee_id,
        write_offs,
        run.id,
        now,
        session=session,
    )


def matching_deposit_write_offs(
    settings: Mapping[str, Any],
    period: PayrollPeriod,
    employees_by_id: Mapping[uuid.UUID, Employee],
) -> list[dict[str, Any]]:
    write_offs = settings.get("payroll.deposit_write_offs") or []
    if not isinstance(write_offs, list):
        return []
    by_iiko_id = {employee.iiko_id: employee for employee in employees_by_id.values()}
    result = []
    for item in write_offs:
        if not isinstance(item, Mapping):
            continue
        period_start = item.get("period_start")
        if period_start and str(period_start) != period.start_date.isoformat():
            continue
        employee: Employee | None = None
        employee_id = item.get("employee_id")
        employee_iiko_id = item.get("employee_iiko_id")
        if employee_id:
            employee = employees_by_id.get(uuid.UUID(str(employee_id)))
        elif employee_iiko_id:
            employee = by_iiko_id.get(str(employee_iiko_id))
        if employee is None:
            continue
        result.append({"employee_id": employee.id, "amount": decimal(item.get("amount", 0))})
    return result


def apply_deposit_write_offs_to_accounts(
    accounts_by_employee_id: dict[uuid.UUID, DepositAccount],
    write_offs: Iterable[Mapping[str, Any]],
    run_id: uuid.UUID | None,
    now: datetime,
    *,
    session: AsyncSession | None = None,
) -> list[DepositTransaction]:
    transactions = []
    for item in write_offs:
        employee_id = item["employee_id"]
        account = accounts_by_employee_id.get(employee_id)
        if account is None:
            continue
        amount = min(decimal(item.get("amount", 0)), decimal(account.balance))
        if amount <= 0:
            continue
        account.balance = decimal(account.balance) - amount
        account.last_updated = now
        transaction = DepositTransaction(
            employee_id=employee_id,
            run_id=run_id,
            transaction_type="write_off",
            amount=amount,
            created_at=now,
        )
        if session is not None:
            session.add(transaction)
        transactions.append(transaction)
    return transactions


async def accrue_fund(
    session: AsyncSession,
    period: PayrollPeriod,
    lines: Iterable[PayrollLine],
    run: PayrollRun,
) -> Decimal:
    await reset_fund_accruals_for_run(session, run.id)
    lines = list(lines)
    employee_ids = {line.employee_id for line in lines if decimal(line.fund_accrual) > Decimal("0")}
    employees_by_id: dict[uuid.UUID, Employee] = {}
    if employee_ids:
        employees_by_id = {
            employee.id: employee
            for employee in (
                await session.scalars(select(Employee).where(Employee.id.in_(employee_ids)))
            ).all()
        }
    total = Decimal("0")
    now = datetime.now(UTC)
    for line in lines:
        amount = decimal(line.fund_accrual)
        if amount <= 0:
            continue
        employee = employees_by_id.get(line.employee_id)
        if employee is not None and employee.position not in production_payroll_positions():
            continue
        account = await get_or_create_fund_account(session, line.employee_id, period.end_date.year)
        account.accumulated_amount = decimal(account.accumulated_amount) + amount
        account.status = "active"
        base_pay = decimal(line.base_pay)
        rate = (amount / base_pay) if base_pay > 0 else Decimal("0")
        session.add(
            AccumulationFundTransaction(
                id=uuid.uuid4(),
                account_id=account.id,
                employee_id=line.employee_id,
                year=period.end_date.year,
                run_id=run.id,
                transaction_type="accrual",
                amount=amount,
                rate_percent=rate.quantize(Decimal("0.00001")),
                base_pay_amount=base_pay,
                created_at=now,
            )
        )
        total += amount
    return total


async def reset_fund_accruals_for_run(session: AsyncSession, run_id: uuid.UUID) -> None:
    transactions = (
        await session.scalars(
            select(AccumulationFundTransaction)
            .where(
                AccumulationFundTransaction.run_id == run_id,
                AccumulationFundTransaction.transaction_type == "accrual",
            )
            .with_for_update()
        )
    ).all()
    if not transactions:
        return
    account_ids = {transaction.account_id for transaction in transactions}
    accounts = {
        account.id: account
        for account in (
            await session.scalars(
                select(AccumulationFundAccount)
                .where(AccumulationFundAccount.id.in_(account_ids))
                .with_for_update()
            )
        ).all()
    }
    for transaction in transactions:
        account = accounts.get(transaction.account_id)
        if account is not None:
            account.accumulated_amount = max(
                decimal(account.accumulated_amount) - decimal(transaction.amount),
                Decimal("0"),
            )
        await session.delete(transaction)
    await session.flush()


async def get_or_create_fund_account(
    session: AsyncSession,
    employee_id: uuid.UUID,
    year: int,
) -> AccumulationFundAccount:
    account = await session.scalar(
        select(AccumulationFundAccount).where(
            AccumulationFundAccount.employee_id == employee_id,
            AccumulationFundAccount.year == year,
        )
    )
    if account is not None:
        return account
    account = AccumulationFundAccount(
        employee_id=employee_id,
        year=year,
        accumulated_amount=Decimal("0"),
        paid_out_amount=Decimal("0"),
        forfeited_amount=Decimal("0"),
        status="active",
    )
    session.add(account)
    await session.flush()
    return account


async def payout_previous_year_fund_if_due(
    session: AsyncSession,
    period: PayrollPeriod,
    run: PayrollRun,
) -> Decimal:
    payment_date = date(period.payroll_date.year, 1, 15)
    if not (period.start_date <= payment_date <= period.end_date):
        return Decimal("0")
    target_year = payment_date.year - 1
    result = await payout_fund_accounts_for_year(
        session,
        target_year,
        run_id=run.id,
        comment=f"Выплата фонда за {target_year} год",
    )
    return result.total_paid_out


def apply_fund_payouts_to_accounts(accounts: Iterable[AccumulationFundAccount]) -> Decimal:
    total = Decimal("0")
    for account in accounts:
        amount = fund_outstanding(account)
        if amount <= 0:
            continue
        account.paid_out_amount = decimal(account.paid_out_amount) + amount
        account.status = "paid_out"
        account.paid_out_at = datetime.now(UTC)
        total += amount
    return total


def apply_fund_payouts_if_due(
    accounts: Iterable[AccumulationFundAccount],
    payroll_date: date,
    payment_day: str = "01-15",
) -> Decimal:
    if payroll_date.strftime("%m-%d") != payment_day:
        return Decimal("0")
    return apply_fund_payouts_to_accounts(accounts)


async def finalize_payroll_run(
    session: AsyncSession,
    run_id: uuid.UUID,
    *,
    finalized_by_user_id: uuid.UUID | None = None,
) -> PayrollRun:
    run = await session.get(PayrollRun, run_id)
    if run is None:
        raise PayrollNotFoundError("Payroll run not found")
    if run.status == "finalized":
        raise PayrollConflictError("Payroll run is already finalized")
    if run.blocking_issues:
        raise PayrollConflictError("Payroll run has blocking issues")
    if run.status != "completed":
        raise PayrollConflictError("Payroll run is not ready for finalization")

    period = await session.get(PayrollPeriod, run.period_id)
    if period is None:
        raise PayrollNotFoundError("Payroll period not found")
    if period.status == "finalized":
        raise PayrollConflictError("Payroll period is already finalized")
    if await run_has_unaccounted_advances(session, run, period):
        raise PayrollConflictError(
            "Ведомость устарела: появились авансы/займы, не учтённые в расчёте. "
            "Пересчитайте ведомость перед финализацией."
        )
    if await run_has_stale_freelancer_settlements(session, run, period):
        raise PayrollConflictError(
            "Ведомость устарела: посменные выплаты внештатников изменились после расчёта. "
            "Пересчитайте ведомость перед финализацией."
        )

    now = datetime.now(UTC)
    previous_run_status = run.status
    previous_period_status = period.status
    previous_finalized_at = period.finalized_at
    previous_finalized_by_user_id = period.finalized_by_user_id

    deposit_payload = await apply_deposit_transactions_to_balances(session, run, now, reverse=False)
    await apply_advance_recoveries_to_balances(session, run, now, reverse=False)
    await apply_employee_payout_offsets_to_balances(session, run, now, reverse=False)
    # Внештат (вариант Б): отдельного перехода смен нет — при финализации период уходит из
    # «открытых», его неоплаченные смены исчезают из кассы, их сумма уже в gross ведомости
    # (которая их и платит), а оплаченные налом вычтены из net на расчёте.
    # Запланированная выдача депозита исполнена этим прогоном → планы в processed.
    payout_employee_ids = (
        await session.scalars(
            select(DepositTransaction.employee_id).where(
                DepositTransaction.run_id == run.id,
                DepositTransaction.transaction_type == "payout",
            )
        )
    ).all()
    await mark_schedules_processed_for_run(session, run.id, payout_employee_ids)

    run.summary = payroll_summary_with_rate_snapshot(run.summary or {}, locked=True)
    run.status = "finalized"
    run.finished_at = run.finished_at or now
    period.status = "finalized"
    period.finalized_at = now
    period.finalized_by_user_id = finalized_by_user_id
    session.add(
        PayrollRunEvent(
            run_id=run.id,
            period_id=period.id,
            action="finalize",
            actor_user_id=finalized_by_user_id,
            payload=payroll_run_event_payload(
                run_status_before=previous_run_status,
                run_status_after=run.status,
                period_status_before=previous_period_status,
                period_status_after=period.status,
                finalized_at_before=previous_finalized_at,
                finalized_at_after=period.finalized_at,
                finalized_by_user_id_before=previous_finalized_by_user_id,
                finalized_by_user_id_after=period.finalized_by_user_id,
                deposit_payload=deposit_payload,
            ),
        )
    )
    await session.commit()
    await session.refresh(run)
    return run


async def unfinalize_payroll_run(
    session: AsyncSession,
    run_id: uuid.UUID,
    *,
    reason: str,
    actor_user_id: uuid.UUID | None,
) -> PayrollRun:
    run = await session.get(PayrollRun, run_id)
    if run is None:
        raise PayrollNotFoundError("Payroll run not found")
    if run.is_imported_legacy:
        raise PayrollConflictError("Импортированную ведомость нельзя откатывать")
    if run.status != "finalized":
        raise PayrollConflictError("Ведомость не финализирована")
    clean_reason = reason.strip()
    if not clean_reason:
        raise PayrollConflictError("Нужна причина отката")

    period = await session.get(PayrollPeriod, run.period_id)
    if period is None:
        raise PayrollNotFoundError("Payroll period not found")

    now = datetime.now(UTC)
    previous_run_status = run.status
    previous_period_status = period.status
    previous_finalized_at = period.finalized_at
    previous_finalized_by_user_id = period.finalized_by_user_id

    deposit_payload = await apply_deposit_transactions_to_balances(session, run, now, reverse=True)
    await apply_advance_recoveries_to_balances(session, run, now, reverse=True)
    await apply_employee_payout_offsets_to_balances(session, run, now, reverse=True)
    # Откат внештата: дефинализация возвращает период в «открытые» → его неоплаченные смены
    # снова видны в кассе. Оплаченные налом (paid_cash) не трогаем — они уже выданы.
    # Откат: исполненные планы выдачи депозита возвращаются в pending (как и реверс баланса).
    await revert_schedules_for_run(session, run.id)

    run.status = "completed"
    period.status = "open"
    period.finalized_at = None
    period.finalized_by_user_id = None
    session.add(
        PayrollRunEvent(
            run_id=run.id,
            period_id=period.id,
            action="unfinalize",
            reason=clean_reason,
            actor_user_id=actor_user_id,
            payload=payroll_run_event_payload(
                run_status_before=previous_run_status,
                run_status_after=run.status,
                period_status_before=previous_period_status,
                period_status_after=period.status,
                finalized_at_before=previous_finalized_at,
                finalized_at_after=period.finalized_at,
                finalized_by_user_id_before=previous_finalized_by_user_id,
                finalized_by_user_id_after=period.finalized_by_user_id,
                deposit_payload=deposit_payload,
            ),
        )
    )
    await session.commit()
    await session.refresh(run)
    return run


async def apply_deposit_transactions_to_balances(
    session: AsyncSession,
    run: PayrollRun,
    now: datetime,
    *,
    reverse: bool,
) -> dict[str, Any]:
    # До finalize deposit-транзакции являются превью; эта функция переводит их
    # в движение balance или применяет точную обратную операцию при reopen.
    run_transactions = (
        await session.scalars(select(DepositTransaction).where(DepositTransaction.run_id == run.id))
    ).all()
    balance_delta_total = Decimal("0")
    balance_abs_delta_total = Decimal("0")
    deltas: list[dict[str, Any]] = []
    if run_transactions:
        employee_ids = {tx.employee_id for tx in run_transactions}
        accounts = await get_deposit_accounts(session, employee_ids)
        for tx in run_transactions:
            account = accounts.get(tx.employee_id)
            if account is None:
                account = DepositAccount(
                    employee_id=tx.employee_id,
                    balance=Decimal("0"),
                    initial_balance=Decimal("0"),
                )
                session.add(account)
                accounts[tx.employee_id] = account
                await session.flush()

            amount = decimal(tx.amount)
            delta = amount if tx.transaction_type == "accrual" else -amount
            if reverse:
                delta = -delta
            account.balance = decimal(account.balance) + delta
            account.last_updated = now
            balance_delta_total += delta
            balance_abs_delta_total += abs(delta)
            deltas.append(
                {
                    "transaction_id": str(tx.id),
                    "employee_id": str(tx.employee_id),
                    "transaction_type": tx.transaction_type,
                    "amount": money_text(amount),
                    "balance_delta": money_text(delta),
                }
            )

    return {
        "deposit_transaction_count": len(run_transactions),
        "deposit_balance_delta_total": money_text(balance_delta_total),
        "deposit_balance_abs_delta_total": money_text(balance_abs_delta_total),
        "deposit_balance_deltas": deltas,
    }


def payroll_run_event_payload(
    *,
    run_status_before: str,
    run_status_after: str,
    period_status_before: str,
    period_status_after: str,
    finalized_at_before: datetime | None,
    finalized_at_after: datetime | None,
    finalized_by_user_id_before: uuid.UUID | None,
    finalized_by_user_id_after: uuid.UUID | None,
    deposit_payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        "run_status": {"before": run_status_before, "after": run_status_after},
        "period_status": {"before": period_status_before, "after": period_status_after},
        "period_finalized_at": {
            "before": finalized_at_before.isoformat() if finalized_at_before else None,
            "after": finalized_at_after.isoformat() if finalized_at_after else None,
        },
        "period_finalized_by_user_id": {
            "before": str(finalized_by_user_id_before) if finalized_by_user_id_before else None,
            "after": str(finalized_by_user_id_after) if finalized_by_user_id_after else None,
        },
        **deposit_payload,
    }


def money_text(value: Any) -> str:
    return f"{decimal(value).quantize(Decimal('0.01'))}"


async def list_runs(session: AsyncSession) -> list[dict[str, Any]]:
    result = await session.execute(
        select(PayrollRun, PayrollPeriod)
        .join(PayrollPeriod, PayrollRun.period_id == PayrollPeriod.id)
        .order_by(desc(PayrollRun.started_at))
    )
    rows = result.all()
    run_ids = [run.id for run, _ in rows]
    metrics = await _run_line_metrics(session, run_ids)
    payment_metrics = await _run_payment_metrics(session, run_ids)
    return [
        serialize_run(
            run,
            period,
            metrics=metrics.get(run.id),
            payment_metrics=payment_metrics.get(run.id),
        )
        for run, period in rows
    ]


def _coerce_revenue(value: Any) -> float:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return 0.0
    return amount if amount > 0 else 0.0


async def _run_line_metrics(
    session: AsyncSession, run_ids: list[uuid.UUID]
) -> dict[uuid.UUID, dict[str, Any]]:
    """Aggregate per-run revenue and headcount from line components in a single query.

    Mirrors the frontend ``runRevenue`` logic (sum of unique ``daily_revenue`` per
    date) so the runs list can render the FOT ratio without the client fetching every
    run's lines (the previous N+1 that flooded the API with one request per run).
    """
    if not run_ids:
        return {}
    rows = await session.execute(
        select(
            PayrollLine.run_id,
            PayrollLine.employee_id,
            PayrollLine.components["days"].label("days"),
        ).where(PayrollLine.run_id.in_(run_ids))
    )
    revenue_by_date: dict[uuid.UUID, dict[str, float]] = defaultdict(dict)
    employees: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
    for run_id, employee_id, days in rows:
        employees[run_id].add(employee_id)
        if not isinstance(days, list):
            continue
        bucket = revenue_by_date[run_id]
        for day in days:
            if not isinstance(day, Mapping):
                continue
            date_key = str(day.get("date") or "")
            if not date_key or date_key in bucket:
                continue
            revenue = _coerce_revenue(day.get("daily_revenue"))
            if revenue > 0:
                bucket[date_key] = revenue
    return {
        run_id: {
            "revenue_total": round(sum(revenue_by_date.get(run_id, {}).values()), 2),
            "employee_count": len(employees.get(run_id, ())),
        }
        for run_id in run_ids
    }


async def _run_payment_metrics(
    session: AsyncSession, run_ids: list[uuid.UUID]
) -> dict[uuid.UUID, dict[str, Any]]:
    """Per-run агрегаты выплаты для списка ведомостей: выплачено, недоплата и её признак.

    ``payment_state``: ``partial`` — есть недоплаченные сотрудники (ведомость закрепляется и
    подсвечивается); ``paid`` — все выплачены полностью; ``in_progress`` — часть выдана без
    недоплат; ``unpaid`` — выплат ещё нет. ``remaining_shortfall`` — сумма недоплат ТОЛЬКО по
    сотрудникам со статусом ``partially_paid`` (не включает ещё не тронутых).
    """
    if not run_ids:
        return {}
    accrued_rows = await session.execute(
        select(
            PayrollLine.run_id,
            PayrollLine.employee_id,
            func.sum(PayrollLine.total_payable),
        )
        .where(PayrollLine.run_id.in_(run_ids))
        .group_by(PayrollLine.run_id, PayrollLine.employee_id)
    )
    accrued_by_employee: dict[uuid.UUID, dict[uuid.UUID, Decimal]] = defaultdict(dict)
    employee_count: dict[uuid.UUID, int] = defaultdict(int)
    for run_id, employee_id, amount in accrued_rows:
        accrued_by_employee[run_id][employee_id] = Decimal(amount or 0)
        employee_count[run_id] += 1
    payment_rows = await session.execute(
        select(
            PayrollPayment.run_id,
            PayrollPayment.employee_id,
            PayrollPayment.amount,
            PayrollPayment.status,
        ).where(PayrollPayment.run_id.in_(run_ids))
    )
    paid_total: dict[uuid.UUID, Decimal] = defaultdict(lambda: Decimal("0"))
    shortfall: dict[uuid.UUID, Decimal] = defaultdict(lambda: Decimal("0"))
    underpaid_count: dict[uuid.UUID, int] = defaultdict(int)
    fully_paid_count: dict[uuid.UUID, int] = defaultdict(int)
    for run_id, employee_id, amount, status in payment_rows:
        paid = Decimal(amount or 0)
        paid_total[run_id] += paid
        if status == "partially_paid":
            underpaid_count[run_id] += 1
            accrued = accrued_by_employee[run_id].get(employee_id, paid)
            shortfall[run_id] += max(Decimal("0"), accrued - paid)
        elif status == "paid":
            fully_paid_count[run_id] += 1
    metrics: dict[uuid.UUID, dict[str, Any]] = {}
    for run_id in run_ids:
        total_employees = employee_count.get(run_id, 0)
        underpaid = underpaid_count.get(run_id, 0)
        fully = fully_paid_count.get(run_id, 0)
        if underpaid > 0:
            state = "partial"
        elif total_employees > 0 and fully >= total_employees:
            state = "paid"
        elif fully > 0:
            state = "in_progress"
        else:
            state = "unpaid"
        metrics[run_id] = {
            "payment_state": state,
            "paid_total": round(float(paid_total.get(run_id, Decimal("0"))), 2),
            "remaining_shortfall": round(float(shortfall.get(run_id, Decimal("0"))), 2),
            "underpaid_count": underpaid,
        }
    return metrics


async def get_run(session: AsyncSession, run_id: uuid.UUID) -> dict[str, Any]:
    row = (
        await session.execute(
            select(PayrollRun, PayrollPeriod)
            .join(PayrollPeriod, PayrollRun.period_id == PayrollPeriod.id)
            .where(PayrollRun.id == run_id)
        )
    ).one_or_none()
    if row is None:
        raise PayrollNotFoundError("Payroll run not found")
    run, period = row
    needs_recalc = False
    if run.status == "completed":
        needs_recalc = await run_has_unaccounted_advances(session, run, period)
    return serialize_run(run, period, needs_recalc=needs_recalc)


async def get_run_lines(session: AsyncSession, run_id: uuid.UUID) -> list[PayrollLine]:
    run = await session.get(PayrollRun, run_id)
    if run is None:
        raise PayrollNotFoundError("Payroll run not found")
    result = await session.scalars(
        select(PayrollLine)
        .where(PayrollLine.run_id == run_id)
        .order_by(PayrollLine.role, PayrollLine.employee_id)
    )
    return list(result.all())


def serialize_period(period: PayrollPeriod) -> dict[str, Any]:
    return {
        "id": period.id,
        "period_type": period.period_type,
        "start_date": period.start_date,
        "end_date": period.end_date,
        "payroll_date": period.payroll_date,
        "status": period.status,
        "finalized_at": period.finalized_at,
        "finalized_by_user_id": period.finalized_by_user_id,
    }


def serialize_run(
    run: PayrollRun,
    period: PayrollPeriod | None = None,
    *,
    metrics: dict[str, Any] | None = None,
    payment_metrics: dict[str, Any] | None = None,
    needs_recalc: bool = False,
) -> dict[str, Any]:
    summary = dict(run.summary or {})
    if metrics is not None:
        summary["revenue_total"] = metrics.get("revenue_total", 0.0)
        summary["employee_count"] = metrics.get("employee_count", 0)
    if payment_metrics is not None:
        summary["payment_state"] = payment_metrics.get("payment_state", "unpaid")
        summary["paid_total"] = payment_metrics.get("paid_total", 0.0)
        summary["remaining_shortfall"] = payment_metrics.get("remaining_shortfall", 0.0)
        summary["underpaid_count"] = payment_metrics.get("underpaid_count", 0)
    data = {
        "id": run.id,
        "period_id": run.period_id,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "status": run.status,
        "blocking_issues": run.blocking_issues or [],
        "summary": summary,
        "is_imported_legacy": bool(getattr(run, "is_imported_legacy", False)),
        "payout_cash_total": float(run.payout_cash_total or 0),
        "payout_cash_wallet_id": run.payout_cash_wallet_id,
        "needs_recalc": bool(needs_recalc),
    }
    if period is not None:
        data["period"] = serialize_period(period)
    return data


def summarize_persisted_lines(lines: Iterable[PayrollLine]) -> dict[str, Any]:
    return summarize_lines(lines)


def clean_optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
