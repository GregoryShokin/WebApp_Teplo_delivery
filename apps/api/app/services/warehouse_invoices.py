"""Создание и просмотр складских накладных («Управление складом → Накладные»).

Phase 2: ручное создание обычной приходной накладной со строками номенклатуры и
флагом «персонал» на каждой строке. Строки — источник правды (``invoice_line_item``);
их минимальный срез зеркалится в ``SupplierInvoice.line_items`` (JSONB), чтобы
номенклатурный бартер-матчинг продолжал работать. Персонал-строки входят в полную
сумму накладной (``amount``) и в ``staff_amount``, но позже исключаются из пуша в iiko.

Push в iiko (Phase 3), сплит-оплата и ДДС-мэтч (Phase 4), явный бартер (Phase 5) —
отдельными слоями поверх этого сервиса.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Counterparty,
    CounterpartyPayableProfile,
    IikoProduct,
    InvoiceLineItem,
    SupplierInvoice,
)


class WarehouseInvoiceError(RuntimeError):
    """Domain error for warehouse invoice creation (maps to HTTP 409/422)."""


async def _ensure_barter_relationship(session: AsyncSession, counterparty_id: uuid.UUID) -> None:
    """A counterparty we issue/receive a barter loan with is a barter partner."""
    profile = await session.scalar(
        select(CounterpartyPayableProfile).where(
            CounterpartyPayableProfile.counterparty_id == counterparty_id
        )
    )
    if profile is None:
        session.add(
            CounterpartyPayableProfile(counterparty_id=counterparty_id, relationship="barter")
        )
        await session.flush()
    elif profile.relationship != "barter":
        profile.relationship = "barter"


def _money(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _qty(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


@dataclass
class LineInput:
    name: str
    quantity: Decimal
    price: Decimal
    iiko_product_id: uuid.UUID | None = None
    vat_percent: Decimal | None = None
    is_staff: bool = False


async def next_invoice_number(session: AsyncSession) -> str:
    """Next sequential number for a manually-created invoice: max numeric one + 1."""
    numbers = (
        await session.scalars(
            select(SupplierInvoice.number).where(
                SupplierInvoice.source == "manual", SupplierInvoice.number.isnot(None)
            )
        )
    ).all()
    top = 0
    for raw in numbers:
        digits = "".join(ch for ch in (raw or "") if ch.isdigit())
        if digits:
            top = max(top, int(digits))
    return str(top + 1)


async def create_warehouse_invoice(
    session: AsyncSession,
    *,
    counterparty_id: uuid.UUID,
    issued_at: datetime,
    lines: Sequence[LineInput],
    mode: str = "normal",
    we_lend: bool = True,
    number: str | None = None,
    due_date: date | None = None,
    store_guid: str | None = None,
    actor_user_id: uuid.UUID | None = None,
) -> SupplierInvoice:
    if not lines:
        raise WarehouseInvoiceError("Добавьте хотя бы одну строку накладной")
    if issued_at is None:
        raise WarehouseInvoiceError("Укажите дату и время чека")

    product_ids = [line.iiko_product_id for line in lines if line.iiko_product_id]
    products: dict[uuid.UUID, IikoProduct] = {}
    if product_ids:
        rows = (
            await session.scalars(select(IikoProduct).where(IikoProduct.id.in_(product_ids)))
        ).all()
        products = {product.id: product for product in rows}

    # Barter loan: расход (we_lend=мы выдаём) или приход (нам выдают); явная роль «loan».
    if mode == "loan":
        direction = "receivable" if we_lend else "payable"
        barter_role: str | None = "loan"
        barter_return_status: str | None = "open"
        await _ensure_barter_relationship(session, counterparty_id)
    else:
        direction = "payable"
        barter_role = None
        barter_return_status = None

    invoice = SupplierInvoice(
        counterparty_id=counterparty_id,
        source="manual",
        direction=direction,
        barter_role=barter_role,
        barter_return_status=barter_return_status,
        number=number or await next_invoice_number(session),
        invoice_date=issued_at.date(),
        issued_at=issued_at,
        due_date=due_date,
        amount=Decimal("0.00"),
        payment_status="unpaid",
        store_guid=store_guid,
        created_by_user_id=actor_user_id,
    )
    session.add(invoice)
    await session.flush()

    total = Decimal("0.00")
    staff_total = Decimal("0.00")
    vat_total = Decimal("0.00")
    mirror: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        product = products.get(line.iiko_product_id) if line.iiko_product_id else None
        quantity = _qty(line.quantity)
        price = _money(line.price)
        line_sum = _money(quantity * price)
        vat_sum: Decimal | None = None
        if line.vat_percent:
            rate = Decimal(str(line.vat_percent))
            vat_sum = _money(line_sum * rate / (Decimal("100") + rate))  # gross-inclusive
            vat_total += vat_sum
        item_name = line.name or (product.name if product else "Позиция")
        session.add(
            InvoiceLineItem(
                invoice_id=invoice.id,
                iiko_product_id=line.iiko_product_id,
                product_guid=product.iiko_id if product else None,
                name=item_name,
                article=product.code if product else None,
                unit=product.unit if product else None,
                quantity=quantity,
                price=price,
                sum=line_sum,
                vat_percent=Decimal(str(line.vat_percent)) if line.vat_percent else None,
                vat_sum=vat_sum,
                is_staff=line.is_staff,
                sort_order=index,
            )
        )
        total += line_sum
        if line.is_staff:
            staff_total += line_sum
        # Mirror keeps product_id/article so counterparty_barter_match._products() works.
        mirror.append(
            {
                "product_id": product.iiko_id if product else None,
                "article": product.code if product else None,
                "name": item_name,
                "quantity": str(quantity),
                "amount": str(line_sum),
            }
        )

    invoice.amount = _money(total)
    invoice.staff_amount = _money(staff_total)
    invoice.vat_total = _money(vat_total)
    invoice.line_items = mirror
    await session.commit()
    await session.refresh(invoice)
    return invoice


async def list_warehouse_invoices(
    session: AsyncSession,
    *,
    statuses: Sequence[str] | None = None,
    counterparty_id: uuid.UUID | None = None,
    has_staff: bool | None = None,
) -> list[dict[str, Any]]:
    query = (
        select(SupplierInvoice, Counterparty.name)
        .join(Counterparty, Counterparty.id == SupplierInvoice.counterparty_id)
        .order_by(
            SupplierInvoice.issued_at.nulls_last(),
            SupplierInvoice.invoice_date.nulls_last(),
        )
    )
    if statuses:
        query = query.where(SupplierInvoice.payment_status.in_(tuple(statuses)))
    if counterparty_id is not None:
        query = query.where(SupplierInvoice.counterparty_id == counterparty_id)
    if has_staff is True:
        query = query.where(SupplierInvoice.staff_amount > 0)

    rows = list(await session.execute(query))
    return [_invoice_summary(invoice, name) for invoice, name in rows]


def _invoice_summary(invoice: SupplierInvoice, counterparty_name: str) -> dict[str, Any]:
    return {
        "id": str(invoice.id),
        "counterparty_id": str(invoice.counterparty_id),
        "counterparty_name": counterparty_name,
        "source": invoice.source,
        "direction": invoice.direction,
        "number": invoice.number,
        "issued_at": invoice.issued_at.isoformat() if invoice.issued_at else None,
        "invoice_date": invoice.invoice_date.isoformat() if invoice.invoice_date else None,
        "amount": float(_money(invoice.amount)),
        "staff_amount": float(_money(invoice.staff_amount)),
        "payment_status": invoice.payment_status,
        "iiko_push_status": invoice.iiko_push_status,
        "barter_role": invoice.barter_role,
        "barter_return_status": invoice.barter_return_status,
    }


async def get_warehouse_invoice(
    session: AsyncSession, invoice_id: uuid.UUID
) -> dict[str, Any] | None:
    invoice = await session.get(SupplierInvoice, invoice_id)
    if invoice is None:
        return None
    counterparty = await session.get(Counterparty, invoice.counterparty_id)
    lines = (
        await session.scalars(
            select(InvoiceLineItem)
            .where(InvoiceLineItem.invoice_id == invoice_id)
            .order_by(InvoiceLineItem.sort_order)
        )
    ).all()
    summary = _invoice_summary(invoice, counterparty.name if counterparty else "—")
    summary["due_date"] = invoice.due_date.isoformat() if invoice.due_date else None
    summary["iiko_push_error"] = invoice.iiko_push_error
    summary["lines"] = [
        {
            "id": str(line.id),
            "name": line.name,
            "article": line.article,
            "unit": line.unit,
            "quantity": float(_qty(line.quantity)),
            "price": float(_money(line.price)),
            "sum": float(_money(line.sum)),
            "vat_percent": float(line.vat_percent) if line.vat_percent is not None else None,
            "is_staff": line.is_staff,
        }
        for line in lines
    ]
    return summary
