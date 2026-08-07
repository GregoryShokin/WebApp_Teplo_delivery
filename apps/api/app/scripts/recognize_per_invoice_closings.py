"""Завести начисления закрывающим режима «счёт + УПД», лежащим без периода.

ЗАЧЕМ. ``sync_invoice_accrual`` зовётся при ПРИЁМЕ документа. Правка, научившая его выводить
период из даты документа для режима ``per_invoice``, к уже принятым документам сама не
вернётся: они лежат в базе с пустым периодом и без начисления, и расход по ним не признан
ничем. На 07.08.2026 таких четыре — Манго Телеком ×2 за 31.07 (11 695,54 ₽, из-за них строка
«Телекоммуникации» показывала 3 230 ₽ вместо 14 925,54 ₽) и ЭкоЦентр ×2 за 31.08 (4 957,65 ₽,
будущий период — начисление встанет в ``scheduled`` и признается в конце августа).

ЧТО ДЕЛАЕТ. Отбирает ровно те документы, которые захватывает ограничитель
``_period_from_document_date``, и прогоняет по ним штатный ``sync_invoice_accrual``. Своей
логики признания у скрипта нет: он только зовёт функцию, поэтому разойтись с приёмом
документа не может. Полей накладной не трогает — период живёт в начислении.

Идемпотентен: документ с уже существующим начислением отбором не берётся, а повторный прогон
на том же наборе ничего не добавит.

    python -m app.scripts.recognize_per_invoice_closings           # что изменится
    python -m app.scripts.recognize_per_invoice_closings --apply
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models import (
    Counterparty,
    CounterpartyPayableProfile,
    SupplierExpenseAccrual,
    SupplierInvoice,
)
from app.services import accounting_periods, supplier_service_periods


async def main(*, apply: bool) -> None:
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(SupplierInvoice, Counterparty.name)
                .join(Counterparty, Counterparty.id == SupplierInvoice.counterparty_id)
                .join(
                    CounterpartyPayableProfile,
                    CounterpartyPayableProfile.counterparty_id == SupplierInvoice.counterparty_id,
                )
                .outerjoin(
                    SupplierExpenseAccrual,
                    SupplierExpenseAccrual.invoice_id == SupplierInvoice.id,
                )
                .where(
                    SupplierInvoice.doc_kind == "closing",
                    SupplierInvoice.operational_scope == "finance",
                    SupplierInvoice.direction == "payable",
                    SupplierInvoice.service_period_start.is_(None),
                    SupplierInvoice.service_period_end.is_(None),
                    SupplierInvoice.invoice_date >= accounting_periods.ACCOUNTING_START,
                    SupplierInvoice.payment_status != "void",
                    SupplierInvoice.informational.is_(False),
                    CounterpartyPayableProfile.service_billing_mode
                    == supplier_service_periods.PER_INVOICE_BILLING_MODE,
                    SupplierExpenseAccrual.id.is_(None),
                )
                .order_by(SupplierInvoice.invoice_date, SupplierInvoice.number)
            )
        ).all()

        if not rows:
            print("Документов без начисления не найдено — делать нечего.")
            return

        print(f"{'КОНТРАГЕНТ':<24} {'ДОКУМЕНТ':<16} {'ДАТА':<11} {'СУММА':>11}  ПЕРИОД → СТАТУС")
        created = 0
        total = 0
        for invoice, name in rows:
            accrual = await supplier_service_periods.sync_invoice_accrual(session, invoice)
            if accrual is None:
                print(f"{name[:23]:<24} {invoice.number or '—':<16} ПРОПУЩЕН (ограничитель)")
                continue
            created += 1
            total += float(invoice.amount)
            print(
                f"{name[:23]:<24} {(invoice.number or '—')[:15]:<16} "
                f"{invoice.invoice_date!s:<11} {invoice.amount:>11} "
                f"{accrual.service_period_start}—{accrual.service_period_end} → {accrual.status}"
            )

        print(f"\nНачислений заведено: {created} на {total:.2f} ₽")
        if apply:
            await session.commit()
            print("ПРИМЕНЕНО.")
        else:
            await session.rollback()
            print("Вхолостую — ничего не записано. Повтори с --apply.")


if __name__ == "__main__":
    asyncio.run(main(apply="--apply" in sys.argv))
