"""Закрыть дебиторки, чья услуга относится к периоду ДО начала учёта (01.07.2026).

На 01.07.2026 по контрагентам разобраны и внесены входящие остатки — расход более ранних
месяцев уже учтён в них. Открытая предоплата за май-июнь дублирует такой остаток: висит в
дебиторке и просит решения, которого принимать нельзя (признание положило бы тот же расход в
P&L второй раз).

ЧТО ЗАКРЫВАЕТСЯ — только строки с ЯВНО указанным периодом, который целиком раньше отсечки.
Месяц платежа для этого не годится: услугу оплачивают в конце предыдущего месяца, и по дате
денег под нож попали бы живые июльские предоплаты от 29.06 — Директ 48 000 ₽, Синапсис
13 000 ₽, ДоксИнБокс 15 580 ₽.

Строку без периода можно закрыть только назвав её поимённо: ``--include <id> --period 2026-05``.
Так закрыты два платежа Манго (30.05 и 26.06) по прямому указанию владельца 02.08.2026 — у
них период не проставлен, а связь оплачивается помесячно и авансом.

    python -m app.scripts.writeoff_pre_accounting                       # что будет сделано
    python -m app.scripts.writeoff_pre_accounting --apply
    python -m app.scripts.writeoff_pre_accounting --include <id>=2026-05 --apply
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from calendar import monthrange
from datetime import date
from decimal import Decimal

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models import Counterparty, SupplierPrepayment
from app.services.accounting_periods import ACCOUNTING_START

NOTE = f"Закрыто как учтённое во входящих остатках на {ACCOUNTING_START:%d.%m.%Y}"


def month_bounds(month: date) -> tuple[date, date]:
    start = month.replace(day=1)
    return start, start.replace(day=monthrange(start.year, start.month)[1])


def parse_includes(argv: list[str]) -> dict[uuid.UUID, date]:
    """``--include <uuid>=YYYY-MM`` — строка без периода, закрываемая по прямому указанию."""
    out: dict[uuid.UUID, date] = {}
    for index, arg in enumerate(argv):
        if arg != "--include":
            continue
        raw = argv[index + 1]
        prepayment_id, _, month = raw.partition("=")
        year, _, month_no = month.partition("-")
        out[uuid.UUID(prepayment_id)] = date(int(year), int(month_no), 1)
    return out


async def main(*, apply: bool, includes: dict[uuid.UUID, date]) -> None:
    async with AsyncSessionLocal() as session:  # type: AsyncSession
        rows = (
            await session.execute(
                select(SupplierPrepayment, Counterparty.name)
                .join(Counterparty, Counterparty.id == SupplierPrepayment.counterparty_id)
                .where(SupplierPrepayment.status.in_(("open", "partially_settled")))
            )
        ).all()

        planned: list[tuple[SupplierPrepayment, str, date, date, Decimal, bool]] = []
        for prepayment, cp_name in rows:
            # Товарный аванс гасит накладная, депозит — возврат: отсечка учётного периода к ним
            # не применяется. Входящий остаток и сам является тем, чем закрывают других.
            if prepayment.kind in ("goods", "deposit") or prepayment.opening:
                continue

            named = includes.get(prepayment.id)
            explicit = (
                prepayment.service_period_status == "ready"
                and prepayment.service_period_end is not None
            )
            if explicit:
                period_start = prepayment.service_period_start
                period_end = prepayment.service_period_end
            elif named is not None:
                period_start, period_end = month_bounds(named)
            else:
                continue

            if period_end >= ACCOUNTING_START:
                continue
            balance = prepayment.amount - prepayment.amount_settled
            if balance <= 0:
                continue
            planned.append((prepayment, cp_name, period_start, period_end, balance, explicit))

        total = sum(item[4] for item in planned)
        print(f"К закрытию: {len(planned)} строк на {total:,.2f} ₽".replace(",", " "))
        for _prepayment, cp_name, period_start, period_end, balance, explicit in sorted(
            planned, key=lambda row: -row[4]
        ):
            mark = "" if explicit else "  (период указан вручную)"
            print(
                f"  {cp_name[:32]:34} {period_start:%d.%m}–{period_end:%d.%m.%Y} "
                f"{balance:>10,.2f} ₽{mark}".replace(",", " ")
            )
        missed = set(includes) - {item[0].id for item in planned}
        for prepayment_id in missed:
            print(f"  ! {prepayment_id} — не найдена среди открытых или уже закрыта")

        if not apply:
            print("\nПробный прогон. Чтобы применить — запустите с --apply")
            return

        for prepayment, _cp_name, period_start, period_end, _balance, explicit in planned:
            if not explicit:
                prepayment.service_period_start = period_start
                prepayment.service_period_end = period_end
                prepayment.service_period_status = "ready"
            prepayment.amount_settled = prepayment.amount
            prepayment.status = "settled"
            prepayment.note = f"{prepayment.note + ' · ' if prepayment.note else ''}{NOTE}"
        await session.commit()
        print(f"\nЗакрыто {len(planned)} строк на {total:,.2f} ₽".replace(",", " "))


if __name__ == "__main__":
    asyncio.run(main(apply="--apply" in sys.argv, includes=parse_includes(sys.argv)))
