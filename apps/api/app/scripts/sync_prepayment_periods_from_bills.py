"""Перенести период услуги со счёта в дебиторку, которую этот счёт породил.

Дебиторка ``prepaid_bill`` — оборотная сторона оплаченного счёта, и период услуги у них общий:
он распознан из самого счёта («Оплата за период 01.08.2026 — 31.08.2026»). Новые дебиторки
наследуют его при создании, а те, что заведены раньше, остались без периода — и очередь просит
по ним решения, хотя ответ написан в счёте.

Отдельно это всплыло после пересчёта зачётов по хронологии: перенос периодов делался по
ОТКРЫТЫМ строкам, а пересчёт поменял, какие из них открыты. У АЙКО и Лемы открытой стала
дебиторка, до которой перенос не дошёл, — период показывался «не указан» при том что в счёте
он есть.

Период у предоплаты, указанный человеком вручную, не трогается: заполняем только пустое.

    python -m app.scripts.sync_prepayment_periods_from_bills
    python -m app.scripts.sync_prepayment_periods_from_bills --apply
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models import Counterparty, SupplierInvoice, SupplierPrepayment


async def main(*, apply: bool) -> None:
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(SupplierPrepayment, SupplierInvoice, Counterparty.name)
                .join(SupplierInvoice, SupplierInvoice.id == SupplierPrepayment.bill_invoice_id)
                .join(Counterparty, Counterparty.id == SupplierPrepayment.counterparty_id)
                .where(
                    SupplierPrepayment.service_period_status != "ready",
                    SupplierInvoice.service_period_start.is_not(None),
                    SupplierInvoice.service_period_end.is_not(None),
                )
                .order_by(Counterparty.name)
            )
        ).all()

        print(f"Дебиторок с периодом из счёта: {len(rows)}")
        for prepayment, bill, cp_name in rows:
            print(
                f"  {cp_name[:30]:32} {prepayment.amount:>10,.2f} ₽  счёт "
                f"{bill.number or '—':18} → {bill.service_period_start:%d.%m}–"
                f"{bill.service_period_end:%d.%m.%Y}".replace(",", " ")
            )

        if not apply:
            print("\nПробный прогон. Чтобы применить — запустите с --apply")
            return

        for prepayment, bill, _cp_name in rows:
            prepayment.service_period_start = bill.service_period_start
            prepayment.service_period_end = bill.service_period_end
            prepayment.service_period_status = "ready"
        await session.commit()
        print(f"\nПеренесено периодов: {len(rows)}")


if __name__ == "__main__":
    asyncio.run(main(apply="--apply" in sys.argv))
