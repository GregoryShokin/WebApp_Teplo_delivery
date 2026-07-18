"""Канон учёта ДЗ/КЗ поставщиков (владелец 17.07.2026): четыре правила.

1. Платёж контрагенту сначала FIFO гасит открытую кредиторку (закрывающие документы),
   излишек становится дебиторкой (предоплатой).
2. УПД/акт (закрывающий документ) — факт выполненных работ: гасит открытую дебиторку зачётом,
   остаток встаёт в кредиторку.
3. Счёт (bill) — НЕ долг: очередь оплат, в баланс ДЗ/КЗ не входит; дебиторку не гасит.
4. Закрывающий документ действует ДАТОЙ ДОКУМЕНТА: будущий УПД ждёт своей даты (pending), в
   свою дату активируется джобой (гасит дебиторку / встаёт в кредиторку).

Плюс переворот email-ingest: УПД/акт материализуются как closing (а не ignored), счета — как
bill (очередь оплат).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date
from decimal import Decimal

import pytest
from cp_helpers import admin_headers, make_counterparty, make_invoice, make_wallet
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.models import (
    CashflowTransaction,
    CounterpartyPayableProfile,
    EmailInvoiceIntake,
    InvoicePaymentAllocation,
    SupplierInvoice,
    SupplierPrepayment,
)
from app.services.invoice_recognition import RecognizedInvoice
from app.services.mail.imap_client import FetchedAttachment
from app.services.supplier_prepayments import (
    activate_due_closing_invoices,
    apply_closing_document,
    ensure_prepayment_from_bank_transaction,
)

BASE = "/api/v1/accounting/suppliers"


def _admin(factory) -> dict[str, str]:
    return asyncio.run(admin_headers(factory))


async def _enable_bank_prepayment(session: AsyncSession, counterparty_id: uuid.UUID) -> None:
    profile = (
        await session.execute(
            select(CounterpartyPayableProfile).where(
                CounterpartyPayableProfile.counterparty_id == counterparty_id
            )
        )
    ).scalar_one()
    profile.bank_payments_create_prepayment = True
    await session.flush()


async def _bank_tx(
    session: AsyncSession, *, wallet_id: uuid.UUID, counterparty_id: uuid.UUID, amount: str
) -> CashflowTransaction:
    tx = CashflowTransaction(
        wallet_id=wallet_id,
        direction="out",
        amount=Decimal(amount),
        operation_date=date(2026, 7, 15),
        counterparty_id=counterparty_id,
        source_kind="bank_operation",
        payment_purpose="оплата поставщику",
        quality_status="auto",
    )
    session.add(tx)
    await session.flush()
    return tx


async def _remaining(session: AsyncSession, invoice_id: uuid.UUID) -> Decimal:
    invoice = await session.get(SupplierInvoice, invoice_id)
    paid = await session.scalar(
        select(func.coalesce(func.sum(InvoicePaymentAllocation.amount), 0)).where(
            InvoicePaymentAllocation.invoice_id == invoice_id
        )
    )
    return Decimal(str(invoice.amount)) - Decimal(str(paid))


# --- Правило 1: платёж FIFO гасит КЗ, излишек → предоплата -----------------------------------


async def test_rule1_payment_settles_open_kz_fifo_then_prepayment(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Правило1-А", inn="6155010101")
        await _enable_bank_prepayment(session, cp.id)
        # Две открытые закрывающие накладные (КЗ 300+200=500), FIFO по дате.
        inv1 = await make_invoice(
            session, counterparty_id=cp.id, amount="300.00", invoice_date=date(2026, 6, 1)
        )
        inv2 = await make_invoice(
            session, counterparty_id=cp.id, amount="200.00", invoice_date=date(2026, 6, 20)
        )
        wallet = await make_wallet(session, name="Банк-1", wallet_type="bank")
        tx = await _bank_tx(session, wallet_id=wallet.id, counterparty_id=cp.id, amount="800.00")

        prepayment = await ensure_prepayment_from_bank_transaction(session, tx)

        # Обе КЗ погашены, излишек 300 стал предоплатой (дебиторкой).
        assert (await session.get(SupplierInvoice, inv1.id)).payment_status == "paid"
        assert (await session.get(SupplierInvoice, inv2.id)).payment_status == "paid"
        assert prepayment is not None
        assert prepayment.amount == Decimal("300.00")
        assert prepayment.cashflow_transaction_id == tx.id


async def test_rule1_payment_fully_absorbed_by_kz_no_prepayment(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Правило1-Б", inn="6155010102")
        await _enable_bank_prepayment(session, cp.id)
        inv = await make_invoice(
            session, counterparty_id=cp.id, amount="1000.00", invoice_date=date(2026, 6, 1)
        )
        wallet = await make_wallet(session, name="Банк-2", wallet_type="bank")
        tx = await _bank_tx(session, wallet_id=wallet.id, counterparty_id=cp.id, amount="600.00")

        prepayment = await ensure_prepayment_from_bank_transaction(session, tx)

        # Платёж 600 < КЗ 1000: частично гасит, предоплаты не возникает.
        assert prepayment is None
        assert (await session.get(SupplierInvoice, inv.id)).payment_status == "partially_paid"
        assert await _remaining(session, inv.id) == Decimal("400.00")
        assert await session.scalar(select(func.count()).select_from(SupplierPrepayment)) == 0


async def test_rule1_reclassify_unwinds_kz_settlement(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Реклассификация/исключение платежа снимает и зачёты КЗ правила 1 — закрывающая
    накладная не остаётся «оплаченной» списанием, которого больше нет (иначе КЗ занижена)."""
    from app.services.banking.classifier import _drop_untouched_bank_prepayments

    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Правило1-реклас", inn="6155010104")
        await _enable_bank_prepayment(session, cp.id)
        inv = await make_invoice(
            session, counterparty_id=cp.id, amount="500.00", invoice_date=date(2026, 6, 1)
        )
        wallet = await make_wallet(session, name="Банк-реклас", wallet_type="bank")
        tx = await _bank_tx(session, wallet_id=wallet.id, counterparty_id=cp.id, amount="800.00")
        pre = await ensure_prepayment_from_bank_transaction(session, tx)
        assert (await session.get(SupplierInvoice, inv.id)).payment_status == "paid"
        assert pre is not None and pre.amount == Decimal("300.00")

        # Оператор исключил/переклассифицировал операцию — cleanup снимает предоплату И зачёт КЗ.
        await _drop_untouched_bank_prepayments(session, {tx.id})
        await session.flush()

        assert (await session.get(SupplierInvoice, inv.id)).payment_status == "unpaid"
        assert await _remaining(session, inv.id) == Decimal("500.00")
        assert await session.scalar(select(func.count()).select_from(SupplierPrepayment)) == 0
        orphans = await session.scalar(
            select(func.count())
            .select_from(InvoicePaymentAllocation)
            .where(InvoicePaymentAllocation.cashflow_transaction_id == tx.id)
        )
        assert orphans == 0


async def test_rule1_frozen_settlement_does_not_double_receivable(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Если зачтённая правилом 1 накладная ушла в банк-черновик (заморожена), повторная
    классификация НЕ задваивает дебиторку: предоплата = сумма проводки − уже проведённые зачёты."""
    from cp_helpers import make_draft

    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Правило1-заморозка", inn="6155010105")
        await _enable_bank_prepayment(session, cp.id)
        inv = await make_invoice(
            session, counterparty_id=cp.id, amount="600.00", invoice_date=date(2026, 6, 1)
        )
        wallet = await make_wallet(session, name="Банк-заморозка", wallet_type="bank")
        tx = await _bank_tx(session, wallet_id=wallet.id, counterparty_id=cp.id, amount="1000.00")
        pre = await ensure_prepayment_from_bank_transaction(session, tx)
        assert pre is not None and pre.amount == Decimal("400.00")

        # Накладную «заморозили» в банк-черновике → unwind её не снимет.
        draft = await make_draft(session, counterparty_id=cp.id, amount="600.00")
        inv_row = await session.get(SupplierInvoice, inv.id)
        inv_row.draft_id = draft.id
        await session.flush()

        again = await ensure_prepayment_from_bank_transaction(session, tx)
        assert again is not None and again.id == pre.id
        assert again.amount == Decimal("400.00")  # НЕ 1000 (зачёт 600 вычтен, не задвоен)


async def test_rule1_frozen_kz_blocks_reclassify_cleanup(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Если зачёт КЗ правила 1 заморожен в банк-черновике, cleanup при реклассификации/удалении
    проводки НЕ осиротляет аллокацию (накладная-фантом), а блокирует операцию понятной ошибкой."""
    from cp_helpers import make_draft

    from app.services.banking.classifier import _drop_untouched_bank_prepayments

    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Правило1-гард", inn="6155010106")
        await _enable_bank_prepayment(session, cp.id)
        inv = await make_invoice(
            session, counterparty_id=cp.id, amount="600.00", invoice_date=date(2026, 6, 1)
        )
        wallet = await make_wallet(session, name="Банк-гард", wallet_type="bank")
        tx = await _bank_tx(session, wallet_id=wallet.id, counterparty_id=cp.id, amount="1000.00")
        await ensure_prepayment_from_bank_transaction(session, tx)
        # Заморозить погашенную закрывающую в банк-черновике.
        draft = await make_draft(session, counterparty_id=cp.id, amount="600.00")
        inv_row = await session.get(SupplierInvoice, inv.id)
        inv_row.draft_id = draft.id
        await session.flush()

        with pytest.raises(ValueError, match="черновик"):
            await _drop_untouched_bank_prepayments(session, {tx.id})


async def test_rule1_bill_is_not_settled_only_closing(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Правило1-В", inn="6155010103")
        await _enable_bank_prepayment(session, cp.id)
        # Счёт (bill) — не долг: платёж его НЕ гасит, целиком уходит в предоплату.
        bill = await make_invoice(
            session, counterparty_id=cp.id, amount="1000.00", doc_kind="bill",
            invoice_date=date(2026, 6, 1),
        )
        wallet = await make_wallet(session, name="Банк-3", wallet_type="bank")
        tx = await _bank_tx(session, wallet_id=wallet.id, counterparty_id=cp.id, amount="700.00")

        prepayment = await ensure_prepayment_from_bank_transaction(session, tx)

        assert prepayment is not None and prepayment.amount == Decimal("700.00")
        assert (await session.get(SupplierInvoice, bill.id)).payment_status == "unpaid"


# --- Правило 2: закрывающий документ гасит открытую предоплату (зачёт) ------------------------


async def test_rule2_closing_document_settles_open_prepayment(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Правило2", inn="6155010201")
        session.add(
            SupplierPrepayment(
                counterparty_id=cp.id,
                kind="subscription",
                amount=Decimal("1000.00"),
                amount_settled=Decimal("0.00"),
                status="open",
            )
        )
        await session.flush()
        # Закрывающий документ (УПД) 600 — зачитывается из предоплаты, остаток КЗ = 0.
        closing = await make_invoice(
            session, counterparty_id=cp.id, amount="600.00", doc_kind="closing",
            invoice_date=date(2026, 7, 1),
        )
        settled = await apply_closing_document(session, closing, as_of=date(2026, 7, 2))

        assert settled == Decimal("600.00")
        assert (await session.get(SupplierInvoice, closing.id)).payment_status == "paid"
        pre = (await session.scalars(select(SupplierPrepayment))).one()
        assert pre.amount_settled == Decimal("600.00")


async def test_rule2_bill_does_not_settle_prepayment(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Правило2-bill", inn="6155010202")
        session.add(
            SupplierPrepayment(
                counterparty_id=cp.id, kind="subscription", amount=Decimal("1000.00"),
                amount_settled=Decimal("0.00"), status="open",
            )
        )
        await session.flush()
        bill = await make_invoice(
            session, counterparty_id=cp.id, amount="600.00", doc_kind="bill",
            invoice_date=date(2026, 7, 1),
        )
        settled = await apply_closing_document(session, bill, as_of=date(2026, 7, 2))

        # Счёт дебиторку не гасит — предоплата нетронута, счёт остаётся в очереди.
        assert settled == Decimal("0.00")
        assert (await session.get(SupplierInvoice, bill.id)).payment_status == "unpaid"
        pre = (await session.scalars(select(SupplierPrepayment))).one()
        assert pre.amount_settled == Decimal("0.00")


# --- Правило 3: счёт вне баланса ДЗ/КЗ, закрывающий формирует КЗ (дашборд) --------------------


def test_rule3_bill_excluded_from_kz_closing_counts(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async def seed() -> tuple[uuid.UUID, uuid.UUID]:
        async with async_session_factory() as session:
            a = await make_counterparty(session, name="Канон-Closing", inn="6155010301")
            await make_invoice(
                session, counterparty_id=a.id, amount="500.00", doc_kind="closing",
                invoice_date=date(2026, 6, 10),
            )
            b = await make_counterparty(session, name="Канон-Bill", inn="6155010302")
            await make_invoice(
                session, counterparty_id=b.id, amount="800.00", doc_kind="bill",
                invoice_date=date(2026, 6, 12),
            )
            await session.commit()
            return a.id, b.id

    a_id, b_id = asyncio.run(seed())
    payload = client.get(f"{BASE}/balances", headers=_admin(async_session_factory)).json()
    by_id = {item["counterparty_id"]: item for item in payload["items"]}

    assert by_id[str(a_id)]["payable"] == 500.0  # закрывающий = кредиторка
    # Контрагент только со счётом в балансе ДЗ/КЗ не появляется (счёт — не долг).
    assert str(b_id) not in by_id


def test_rule3_bill_absent_from_documents_register(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async def seed() -> tuple[uuid.UUID, uuid.UUID]:
        async with async_session_factory() as session:
            cp = await make_counterparty(session, name="Реестр-док", inn="6155010303")
            closing = await make_invoice(
                session, counterparty_id=cp.id, amount="500.00", doc_kind="closing",
                number="УПД-1", invoice_date=date(2026, 6, 10),
            )
            bill = await make_invoice(
                session, counterparty_id=cp.id, amount="800.00", doc_kind="bill",
                number="СЧ-1", invoice_date=date(2026, 6, 12),
            )
            await session.commit()
            return closing.id, bill.id

    closing_id, bill_id = asyncio.run(seed())
    payload = client.get(f"{BASE}/documents", headers=_admin(async_session_factory)).json()
    ids = {row["invoice_id"] for row in payload["items"]}
    assert str(closing_id) in ids
    assert str(bill_id) not in ids  # счёт не документ взаиморасчётов
    assert payload["unpaid_total"] == 500.0


# --- Правило 4: будущий УПД ждёт своей даты, затем активируется -------------------------------


async def test_rule4_future_closing_is_pending_then_activated(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    today = date(2026, 7, 15)
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="ЭкоЦентр-тест", inn="6155010401")
        session.add(
            SupplierPrepayment(
                counterparty_id=cp.id, kind="subscription", amount=Decimal("1000.00"),
                amount_settled=Decimal("0.00"), status="open",
            )
        )
        await session.flush()
        # УПД датирован БУДУЩИМ (31.07) — приходит заранее.
        upd = await make_invoice(
            session, counterparty_id=cp.id, amount="600.00", doc_kind="closing",
            invoice_date=date(2026, 7, 31),
        )
        settled = await apply_closing_document(session, upd, as_of=today)

        # В свою дату ещё не наступил: pending, дебиторку не тронул.
        assert settled == Decimal("0.00")
        row = await session.get(SupplierInvoice, upd.id)
        assert row.activation_status == "pending"
        pre = (await session.scalars(select(SupplierPrepayment))).one()
        assert pre.amount_settled == Decimal("0.00")

        # Джоба до наступления даты его НЕ берёт.
        res_early = await activate_due_closing_invoices(
            session, as_of=date(2026, 7, 30), commit=False
        )
        assert res_early["activated"] == 0

        # В свою дату (31.07) активируется: гасит дебиторку.
        res = await activate_due_closing_invoices(session, as_of=date(2026, 7, 31), commit=False)
        assert res["activated"] == 1
        row = await session.get(SupplierInvoice, upd.id)
        assert row.activation_status == "active"
        assert row.payment_status == "paid"
        await session.refresh(pre)
        assert pre.amount_settled == Decimal("600.00")


def test_rule4_pending_closing_excluded_from_kz_dashboard(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async def seed() -> uuid.UUID:
        async with async_session_factory() as session:
            cp = await make_counterparty(session, name="Будущий-УПД", inn="6155010402")
            await make_invoice(
                session, counterparty_id=cp.id, amount="600.00", doc_kind="closing",
                activation_status="pending", invoice_date=date(2026, 7, 31),
            )
            await session.commit()
            return cp.id

    cp_id = asyncio.run(seed())
    payload = client.get(f"{BASE}/balances", headers=_admin(async_session_factory)).json()
    by_id = {item["counterparty_id"]: item for item in payload["items"]}
    # Контрагент виден (документооборот есть), но кредиторки пока нет — УПД ещё не в силе.
    assert str(cp_id) in by_id
    assert by_id[str(cp_id)]["payable"] == 0.0


# --- Переворот email-ingest: УПД → closing, счёт → bill --------------------------------------


def _rec(document_kind: str, *, amount: str, inn: str) -> RecognizedInvoice:
    return RecognizedInvoice(
        recipient_name="Тестовый поставщик",
        inn=inn,
        amount=Decimal(amount),
        invoice_number="Д-1",
        invoice_date=date(2026, 7, 10),
        document_kind=document_kind,
        confidence=0.95,
        engine="deterministic",
    )


def _attachment(content: bytes = b"%PDF-fake") -> FetchedAttachment:
    return FetchedAttachment(
        mailbox="corporate",
        message_uid="1",
        message_id="<m1@x>",
        from_addr="billing@supplier.test",
        subject="Документ",
        received_at=None,
        filename="doc.pdf",
        mime="application/pdf",
        content=content,
    )


def test_flip_email_upd_materializes_as_closing(
    async_session_factory: async_sessionmaker[AsyncSession], monkeypatch
) -> None:
    """Главбаг: раньше УПД из почты уходил в ignored. Теперь материализуется как closing,
    гасит открытую предоплату, а intake получает статус 'closing' (не в очереди оплат)."""
    from app.services import email_invoice_ingest as ingest

    async def fake_recognize(pdf, *, settings, context_text=None):
        return _rec("upd", amount="600.00", inn="6155010501")

    monkeypatch.setattr(ingest, "recognize", fake_recognize)

    async def run() -> tuple[str, uuid.UUID]:
        async with async_session_factory() as session:
            cp = await make_counterparty(session, name="Поставщик-УПД", inn="6155010501")
            session.add(
                SupplierPrepayment(
                    counterparty_id=cp.id, kind="subscription", amount=Decimal("1000.00"),
                    amount_settled=Decimal("0.00"), status="open",
                )
            )
            await session.commit()
            status = await ingest.process_attachment(
                session, _attachment(b"%PDF-upd"), settings=get_settings()
            )
            await session.commit()
            return status, cp.id

    status, cp_id = asyncio.run(run())
    assert status == "closing"

    async def check() -> None:
        async with async_session_factory() as session:
            inv = (
                await session.scalars(
                    select(SupplierInvoice).where(SupplierInvoice.counterparty_id == cp_id)
                )
            ).one()
            assert inv.doc_kind == "closing"
            assert inv.payment_status == "paid"  # погашен зачётом из предоплаты (правило 2)
            pre = (await session.scalars(select(SupplierPrepayment))).one()
            assert pre.amount_settled == Decimal("600.00")
            intake = (await session.scalars(select(EmailInvoiceIntake))).one()
            assert intake.status == "closing"

    asyncio.run(check())


def test_flip_email_invoice_materializes_as_bill(
    async_session_factory: async_sessionmaker[AsyncSession], monkeypatch
) -> None:
    """Счёт из почты → bill: очередь оплат (status='linked'), дебиторку НЕ гасит."""
    from app.services import email_invoice_ingest as ingest

    async def fake_recognize(pdf, *, settings, context_text=None):
        return _rec("invoice", amount="800.00", inn="6155010502")

    monkeypatch.setattr(ingest, "recognize", fake_recognize)

    async def run() -> tuple[str, uuid.UUID]:
        async with async_session_factory() as session:
            cp = await make_counterparty(session, name="Поставщик-Счёт", inn="6155010502")
            session.add(
                SupplierPrepayment(
                    counterparty_id=cp.id, kind="subscription", amount=Decimal("1000.00"),
                    amount_settled=Decimal("0.00"), status="open",
                )
            )
            await session.commit()
            status = await ingest.process_attachment(
                session, _attachment(b"%PDF-bill"), settings=get_settings()
            )
            await session.commit()
            return status, cp.id

    status, cp_id = asyncio.run(run())
    assert status == "linked"

    async def check() -> None:
        async with async_session_factory() as session:
            inv = (
                await session.scalars(
                    select(SupplierInvoice).where(SupplierInvoice.counterparty_id == cp_id)
                )
            ).one()
            assert inv.doc_kind == "bill"
            assert inv.payment_status == "unpaid"  # счёт не гасит предоплату
            pre = (await session.scalars(select(SupplierPrepayment))).one()
            assert pre.amount_settled == Decimal("0.00")

    asyncio.run(check())


def test_flip_email_reconciliation_stays_ignored(
    async_session_factory: async_sessionmaker[AsyncSession], monkeypatch
) -> None:
    """Акт сверки учёт не двигает — остаётся ignored, накладную не создаёт."""
    from app.services import email_invoice_ingest as ingest

    async def fake_recognize(pdf, *, settings, context_text=None):
        return _rec("reconciliation", amount="500.00", inn="6155010503")

    monkeypatch.setattr(ingest, "recognize", fake_recognize)

    async def run() -> str:
        async with async_session_factory() as session:
            await make_counterparty(session, name="Поставщик-Сверка", inn="6155010503")
            await session.commit()
            status = await ingest.process_attachment(
                session, _attachment(b"%PDF-recon"), settings=get_settings()
            )
            await session.commit()
            count = await session.scalar(select(func.count()).select_from(SupplierInvoice))
            assert count == 0
            return status

    assert asyncio.run(run()) == "ignored"


# --- Оплата счёта (bill) заводит ДЗ; УПД её гасит (блокер-2, все двери оплаты) ----------------
#
# Канон владельца: «Сам по себе счёт ничего не делает… Оплата счёта уходит в дебиторскую
# задолженность». Раньше двери оплаты (черновик «Страницы на оплату», ручная оплата с кошелька,
# банковская сверка) гасили счёт как долг БЕЗ ДЗ → пришедший позже закрывающий УПД повисал
# фантомной КЗ на уже оплаченный счёт (риск повторной оплаты, блокер-2). Теперь оплата счёта
# заводит предоплату (ДЗ), а сверка счёта как долга запрещена.


_PAYER_ACCT = "40702810900000012345"


async def _seed_payer_bank_wallet(session: AsyncSession, draft) -> None:
    """Завести банк-кошелёк плательщика, чтобы apply_payment_status пошёл ОСНОВНОЙ веткой
    (prebooked-проводка «Оплата по статусу», как в проде), а не деградированным fallback'ом
    (кошелёк не резолвится → расход и его ДЗ добираются классификацией приходящей выписки)."""
    from cp_helpers import make_account, make_wallet

    account = await make_account(session, account_number=_PAYER_ACCT)
    await make_wallet(session, name="Т-Банк р/с", wallet_type="bank", account_id=account.id)
    draft.payload = {"accountNumber": _PAYER_ACCT}
    await session.flush()


async def test_bill_payment_via_draft_creates_prepayment_not_debt(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Дверь #4 (основной путь «Страница на оплату → В банк»): оплата счёта уводит его из очереди
    (оплачен), но встаёт дебиторкой (ДЗ), а не гасит его как обязательство."""
    from cp_helpers import make_draft

    from app.services.bank_payment_status import apply_payment_status

    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Директ-счёт", inn="6155010601")
        bill = await make_invoice(
            session, counterparty_id=cp.id, amount="1000.00", doc_kind="bill",
            number="СЧ-Директ", invoice_date=date(2026, 7, 1),
        )
        draft = await make_draft(session, counterparty_id=cp.id, amount="1000.00")
        await _seed_payer_bank_wallet(session, draft)
        bill.draft_id = draft.id
        await session.flush()

        await apply_payment_status(session, draft=draft, raw_status="executed", commit=False)

        # Счёт ушёл из очереди (оплачен), но его оплата стала дебиторкой (ДЗ).
        assert (await session.get(SupplierInvoice, bill.id)).payment_status == "paid"
        pre = (
            await session.scalars(
                select(SupplierPrepayment).where(SupplierPrepayment.counterparty_id == cp.id)
            )
        ).one()
        assert pre.status == "open"
        assert pre.amount == Decimal("1000.00")
        assert pre.kind == "prepaid_bill"
        assert pre.bill_invoice_id == bill.id  # единый чокпоинт связал ДЗ со счётом
        # ДЗ денег не двигает — они уже учтены оплатой счёта (второй проводки нет).
        assert pre.cashflow_transaction_id is None


async def test_bill_payment_then_later_upd_nets_to_zero(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Полный контур блокера-2: оплата счёта → ДЗ; пришедший позже закрывающий УПД гасит ДЗ
    зачётом (правило 2) → ни дебиторки, ни фантомной кредиторки на оплаченный счёт."""
    from cp_helpers import make_draft

    from app.services.bank_payment_status import apply_payment_status

    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Директ-цикл", inn="6155010602")
        bill = await make_invoice(
            session, counterparty_id=cp.id, amount="1000.00", doc_kind="bill",
            number="СЧ-цикл", invoice_date=date(2026, 7, 1),
        )
        draft = await make_draft(session, counterparty_id=cp.id, amount="1000.00")
        await _seed_payer_bank_wallet(session, draft)
        bill.draft_id = draft.id
        await session.flush()
        await apply_payment_status(session, draft=draft, raw_status="executed", commit=False)
        pre = (
            await session.scalars(
                select(SupplierPrepayment).where(SupplierPrepayment.counterparty_id == cp.id)
            )
        ).one()
        assert pre.amount == Decimal("1000.00") and pre.status == "open"

        # Закрывающий УПД на ту же сумму приходит позже — гасит ДЗ, фантомной КЗ не образует.
        upd = await make_invoice(
            session, counterparty_id=cp.id, amount="1000.00", doc_kind="closing",
            number="УПД-цикл", invoice_date=date(2026, 7, 20),
        )
        settled = await apply_closing_document(session, upd, as_of=date(2026, 7, 21))

        assert settled == Decimal("1000.00")
        assert (await session.get(SupplierInvoice, upd.id)).payment_status == "paid"
        await session.refresh(pre)
        assert pre.amount_settled == Decimal("1000.00")  # ДЗ полностью зачтена → баланс 0/0


async def test_bill_payment_from_wallet_creates_prepayment(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Дверь #5 (ручная оплата счёта с кошелька, нал/карта): тоже заводит ДЗ, а не гасит долг."""
    from cp_helpers import make_expense_article

    from app.services.counterparty_payments import pay_invoice_from_wallet

    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Кошелёк-счёт", inn="6155010603")
        bill = await make_invoice(
            session, counterparty_id=cp.id, amount="500.00", doc_kind="bill",
            number="СЧ-кошелёк", invoice_date=date(2026, 7, 1),
        )
        wallet = await make_wallet(session, name="Касса-счёт", wallet_type="cash_safe")
        article = await make_expense_article(session)
        await session.commit()

        await pay_invoice_from_wallet(
            session, invoice_id=bill.id, wallet_id=wallet.id, amount=Decimal("500.00"),
            operation_date=date(2026, 7, 2), article_id=article.id,
        )

        assert (await session.get(SupplierInvoice, bill.id)).payment_status == "paid"
        pre = (
            await session.scalars(
                select(SupplierPrepayment).where(SupplierPrepayment.counterparty_id == cp.id)
            )
        ).one()
        assert pre.kind == "prepaid_bill" and pre.amount == Decimal("500.00")


async def test_bill_settled_via_reconcile_matcher_creates_prepayment(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Дверь банковской сверки/авто-match (allocate_bank_operation_to_draft) — подтверждённо
    пропущенная. Единый чокпоинт закрывает её без индивидуальной правки: гашение счёта операцией
    заводит ДЗ prepaid_bill (иначе пришедший позже УПД повис бы фантомной КЗ)."""
    from cp_helpers import make_bank_operation, make_draft

    from app.services.counterparty_matching import allocate_bank_operation_to_draft

    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Автоматч-счёт", inn="6155010604")
        bill = await make_invoice(
            session, counterparty_id=cp.id, amount="1000.00", doc_kind="bill",
            number="СЧ-автоматч", invoice_date=date(2026, 7, 1),
        )
        draft = await make_draft(session, counterparty_id=cp.id, amount="1000.00")
        bill.draft_id = draft.id
        op = await make_bank_operation(session, amount="1000.00", inn="6155010604")
        await session.commit()

        await allocate_bank_operation_to_draft(
            session, bank_operation_id=op.id, draft_id=draft.id, commit=False
        )

        assert (await session.get(SupplierInvoice, bill.id)).payment_status == "paid"
        pre = (
            await session.scalars(
                select(SupplierPrepayment).where(SupplierPrepayment.counterparty_id == cp.id)
            )
        ).one()
        assert pre.kind == "prepaid_bill" and pre.amount == Decimal("1000.00")
        assert pre.bill_invoice_id == bill.id


async def test_bill_settled_via_allocate_cash_creates_prepayment(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Дверь ручной наличной аллокации (allocate_cash_to_invoice) — подтверждённо пропущенная.
    Единый чокпоинт заводит ДЗ и здесь."""
    from app.services.counterparty_matching import allocate_cash_to_invoice

    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Кэш-аллок-счёт", inn="6155010605")
        bill = await make_invoice(
            session, counterparty_id=cp.id, amount="400.00", doc_kind="bill",
            number="СЧ-кэшаллок", invoice_date=date(2026, 7, 1),
        )
        await session.commit()

        await allocate_cash_to_invoice(session, invoice_id=bill.id, amount=Decimal("400.00"))

        assert (await session.get(SupplierInvoice, bill.id)).payment_status == "paid"
        pre = (
            await session.scalars(
                select(SupplierPrepayment).where(SupplierPrepayment.counterparty_id == cp.id)
            )
        ).one()
        assert pre.kind == "prepaid_bill" and pre.amount == Decimal("400.00")


async def test_reverse_order_upd_before_payment_nets_via_chokepoint(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Обратный порядок (подтверждённый пробел): закрывающий УПД приходит РАНЬШЕ оплаты счёта →
    висит открытой КЗ. Затем оплата счёта через чокпоинт неттит эту КЗ (правило 2 в обратную
    сторону) → ни дебиторки, ни фантомной кредиторки."""
    from app.services.counterparty_matching import allocate_cash_to_invoice

    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Обратный-порядок", inn="6155010606")
        # 1) Закрывающий УПД приходит первым — открытых предоплат нет → висит КЗ=1000.
        upd = await make_invoice(
            session, counterparty_id=cp.id, amount="1000.00", doc_kind="closing",
            number="УПД-обр", invoice_date=date(2026, 7, 1),
        )
        settled_now = await apply_closing_document(session, upd, as_of=date(2026, 7, 2))
        assert settled_now == Decimal("0.00")
        assert (await session.get(SupplierInvoice, upd.id)).payment_status == "unpaid"

        # 2) Затем оплачивается счёт того же контрагента → чокпоинт заводит ДЗ и сразу неттит КЗ.
        bill = await make_invoice(
            session, counterparty_id=cp.id, amount="1000.00", doc_kind="bill",
            number="СЧ-обр", invoice_date=date(2026, 7, 3),
        )
        await session.commit()
        await allocate_cash_to_invoice(session, invoice_id=bill.id, amount=Decimal("1000.00"))

        # УПД погашен зачётом из ДЗ по счёту → контур в ноль (ни ДЗ-остатка, ни КЗ).
        assert (await session.get(SupplierInvoice, upd.id)).payment_status == "paid"
        pre = (
            await session.scalars(
                select(SupplierPrepayment).where(SupplierPrepayment.counterparty_id == cp.id)
            )
        ).one()
        assert pre.amount_settled == Decimal("1000.00")  # ДЗ полностью зачтена в КЗ УПД


def test_prepaid_bill_visible_in_dz_tile_not_recognition_queue(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Видимость ДЗ по счёту (канон «оплата счёта → дебиторка»): предоплата prepaid_bill входит в
    плитку «Дебиторская» дашборда /balances, но НЕ засоряет очередь «Признание расходов»
    (period-распределение) — её гасит УПД, а не ручная разметка периода."""
    from app.services.counterparty_matching import allocate_cash_to_invoice

    async def seed() -> uuid.UUID:
        async with async_session_factory() as session:
            cp = await make_counterparty(session, name="Видимость-ДЗ", inn="6155010607")
            bill = await make_invoice(
                session, counterparty_id=cp.id, amount="1000.00", doc_kind="bill",
                number="СЧ-вид", invoice_date=date(2026, 7, 1),
            )
            await session.commit()
            await allocate_cash_to_invoice(session, invoice_id=bill.id, amount=Decimal("1000.00"))
            return cp.id

    cp_id = asyncio.run(seed())
    headers = _admin(async_session_factory)

    # Плитка «Дебиторская» дашборда видит предоплату по оплаченному счёту.
    balances = client.get(f"{BASE}/balances", headers=headers).json()
    by_id = {item["counterparty_id"]: item for item in balances["items"]}
    assert by_id[str(cp_id)]["receivable"] == 1000.0

    # Очередь распределения (list_supplier_accounting) её НЕ показывает — prepaid_bill исключён.
    accounting = client.get(BASE, headers=headers).json()
    cp_items = [i for i in accounting["items"] if i["counterparty_id"] == str(cp_id)]
    assert cp_items == []


async def test_bill_payment_reduction_unwinds_closing_settlement(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Ре-аудит чокпоинта (асимметрия самолечения): если оплату счёта УМЕНЬШИЛИ после того как его
    ДЗ уже зачла закрывающий УПД (напр. пере-разбор банк-операции снял строку счёта), чокпоинт
    откатывает зачёт — УПД возвращается в КЗ, ДЗ снимается. Иначе УПД остался бы фантомно
    погашенным,
    долг поставщику занижен."""
    from app.models import InvoicePaymentAllocation
    from app.services.counterparty_matching import _recompute_status, allocate_cash_to_invoice

    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Откат-оплаты", inn="6155010608")
        upd = await make_invoice(
            session, counterparty_id=cp.id, amount="1000.00", doc_kind="closing",
            number="УПД-откат", invoice_date=date(2026, 7, 1),
        )
        await apply_closing_document(session, upd, as_of=date(2026, 7, 2))
        bill = await make_invoice(
            session, counterparty_id=cp.id, amount="1000.00", doc_kind="bill",
            number="СЧ-откат", invoice_date=date(2026, 7, 3),
        )
        await session.commit()
        # Оплатили счёт → ДЗ зачла открытый УПД (обратный порядок).
        await allocate_cash_to_invoice(session, invoice_id=bill.id, amount=Decimal("1000.00"))
        assert (await session.get(SupplierInvoice, upd.id)).payment_status == "paid"

        # Пере-разбор снял оплату счёта → чокпоинт откатывает зачёт УПД.
        alloc = (
            await session.scalars(
                select(InvoicePaymentAllocation).where(
                    InvoicePaymentAllocation.invoice_id == bill.id
                )
            )
        ).one()
        await session.delete(alloc)
        await session.flush()
        await _recompute_status(session, bill)
        await session.commit()

        assert (await session.get(SupplierInvoice, bill.id)).payment_status == "unpaid"
        # КЗ восстановлена
        assert (await session.get(SupplierInvoice, upd.id)).payment_status == "unpaid"
        cnt = await session.scalar(
            select(func.count())
            .select_from(SupplierPrepayment)
            .where(SupplierPrepayment.counterparty_id == cp.id)
        )
        assert cnt == 0  # фантомной ДЗ не осталось


async def test_bill_reduction_with_frozen_closing_no_check_violation(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Фикс-подтверждение (новый дефект клоубэка): если зачёт закрывающего заморожен в
    банк-черновике,
    _unwind его не снимает, поэтому amount_settled остаётся > paid — тогда amount НЕЛЬЗЯ опускать до
    paid (иначе CHECK amount>=amount_settled → IntegrityError, пере-разбор падает). Проверяем:
    усадка
    оплаты счёта при замороженном зачёте НЕ роняет транзакцию."""
    from cp_helpers import make_draft

    from app.models import InvoicePaymentAllocation
    from app.services.counterparty_matching import allocate_cash_to_invoice

    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Заморозка-усадка", inn="6155010610")
        closing = await make_invoice(
            session, counterparty_id=cp.id, amount="1500.00", doc_kind="closing",
            number="УПД-фриз", invoice_date=date(2026, 7, 1),
        )
        await apply_closing_document(session, closing, as_of=date(2026, 7, 2))
        bill = await make_invoice(
            session, counterparty_id=cp.id, amount="1000.00", doc_kind="bill",
            number="СЧ-фриз", invoice_date=date(2026, 7, 3),
        )
        await session.commit()
        # Оплата счёта 1000 → ДЗ зачла 1000 из УПД (остаток УПД 500).
        await allocate_cash_to_invoice(session, invoice_id=bill.id, amount=Decimal("1000.00"))
        assert (await session.get(SupplierInvoice, closing.id)).payment_status == "partially_paid"
        pre = (
            await session.scalars(
                select(SupplierPrepayment).where(SupplierPrepayment.counterparty_id == cp.id)
            )
        ).one()
        assert pre.amount_settled == Decimal("1000.00")

        # Зачтённый УПД заморозили в банк-черновике (его остаток отправлен в банк).
        draft = await make_draft(session, counterparty_id=cp.id, amount="500.00")
        closing.draft_id = draft.id
        await session.flush()

        # Усаживаем оплату счёта до 600: _unwind пропускает замороженный УПД → settled(1000)>600.
        alloc = (
            await session.scalars(
                select(InvoicePaymentAllocation).where(
                    InvoicePaymentAllocation.invoice_id == bill.id
                )
            )
        ).one()
        await session.delete(alloc)
        await session.flush()
        # НЕ должно упасть на CHECK amount>=amount_settled: amount=max(600,1000)=1000.
        await allocate_cash_to_invoice(session, invoice_id=bill.id, amount=Decimal("600.00"))
        await session.commit()

        await session.refresh(pre)
        assert pre.amount == Decimal("1000.00")  # не опущен ниже замороженного зачёта
        assert pre.amount_settled == Decimal("1000.00")


async def test_barter_netting_excludes_bill(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Ре-аудит чокпоинта (пропущенная дверь): бартер-зачёт помечал бы счёт paid ПРЯМЫМ
    присвоением в обход _recompute_status → ДЗ не завелась бы, УПД повис бы фантомной КЗ; к тому же
    гасить дебиторку о «намерение оплаты» — двойной учёт. Счёт исключён из кандидатов бартера."""
    from app.services.counterparty_barter_match import _load_open

    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Бартер-счёт", inn="6155010609")
        bill = await make_invoice(
            session, counterparty_id=cp.id, amount="1000.00", doc_kind="bill",
            direction="payable", number="СЧ-бартер",
        )
        closing = await make_invoice(
            session, counterparty_id=cp.id, amount="500.00", doc_kind="closing",
            direction="payable", number="УПД-бартер",
        )
        await session.flush()

        payables, _receivables = await _load_open(session, cp.id)
        payable_ids = {ref.invoice.id for ref in payables}
        assert bill.id not in payable_ids  # счёт из бартер-зачёта исключён
        assert closing.id in payable_ids  # закрывающая — участвует как прежде


async def test_closing_via_draft_still_settles_as_debt_no_prepayment(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Регресс-гард: закрывающая накладная (склад/iiko/ручной реестр), оплаченная черновиком,
    по-прежнему гасится как долг БЕЗ предоплаты — переворот касается только счетов (bill)."""
    from cp_helpers import make_draft

    from app.services.bank_payment_status import apply_payment_status

    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Склад-закрыв", inn="6155010605")
        closing = await make_invoice(
            session, counterparty_id=cp.id, amount="700.00", doc_kind="closing",
            number="Прих-1", invoice_date=date(2026, 7, 1),
        )
        draft = await make_draft(session, counterparty_id=cp.id, amount="700.00")
        await _seed_payer_bank_wallet(session, draft)
        closing.draft_id = draft.id
        await session.flush()

        await apply_payment_status(session, draft=draft, raw_status="executed", commit=False)

        assert (await session.get(SupplierInvoice, closing.id)).payment_status == "paid"
        # Закрывающая — реальный долг: предоплаты (ДЗ) её оплата НЕ создаёт.
        count = await session.scalar(
            select(func.count())
            .select_from(SupplierPrepayment)
            .where(SupplierPrepayment.counterparty_id == cp.id)
        )
        assert count == 0
