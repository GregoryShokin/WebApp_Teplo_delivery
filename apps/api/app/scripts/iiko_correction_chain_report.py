"""Цепочка исправления вхолостую: что она поставила бы по каждой исправленной накладной.

Только чтение: iiko — документы (Cloud ``get``), проводки оплат (OLAP) и план счетов; база —
выборка в транзакции READ ONLY. Нужен до включения ``iiko_correction_offset_enabled`` и после
спорного случая: показать, где цепочка доводится шагами 3–4, а где состояние уже правили руками.

    docker exec -w /app/apps/api <api-контейнер> python -m app.scripts.iiko_correction_chain_report
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

from sqlalchemy import select, text

from app.db.session import AsyncSessionLocal
from app.models import Counterparty, SupplierInvoice
from app.services.iiko_correction_chain import (
    ADVANCES_ACCOUNT_NAME,
    LiveCorrectionIiko,
    inspect_correction,
)
from app.services.iiko_sync import _load_source_credential_env


def _rub(value: Decimal) -> str:
    return f"{Decimal(value):,.2f}".replace(",", " ").replace(".", ",") + " ₽"


async def main() -> None:
    """Напечатать по исправленным накладным документы, оплаты, план и что с ним делать."""
    async with AsyncSessionLocal() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        await _load_source_credential_env(session)
        iiko = LiveCorrectionIiko()
        print(f"счёт «{ADVANCES_ACCOUNT_NAME}»:", iiko.account_id(ADVANCES_ACCOUNT_NAME))
        invoices = (
            await session.scalars(
                select(SupplierInvoice)
                .where(SupplierInvoice.iiko_correction_new_external_id.is_not(None))
                .order_by(SupplierInvoice.invoice_date)
            )
        ).all()
        for invoice in invoices:
            supplier = await session.get(Counterparty, invoice.counterparty_id)
            seen = await inspect_correction(session, invoice, iiko)
            print(f"\n=== {supplier.name if supplier else '—'} · {invoice.number}")
            for label, doc in (
                ("исходная", seen.original),
                ("сторно", seen.returned),
                ("«(испр.)»", seen.correction),
            ):
                if doc is not None:
                    print(f"  {label}: {doc.number} {doc.status}, {_rub(doc.total or 0)}")
            print(
                f"  оплачено в iiko: исходная {_rub(seen.original_paid)} (уплачено"
                f" {_rub(seen.paid_for_original)}), «(испр.)» {_rub(seen.correction_paid)}"
            )
            plan = seen.plan
            if plan is not None:
                shares = ", ".join(
                    f"{share.article_name} {_rub(share.amount)} → «{share.account_name}»"
                    for share in plan.expense_shares
                )
                print(
                    f"  план: зачёт «(испр.)» {_rub(plan.offset_amount)}; расход разницы"
                    f" {shares or '—'}; останется кредитом {_rub(plan.leftover_credit)}"
                )
            if seen.blockers:
                print("  не доводится:", "; ".join(seen.blockers))
            elif seen.correction_paid == 0 and seen.original_paid >= seen.paid_for_original:
                print("  → цепочка доводится шагами 3–4")
            elif seen.original_paid == 0 and plan is not None:
                print(
                    "  → оплату руками перенесли с исходной на «(испр.)»: не хватает только"
                    " изъятия разницы со счёта денег чека"
                )
            else:
                print("  → состояние правили руками, разбор вручную")
        await session.rollback()


if __name__ == "__main__":
    asyncio.run(main())
