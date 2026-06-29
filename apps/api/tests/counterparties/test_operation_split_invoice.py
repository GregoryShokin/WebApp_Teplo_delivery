"""apply_operation_split: гашение накладной статьёй «Оплата поставщикам» с привязкой.

Ручной разбор needs_review-операции: строка «Оплата поставщикам» + invoice_id создаёт
банковскую аллокацию на накладную (гашение), операция → classified. Поддержаны несколько
накладных на операцию (мультисплит) и частичная оплата.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from cp_helpers import (
    make_account,
    make_bank_operation,
    make_counterparty,
    make_expense_article,
    make_invoice,
    make_wallet,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    Counterparty,
    CounterpartyPayableProfile,
    InvoicePaymentAllocation,
    SupplierInvoice,
)
from app.services.banking.classifier import (
    apply_operation_split,
    resolve_or_create_operation_counterparty,
)


async def _fixture(session: AsyncSession, *, op_amount: str, inv_amount: str):
    account = await make_account(session)
    await make_wallet(session, wallet_type="bank", account_id=account.id)
    article = await make_expense_article(session)  # code=payment_to_supplier
    cp = await make_counterparty(session, name="Поставщик", inn="7701234567")
    invoice = await make_invoice(session, counterparty_id=cp.id, amount=inv_amount, number="Н-1")
    op = await make_bank_operation(
        session, amount=op_amount, direction="out", account_id=account.id
    )
    return article, cp, invoice, op


async def _alloc_count(session: AsyncSession, operation_id) -> int:
    return await session.scalar(
        select(func.count())
        .select_from(InvoicePaymentAllocation)
        .where(InvoicePaymentAllocation.bank_operation_id == operation_id)
    )


async def test_split_pays_invoice_full(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        article, cp, invoice, op = await _fixture(
            session, op_amount="5000.00", inv_amount="5000.00"
        )
        await session.commit()
        inv_id = invoice.id

        await apply_operation_split(
            session,
            op,
            splits=[(article.id, Decimal("5000.00"), None, inv_id)],
            counterparty_id=cp.id,
        )
        await session.commit()

        assert (await session.get(SupplierInvoice, inv_id)).payment_status == "paid"
        assert op.classification_status == "classified"
        assert await _alloc_count(session, op.id) == 1


async def test_split_pays_invoice_partial(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        article, cp, invoice, op = await _fixture(
            session, op_amount="3000.00", inv_amount="5000.00"
        )
        await session.commit()
        inv_id = invoice.id

        await apply_operation_split(
            session,
            op,
            splits=[(article.id, Decimal("3000.00"), None, inv_id)],
            counterparty_id=cp.id,
        )
        await session.commit()

        assert (await session.get(SupplierInvoice, inv_id)).payment_status == "partially_paid"


async def test_split_pays_multiple_invoices(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        account = await make_account(session)
        await make_wallet(session, wallet_type="bank", account_id=account.id)
        article = await make_expense_article(session)
        cp = await make_counterparty(session, name="Поставщик", inn="7701234567")
        inv1 = await make_invoice(session, counterparty_id=cp.id, amount="5000.00", number="Н-1")
        inv2 = await make_invoice(session, counterparty_id=cp.id, amount="3000.00", number="Н-2")
        op = await make_bank_operation(
            session, amount="8000.00", direction="out", account_id=account.id
        )
        await session.commit()
        ids = (inv1.id, inv2.id)

        await apply_operation_split(
            session,
            op,
            splits=[
                (article.id, Decimal("5000.00"), None, inv1.id),
                (article.id, Decimal("3000.00"), None, inv2.id),
            ],
            counterparty_id=cp.id,
        )
        await session.commit()

        for invoice_id in ids:
            assert (await session.get(SupplierInvoice, invoice_id)).payment_status == "paid"
        assert await _alloc_count(session, op.id) == 2


async def test_split_rejects_overpay(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        article, cp, invoice, op = await _fixture(
            session, op_amount="6000.00", inv_amount="5000.00"
        )
        await session.commit()

        with pytest.raises(ValueError, match="больше остатка"):
            await apply_operation_split(
                session,
                op,
                splits=[(article.id, Decimal("6000.00"), None, invoice.id)],
                counterparty_id=cp.id,
            )


async def test_split_invoice_requires_supplier_article(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        account = await make_account(session)
        await make_wallet(session, wallet_type="bank", account_id=account.id)
        other = await make_expense_article(session, code="prochie_rashody", name="Прочие расходы")
        cp = await make_counterparty(session, name="Поставщик", inn="7701234567")
        invoice = await make_invoice(session, counterparty_id=cp.id, amount="5000.00")
        op = await make_bank_operation(
            session, amount="5000.00", direction="out", account_id=account.id
        )
        await session.commit()

        with pytest.raises(ValueError, match="Оплата поставщикам"):
            await apply_operation_split(
                session,
                op,
                splits=[(other.id, Decimal("5000.00"), None, invoice.id)],
                counterparty_id=cp.id,
            )


async def test_resplit_reverses_prior_invoice_allocation(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Повторный разбор той же операции снимает прежнее гашение накладной — без двойной оплаты."""
    async with async_session_factory() as session:
        article, cp, invoice, op = await _fixture(
            session, op_amount="5000.00", inv_amount="5000.00"
        )
        other = await make_expense_article(session, code="prochie_rashody", name="Прочие расходы")
        await session.commit()
        inv_id = invoice.id

        await apply_operation_split(
            session,
            op,
            splits=[(article.id, Decimal("5000.00"), None, inv_id)],
            counterparty_id=cp.id,
        )
        await session.commit()
        assert (await session.get(SupplierInvoice, inv_id)).payment_status == "paid"

        # Повторный разбор БЕЗ привязки накладной → откат прежней аллокации, накладная снова unpaid.
        await apply_operation_split(
            session,
            op,
            splits=[(other.id, Decimal("5000.00"), None, None)],
            counterparty_id=cp.id,
        )
        await session.commit()

        assert (await session.get(SupplierInvoice, inv_id)).payment_status == "unpaid"
        assert await _alloc_count(session, op.id) == 0


async def test_create_counterparty_from_operation_new(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Контрагента нет в реестре — создаётся из распознанных данных операции (имя/ИНН/счёт),
    статус requires_setup, расчётный счёт сохранён в реквизитах профиля."""
    async with async_session_factory() as session:
        cp_id = await resolve_or_create_operation_counterparty(
            session, name="ООО СИНАПСИС", inn="3525357535", account="40702810400000123456"
        )
        await session.commit()

        cp = await session.get(Counterparty, cp_id)
        assert cp is not None
        assert cp.name == "ООО СИНАПСИС"
        assert cp.inn == "3525357535"
        assert cp.status == "requires_setup"
        profile = await session.scalar(
            select(CounterpartyPayableProfile).where(
                CounterpartyPayableProfile.counterparty_id == cp_id
            )
        )
        assert profile is not None
        assert profile.requisites.get("bankAcnt") == "40702810400000123456"


async def test_create_counterparty_from_operation_reuses_by_inn(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Контрагент с тем же ИНН уже есть (не нашёлся в селекте) — переиспользуем, не плодим дубль."""
    async with async_session_factory() as session:
        existing = await make_counterparty(session, name="СИНАПСИС (старый)", inn="3525357535")
        await session.commit()

        cp_id = await resolve_or_create_operation_counterparty(
            session, name="ООО СИНАПСИС", inn="3525357535", account=None
        )
        assert cp_id == existing.id
        assert await session.scalar(select(func.count()).select_from(Counterparty)) == 1
