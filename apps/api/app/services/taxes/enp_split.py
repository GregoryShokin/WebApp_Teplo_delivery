"""Разнос зарплатного ЕНП на назначения по эталону-оборотке.

Проблема, которую решает модуль: в бюджет зарплатные налоги уходят единым платежом (ЕНП)
под одним КБК ``18201061201010000510`` — из выписки не прочитать, сколько там НДФЛ, а сколько
взносов за работников. А для расчёта УСН это критично: взносы за работников уменьшают налог
(по факту уплаты), а НДФЛ — НЕТ. Раньше состав приходилось РЕКОНСТРУИРОВАТЬ (травматизм →
база ФОТ → формула взносов → остаток в НДФЛ). Теперь есть точный первоисточник — оборотка
бухгалтера (``tax_payroll_ledger``), где НДФЛ и взносы стоят раздельно по каждому месяцу.

Что делает разнос: агрегирует оборотку по месяцу и материализует её в типизированные строки
``TaxPayment`` (``contrib_employees`` — в вычет, ``ndfl`` — нет), которые движок УСН потребляет
как есть. Взносы за месяц N уплачиваются в составе ЕНП до 28 числа (N+1) — эту дату и ставим
в ``paid_on`` (кассовый принцип вычета опирается на факт ухода денег к сроку).

Идемпотентно: пересборка обновляет строки по слоту ``(for_year, kind, for_period)``, а не
плодит их. Источник помечается ``source_kind='tax_notice'`` (данные бухгалтера) и
``quality_status='confirmed'`` — разнос больше не расчётный, а первичный.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tax import TaxPayment, TaxPayrollLedger

# Виды платежей, которые порождает разнос оборотки. Оба идут в ФНС; травматизм сюда НЕ входит —
# он платится отдельным платежом прямо в СФР, а не в составе ЕНП.
_SPLIT_KINDS = ("contrib_employees", "ndfl")


@dataclass(frozen=True)
class SplitResult:
    year: int
    created: int
    updated: int
    months: list[str]


def payroll_enp_due(year: int, month: int) -> date:
    """Срок уплаты зарплатного ЕНП за месяц N — 28 число месяца N+1."""
    next_month, next_year = (month + 1, year) if month < 12 else (1, year + 1)
    return date(next_year, next_month, 28)


async def _existing_split_row(
    session: AsyncSession, *, year: int, period: str, kind: str
) -> TaxPayment | None:
    return (
        await session.execute(
            select(TaxPayment).where(
                TaxPayment.for_year == year,
                TaxPayment.for_period == period,
                TaxPayment.kind == kind,
                TaxPayment.source_kind == "tax_notice",
            )
        )
    ).scalar_one_or_none()


async def rebuild_payroll_enp_split(session: AsyncSession, *, year: int) -> SplitResult:
    """Пересобрать разнос зарплатного ЕНП из оборотк за год.

    Для каждого месяца с оборот кой создаёт/обновляет строки НДФЛ и взносов за работников.
    Возвращает сводку. Не трогает строки, порождённые другими источниками (банк, ручной ввод).
    """
    aggregates = (
        await session.execute(
            select(
                TaxPayrollLedger.month,
                func.coalesce(func.sum(TaxPayrollLedger.contributions), 0),
                func.coalesce(func.sum(TaxPayrollLedger.ndfl), 0),
            )
            .where(TaxPayrollLedger.year == year)
            .group_by(TaxPayrollLedger.month)
            .order_by(TaxPayrollLedger.month)
        )
    ).all()

    created = updated = 0
    touched_months: list[str] = []
    for month, contributions, ndfl in aggregates:
        period = f"{year}-{month:02d}"
        due = payroll_enp_due(year, month)
        # Точный разнос из оборотки ЗАМЕЩАЕТ реконструированный за тот же месяц — иначе
        # старая расчётная строка и новая первичная сложатся (двойной счёт в вычете и сверке).
        # БАНКОВСКИЕ факт-строки (source_kind='bank_statement', их создаёт bank_facts._split_enp
        # тоже с quality='reconstructed') НЕ трогаем: иначе каждый повторный прогон rebuild
        # удалял бы факты уплаты — временная потеря вычета и вечный пинг-понг с факт-слоем
        # (находка аудита 27.07.2026).
        await session.execute(
            delete(TaxPayment).where(
                TaxPayment.for_year == year,
                TaxPayment.for_period == period,
                TaxPayment.kind.in_(_SPLIT_KINDS),
                TaxPayment.quality_status == "reconstructed",
                TaxPayment.source_kind != "bank_statement",
            )
        )
        amounts = {
            "contrib_employees": Decimal(str(contributions or 0)),
            "ndfl": Decimal(str(ndfl or 0)),
        }
        # Один банковский «зарплатный пакет» месяца — общий bundle для его строк.
        bundle = uuid.uuid4()
        month_touched = False
        for kind in _SPLIT_KINDS:
            amount = amounts[kind]
            existing = await _existing_split_row(
                session, year=year, period=period, kind=kind
            )
            if amount <= 0:
                # Начисления нет — но если строка осталась от прошлой оборотки, обнулять
                # нельзя (amount>0 в CHECK); оставляем как есть, это не наш случай для ЗП.
                continue
            if existing is not None:
                existing.amount = amount
                existing.paid_on = due
                existing.quality_status = "confirmed"
                existing.source_kind = "tax_notice"
                updated += 1
            else:
                session.add(
                    TaxPayment(
                        id=uuid.uuid4(),
                        bundle_id=bundle,
                        paid_on=due,
                        kind=kind,
                        amount=amount,
                        recipient="fns",
                        for_year=year,
                        for_period=period,
                        status="paid",
                        source_kind="tax_notice",
                        quality_status="confirmed",
                        note="Разнос зарплатного ЕНП из оборотки",
                    )
                )
                created += 1
            month_touched = True
        if month_touched:
            touched_months.append(period)

    await session.flush()
    return SplitResult(year=year, created=created, updated=updated, months=touched_months)
