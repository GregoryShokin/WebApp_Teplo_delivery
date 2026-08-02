"""Пересобрать зачёты закрывающих документов по хронологии денег.

ЗАЧЕМ. Зачёт сортировал авансы по ``created_at``. Всё, что приехало одним импортом, имеет
одинаковое время до микросекунды — и порядок определяла база. У Манго Телеком УПД за июнь съел
платёж от 07.07, пропустив майский, хотя июньских денег хватало с запасом. Общая дебиторка
сходилась, а висела не на тех платежах: очередь признания показывала закрытым июльский платёж и
открытым — майский.

ЧТО ДЕЛАЕТ. Снимает prepayment-зачёты со всех финансовых закрывающих и применяет их заново, по
возрастанию даты документа. Порядок внутри документа теперь считает исправленный
``_settlement_order``: сначала деньги, ушедшие не позже даты документа, между собой — по дате
денег. Денег не двигает: аллокации ``prepayment`` — это разметка, а не платёж.

Суммы по контрагенту в целом НЕ меняются — меняется, на каком платеже висит остаток.

    python -m app.scripts.resettle_closings_by_date                    # что изменится
    python -m app.scripts.resettle_closings_by_date --apply
    python -m app.scripts.resettle_closings_by_date --counterparty Манго --apply
"""

from __future__ import annotations

import asyncio
import sys
from decimal import Decimal

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models import (
    CashflowTransaction,
    Counterparty,
    SupplierInvoice,
    SupplierPrepayment,
    invoice_binds_settlement,
)
from app.services.counterparty_matching import _recompute_status
from app.services.supplier_prepayments import (
    AUTO_SETTLEMENT_OPERATIONAL_SCOPE,
    apply_closing_document,
    release_invoice_prepayment_allocations,
)


def arg_value(argv: list[str], flag: str) -> str | None:
    return argv[argv.index(flag) + 1] if flag in argv else None


async def main(*, apply: bool, name_like: str | None) -> None:
    async with AsyncSessionLocal() as session:
        query = (
            select(SupplierInvoice)
            .join(Counterparty, Counterparty.id == SupplierInvoice.counterparty_id)
            .where(
                SupplierInvoice.direction == "payable",
                SupplierInvoice.doc_kind == "closing",
                SupplierInvoice.operational_scope == AUTO_SETTLEMENT_OPERATIONAL_SCOPE,
                SupplierInvoice.barter_role.is_(None),
                SupplierInvoice.payment_status != "void",
                invoice_binds_settlement(),
            )
            .order_by(SupplierInvoice.invoice_date.nulls_last(), SupplierInvoice.created_at)
        )
        if name_like:
            query = query.where(Counterparty.name.ilike(f"%{name_like}%"))
        closings = (await session.scalars(query)).all()

        touched = {invoice.counterparty_id for invoice in closings}
        before = await snapshot(session, touched)

        for invoice in closings:
            await release_invoice_prepayment_allocations(session, invoice)
            await _recompute_status(session, invoice)
        await session.flush()
        for invoice in closings:
            await apply_closing_document(session, invoice)
        await session.flush()

        after = await snapshot(session, touched)
        changed = [key for key in after if before.get(key) != after[key]]
        print(
            f"Документов пересобрано: {len(closings)}; "
            f"строк с изменившимся остатком: {len(changed)}"
        )
        for key in sorted(changed, key=lambda item: (item[0], str(item[1]))):
            print(
                f"  {key[0][:30]:32} платёж {key[2] or '—':10} "
                f"остаток {before.get(key, Decimal('0')):>9,.2f} → {after[key]:>9,.2f} ₽".replace(
                    ",", " "
                )
            )

        if not apply:
            await session.rollback()
            print("\nПробный прогон, изменения откачены. Чтобы применить — запустите с --apply")
            return
        await session.commit()
        print("\nПрименено.")


async def snapshot(session, counterparty_ids: set) -> dict:
    """Остаток по каждому авансу: ключ — контрагент, id аванса и дата денег."""
    if not counterparty_ids:
        return {}
    rows = (
        await session.execute(
            select(SupplierPrepayment, Counterparty.name, CashflowTransaction.operation_date)
            .join(Counterparty, Counterparty.id == SupplierPrepayment.counterparty_id)
            .outerjoin(
                CashflowTransaction,
                CashflowTransaction.id == SupplierPrepayment.cashflow_transaction_id,
            )
            .where(SupplierPrepayment.counterparty_id.in_(counterparty_ids))
        )
    ).all()
    return {
        (cp_name, prepayment.id, str(operation_date) if operation_date else None): (
            prepayment.amount - prepayment.amount_settled
        )
        for prepayment, cp_name, operation_date in rows
    }


if __name__ == "__main__":
    asyncio.run(
        main(apply="--apply" in sys.argv, name_like=arg_value(sys.argv, "--counterparty"))
    )
