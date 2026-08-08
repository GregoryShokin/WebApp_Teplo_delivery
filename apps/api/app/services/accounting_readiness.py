"""Готовность месяца к закрытию: что молча не доехало до отчёта.

ЗАЧЕМ. Ночное признание расходов ПРОПУСКАЕТ начисление, чей период задевает закрытый месяц
(``supplier_service_periods.recognize_due_expenses``, ветка ``continue``), и делает это молча.
Начисление навсегда остаётся ``scheduled``: расхода нет, документ есть, кредиторка есть, а
сигнала нет ни одного — страница ДЗ/КЗ вдобавок пишет «расход признаётся сам, делать ничего не
нужно». Пропажу такого рода не видно ни в одной витрине: она не ломает равенств и не портит
остатки, просто в отчёте меньше расхода, чем было на самом деле.

Дефект спящий ровно до первого закрытия месяца — и просыпается в тот же миг. Поэтому проверка
должна существовать РАНЬШЕ, чем владелец первый раз закроет месяц ради балансового снимка.

ПОЧЕМУ ТОЛЬКО ЧТЕНИЕ. Автопочинка здесь была бы хуже болезни: «доложить расход в закрытый
месяц» — решение человека, а не джобы, и принимается оно вместе с решением, открывать ли месяц
заново. Сервис показывает и объясняет, чинит человек.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Counterparty, SupplierExpenseAccrual
from app.services import accounting_periods, clock

# Начисление, чей период кончился, а расхода нет. Отсечка «кончился» — по дате, а не по
# статусу документа: будущий период ждать признания и должен.
_STUCK_STATUS = "scheduled"


def _money(value: object) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"))


@dataclass(frozen=True)
class ReadinessItem:
    """Одна проверка: что не так, сколько этого и на какую сумму."""

    code: str
    # blocking — закрывать месяц с этим нельзя, цифра будет неверной;
    # warning — закрыть можно, но владелец должен знать.
    severity: str
    title: str
    explanation: str
    count: int
    amount: Decimal
    rows: list[dict[str, object]] = field(default_factory=list)


@dataclass(frozen=True)
class MonthReadiness:
    month: date
    items: list[ReadinessItem]

    @property
    def blocking(self) -> list[ReadinessItem]:
        return [item for item in self.items if item.severity == "blocking"]

    @property
    def ready(self) -> bool:
        return not self.blocking


async def _stuck_accruals(session: AsyncSession, *, as_of: date) -> list[tuple[object, str]]:
    """Начисления с истёкшим периодом, так и не ставшие расходом.

    Возвращает пары «строка, причина»: причина разная и лечится по-разному — пересечение с
    закрытым месяцем требует решения владельца, а его отсутствие означает, что ночная джоба
    просто не отработала.
    """
    rows = (
        await session.execute(
            select(SupplierExpenseAccrual, Counterparty.name)
            .join(Counterparty, Counterparty.id == SupplierExpenseAccrual.counterparty_id)
            .where(
                SupplierExpenseAccrual.status == _STUCK_STATUS,
                SupplierExpenseAccrual.service_period_end < as_of,
            )
            .order_by(SupplierExpenseAccrual.service_period_end)
        )
    ).all()
    if not rows:
        return []

    closed = await accounting_periods.closed_months(session)
    out: list[tuple[object, str]] = []
    for accrual, counterparty_name in rows:
        months = accounting_periods.months_between(
            accrual.service_period_start, accrual.service_period_end
        )
        reason = "closed_month" if closed.intersection(months) else "job_missed"
        out.append(((accrual, counterparty_name), reason))
    return out


def _row_payload(accrual: SupplierExpenseAccrual, counterparty_name: str) -> dict[str, object]:
    return {
        "accrual_id": str(accrual.id),
        "counterparty": counterparty_name,
        "amount": _money(accrual.amount),
        "service_period_start": accrual.service_period_start,
        "service_period_end": accrual.service_period_end,
    }


async def build_month_readiness(
    session: AsyncSession, *, month: date, as_of: date | None = None
) -> MonthReadiness:
    """Проверки перед закрытием месяца. Ничего не пишет."""
    today = as_of or clock.moscow_today()
    stuck = await _stuck_accruals(session, as_of=today)

    blocked = [payload for payload, reason in stuck if reason == "closed_month"]
    missed = [payload for payload, reason in stuck if reason == "job_missed"]

    items = [
        ReadinessItem(
            code="accruals_blocked_by_closed_period",
            severity="blocking",
            title="Признание упёрлось в закрытый месяц",
            explanation=(
                "Период услуги задевает уже закрытый месяц, поэтому ночное признание пропускает "
                "эти начисления — молча и навсегда. Расхода по ним нет ни в одном отчёте. "
                "Лечится открытием месяца: джоба доберёт их сама."
            ),
            count=len(blocked),
            amount=_money(sum((_money(item[0].amount) for item in blocked), Decimal("0"))),
            rows=[_row_payload(accrual, name) for accrual, name in blocked],
        ),
        ReadinessItem(
            code="accruals_overdue_not_recognized",
            severity="blocking",
            title="Период услуги кончился, а расход не признан",
            explanation=(
                "Закрытые месяцы этим начислениям не мешают — значит ночная джоба до них не "
                "дошла. Расход отсутствует в отчёте, хотя услуга оказана."
            ),
            count=len(missed),
            amount=_money(sum((_money(item[0].amount) for item in missed), Decimal("0"))),
            rows=[_row_payload(accrual, name) for accrual, name in missed],
        ),
    ]
    return MonthReadiness(
        month=month.replace(day=1),
        items=[item for item in items if item.count],
    )
