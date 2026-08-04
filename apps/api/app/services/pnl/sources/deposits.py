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

ОТМЕНИТЬ МОЖНО ТОЛЬКО ТО, ЧТО ОТЧЁТ ПРИЗНАВАЛ. Пачкой от 23.07.2026 закрыты фонды 2024 и
2025 годов давно уволенных — 93 762 ₽. Управленческий учёт ведётся с 01.07.2026, тех
начислений отчёт не видел, и их отмена не уменьшает расход июля ни на рубль: владелец назвал
такие списания рудиментарными и попросил убрать их из строки целиком. Сначала они входили в
строку с предупреждением — предупреждение объясняло цифру, но цифра всё равно была чужой.

Отсекаются они не по году, а по ДАТАМ НАЧИСЛЕНИЯ фонда: считается, какая доля счёта накоплена
уже внутри горизонта отчёта, и отменяется только она. Год для этого слишком груб — фонд
копится помесячно, и счёт текущего года наполовину относится к месяцам до начала учёта.
Проверка на данных: у всех восьми июльских списаний доля внутри горизонта равна нулю, включая
счёт 2026 года.

МЕСЯЦ СЧИТАЕТСЯ ПО МОСКОВСКОМУ ВРЕМЕНИ. У обеих таблиц единственная дата — ``created_at``
в UTC. Списание, сделанное 1 августа в 00:30 МСК, при наивной группировке уехало бы в июль.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import case, func, select
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
FUND_ACCRUAL_TYPE = "accrual"

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
    #: Отмена расхода, признанного ЭТИМ отчётом, — только она и попадает в строку.
    fund_forfeited: Decimal = Decimal("0.00")
    #: Списание фондов, начисленных до начала управленческого учёта. В строку НЕ идёт.
    fund_forfeited_out_of_horizon: Decimal = Decimal("0.00")
    fund_forfeited_out_of_horizon_count: int = 0
    fund_forfeited_out_of_horizon_years: set[int] = field(default_factory=set)
    deposit_entries: list[ReleaseEntry] = field(default_factory=list)
    #: Только те списания, что относятся к отчёту. Разбор исторических хвостов сюда не идёт.
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
    in_horizon_share = await _in_horizon_accrual_share(
        session, {transaction.account_id for transaction in fund_rows}, horizon_start
    )
    for transaction in fund_rows:
        amount = transaction.amount or Decimal("0.00")
        # Отменять можно только тот расход, который отчёт когда-то признал. Долю фонда,
        # начисленную до начала учёта, он не видел — её отмена сюда не относится.
        share = in_horizon_share.get(transaction.account_id, Decimal("0.00"))
        counted = (amount * share).quantize(Decimal("0.01"))
        skipped = amount - counted
        if skipped > 0:
            result.fund_forfeited_out_of_horizon += skipped
            result.fund_forfeited_out_of_horizon_count += 1
            result.fund_forfeited_out_of_horizon_years.add(transaction.year)
        if counted <= 0:
            continue
        result.fund_forfeited += counted
        result.fund_entries.append(
            ReleaseEntry(
                employee_id=transaction.employee_id,
                amount=counted,
                happened_on=_msk_date(transaction.created_at),
                period_year=transaction.year,
                comment=transaction.comment,
            )
        )

    return result


async def _in_horizon_accrual_share(
    session: AsyncSession, account_ids: set[uuid.UUID], horizon_start: date
) -> dict[uuid.UUID, Decimal]:
    """Какая доля фонда каждого счёта начислена уже внутри горизонта отчёта.

    Доля, а не флаг: фонд копится помесячно весь год, и счёт 2026 года у работающего
    сотрудника наполовину относится к месяцам до начала учёта. Резать по ``year`` было бы
    грубее — январский и августовский рубль этого года попали бы в одну корзину.
    """
    if not account_ids:
        return {}
    cutoff = datetime.combine(horizon_start, datetime.min.time()) - MOSCOW_OFFSET
    rows = (
        await session.execute(
            select(
                AccumulationFundTransaction.account_id,
                func.sum(AccumulationFundTransaction.amount),
                func.sum(
                    case(
                        (
                            AccumulationFundTransaction.created_at >= cutoff,
                            AccumulationFundTransaction.amount,
                        ),
                        else_=0,
                    )
                ),
            )
            .where(
                AccumulationFundTransaction.account_id.in_(account_ids),
                AccumulationFundTransaction.transaction_type == FUND_ACCRUAL_TYPE,
            )
            .group_by(AccumulationFundTransaction.account_id)
        )
    ).all()
    shares: dict[uuid.UUID, Decimal] = {}
    for account_id, total, inside in rows:
        total = total or Decimal("0.00")
        shares[account_id] = (inside or Decimal("0.00")) / total if total > 0 else Decimal("0.00")
    return shares
