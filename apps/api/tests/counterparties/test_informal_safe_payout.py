"""Оплата накладных неофициальных поставщиков через вывод на карту ИП (Сейф).

Черновик informal-поставщика выписывается на карту ИП (реквизиты зарплатных выплат,
``payroll.bank_payout_requisites`` — засеяны миграцией). Paid-переход вместо расхода
«Оплата поставщикам» заводит транзит р/с→Сейф и целевой резерв Сейфа на поставщика;
расход по статье случится при выплате резерва (``pay_allocation``).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from cp_helpers import make_counterparty, make_expense_article, make_invoice
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_admin_payout_split import _payer_wallet, _safe_wallet

from app.models import (
    AppSetting,
    CashflowTransaction,
    InvoicePaymentAllocation,
    ReconciliationCase,
    SafeAllocation,
)
from app.services.bank_payment_status import (
    SUPPLIER_BANK_TO_SAFE_SOURCE_KIND,
    apply_payment_status,
)
from app.services.banking.safe_allocations import (
    SAFE_PAYOUT_SOURCE_KIND,
    pay_allocation,
)
from app.services.counterparty_payments import (
    CounterpartyPaymentError,
    create_payment_draft_for_invoices,
    create_standalone_payment_draft,
)


async def _informal_supplier(session: AsyncSession, *, name: str = "Местный закуп Ромашка"):
    # У неофициального поставщика нет ни реквизитов, ни их подтверждения — и не нужно.
    return await make_counterparty(
        session, name=name, relationship="informal", requisites_verified=False
    )


async def _transfer_legs(session: AsyncSession, draft_id) -> list[CashflowTransaction]:
    return list(
        (
            await session.scalars(
                select(CashflowTransaction).where(
                    CashflowTransaction.source_kind == SUPPLIER_BANK_TO_SAFE_SOURCE_KIND,
                    CashflowTransaction.source_id == draft_id,
                )
            )
        ).all()
    )


async def _draft_reserve(session: AsyncSession, draft_id) -> SafeAllocation | None:
    return await session.scalar(
        select(SafeAllocation).where(SafeAllocation.source_draft_id == draft_id)
    )


async def test_informal_draft_targets_ip_card(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        supplier = await _informal_supplier(session)
        inv1 = await make_invoice(
            session, counterparty_id=supplier.id, amount="1000.00", number="14"
        )
        inv2 = await make_invoice(
            session, counterparty_id=supplier.id, amount="2000.00", number="16"
        )
        await session.commit()

        draft = await create_payment_draft_for_invoices(
            session, invoice_ids=[inv1.id, inv2.id], actor_user_id=None
        )

        assert draft.pays_via_safe is True
        assert draft.status == "created"
        assert draft.amount == Decimal("3000.00")
        # Получатель — карта ИП (реквизиты зарплатных выплат из настройки), не поставщик.
        payout_setting = await session.scalar(
            select(AppSetting).where(AppSetting.key == "payroll.bank_payout_requisites")
        )
        assert draft.payload["recipientName"] == payout_setting.value["recipientName"]
        assert draft.payload["recipientName"] != supplier.name
        purpose = draft.payload["paymentPurpose"]
        assert purpose.startswith("Закуп: Местный закуп Ромашка")
        assert "№14" in purpose and "№16" in purpose
        assert "НДС" not in purpose

        await session.refresh(inv1)
        await session.refresh(inv2)
        assert inv1.draft_id == draft.id and inv2.draft_id == draft.id
        # Черновик — не оплата: накладные ещё «К оплате».
        assert inv1.payment_status == "unpaid"


async def test_official_supplier_path_unchanged(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        supplier = await make_counterparty(
            session,
            name="ООО Официальный",
            inn="7701234567",
            requisites={
                "bankAcnt": "40702810400000012345",
                "bankBik": "044525225",
                "recipientCorrAccountNumber": "30101810400000000225",
            },
            requisites_verified=True,
        )
        invoice = await make_invoice(session, counterparty_id=supplier.id, amount="500.00")
        await session.commit()

        draft = await create_payment_draft_for_invoices(
            session, invoice_ids=[invoice.id], actor_user_id=None
        )
        assert draft.pays_via_safe is False
        # Официальный путь: получатель — сам поставщик по его реквизитам.
        assert draft.payload["recipientName"] == "ООО Официальный"


async def test_informal_standalone_prepayment_still_blocked(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Предоплата (standalone-черновик) для informal остаётся под запретом."""
    async with async_session_factory() as session:
        supplier = await _informal_supplier(session, name="Ромашка Предоплата")
        await session.commit()

        with pytest.raises(CounterpartyPaymentError, match="картой/наличными"):
            await create_standalone_payment_draft(
                session, counterparty_id=supplier.id, amount=Decimal("1000")
            )


async def test_paid_books_transfer_settles_invoices_creates_reserve(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        _account, payer_wallet = await _payer_wallet(session)
        safe_wallet = await _safe_wallet(session)
        article = await make_expense_article(session)
        supplier = await _informal_supplier(session)
        inv1 = await make_invoice(
            session, counterparty_id=supplier.id, amount="1000.00", number="14"
        )
        inv2 = await make_invoice(
            session, counterparty_id=supplier.id, amount="2000.00", number="16"
        )
        await session.commit()
        draft = await create_payment_draft_for_invoices(
            session, invoice_ids=[inv1.id, inv2.id], actor_user_id=None
        )

        status = await apply_payment_status(session, draft=draft, raw_status="executed")
        assert status == "paid"

        # Накладные погашены статус-аллокациями (без bank_operation_id).
        for inv in (inv1, inv2):
            await session.refresh(inv)
            assert inv.payment_status == "paid"
        allocations = (
            await session.scalars(
                select(InvoicePaymentAllocation).where(
                    InvoicePaymentAllocation.invoice_id.in_([inv1.id, inv2.id])
                )
            )
        ).all()
        assert len(allocations) == 2
        assert all(a.source_kind == "bank" and a.bank_operation_id is None for a in allocations)

        # Ровно две ноги транзита р/с→Сейф — и ноль расходных проводок.
        legs = await _transfer_legs(session, draft.id)
        assert len(legs) == 2
        bank_leg = next(t for t in legs if t.wallet_id == payer_wallet.id)
        safe_leg = next(t for t in legs if t.wallet_id == safe_wallet.id)
        assert bank_leg.direction == "out" and bank_leg.amount == Decimal("3000.00")
        assert safe_leg.direction == "in" and safe_leg.amount == Decimal("3000.00")
        expense_count = await session.scalar(
            select(func.count())
            .select_from(CashflowTransaction)
            .where(CashflowTransaction.source_kind == "counterparty_payment")
        )
        assert expense_count == 0

        # Целевой резерв: сумма черновика, поставщик, статья «Оплата поставщикам»,
        # привязка к черновику, читаемое назначение.
        reserve = await _draft_reserve(session, draft.id)
        assert reserve is not None
        assert reserve.amount == Decimal("3000.00")
        assert reserve.counterparty_id == supplier.id
        assert reserve.article_id == article.id
        assert reserve.status == "reserved"
        assert reserve.purpose is not None
        assert "Местный закуп Ромашка" in reserve.purpose
        assert "№14" in reserve.purpose


async def test_paid_is_idempotent(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        await _payer_wallet(session)
        await _safe_wallet(session)
        supplier = await _informal_supplier(session, name="Ромашка Дубль")
        invoice = await make_invoice(
            session, counterparty_id=supplier.id, amount="1000.00", number="7"
        )
        await session.commit()
        draft = await create_payment_draft_for_invoices(
            session, invoice_ids=[invoice.id], actor_user_id=None
        )

        await apply_payment_status(session, draft=draft, raw_status="executed")
        await apply_payment_status(session, draft=draft, raw_status="executed")

        assert len(await _transfer_legs(session, draft.id)) == 2
        reserves = (
            await session.scalars(
                select(SafeAllocation).where(SafeAllocation.source_draft_id == draft.id)
            )
        ).all()
        assert len(reserves) == 1
        alloc_count = await session.scalar(
            select(func.count())
            .select_from(InvoicePaymentAllocation)
            .where(InvoicePaymentAllocation.invoice_id == invoice.id)
        )
        assert alloc_count == 1


async def test_failed_unlinks_invoices_without_reserve(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        await _payer_wallet(session)
        await _safe_wallet(session)
        supplier = await _informal_supplier(session, name="Ромашка Отказ")
        invoice = await make_invoice(
            session, counterparty_id=supplier.id, amount="1000.00", number="9"
        )
        await session.commit()
        draft = await create_payment_draft_for_invoices(
            session, invoice_ids=[invoice.id], actor_user_id=None
        )

        status = await apply_payment_status(session, draft=draft, raw_status="declined")
        assert status == "failed"
        await session.refresh(invoice)
        assert invoice.draft_id is None
        assert invoice.payment_status == "unpaid"
        assert await _draft_reserve(session, draft.id) is None
        assert await _transfer_legs(session, draft.id) == []


async def test_paid_without_payer_wallet_still_fills_safe(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Банк-кошелёк плательщика не привязан: Сейф-нога и резерв всё равно создаются
    (деньги на карте — факт), плюс кейс разбора на настройку счёта."""
    async with async_session_factory() as session:
        # Гасим payer-кошелёк (мог остаться от соседних тестов), чтобы resolve вернул None.
        _account, payer_wallet = await _payer_wallet(session)
        payer_wallet.status = "inactive"
        safe_wallet = await _safe_wallet(session)
        supplier = await _informal_supplier(session, name="Ромашка Без Кошелька")
        invoice = await make_invoice(
            session, counterparty_id=supplier.id, amount="800.00", number="3"
        )
        await session.commit()
        try:
            draft = await create_payment_draft_for_invoices(
                session, invoice_ids=[invoice.id], actor_user_id=None
            )

            await apply_payment_status(session, draft=draft, raw_status="executed")

            await session.refresh(invoice)
            assert invoice.payment_status == "paid"
            legs = await _transfer_legs(session, draft.id)
            assert len(legs) == 1
            assert legs[0].wallet_id == safe_wallet.id and legs[0].direction == "in"
            assert await _draft_reserve(session, draft.id) is not None
            case = await session.scalar(
                select(ReconciliationCase).where(
                    ReconciliationCase.kind == "payer_wallet_unresolved",
                    ReconciliationCase.payload["draft_id"].astext == str(draft.id),
                )
            )
            assert case is not None
        finally:
            payer_wallet.status = "active"
            await session.commit()


async def test_pay_auto_reserve_books_supplier_expense(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Выплата авто-резерва — существующая механика: out-проводка с Сейфа по статье
    «Оплата поставщикам» с контрагентом-поставщиком."""
    async with async_session_factory() as session:
        await _payer_wallet(session)
        safe_wallet = await _safe_wallet(session)
        article = await make_expense_article(session)
        supplier = await _informal_supplier(session, name="Ромашка Выплата")
        invoice = await make_invoice(
            session, counterparty_id=supplier.id, amount="1500.00", number="21"
        )
        await session.commit()
        draft = await create_payment_draft_for_invoices(
            session, invoice_ids=[invoice.id], actor_user_id=None
        )
        await apply_payment_status(session, draft=draft, raw_status="executed")

        reserve = await _draft_reserve(session, draft.id)
        assert reserve is not None
        leg_id = await pay_allocation(
            session, reserve, amount=Decimal("1500.00"), operation_date=date(2026, 7, 5)
        )
        await session.commit()

        assert reserve.status == "paid"
        leg = await session.get(CashflowTransaction, leg_id)
        assert leg is not None
        assert leg.wallet_id == safe_wallet.id and leg.direction == "out"
        assert leg.source_kind == SAFE_PAYOUT_SOURCE_KIND
        assert leg.article_id == article.id
        assert leg.counterparty_id == supplier.id
        assert leg.amount == Decimal("1500.00")
