"""Невостребованные обязательства перед сотрудниками: депозиты и накопительный фонд.

ДВА ПОХОЖИХ СОБЫТИЯ С РАЗНОЙ ЭКОНОМИКОЙ, и путать их нельзя.

**Списание депозита — ЧИСТЫЙ ДОХОД.** Депозит удерживается из суммы К ВЫДАЧЕ, а расход ОПиУ
берётся по НАЧИСЛЕНИЮ: удержание никогда не уменьшало зарплатную строку, оно превращало
деньги в обязательство перед сотрудником. Списание гасит обязательство, ничего не возвращая
в расход, — поэтому строка живёт в «Доходах ниже EBITDA» с положительной величиной. Так же
это записано в методологии и в решении владельца от 24.05.2026.

**Списание накопительного фонда — ОТМЕНА РАСХОДА.** Фонд, в отличие от депозита, был
начислен сверх зарплаты и уже прошёл расходом через строку «Накопительный фонд». Когда
сотрудник уволился, не получив накопленное, признанный расход отменяется — и отменяется
там же, где был признан, отрицательным компонентом той же строки. Отдельной строкой дохода
это показывать нельзя: расход и его отмена разъехались бы по разным блокам отчёта.

ЦЕНА ВТОРОГО ПРАВИЛА ВИДНА НА ИЮЛЕ 2026. Пачкой от 23.07 закрыты фонды 2024 и 2025 годов
давно уволенных — 93 752 ₽ из 93 762 ₽. Начисления тех лет в этот отчёт не входили
(управленческий учёт ведётся с 01.07.2026), поэтому отмена приходит без своей пары и
уменьшает июльский расход на величину, к июлю не относящуюся. Спрятать её нельзя — это
настоящие деньги бизнеса; поэтому она попадает в строку и одновременно называется вслух
предупреждением с суммой и годами.

МЕСЯЦ СЧИТАЕТСЯ ПО МОСКОВСКОМУ ВРЕМЕНИ. У обеих таблиц единственная дата — ``created_at``
в UTC. Списание, сделанное 1 августа в 00:30 МСК, при наивной группировке уехало бы в июль.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.payroll import (
    AccumulationFundTransaction,
    DepositTransaction,
    PayrollRun,
)

#: Оба типа означают «депозит остался у бизнеса». Фильтр только по ``write_off`` дал бы за
#: июль 2026 ноль: единственное июльское списание сделано при увольнении.
DEPOSIT_WRITEOFF_TYPES = ("write_off", "dismissal_writeoff")

FUND_FORFEIT_TYPE = "forfeit"

#: Строка ведомости считается фактом только после финализации прогона. До неё списание —
#: превью расчёта: баланс сотрудника оно не двигает, а при переоткрытии прогона откатывается,
#: НЕ исчезая из таблицы. Ручные операции (``run_id`` пуст) применяются сразу и авторитетны
#: всегда. Это канон модуля депозитов, а не местное правило.
COUNTED_RUN_STATUSES = ("finalized",)

MOSCOW_OFFSET = timedelta(hours=3)


@dataclass(slots=True)
class ReleaseEntry:
    """Одно списание — для расшифровки строки."""

    employee_id: uuid.UUID | None
    amount: Decimal
    happened_on: date
    #: Год фонда. У депозита года нет — там обязательство без периода.
    period_year: int | None = None
    comment: str | None = None


@dataclass(slots=True)
class ReleaseMonth:
    deposits_written_off: Decimal = Decimal("0.00")
    fund_forfeited: Decimal = Decimal("0.00")
    #: Часть отменённого фонда, начисленная до начала управленческого учёта.
    fund_forfeited_before_horizon: Decimal = Decimal("0.00")
    fund_forfeited_years: set[int] = field(default_factory=set)
    deposit_entries: list[ReleaseEntry] = field(default_factory=list)
    fund_entries: list[ReleaseEntry] = field(default_factory=list)


def _msk_bounds(month_start: date, month_end: date) -> tuple[datetime, datetime]:
    """Границы месяца в UTC, соответствующие московским суткам.

    Сравнение делаем по UTC-значению самой колонки: так индекс по ``created_at`` остаётся
    рабочим, в отличие от ``created_at AT TIME ZONE ...`` в условии.
    """
    start = datetime.combine(month_start, datetime.min.time()) - MOSCOW_OFFSET
    end = datetime.combine(month_end + timedelta(days=1), datetime.min.time()) - MOSCOW_OFFSET
    return start, end


def _msk_date(moment: datetime) -> date:
    return (moment.replace(tzinfo=None) + MOSCOW_OFFSET).date()


async def build_release_month(
    session: AsyncSession,
    month_start: date,
    month_end: date,
    *,
    horizon_start: date,
) -> ReleaseMonth:
    """Списания депозитов и фонда за месяц.

    ``horizon_start`` — первое число месяца, с которого ведётся управленческий учёт. Нужен,
    чтобы отличить отмену расхода, признанного ЭТИМ отчётом, от отмены расхода, которого он
    никогда не видел.
    """
    result = ReleaseMonth()
    start, end = _msk_bounds(month_start, month_end)

    deposit_rows = (
        await session.execute(
            select(DepositTransaction, PayrollRun.status)
            .outerjoin(PayrollRun, PayrollRun.id == DepositTransaction.run_id)
            .where(
                DepositTransaction.transaction_type.in_(DEPOSIT_WRITEOFF_TYPES),
                DepositTransaction.created_at >= start,
                DepositTransaction.created_at < end,
            )
        )
    ).all()
    for transaction, run_status in deposit_rows:
        if transaction.run_id is not None and run_status not in COUNTED_RUN_STATUSES:
            continue
        amount = transaction.amount or Decimal("0.00")
        result.deposits_written_off += amount
        result.deposit_entries.append(
            ReleaseEntry(
                employee_id=transaction.employee_id,
                amount=amount,
                happened_on=_msk_date(transaction.created_at),
            )
        )

    fund_rows = (
        (
            await session.execute(
                select(AccumulationFundTransaction).where(
                    AccumulationFundTransaction.transaction_type == FUND_FORFEIT_TYPE,
                    AccumulationFundTransaction.created_at >= start,
                    AccumulationFundTransaction.created_at < end,
                )
            )
        )
        .scalars()
        .all()
    )
    for transaction in fund_rows:
        amount = transaction.amount or Decimal("0.00")
        result.fund_forfeited += amount
        result.fund_forfeited_years.add(transaction.year)
        if transaction.year < horizon_start.year:
            result.fund_forfeited_before_horizon += amount
        result.fund_entries.append(
            ReleaseEntry(
                employee_id=transaction.employee_id,
                amount=amount,
                happened_on=_msk_date(transaction.created_at),
                period_year=transaction.year,
                comment=transaction.comment,
            )
        )

    return result
