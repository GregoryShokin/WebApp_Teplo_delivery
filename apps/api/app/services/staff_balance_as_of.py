"""Обязательства перед сотрудниками НА ДАТУ — вторая половина расчётов в балансе.

ЧЕМ ОТЛИЧАЕТСЯ ОТ ВИТРИНЫ «Расчёты с сотрудниками». Та отвечает на вопрос «сколько мы должны
СЕЙЧАС» и делает это ТЕКУЩИМИ СТАТУСАМИ: ``SalaryAdvance.status='issued'``,
``EmployeePayout.status='paid'``, ``AccumulationFundAccount.status='active'``,
``Employee.status in (active, dismissing)``. Статус истории не хранит: заём, погашенный в
августе, в срезе на 31 июля не появится ни при какой доработке фильтра — строка просто больше
не проходит по ``status``. Поэтому срез собран РЯДОМ, а витрина не тронута: её цифры сверены
владельцем вручную, и ломалась она уже несколько раз.

ЧЕМ ОТСЕКАЕМ ПЕРИОД. У каждого слагаемого — своя хозяйственная дата, и это не небрежность:

* удержание депозита и начисление фонда — ``happened_on``, то есть КОНЕЦ ПЕРИОДА РАБОТЫ
  (решение владельца 08.08.2026, миграция 0272). Неделя 25–31.08 платится 2 сентября и тогда
  же финализируется, но заработаны эти деньги в августе — и в августовском балансе им место;
* хвост ведомости — ``PayrollPeriod.finalized_at``, а не ``status``. Ведомость, закрытая
  5 сентября, на 31 августа ещё не была обязательством определённого размера: строки прогона
  до финализации — превью, их пересчитывают;
* выплата — дата ПРОВОДКИ ДДС, которой она заплачена, а снятие отметки — дата её реверса.
  Отдельно расписано в ``_paid_by_employee``;
* аванс/заём — ``issued_on`` и ``closed_on``, удержание — ``applied_at`` своей строки.

ВЕДОМОСТЬ «В ПОЛЁТЕ» В СРЕЗ НЕ ВХОДИТ ВОВСЕ (решение владельца 08.08.2026). Нефинализированный
прогон не приносит ни хвоста, ни удержания депозита, ни начисления фонда: пока прогон открыт,
его строки — превью, а не факт. Заработанное за уже отработанные дни такого периода приходит
не оттуда, а из ``earned_as_of`` — расчёта по явкам.

ДО 01.07.2026 СРЕЗ ОТКАЗЫВАЕТ. Управленческий учёт начат этой датой (``ACCOUNTING_START``), и
всё, что раньше, свёрнуто во входящие остатки. Ответить «на 30 июня было столько-то» этот
расчёт не может: движений до опорной точки в системе нет, а уверенная цифра, собранная из их
отсутствия, — ложь. Та же граница и по той же причине стоит у признания расходов.

ЧЕГО ЗДЕСЬ НЕТ. Это обязательства перед СОТРУДНИКАМИ: ни поставщиков, ни денег на счетах, ни
основных средств. Баланс соберётся из нескольких таких источников — этот закрывает свой.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AccumulationFundTransaction,
    AttendanceEntry,
    CashflowTransaction,
    CourierDepositAccount,
    DdsArticle,
    DepositTransaction,
    Employee,
    EmployeePayout,
    EmployeePayoutOffset,
    PayrollLine,
    PayrollPayment,
    PayrollPayoutBooking,
    PayrollPeriod,
    PayrollRun,
    SalaryAdvance,
    SalaryAdvanceRecovery,
)
from app.services.accounting_periods import ACCOUNTING_START
from app.services.couriers.deposit_service import balances_as_of as courier_balances_as_of
from app.services.deposit_integrity import ledger_delta
from app.services.employee_effective_events import get_position_on_date
from app.services.payroll_admin import (
    _ONDEMAND_PAYOUT_STATUSES,
    EMPLOYEE_PAYOUT_KIND_OWNER_SALARY,
    PAYOUT_MODE_ON_DEMAND,
    _load_okladnik_payout_modes,
)
from app.services.payroll_advance_availability import _finalized_by, earned_as_of
from app.services.payroll_payouts import DDS_ARTICLE_DEPOSIT_PAYOUT
from app.services.position_registry import eligible_for_personal_report

_CENTS = Decimal("0.01")
_ZERO = Decimal("0.00")

# Сколько дней назад смотрим явки, подбирая, кому вообще считать заработанное. Статус
# сотрудника — свойство «сейчас»: уволенный в сентябре на 31 августа работал и деньги
# заработал, а по фильтру ``status in (active, dismissing)`` он бы из среза выпал целиком.
# 45 дней покрывают и недельный контур, и полумесячный с запасом.
_EARNED_LOOKBACK_DAYS = 45

# День, когда миграция 0272 завела ``salary_advance.closed_on`` и заполнила его бэкфиллом по
# ``updated_at`` — то есть по ПОСЛЕДНЕМУ КАСАНИЮ строки, а не по дате решения о списании.
# Раньше этого дня колонки не существовало, поэтому любое значение не позже него — бэкфилл, и
# сумма такого займа обязана попасть в ``approximate_write_offs``. Значения после ставит код
# (``write_off_advance``/``cancel_advance``) в день самого решения — они точны.
_CLOSED_ON_BACKFILL_DATE = date(2026, 8, 8)

# Статусы выдачи, где деньги сотруднику ещё не ушли: разрешение на выдачу оформлено, но
# ``disburse_kassa_advance``/банк его не исполнили. Долга не существовало ни на один день —
# ``issued_on`` у такой строки лишь намерение, и при исполнении он переписывается на день
# фактической выдачи.
_ADVANCE_NOT_YET_MONEY = ("awaiting_payout",)


class BeforeAccountingStart(ValueError):
    """Срез запрошен на дату раньше начала учёта — данных за неё нет вовсе.

    Отдельный класс, а не ``PeriodClosed``: закрытый месяц — это «менять нельзя, но цифры
    есть», а здесь цифр нет вовсе и никакое открытие периода их не создаст. Роут переводит
    это в 422 (негодный ввод), а не в 409 (конфликт состояния).
    """


def _money(value: object) -> Decimal:
    return Decimal(str(value or 0)).quantize(_CENTS)


def _msk_date(column):
    """Момент записи → календарный день ПО МОСКВЕ.

    Так считает дату весь леджерный код (``pnl/sources/deposits.py``, бэкфилл миграции 0272).
    По UTC всё, что заведено между полуночью и тремя часами ночи, уезжало бы в предыдущий
    день, а вместе с ним — в предыдущий месяц баланса.
    """
    return func.date(func.timezone("Europe/Moscow", column))


def _month_key(value: date) -> str:
    return f"{value.year:04d}-{value.month:02d}"


@dataclass(frozen=True)
class StaffBalanceRow:
    """Расчёты с одним сотрудником на дату. Компоненты валовые — как в витрине."""

    employee_id: uuid.UUID
    full_name: str
    # Должность ТЕКУЩАЯ — подпись строки для человека, а не участник расчёта. В самом расчёте
    # должность берётся на дату (``_earned_in_open_period``), здесь она только для глаз.
    position: str | None
    # Зарплатный долг целиком: хвост финализированных ведомостей + заработанное в открытом
    # периоде + отпускной транш + остаток «по востребованию».
    salary: Decimal
    # Отпускные внутри ``salary`` — справочно, вторым слагаемым не считать.
    vacation: Decimal
    fund: Decimal
    production_deposit: Decimal
    courier_deposit: Decimal
    # Нам должен сотрудник: непогашенные авансы/займы, выплаты вне ведомости, переплата
    # «по востребованию».
    receivable: Decimal

    @property
    def payable(self) -> Decimal:
        return _money(
            self.salary + self.fund + self.production_deposit + self.courier_deposit
        )


@dataclass(frozen=True)
class StaffBalanceAsOf:
    """Срез расчётов с сотрудниками плюс честные признаки неполноты.

    ``approximate_payments`` — выплаты, которых нет в иммутабельном леджере
    ``PayrollPayoutBooking``: их пришлось взять из ``PayrollPayment``, а эта строка при снятии
    отметки удаляется физически. Пока цифра не ноль, срез прошлого месяца может показать
    выплату, которую к тому дню ещё отменили (или наоборот).

    ``approximate_write_offs`` — остатки займов, чья дата закрытия получена бэкфиллом
    (``updated_at``, последнее касание строки). На срез они попали или не попали по этой дате.
    """

    as_of: date
    rows: list[StaffBalanceRow]
    payable_total: Decimal
    receivable_total: Decimal
    salary_total: Decimal
    vacation_total: Decimal
    fund_total: Decimal
    production_deposit_total: Decimal
    courier_deposit_total: Decimal
    approximate_payments: Decimal
    approximate_write_offs: Decimal


async def _finalized_runs(session: AsyncSession, as_of: date) -> list[uuid.UUID]:
    """Прогоны, чьи начисления на дату уже были обязательством: по одному на период.

    Три условия, и каждое куплено разбором:

    * ``finalized_at``, а НЕ ``status``. Витрина «на сейчас» берёт множество финализированных
      периодов без даты и потому в историческом срезе показала бы ведомость, закрытую только
      в сентябре, уже в августе. Правило совпадает до бита с ``_finalized_by`` из
      ``payroll_advance_availability`` — оно импортировано, а не переписано: разойдись эти два
      места на один день, и заработок недели либо пропадёт из среза целиком (тут ещё нет
      хвоста, там уже нет заработанного), либо посчитается дважды;
    * ПОСЛЕДНИЙ прогон периода со ``status='finalized'``. Пересчёт заводит новый прогон, а
      старый остаётся в базе со своими строками — сложить их значило бы удвоить ведомость;
    * ``is_imported_legacy=False``. Легаси-заливка выплачена вне системы; включи её — и в
      кредиторке всплывут фантомные миллионы, которых никто никому не должен.
    """
    rows = (
        await session.execute(
            select(PayrollRun.id, PayrollRun.period_id, PayrollPeriod)
            .join(PayrollPeriod, PayrollPeriod.id == PayrollRun.period_id)
            .where(
                PayrollRun.status == "finalized",
                PayrollRun.is_imported_legacy.is_(False),
            )
            .order_by(PayrollRun.period_id, PayrollRun.started_at.desc())
        )
    ).all()
    latest: dict[uuid.UUID, uuid.UUID] = {}
    for run_id, period_id, period in rows:
        if period_id in latest or not _finalized_by(period, as_of):
            continue
        latest[period_id] = run_id
    return list(latest.values())


async def _accrued_by_employee(
    session: AsyncSession, run_ids: list[uuid.UUID]
) -> dict[tuple[uuid.UUID, uuid.UUID], Decimal]:
    """Начислено ведомостью по (прогон, сотрудник) — ``PayrollLine.total_payable``.

    Ключ парный, потому что выплату ниже приходится искать тоже по паре: у сотрудника с двумя
    ролями в одном прогоне две строки, а отметка выплаты одна на прогон.
    """
    rows = (
        await session.execute(
            select(
                PayrollLine.run_id,
                PayrollLine.employee_id,
                func.coalesce(func.sum(PayrollLine.total_payable), 0),
            )
            .where(PayrollLine.run_id.in_(run_ids))
            .group_by(PayrollLine.run_id, PayrollLine.employee_id)
        )
    ).all()
    return {(run_id, employee_id): _money(total) for run_id, employee_id, total in rows}


async def _paid_by_employee(
    session: AsyncSession,
    accrued: dict[tuple[uuid.UUID, uuid.UUID], Decimal],
    as_of: date,
) -> tuple[dict[tuple[uuid.UUID, uuid.UUID], Decimal], Decimal]:
    """Выплачено по ведомости на дату — по ИММУТАБЕЛЬНОМУ леджеру, а не по отметке выплаты.

    СНЯТАЯ ОТМЕТКА ЗНАЧИТ «ДЕНЬГИ ВЕРНУЛИСЬ» (решение владельца 08.08.2026), и это событие
    своей даты: срез на 31 августа обязан видеть выплату, отменённую 5 сентября. По
    ``PayrollPayment`` этого не увидеть никогда — ``unmark_payment`` удаляет строку ФИЗИЧЕСКИ,
    вместе со всей историей. Зато остаётся ``PayrollPayoutBooking``: он переживает удаление
    отметки (``payment_id`` обнуляется, ключ «прогон + сотрудник» остаётся) и хранит обе
    даты — исходной проводки ДДС и её финансового реверса.

    Отсюда формула: выплата существует на дату, если проводка прошла не позже неё И реверса
    либо нет, либо он позже даты.

    ВЫДАЧА ДЕПОЗИТА ИЗ СУММЫ ВЫПЛАТ ИСКЛЮЧЕНА. Тем же механизмом книжится расход по статье
    «Выдача депозита сотруднику»: физически это одна выдача денег вместе с зарплатой, но
    обязательство гасится РАЗНОЕ. Депозит уже уменьшен своей строкой леджера
    (``deposit_transaction`` типа ``payout``), и посчитай мы его ещё и здесь — зарплатный
    хвост уменьшился бы на депозитные деньги, то есть одно погашение закрыло бы два долга.

    ФОЛБЭК И ЧЕСТНОСТЬ. У пары (прогон, сотрудник), где booking'а нет вовсе (старые выплаты
    до появления леджера), берём ``PayrollPayment`` — но такие суммы возвращаются отдельно и
    попадают в ``approximate_payments``. Выдавать их за факт нельзя: строка отметки живёт
    «сейчас», и её снятие стирает прошлое. Отметка без ``paid_at`` в срез не идёт: датировать
    её нечем, а недатированная выплата в историческом расчёте — это выплата на любую дату.
    """
    if not accrued:
        return {}, _ZERO
    run_ids = list({run_id for run_id, _ in accrued})
    reversal = CashflowTransaction.__table__.alias("reversal")
    original = CashflowTransaction.__table__.alias("original")
    booking_rows = (
        await session.execute(
            select(
                PayrollPayoutBooking.run_id,
                PayrollPayoutBooking.employee_id,
                original.c.operation_date,
                reversal.c.operation_date,
                PayrollPayoutBooking.amount,
                DdsArticle.code,
            )
            .join(original, original.c.id == PayrollPayoutBooking.cashflow_transaction_id)
            .outerjoin(
                reversal, reversal.c.id == PayrollPayoutBooking.reversal_transaction_id
            )
            .outerjoin(DdsArticle, DdsArticle.id == original.c.article_id)
            .where(PayrollPayoutBooking.run_id.in_(run_ids))
        )
    ).all()

    paid: dict[tuple[uuid.UUID, uuid.UUID], Decimal] = {}
    booked_pairs: set[tuple[uuid.UUID, uuid.UUID]] = set()
    for run_id, employee_id, paid_on, reversed_on, amount, article_code in booking_rows:
        key = (run_id, employee_id)
        if article_code == DDS_ARTICLE_DEPOSIT_PAYOUT:
            # Выдача депозита идёт тем же механизмом, но зарплатным долгом не является и
            # доказательством покрытия — тоже: пара, у которой в леджере только депозитная
            # строка, зарплату получила мимо него, и фолбэк по ``PayrollPayment`` ей нужен.
            continue
        booked_pairs.add(key)
        if paid_on is None or paid_on > as_of:
            continue
        if reversed_on is not None and reversed_on <= as_of:
            continue
        paid[key] = paid.get(key, _ZERO) + _money(amount)

    missing = [key for key in accrued if key not in booked_pairs]
    approximate = _ZERO
    if missing:
        fallback_rows = (
            await session.execute(
                select(
                    PayrollPayment.run_id,
                    PayrollPayment.employee_id,
                    PayrollPayment.amount,
                ).where(
                    PayrollPayment.run_id.in_({run_id for run_id, _ in missing}),
                    PayrollPayment.status.in_(("paid", "partially_paid")),
                    PayrollPayment.paid_at <= as_of,
                )
            )
        ).all()
        missing_set = set(missing)
        for run_id, employee_id, amount in fallback_rows:
            key = (run_id, employee_id)
            if key not in missing_set:
                continue
            value = _money(amount)
            paid[key] = paid.get(key, _ZERO) + value
            approximate += value
    return paid, _money(approximate)


async def _salary_tail(
    session: AsyncSession, as_of: date
) -> tuple[dict[uuid.UUID, Decimal], Decimal]:
    """Невыплаченный хвост ведомостей, финализированных к дате.

    Зачёт по сотруднику, а не по прогону: переплата одной ведомости гасит недоплату другой —
    ровно так же считает витрина, и расхождение двух цифр об одном человеке было бы хуже
    любой из двух методик.
    """
    run_ids = await _finalized_runs(session, as_of)
    if not run_ids:
        return {}, _ZERO
    accrued = await _accrued_by_employee(session, run_ids)
    paid, approximate = await _paid_by_employee(session, accrued, as_of)

    totals: dict[uuid.UUID, Decimal] = {}
    for (run_id, employee_id), amount in accrued.items():
        totals[employee_id] = (
            totals.get(employee_id, _ZERO) + amount - paid.get((run_id, employee_id), _ZERO)
        )
    return {
        employee_id: _money(value) for employee_id, value in totals.items() if value > 0
    }, approximate


async def _employees_for_earned(session: AsyncSession, as_of: date) -> list[Employee]:
    """Кому вообще считать заработанное в открытом на дату периоде.

    Фильтр по ``Employee.status`` для исторического среза не годится: уволенный в сентябре
    31 августа работал, и его последняя неделя — такой же долг, как у остальных. Поэтому к
    работающим сейчас добавляются те, у кого есть явка за последние ``_EARNED_LOOKBACK_DAYS``
    дней до даты, и те, у кого есть строка ведомости периода, содержащего дату (окладник
    явок не даёт вовсе — его в списке держит только это условие).
    """
    recent_attendance = select(AttendanceEntry.employee_id).where(
        AttendanceEntry.work_date <= as_of,
        AttendanceEntry.work_date >= as_of - timedelta(days=_EARNED_LOOKBACK_DAYS),
    )
    lines_in_period = (
        select(PayrollLine.employee_id)
        .join(PayrollRun, PayrollRun.id == PayrollLine.run_id)
        .join(PayrollPeriod, PayrollPeriod.id == PayrollRun.period_id)
        .where(PayrollPeriod.start_date <= as_of, PayrollPeriod.end_date >= as_of)
    )
    rows = await session.scalars(
        select(Employee)
        .where(
            Employee.is_freelancer_placeholder.is_(False),
            Employee.admin_payroll_excluded.is_(False),
            or_(
                Employee.status.in_(("active", "dismissing")),
                Employee.id.in_(recent_attendance),
                Employee.id.in_(lines_in_period),
            ),
        )
        .order_by(Employee.full_name)
    )
    return list(rows.all())


async def _earned_in_open_period(
    session: AsyncSession, as_of: date, on_demand_months: dict[uuid.UUID, set[str]]
) -> tuple[dict[uuid.UUID, tuple[Decimal, Decimal]], dict[uuid.UUID, Decimal]]:
    """Заработанное в периоде, который дата содержит, — то, чего ещё нет ни в одной ведомости.

    Считает ``earned_as_of``: он выбирает период ПО ДАТЕ, а не по статусу, и сам говорит,
    был ли период финализирован к этому дню. Если был — заработок не добавляем: его целиком
    несёт хвост из ``_salary_tail``, и второе слагаемое было бы задвоением тех же денег.

    Должность берём НА ДАТУ (``get_position_on_date``): курьера, ставшего в октябре поваром,
    августовский срез не должен считать поваром — заработок посчитался бы по чужому контуру.

    Режим «по востребованию» — единственный, где заработок и начисление конкурируют за одну
    сумму: месяц начисляется ЦЕЛИКОМ вместе с прогоном ведомости. Если начисление за месяц
    даты уже проведено, прорейт обнуляем — иначе те же деньги встанут в срез дважды.

    Вторым словарём отдаётся ПРОВИЗОРНОЕ УДЕРЖАНИЕ В ДЕПОЗИТ (решение владельца 08.08.2026:
    «добавлять предварительно»). Из заработка оно уже вычтено, а на депозитный счёт ляжет
    только при финализации — строка леджера датируется концом периода. Поэтому на дату внутри
    периода эти деньги не лежат нигде: из зарплаты ушли, до счёта не дошли. Долг перед
    сотрудником занижался ровно на них, у каждого производственника, на каждую дату среза,
    не совпадающую с концом недели.
    """
    earned: dict[uuid.UUID, tuple[Decimal, Decimal]] = {}
    provisional_deposit: dict[uuid.UUID, Decimal] = {}
    month_key = _month_key(as_of)
    for employee in await _employees_for_earned(session, as_of):
        position = await get_position_on_date(session, employee.id, as_of)
        position = position or employee.position or ""
        if not eligible_for_personal_report(
            position, admin_payroll_excluded=employee.admin_payroll_excluded
        ):
            continue
        snapshot = await earned_as_of(session, employee, as_of)
        if snapshot.period_finalized:
            continue
        value = _money(snapshot.earned)
        vacation = _money(snapshot.vacation_lump)
        if month_key in on_demand_months.get(employee.id, frozenset()):
            # Обнуляем ИМЕННО прорейт: отпускной транш этот месяц начислением не несёт,
            # и вместе с прорейтом он пропал бы из среза ни за что.
            value = _ZERO
        if value or vacation:
            earned[employee.id] = (value, vacation)
        withheld = _money(snapshot.deposit_withholding)
        if withheld:
            provisional_deposit[employee.id] = withheld
    return earned, provisional_deposit


async def _on_demand_as_of(
    session: AsyncSession, as_of: date
) -> tuple[dict[uuid.UUID, Decimal], dict[uuid.UUID, set[str]]]:
    """Остаток зарплаты «по востребованию» на дату: начислено минус забрано.

    Историческая версия ``payroll_admin.compute_on_demand_debt``. Та складывает ВСЕ
    on_demand-начисления и ВСЕ выплаты, потому что отвечает про «сейчас». Здесь у обеих
    сторон должна быть дата:

    * начисление датируем концом периода прогона — той же хозяйственной датой, что удержание
      депозита и начисление фонда (решение владельца 08.08.2026). Прогон при этом обязан быть
      финализирован: строки открытого прогона — превью, ведомость «в полёте» в срез не входит;
    * выплата — ``EmployeePayout.payout_date``. Статусы те же, что у витрины
      (``included/pending/paid``): истории статуса в данных нет, поэтому дату берём из
      строки, а состояние — текущее. Черновик и отменённую выплату это не пускает никогда.

    Возвращает ещё и месяцы, за которые начисление уже проведено: по ним обнуляется
    синтетический прорейт в ``_earned_in_open_period``.
    """
    # Кандидаты берутся БЕЗ фильтра по статусу сотрудника — в отличие от диалога выплаты
    # (``list_on_demand_employees``), который справедливо показывает только работающих.
    # Здесь фильтр по «сейчас» стирал бы прошлое: собственник, ушедший в октябре, обнулял бы
    # свой августовский долг задним числом — ровно тот дефект, ради которого пишется срез.
    modes = await _load_okladnik_payout_modes(session)
    on_demand_positions = [
        position for position, mode in modes.items() if mode == PAYOUT_MODE_ON_DEMAND
    ]
    if not on_demand_positions:
        return {}, {}
    employee_ids = list(
        (
            await session.scalars(
                select(Employee.id).where(Employee.position.in_(on_demand_positions))
            )
        ).all()
    )
    if not employee_ids:
        return {}, {}

    rows = (
        await session.execute(
            select(PayrollLine.employee_id, PayrollLine.components, PayrollPeriod.end_date)
            .join(PayrollRun, PayrollRun.id == PayrollLine.run_id)
            .join(PayrollPeriod, PayrollPeriod.id == PayrollRun.period_id)
            .where(
                PayrollLine.employee_id.in_(employee_ids),
                PayrollRun.status == "finalized",
                PayrollPeriod.end_date <= as_of,
                # Финализация НА ДАТУ, а не текущий статус: ведомость, закрытая 3 сентября,
                # не имеет права приносить своё начисление в срез на 31 августа — там её
                # ещё не существовало. Тот же предикат, что у зарплатного хвоста.
                PayrollPeriod.finalized_at.is_not(None),
                func.date(PayrollPeriod.finalized_at) <= as_of,
            )
            .order_by(PayrollPeriod.end_date, PayrollRun.started_at)
        )
    ).all()
    accrued_by_month: dict[tuple[uuid.UUID, str], Decimal] = {}
    months: dict[uuid.UUID, set[str]] = {}
    for employee_id, components, period_end in rows:
        proration = components.get("proration") if isinstance(components, dict) else None
        if not isinstance(proration, dict) or not proration.get("on_demand"):
            continue
        raw = proration.get("accrual_amount")
        if raw is None:
            continue
        # Обе половины месяца несут ПОЛНЫЙ оклад — начисление считаем один раз на
        # (сотрудник, месяц). Без ``period_month`` месяц берём у периода: строка всё равно
        # датирована, и терять её молча нельзя.
        month = str(proration.get("period_month") or _month_key(period_end))
        accrued_by_month[(employee_id, month)] = _money(raw)
        months.setdefault(employee_id, set()).add(month)

    paid_rows = (
        await session.execute(
            select(
                EmployeePayout.employee_id,
                func.coalesce(func.sum(EmployeePayout.amount), 0),
            )
            .where(
                EmployeePayout.employee_id.in_(employee_ids),
                EmployeePayout.kind == EMPLOYEE_PAYOUT_KIND_OWNER_SALARY,
                EmployeePayout.status.in_(_ONDEMAND_PAYOUT_STATUSES),
                EmployeePayout.payout_date <= as_of,
            )
            .group_by(EmployeePayout.employee_id)
        )
    ).all()
    paid = {employee_id: _money(total) for employee_id, total in paid_rows}

    balance: dict[uuid.UUID, Decimal] = {}
    for (employee_id, _month), amount in accrued_by_month.items():
        balance[employee_id] = balance.get(employee_id, _ZERO) + amount
    for employee_id, amount in paid.items():
        balance[employee_id] = balance.get(employee_id, _ZERO) - amount
    return {
        employee_id: _money(value) for employee_id, value in balance.items() if value
    }, months


async def _production_deposits_as_of(
    session: AsyncSession, as_of: date
) -> dict[uuid.UUID, Decimal]:
    """Производственные депозиты на дату — суммой ЛЕДЖЕРА, а не полем ``balance``.

    ``DepositAccount.balance`` — состояние на сегодня, истории у него нет вовсе. Леджер её
    хранит, и авторитетны в нём ровно те же строки, что для инварианта баланса
    (``deposit_integrity``): ручные (``run_id IS NULL``) и строки ФИНАЛИЗИРОВАННЫХ прогонов.
    Превью открытого прогона баланс не двигает — его удержание ещё пересчитают.

    Входящий остаток счёта отдельным слагаемым НЕ идёт: он материализован строкой леджера
    (ручной ``accrual``), и реконсиляция 2026-07-09 чистила ровно ту ситуацию, когда одни и
    те же исторические деньги лежали в леджере и в поле одновременно
    (``scripts/reconcile_deposit_ledger.py``). Сложить их снова значило бы вернуть тот баг.

    Знак: депозит — это наш долг сотруднику, отрицательным он быть не может. Минус означает
    дрейф леджера, а не дебиторку; его ловит ``find_deposit_balance_drift``, и делать вид,
    что сотрудник должен компании депозит, здесь нельзя.
    """
    rows = (
        await session.execute(
            select(
                DepositTransaction.employee_id,
                DepositTransaction.transaction_type,
                func.coalesce(func.sum(DepositTransaction.amount), 0),
            )
            .outerjoin(PayrollRun, PayrollRun.id == DepositTransaction.run_id)
            .where(
                or_(
                    DepositTransaction.run_id.is_(None),
                    PayrollRun.status == "finalized",
                ),
                func.coalesce(
                    DepositTransaction.happened_on, _msk_date(DepositTransaction.created_at)
                )
                <= as_of,
            )
            .group_by(DepositTransaction.employee_id, DepositTransaction.transaction_type)
        )
    ).all()
    totals: dict[uuid.UUID, Decimal] = {}
    for employee_id, transaction_type, amount in rows:
        totals[employee_id] = totals.get(employee_id, _ZERO) + ledger_delta(
            transaction_type, amount
        )
    return {
        employee_id: _money(value) for employee_id, value in totals.items() if value > 0
    }


async def _fund_as_of(session: AsyncSession, as_of: date) -> dict[uuid.UUID, Decimal]:
    """Накопительный фонд на дату — тоже суммой леджера.

    Статус счёта и статус сотрудника здесь не смотрим сознательно (решение владельца
    08.08.2026). Витрина «на сейчас» держится за ``status='active'`` и видимость в ростере, и
    для вопроса «сколько должны сегодня» это верно: списанный фонд уволенного — прибыль
    компании. Но 31 августа он ещё был долгом, а списали его в сентябре — и списание придёт
    своей датированной строкой ``forfeit``. Статус ту же работу делает задним числом и без
    следа.

    ``initial_balance`` — такая же строка леджера, как начисление, поэтому складывается
    наравне; ``payout`` и ``forfeit`` вычитаются.
    """
    rows = (
        await session.execute(
            select(
                AccumulationFundTransaction.employee_id,
                AccumulationFundTransaction.transaction_type,
                func.coalesce(func.sum(AccumulationFundTransaction.amount), 0),
            )
            .outerjoin(PayrollRun, PayrollRun.id == AccumulationFundTransaction.run_id)
            .where(
                func.coalesce(
                    AccumulationFundTransaction.happened_on,
                    _msk_date(AccumulationFundTransaction.created_at),
                )
                <= as_of,
                # Строка нефинализированного прогона в срез не входит (решение владельца).
                # Фонд кредитуется НА РАСЧЁТЕ, а не при финализации, поэтому без этого фильтра
                # начисление ведомости «в полёте» попадало бы в обязательства — вопреки
                # докстрингу этого же модуля.
                or_(
                    AccumulationFundTransaction.run_id.is_(None),
                    PayrollRun.status == "finalized",
                ),
            )
            .group_by(
                AccumulationFundTransaction.employee_id,
                AccumulationFundTransaction.transaction_type,
            )
        )
    ).all()
    totals: dict[uuid.UUID, Decimal] = {}
    for employee_id, transaction_type, amount in rows:
        delta = _money(amount)
        if transaction_type in ("payout", "forfeit"):
            delta = -delta
        totals[employee_id] = totals.get(employee_id, _ZERO) + delta
    return {
        employee_id: _money(value) for employee_id, value in totals.items() if value > 0
    }


async def _advances_as_of(
    session: AsyncSession, as_of: date
) -> tuple[dict[uuid.UUID, Decimal], Decimal]:
    """Непогашенные авансы и займы на дату — дебиторка, которую статус стирает.

    ``SalaryAdvance.recovered_amount`` — бегущий итог «сколько удержано СЕЙЧАС», для среза он
    бесполезен. Зато каждое удержание оставляет иммутабельную датированную строку
    ``SalaryAdvanceRecovery``: ``applied_at`` ставится финализацией ведомости и снимается её
    откатом, поэтому превью-строки (``applied_at IS NULL``) в срез не попадают сами собой.

    Дата у удержания — момент ФИНАЛИЗАЦИИ, а не конец периода работы. Это отличается от
    удержания депозита (там хозяйственная дата — конец периода, решение владельца
    08.08.2026), и разница видна на последней неделе месяца: депозит уменьшится в августе, а
    заём — в сентябре. Другой даты у ``SalaryAdvanceRecovery`` в данных нет.

    ``awaiting_payout`` исключён: разрешение на выдачу оформлено, но деньги не ушли, а
    ``issued_on`` у такой строки — намерение, которое при исполнении переписывается на день
    фактической выдачи.

    ``closed_on`` — день, когда заём перестал быть долгом (списан или отменён). У значений,
    доставшихся от бэкфилла миграции 0272, это ``updated_at``, то есть последнее касание
    строки: остаток такого займа уходит в ``approximate_write_offs`` независимо от того, по
    какую сторону даты он оказался.

    ОТМЕНА ВЫДАЧИ ЗДЕСЬ АСИММЕТРИЧНА ДЕНЬГАМ, и знать об этом надо. ``cancel_advance``
    УДАЛЯЕТ денежную проводку выдачи физически, то есть денежная половина баланса задним
    числом считает, что выдачи не было вовсе; дебиторка же по ``closed_on`` держит долг до
    дня отмены — так велит хозяйственная дата события. На срез между выдачей и отменой это
    даёт расхождение ровно на сумму отменённой выдачи. Отмена разрешена только пока не
    удержано ни рубля, поэтому речь всегда о недавних и обычно ошибочных строках.
    """
    recovered = (
        select(
            SalaryAdvanceRecovery.advance_id.label("advance_id"),
            func.coalesce(func.sum(SalaryAdvanceRecovery.amount), 0).label("recovered"),
        )
        .where(
            SalaryAdvanceRecovery.applied_at.is_not(None),
            _msk_date(SalaryAdvanceRecovery.applied_at) <= as_of,
        )
        .group_by(SalaryAdvanceRecovery.advance_id)
        .subquery()
    )
    rows = (
        await session.execute(
            select(
                SalaryAdvance.employee_id,
                SalaryAdvance.amount,
                SalaryAdvance.closed_on,
                func.coalesce(recovered.c.recovered, 0),
            )
            .outerjoin(recovered, recovered.c.advance_id == SalaryAdvance.id)
            .where(
                SalaryAdvance.issued_on <= as_of,
                SalaryAdvance.status.not_in(_ADVANCE_NOT_YET_MONEY),
            )
        )
    ).all()

    outstanding: dict[uuid.UUID, Decimal] = {}
    approximate = _ZERO
    for employee_id, amount, closed_on, recovered_amount in rows:
        remainder = max(_money(amount) - _money(recovered_amount), _ZERO)
        if not remainder:
            continue
        if closed_on is not None and closed_on <= _CLOSED_ON_BACKFILL_DATE:
            approximate += remainder
        if closed_on is not None and closed_on <= as_of:
            continue
        outstanding[employee_id] = outstanding.get(employee_id, _ZERO) + remainder
    return {
        employee_id: _money(value) for employee_id, value in outstanding.items()
    }, _money(approximate)


async def _employee_payouts_as_of(
    session: AsyncSession, as_of: date
) -> dict[uuid.UUID, Decimal]:
    """Выплаты сотруднику вне ведомости, ещё не зачтённые ею, — такая же дебиторка, как аванс.

    Без этого слагаемого срез разошёлся бы с витриной на всю сумму таких выплат, а долг
    компании перед человеком выглядел бы больше, чем он есть: деньги ему уже отдали,
    ведомость их ещё не зачла.

    Обе стороны датированы: выдача — ``payout_date``, зачёт — ``applied_at`` строки
    ``EmployeePayoutOffset`` (зеркало ``SalaryAdvanceRecovery``: превью-зачёты без даты в срез
    не попадают). ``kind='owner_salary'`` сюда не входит — тот контур целиком считает
    ``_on_demand_as_of``, и здесь он был бы вторым счётом тех же денег.
    """
    offsets = (
        select(
            EmployeePayoutOffset.employee_payout_id.label("payout_id"),
            func.coalesce(func.sum(EmployeePayoutOffset.amount), 0).label("offset_amount"),
        )
        .where(
            EmployeePayoutOffset.applied_at.is_not(None),
            _msk_date(EmployeePayoutOffset.applied_at) <= as_of,
        )
        .group_by(EmployeePayoutOffset.employee_payout_id)
        .subquery()
    )
    rows = (
        await session.execute(
            select(
                EmployeePayout.employee_id,
                func.coalesce(
                    func.sum(EmployeePayout.amount - func.coalesce(offsets.c.offset_amount, 0)),
                    0,
                ),
            )
            .outerjoin(offsets, offsets.c.payout_id == EmployeePayout.id)
            .where(
                EmployeePayout.kind.in_(("salary", "other")),
                EmployeePayout.status == "paid",
                EmployeePayout.payout_date <= as_of,
            )
            .group_by(EmployeePayout.employee_id)
        )
    ).all()
    return {
        employee_id: _money(total) for employee_id, total in rows if _money(total) > 0
    }


async def build_staff_balance_as_of(session: AsyncSession, *, as_of: date) -> StaffBalanceAsOf:
    """Обязательства и требования по сотрудникам на конец указанной даты (включительно)."""
    if as_of < ACCOUNTING_START:
        raise BeforeAccountingStart(
            f"Управленческий учёт ведётся с {ACCOUNTING_START:%d.%m.%Y} — "
            f"срез расчётов с сотрудниками на {as_of:%d.%m.%Y} собрать не из чего: "
            "движений до этой даты в системе нет, они свёрнуты во входящие остатки"
        )

    tail, approximate_payments = await _salary_tail(session, as_of)
    on_demand, on_demand_months = await _on_demand_as_of(session, as_of)
    earned, provisional_deposit = await _earned_in_open_period(session, as_of, on_demand_months)
    fund = await _fund_as_of(session, as_of)
    production_deposit = await _production_deposits_as_of(session, as_of)
    # Удержание незакрытой ведомости кладём на депозит сами: строки леджера ещё нет, а деньги
    # из зарплаты уже вычтены. Задвоения не будет — строка появится только при финализации,
    # и тогда же ``earned_as_of`` перестанет отдавать провизорную величину.
    for employee_id, withheld in provisional_deposit.items():
        production_deposit[employee_id] = production_deposit.get(employee_id, _ZERO) + withheld
    advances, approximate_write_offs = await _advances_as_of(session, as_of)
    payouts = await _employee_payouts_as_of(session, as_of)

    employee_ids = (
        set(tail)
        | set(on_demand)
        | set(earned)
        | set(fund)
        | set(production_deposit)
        | set(provisional_deposit)
        | set(advances)
        | set(payouts)
    )
    # Курьерские депозиты спрашиваем у своего модуля пачкой: формулу остатка (входящий +
    # пополнения − возвраты/удержания) держит он, и второй её копии здесь быть не должно.
    courier_candidates = (
        await session.scalars(
            select(CourierDepositAccount.employee_id)
            .join(Employee, Employee.id == CourierDepositAccount.employee_id)
            .where(Employee.is_freelancer_placeholder.is_(False))
        )
    ).all()
    courier_cents = await courier_balances_as_of(session, courier_candidates, as_of)
    courier_deposit = {
        employee_id: _money(Decimal(cents) / Decimal("100"))
        for employee_id, cents in courier_cents.items()
        if cents > 0
    }
    employee_ids |= set(courier_deposit)
    if not employee_ids:
        return StaffBalanceAsOf(
            as_of=as_of,
            rows=[],
            payable_total=_ZERO,
            receivable_total=_ZERO,
            salary_total=_ZERO,
            vacation_total=_ZERO,
            fund_total=_ZERO,
            production_deposit_total=_ZERO,
            courier_deposit_total=_ZERO,
            approximate_payments=approximate_payments,
            approximate_write_offs=approximate_write_offs,
        )

    employees = (
        await session.scalars(
            select(Employee)
            .where(
                Employee.id.in_(employee_ids),
                # Плейсхолдер внештатника — не человек, а строка-заглушка под смену: долга
                # перед ним не существует. Так же его выбрасывает витрина.
                Employee.is_freelancer_placeholder.is_(False),
            )
            .order_by(Employee.full_name)
        )
    ).all()

    rows: list[StaffBalanceRow] = []
    for employee in employees:
        earned_value, vacation = earned.get(employee.id, (_ZERO, _ZERO))
        debt = on_demand.get(employee.id, _ZERO)
        salary = _money(
            earned_value + vacation + tail.get(employee.id, _ZERO) + max(debt, _ZERO)
        )
        receivable = _money(
            advances.get(employee.id, _ZERO)
            + payouts.get(employee.id, _ZERO)
            + max(-debt, _ZERO)
        )
        row = StaffBalanceRow(
            employee_id=employee.id,
            full_name=employee.full_name,
            position=employee.position,
            salary=salary,
            vacation=vacation,
            fund=fund.get(employee.id, _ZERO),
            production_deposit=production_deposit.get(employee.id, _ZERO),
            courier_deposit=courier_deposit.get(employee.id, _ZERO),
            receivable=receivable,
        )
        if row.payable or row.receivable:
            rows.append(row)

    rows.sort(key=lambda row: max(row.payable, row.receivable), reverse=True)
    return StaffBalanceAsOf(
        as_of=as_of,
        rows=rows,
        payable_total=_money(sum((row.payable for row in rows), _ZERO)),
        receivable_total=_money(sum((row.receivable for row in rows), _ZERO)),
        salary_total=_money(sum((row.salary for row in rows), _ZERO)),
        vacation_total=_money(sum((row.vacation for row in rows), _ZERO)),
        fund_total=_money(sum((row.fund for row in rows), _ZERO)),
        production_deposit_total=_money(sum((row.production_deposit for row in rows), _ZERO)),
        courier_deposit_total=_money(sum((row.courier_deposit for row in rows), _ZERO)),
        approximate_payments=approximate_payments,
        approximate_write_offs=approximate_write_offs,
    )
