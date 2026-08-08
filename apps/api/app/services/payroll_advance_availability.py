"""Доступно к авансу — earned-to-date по сотруднику на дату.

Аванс (право A) ограничен заработанным на дату. Считаем единообразно по принципу
«сколько сотрудник заработал бы, если бы текущий открытый период закончился сегодня»:

- Окладник (админ-должности): оклад × прошло_дней / дней_в_полупериоде — чистая
  формула без данных (см. `okladnik_earned_to_date`).
- Мойщица: смены ≤ as_of × ставку-из-пула.
- Производственный (повар/кассир): провизорный прогон реального недельного
  калькулятора с `period.end = as_of` по УЖЕ загруженным явкам ≤ as_of (решение:
  «из загруженных явок», без live-iiko в момент выдачи). `total_payable` — netto.

Отпускные одним траншем (`vacation_period.payout_date`) в earned-to-date НЕ входят
(решение владельца 02.08.2026: «аванс только за отработанное»). Транш цепляется к
ведомости по `payroll_date`, а не по окну дат, поэтому в провизорном прогоне он давал
бы полную сумму ЕЩЁ НЕ ОТГУЛЯННОГО отпуска с первого дня недели — то есть кредит под
будущее, а не раннюю выплату заработанного. Ведомость их платит по-прежнему, а диалог
выдачи объясняет разницу нотой (`_vacation_lump_note`). Подённые отпускные (отпуск без
`payout_date`) окном дат ограничены — там за as_of ничего не утекает, они остаются.

Доступно к авансу = earned-to-date − уже выданные авансы за текущий период.

Отсечка «день выплаты»: в САМ день выплаты ведомости заработанное уходит сотруднику
вместе с ней (даже если она ещё не финализирована/не выплачена физически), поэтому
аванс за этот период теряет смысл — `available` обнуляется, `payout_reached=True`.
День выплаты определяется по «только что закрытой» оплачиваемой ведомости:
- полумесяц (админ): 15-е (платится [1..15]) и 1-е (платится [16..конец] прошлого
  месяца) — на 1-е новый период уже начался, но по правилу «блокируем весь день»;
- недельный (производство): день = `payroll_date` завершившейся недели (ищем её
  напрямую, не полагаясь на «период, содержащий as_of», — иначе предсозданная
  следующая неделя глушит отсечку).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AttendanceEntry, Employee, PayrollPeriod, SalaryAdvance
from app.services import vacation_service
from app.services.employee_effective_events import get_position_on_date
from app.services.payroll_admin import (
    HALF_MONTH_SPLIT_DAY,
    PAYOUT_MODE_FIRST_HALF,
    PAYOUT_MODE_ON_DEMAND,
    PAYOUT_MODE_SECOND_HALF,
    _first_half,
    _load_dishwasher_shift_counts,
    _load_dishwasher_shift_rate,
    _load_okladnik_payout_modes,
    _okladnik_payout_mode,
    _second_half,
    load_admin_oklad,
    okladnik_earned_to_date,
)
from app.services.payroll_calculator import calculate_payroll_lines, decimal
from app.services.position_registry import dishwasher_positions, okladnik_positions

_CENTS = Decimal("0.01")


@dataclass(slots=True)
class AdvanceAvailability:
    employee_id: uuid.UUID
    as_of: date
    period_start: date | None
    period_end: date | None
    basis: str  # okladnik | dishwasher | production | none
    earned_to_date: Decimal
    already_advanced: Decimal
    available: Decimal
    # Отпускные одним траншем в ведомости периода. В `earned_to_date`/`available` НЕ
    # входят (аванс — только за отработанное), но ведомость их платит, поэтому «Учёт
    # ДЗ/КЗ» складывает их в долг перед сотрудником.
    vacation_payout_lump: Decimal = Decimal("0.00")
    note: str | None = None
    # True в день выплаты периода: заработанное уходит с ведомостью, аванс за этот
    # период уже недоступен (available обнулён).
    payout_reached: bool = False


def _half_month_bounds(as_of: date) -> tuple[date, date]:
    """Границы полумесячного периода, содержащего `as_of`."""
    if as_of.day <= HALF_MONTH_SPLIT_DAY:
        start, end, _ = _first_half(as_of.year, as_of.month)
    else:
        start, end, _ = _second_half(as_of.year, as_of.month)
    return start, end


def _half_month_payout_paid_end(check: date) -> date | None:
    """Если `check` — день выплаты полумесячной ведомости, вернуть последний день
    ОПЛАЧИВАЕМОГО полупериода; иначе None.

    - 15-е: платится текущий 1-й полупериод [1..15] → конец = 15-е.
    - 1-е: платится 2-й полупериод ПРЕДЫДУЩЕГО месяца [16..конец] → конец = последний
      день прошлого месяца (на 1-е новый период уже стартовал, но день выплаты — этот).
    """
    if check.day == HALF_MONTH_SPLIT_DAY:
        return check
    if check.day == 1:
        return check - timedelta(days=1)
    return None


async def _weekly_payout_paid_end(session: AsyncSession, check: date) -> date | None:
    """Если `check` — `payroll_date` завершившейся недельной ведомости (ещё не
    финализированной), вернуть её `end_date`; иначе None."""
    period = await session.scalar(
        select(PayrollPeriod)
        .where(
            PayrollPeriod.period_type == "week",
            PayrollPeriod.status != "finalized",
            PayrollPeriod.payroll_date == check,
        )
        .order_by(PayrollPeriod.start_date.desc())
        .limit(1)
    )
    return period.end_date if period is not None else None


def _okladnik_payout_paid_end(mode: str, check: date) -> date | None:
    """День выплаты окладника зависит от режима: on_demand не выплачивается автоматически
    (нет дня выплаты); first_half — весь оклад в первой ведомости (15-е); second_half — в
    последней (1-е); split — обе половины (15-е и 1-е)."""
    if mode == PAYOUT_MODE_ON_DEMAND:
        return None
    on_first_half_payday = check.day == HALF_MONTH_SPLIT_DAY  # 15-е → платится [1..15]
    on_second_half_payday = check.day == 1  # 1-е → платится [16..конец] прошлого месяца
    if mode == PAYOUT_MODE_FIRST_HALF:
        return check if on_first_half_payday else None
    if mode == PAYOUT_MODE_SECOND_HALF:
        return (check - timedelta(days=1)) if on_second_half_payday else None
    # split (дефолт): выплачиваются обе половины.
    if on_first_half_payday:
        return check
    if on_second_half_payday:
        return check - timedelta(days=1)
    return None


async def _payout_paid_end(
    session: AsyncSession, basis: str, position: str, check: date
) -> date | None:
    """Последний день оплачиваемого периода, если `check` — его день выплаты; иначе None."""
    if basis == "dishwasher":
        # Мойщица выплачивается вместе с полумесячной ведомостью (обе половины).
        return _half_month_payout_paid_end(check)
    if basis == "okladnik":
        modes = await _load_okladnik_payout_modes(session)
        mode = _okladnik_payout_mode(modes, position)
        return _okladnik_payout_paid_end(mode, check)
    if basis == "production":
        return await _weekly_payout_paid_end(session, check)
    return None


async def _open_weekly_period(session: AsyncSession, as_of: date) -> PayrollPeriod | None:
    containing = await session.scalar(
        select(PayrollPeriod)
        .where(
            PayrollPeriod.period_type == "week",
            PayrollPeriod.status != "finalized",
            PayrollPeriod.start_date <= as_of,
            PayrollPeriod.end_date >= as_of,
        )
        .order_by(PayrollPeriod.start_date.desc())
        .limit(1)
    )
    if containing is not None:
        return containing
    return await session.scalar(
        select(PayrollPeriod)
        .where(
            PayrollPeriod.period_type == "week",
            PayrollPeriod.status != "finalized",
            PayrollPeriod.payroll_date >= as_of,
        )
        .order_by(PayrollPeriod.payroll_date, PayrollPeriod.start_date)
        .limit(1)
    )


async def _okladnik_earned(
    session: AsyncSession,
    employee: Employee,
    position: str,
    as_of: date,
) -> tuple[Decimal, date, date]:
    start, end = _half_month_bounds(as_of)
    period = PayrollPeriod(
        period_type="half_month",
        start_date=start,
        end_date=end,
        payroll_date=end,
        status="open",
    )
    oklad = await load_admin_oklad(session, employee.id, position, end)
    if oklad is None:
        return Decimal("0.00"), start, end
    modes = await _load_okladnik_payout_modes(session)
    mode = _okladnik_payout_mode(modes, position)
    earned = okladnik_earned_to_date(oklad, mode, employee, period, as_of)
    return earned, start, end


async def _dishwasher_earned(
    session: AsyncSession,
    employee: Employee,
    as_of: date,
) -> tuple[Decimal, date, date]:
    start, end = _half_month_bounds(as_of)
    rate = await _load_dishwasher_shift_rate(session)
    # Смены только по прошедшие дни (≤ as_of) текущего полупериода.
    counts = await _load_dishwasher_shift_counts(session, [employee.id], start, as_of)
    shifts = counts.get(employee.id, 0)
    earned = (rate * Decimal(shifts)).quantize(_CENTS)
    return earned, start, end


def _money(amount: Decimal) -> str:
    """1234567.00 → «1 234 567», 1234.50 → «1 234,50».

    Разряды разделяет НЕРАЗРЫВНЫЙ пробел (U+00A0) — так же форматирует деньги фронт
    (`Intl.NumberFormat('ru-RU')` в `formatMoney`), поэтому сумма не переносится по
    строке посередине числа.
    """
    quantized = amount.quantize(_CENTS)
    text = f"{quantized:,.2f}".replace(",", " ").replace(".", ",")
    return text[:-3] if text.endswith(",00") else text


async def vacation_lump_for_payroll_date(
    session: AsyncSession,
    employee_id: uuid.UUID,
    payroll_date: date,
) -> Decimal:
    """Отпускные сотрудника, выплачиваемые ведомостью этой даты одним траншем.

    В АВАНС не входят (отпуск на дату аванса ещё не отгулян), но в ДОЛГ перед
    сотрудником входят: ведомость их заплатит. Поэтому величина отдаётся наружу
    отдельным полем ``AdvanceAvailability.vacation_payout_lump`` — «Учёт ДЗ/КЗ»
    складывает её в задолженность, а потолок аванса — нет.
    """
    payouts = await vacation_service.vacation_payouts_for_payroll_date(
        session, payroll_date=payroll_date
    )
    total = sum(
        (decimal(payout.amount) for payout in payouts if payout.employee_id == employee_id),
        Decimal("0"),
    )
    return total.quantize(_CENTS)


def _vacation_lump_note(lump: Decimal, payroll_date: date) -> str | None:
    """Пояснение, если в ведомости периода лежат отпускные одним траншем.

    Они выплачиваются ведомостью, но в аванс не входят: отпуск на дату аванса ещё не
    отгулян, а аванс — только в пределах заработанного (см. докстринг модуля). Без
    этой строки разница между «доступно к авансу» и суммой ведомости выглядит ошибкой.
    """
    if lump <= 0:
        return None
    return (
        f"Отпускные {_money(lump)}\u00a0₽ выплатятся ведомостью "
        f"{payroll_date.strftime('%d.%m.%Y')} и в аванс не входят"
    )


async def _production_earned(
    session: AsyncSession,
    employee: Employee,
    as_of: date,
    *,
    period: PayrollPeriod | None = None,
) -> tuple[Decimal, date | None, date | None, str | None, Decimal, Decimal]:
    """``period`` передают, когда период уже выбран снаружи — так делает исторический срез.

    Сам по себе этот расчёт ищет ОТКРЫТЫЙ период (аванс выдают из открытого), и для прошедшей
    даты это неверный выбор: содержащий её период давно финализирован, и поиск уехал бы на
    следующий открытый — то есть вернул бы заработанное не за тот период.
    """
    period = period or await _open_weekly_period(session, as_of)
    if period is None:
        return (
            Decimal("0.00"),
            None,
            None,
            "Нет открытого недельного периода",
            Decimal("0.00"),
            Decimal("0.00"),
        )
    earned_through = min(as_of, period.end_date)
    # Только уже загруженные явки внутри рабочего периода (без live-iiko). В день выплаты
    # as_of может быть позже end_date, но заработанное считаем только до конца недели.
    # ИДУЩИЕ смены (is_open) исключены: у них ended_at/минуты синтезированы загрузчиком
    # (min(22:00, старт+12ч)), то есть смена, начатая утром, сразу выглядит полной
    # 12-часовой. Заработанное — это факт, а не прогноз: сегодняшняя смена войдёт в окно
    # аванса после того, как её закроют в iiko.
    entries = list(
        (
            await session.scalars(
                select(AttendanceEntry).where(
                    AttendanceEntry.period_id == period.id,
                    AttendanceEntry.work_date <= earned_through,
                    AttendanceEntry.is_open.is_(False),
                )
            )
        ).all()
    )
    lump = await vacation_lump_for_payroll_date(session, employee.id, period.payroll_date)
    if not entries:
        return (
            Decimal("0.00"),
            period.start_date,
            as_of,
            "Явки за период ещё не загружены",
            lump,
            Decimal("0.00"),
        )
    provisional = PayrollPeriod(
        period_type="week",
        start_date=period.start_date,
        end_date=earned_through,
        payroll_date=period.payroll_date,
        status="open",
    )
    result = await calculate_payroll_lines(
        session,
        provisional,
        uuid.uuid4(),
        entries,
        include_vacation_payout_lump=False,
    )
    if result.blocking_issues:
        return (
            Decimal("0.00"),
            period.start_date,
            as_of,
            "Расчёт заблокирован — проверьте явки/ставки",
            lump,
            Decimal("0.00"),
        )
    earned = sum(
        (decimal(line.total_payable) for line in result.lines if line.employee_id == employee.id),
        Decimal("0"),
    )
    # Удержание в депозит: из ``total_payable`` оно уже вычтено, но на депозитный счёт ляжет
    # только при финализации. Для среза на дату внутри периода эти деньги иначе исчезают —
    # из зарплаты вычтены, на счёт не попали.
    withheld = sum(
        (
            decimal((line.components or {}).get("deposit_withholding", 0))
            for line in result.lines
            if line.employee_id == employee.id
        ),
        Decimal("0"),
    )
    note = _vacation_lump_note(lump, period.payroll_date)
    return (
        earned.quantize(_CENTS),
        period.start_date,
        as_of,
        note,
        lump,
        withheld.quantize(_CENTS),
    )


async def _already_advanced_in_period(
    session: AsyncSession,
    employee_id: uuid.UUID,
    period_start: date,
    period_end: date,
) -> Decimal:
    """Сумма выданных за период авансов (не отменённых) — уменьшает доступное."""
    rows = (
        await session.scalars(
            select(SalaryAdvance).where(
                SalaryAdvance.employee_id == employee_id,
                SalaryAdvance.issued_on >= period_start,
                SalaryAdvance.issued_on <= period_end,
                SalaryAdvance.status != "cancelled",
            )
        )
    ).all()
    return sum((decimal(row.amount) for row in rows), Decimal("0")).quantize(_CENTS)


async def _basis_earned(
    session: AsyncSession,
    employee: Employee,
    position: str,
    basis: str,
    as_of: date,
) -> tuple[Decimal, date | None, date | None, str | None, Decimal]:
    """Заработанное, границы периода и лумп отпускных для базиса на дату `as_of`.

    Отпускные бывают только у Поваров и Кассиров (`vacation_positions()`), а это всегда
    производственный базис, — у окладника и мойщицы лумп нулевой по построению.
    """
    if basis == "okladnik":
        earned, start, end = await _okladnik_earned(session, employee, position, as_of)
        return earned, start, end, None, Decimal("0.00")
    if basis == "dishwasher":
        earned, start, end = await _dishwasher_earned(session, employee, as_of)
        return earned, start, end, None, Decimal("0.00")
    earned, start, end, note, lump, _withheld = await _production_earned(session, employee, as_of)
    return earned, start, end, note, lump


def _upcoming_weekly_payslips(today: date, count: int) -> list[tuple[date, date, date]]:
    from app.services.payroll_runner import current_week_bounds

    start, _, _ = current_week_bounds(today)
    out: list[tuple[date, date, date]] = []
    for i in range(count + 2):
        s, e, p = current_week_bounds(start + timedelta(days=7 * i))
        if p >= today:
            out.append((s, e, p))
        if len(out) >= count:
            break
    return out[:count]


def _upcoming_half_month_payslips(
    today: date, count: int, *, mode: str | None
) -> list[tuple[date, date, date]]:
    if mode == PAYOUT_MODE_ON_DEMAND:
        return []  # нет автоматических выплат — удерживать неоткуда
    out: list[tuple[date, date, date]] = []
    year, month = today.year, today.month
    for _ in range(count + 3):
        for start, end, payout in (_first_half(year, month), _second_half(year, month)):
            if payout < today:
                continue
            # first_half платит только 15-го (payout.day==15), second_half — только 1-го.
            if mode == PAYOUT_MODE_FIRST_HALF and payout.day != HALF_MONTH_SPLIT_DAY:
                continue
            if mode == PAYOUT_MODE_SECOND_HALF and payout.day == HALF_MONTH_SPLIT_DAY:
                continue
            out.append((start, end, payout))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        if len(out) >= count:
            break
    out.sort(key=lambda item: item[2])
    return out[:count]


async def upcoming_payslips(
    session: AsyncSession, employee: Employee | None, today: date, *, count: int = 2
) -> list[tuple[date, date, date]]:
    """Ведомости для выбора удержания займа.

    Все уже созданные нефинализированные ведомости нужного контура доступны всегда,
    даже когда их ``payroll_date`` уже прошла. Это позволяет направить удержание в
    ближайшую ещё открытую ведомость. К ним добавляются `count` дат из будущего
    расписания (там строки ``PayrollPeriod`` могут ещё не существовать).

    Возвращает ``(period_start, period_end, payout_date)``. Без сотрудника используется
    недельный контур по умолчанию; с сотрудником — его payroll-пайплайн.
    """
    if employee is None:
        period_type = "week"
        scheduled = _upcoming_weekly_payslips(today, count)
    else:
        position = await get_position_on_date(session, employee.id, today)
        position = position or employee.position or ""
        if position in okladnik_positions():
            period_type = "half_month"
            modes = await _load_okladnik_payout_modes(session)
            mode = _okladnik_payout_mode(modes, position)
            scheduled = _upcoming_half_month_payslips(today, count, mode=mode)
        elif position in dishwasher_positions():
            period_type = "half_month"
            scheduled = _upcoming_half_month_payslips(today, count, mode=None)
        else:
            period_type = "week"
            scheduled = _upcoming_weekly_payslips(today, count)

    open_periods = (
        await session.scalars(
            select(PayrollPeriod)
            .where(
                PayrollPeriod.period_type == period_type,
                PayrollPeriod.status != "finalized",
            )
            .order_by(PayrollPeriod.payroll_date, PayrollPeriod.start_date)
        )
    ).all()
    # Реальные ведомости приоритетнее расчётных дат: они могут быть уже рассчитаны,
    # но всё ещё не финализированы. Ключ по границам не даёт продублировать такую
    # ведомость её виртуальной датой из расписания.
    payslips = {
        (period.start_date, period.end_date): (
            period.start_date,
            period.end_date,
            period.payroll_date,
        )
        for period in open_periods
    }
    for period_start, period_end, payout_date in scheduled:
        payslips.setdefault((period_start, period_end), (period_start, period_end, payout_date))
    return sorted(payslips.values(), key=lambda item: (item[2], item[0], item[1]))


async def available_to_advance(
    session: AsyncSession,
    employee: Employee,
    as_of: date,
    *,
    apply_payout_gate: bool = True,
) -> AdvanceAvailability:
    """Доступно к авансу = earned-to-date − уже выданные авансы за текущий период.

    Отсечка «день выплаты»: если `as_of` приходится на день выплаты ведомости сотрудника
    (с учётом режима выплат окладника), `available` обнуляется, а `payout_reached=True`;
    заработанное показываем по ОПЛАЧИВАЕМОМУ периоду (справочно).

    `apply_payout_gate=False` полностью выключает отсечку — для реконсиляции уже прошедших
    операций (дата операции в прошлом не должна «обнулять» исторический аванс) и для
    выдачи «через ведомость», где блокировку решает вызывающий по дате выплаты периода.
    """
    position = await get_position_on_date(session, employee.id, as_of)
    position = position or employee.position or ""

    if position in okladnik_positions():
        basis = "okladnik"
    elif position in dishwasher_positions():
        basis = "dishwasher"
    else:
        basis = "production"

    paid_end = (
        await _payout_paid_end(session, basis, position, as_of) if apply_payout_gate else None
    )
    # В день выплаты earned показываем по ОПЛАЧИВАЕМОМУ периоду (as_of → его конец),
    # иначе — по периоду, содержащему as_of.
    earned_as_of = paid_end if paid_end is not None else as_of

    earned, start, end, note, lump = await _basis_earned(
        session, employee, position, basis, earned_as_of
    )

    if start is None or end is None:
        return AdvanceAvailability(
            employee_id=employee.id,
            as_of=as_of,
            period_start=None,
            period_end=None,
            basis="none",
            earned_to_date=Decimal("0.00"),
            already_advanced=Decimal("0.00"),
            available=Decimal("0.00"),
            vacation_payout_lump=Decimal("0.00"),
            note=note,
        )

    already = await _already_advanced_in_period(session, employee.id, start, end)
    if paid_end is not None:
        return AdvanceAvailability(
            employee_id=employee.id,
            as_of=as_of,
            period_start=start,
            period_end=end,
            basis=basis,
            earned_to_date=earned.quantize(_CENTS),
            already_advanced=already,
            available=Decimal("0.00"),
            vacation_payout_lump=lump,
            note=(
                f"Наступил день выплаты ({as_of.strftime('%d.%m.%Y')}) — "
                "заработанное уходит с ведомостью, аванс за этот период уже недоступен"
            ),
            payout_reached=True,
        )

    available = max(earned - already, Decimal("0")).quantize(_CENTS)
    return AdvanceAvailability(
        employee_id=employee.id,
        as_of=as_of,
        period_start=start,
        period_end=end,
        basis=basis,
        earned_to_date=earned.quantize(_CENTS),
        already_advanced=already,
        available=available,
        vacation_payout_lump=lump,
        note=note,
        payout_reached=False,
    )


@dataclass(frozen=True)
class EarnedAsOf:
    """Заработанное сотрудником на дату — историческая половина ``available_to_advance``.

    Отдельная функция, а не флаг внутри существующей: та отвечает на вопрос «сколько можно
    выдать авансом ПРЯМО СЕЙЧАС» и потому ищет ОТКРЫТЫЙ период. Для прошедшей даты это неверный
    выбор — содержащий её период уже финализирован, и поиск уходит на следующий открытый,
    возвращая заработанное не за тот период.
    """

    employee_id: uuid.UUID
    as_of: date
    basis: str
    period_start: date | None
    period_end: date | None
    earned: Decimal
    vacation_lump: Decimal
    # Удержание в депозит, посчитанное провизорным прогоном. Из ``earned`` оно уже вычтено, а на
    # депозитный счёт ляжет только при финализации: строка леджера датируется концом периода.
    # Поэтому на дату внутри периода эти деньги не лежат нигде, и срез обязан добавить их в
    # депозит сам — иначе долг перед сотрудником занижен ровно на них.
    deposit_withholding: Decimal
    # Период был финализирован НА ЭТУ ДАТУ: заработанное уже несёт хвост ведомости, и
    # складывать его сюда значило бы посчитать одни деньги дважды.
    period_finalized: bool


async def _period_containing(session: AsyncSession, as_of: date) -> PayrollPeriod | None:
    """Недельный период, содержащий дату, БЕЗ фильтра по статусу.

    Статус — свойство «сейчас», а вопрос исторический: на 31.08 нужен тот период, в который
    31.08 попадает, независимо от того, закрыли его потом или нет.
    """
    return await session.scalar(
        select(PayrollPeriod)
        .where(
            PayrollPeriod.period_type == "week",
            PayrollPeriod.start_date <= as_of,
            PayrollPeriod.end_date >= as_of,
        )
        .order_by(PayrollPeriod.start_date.desc())
        .limit(1)
    )


def _finalized_by(period: PayrollPeriod | None, as_of: date) -> bool:
    """Был ли период финализирован на конец дня ``as_of``.

    Смотрим на ДАТУ финализации, а не на статус: витрина «на сейчас» сравнивает границы со
    множеством финализированных периодов и потому на историческую дату обнуляет заработок
    периода, который закрыли только в сентябре. Хвост ведомости его тоже не подхватит — он
    появится позже as_of. Зарплата за такую неделю исчезала из среза целиком.
    """
    if period is None or period.finalized_at is None:
        return False
    return period.finalized_at.date() <= as_of


async def earned_as_of(session: AsyncSession, employee: Employee, as_of: date) -> EarnedAsOf:
    """Сколько сотрудник заработал к дате в периоде, который эту дату содержит."""
    position = await get_position_on_date(session, employee.id, as_of)
    position = position or employee.position or ""

    if position in okladnik_positions():
        basis = "okladnik"
    elif position in dishwasher_positions():
        basis = "dishwasher"
    else:
        basis = "production"

    if basis == "production":
        period = await _period_containing(session, as_of)
        if _finalized_by(period, as_of):
            return EarnedAsOf(
                employee_id=employee.id,
                as_of=as_of,
                basis=basis,
                period_start=period.start_date if period else None,
                period_end=period.end_date if period else None,
                earned=Decimal("0.00"),
                vacation_lump=Decimal("0.00"),
                deposit_withholding=Decimal("0.00"),
                period_finalized=True,
            )
        if period is None:
            # Недельного периода, содержащего дату, нет вовсе (начало горизонта учёта).
            # Звать расчёт нельзя: без периода он падает на авансовый фолбэк «первый
            # нефинализированный период с payroll_date >= as_of», то есть на период ИЗ
            # БУДУЩЕГО, и приносит в исторический срез его отпускной транш.
            return EarnedAsOf(
                employee_id=employee.id,
                as_of=as_of,
                basis=basis,
                period_start=None,
                period_end=None,
                earned=Decimal("0.00"),
                vacation_lump=Decimal("0.00"),
                deposit_withholding=Decimal("0.00"),
                period_finalized=False,
            )
        earned, start, end, _note, lump, withheld = await _production_earned(
            session, employee, as_of, period=period
        )
    else:
        start, end = _half_month_bounds(as_of)
        # Полумесячный период строится по календарю, но узнать о его финализации можно только
        # у реальной записи с теми же границами.
        real = await session.scalar(
            select(PayrollPeriod).where(
                PayrollPeriod.period_type == "half_month",
                PayrollPeriod.start_date == start,
                PayrollPeriod.end_date == end,
            )
        )
        if _finalized_by(real, as_of):
            return EarnedAsOf(
                employee_id=employee.id,
                as_of=as_of,
                basis=basis,
                period_start=start,
                period_end=end,
                earned=Decimal("0.00"),
                vacation_lump=Decimal("0.00"),
                deposit_withholding=Decimal("0.00"),
                period_finalized=True,
            )
        earned, start, end, _note, lump = await _basis_earned(
            session, employee, position, basis, as_of
        )
        # У окладника и мойщицы депозит не удерживается — контур только производственный.
        withheld = Decimal("0.00")

    return EarnedAsOf(
        employee_id=employee.id,
        as_of=as_of,
        basis=basis,
        period_start=start,
        period_end=end,
        earned=decimal(earned).quantize(Decimal("0.01")),
        vacation_lump=decimal(lump).quantize(Decimal("0.01")),
        deposit_withholding=decimal(withheld).quantize(Decimal("0.01")),
        period_finalized=False,
    )
