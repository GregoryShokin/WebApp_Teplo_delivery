"""Пакет «счёт + УПД» одним PDF и свободный платёж контрагенту — два разрыва канона ДЗ/КЗ.

Кейс СДЭК (27.07.2026, прод). Поставщик прислал ОДИН файл с тремя страницами: счёт на оплату,
УПД и приложение к УПД. Система завела из него один документ — УПД — и повесила кредиторку, а
счёт исчез из очереди оплат. Оплату владелец сделал свободным платежом («Новый платёж» → расход
по реквизитам), и она не погасила кредиторку и не создала дебиторку: путь
``apply_payment_status`` для черновика без накладных не звал правило 1 вовсе.

Проверяем канон владельца (17.07): счёт — не долг, он живёт в очереди оплат; УПД создаёт
кредиторку; платёж поставщику FIFO гасит открытую кредиторку, излишек становится дебиторкой.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

from cp_helpers import make_counterparty
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_admin_payout_split import _payer_wallet, _safe_wallet

from app.models import (
    CashflowTransaction,
    DdsArticle,
    EmailInvoiceIntake,
    SupplierInvoice,
    SupplierPrepayment,
)
from app.services.bank_payment_status import apply_payment_status
from app.services.counterparty_payments import ExpenseLineInput, create_expense_payment_draft
from app.services.email_invoice_ingest import (
    delete_intake_forever,
    exclude_intake,
    materialize_from_intake,
    restore_intake,
)
from app.services.new_payment import new_payment_article_flow

PAST = date.today() - timedelta(days=8)


async def _receivable(session: AsyncSession, counterparty_id: uuid.UUID) -> Decimal:
    """ДЗ так, как её считает плитка: остатки открытых предоплат."""
    rows = (
        await session.scalars(
            select(SupplierPrepayment).where(
                SupplierPrepayment.counterparty_id == counterparty_id,
                SupplierPrepayment.status.in_(("open", "partially_settled")),
            )
        )
    ).all()
    return sum(
        (Decimal(str(r.amount)) - Decimal(str(r.amount_settled)) for r in rows), Decimal("0.00")
    )


async def _payable(session: AsyncSession, counterparty_id: uuid.UUID) -> Decimal:
    """КЗ так, как её считает плитка: неоплаченный остаток активных закрывающих."""
    from app.services.counterparty_matching import _invoice_remaining

    rows = (
        await session.scalars(
            select(SupplierInvoice).where(
                SupplierInvoice.counterparty_id == counterparty_id,
                SupplierInvoice.direction == "payable",
                SupplierInvoice.doc_kind == "closing",
                SupplierInvoice.activation_status == "active",
                SupplierInvoice.payment_status.in_(("unpaid", "partially_paid")),
            )
        )
    ).all()
    total = Decimal("0.00")
    for inv in rows:
        total += max(await _invoice_remaining(session, inv), Decimal("0.00"))
    return total


def _package_intake(counterparty_id: uuid.UUID | None) -> EmailInvoiceIntake:
    """Вложение-пакет СДЭК: счёт СКБ-0437096 + закрывающий УПД СКБ-0008640 на ту же сумму."""
    return EmailInvoiceIntake(
        mailbox="personal",
        from_addr="noreply-oplata@cdek.ru",
        subject="Пакет документов СКБ-0437096 от 19.07.2026",
        attachment_sha256=uuid.uuid4().hex + uuid.uuid4().hex,
        status="needs_review",
        counterparty_id=counterparty_id,
        recognition={
            "amount": "7984.90",
            "document_kind": "invoice",
            "invoice_number": "СКБ-0437096",
            "invoice_date": PAST.isoformat(),
            "companion": {
                "amount": "7984.90",
                "document_kind": "upd",
                "invoice_number": "СКБ-0008640",
                "invoice_date": PAST.isoformat(),
            },
        },
    )


async def _free_expense_article(session: AsyncSession) -> DdsArticle:
    rows = await session.scalars(select(DdsArticle).where(DdsArticle.is_active.is_(True)))
    return next(a for a in rows.all() if new_payment_article_flow(a) == "expense")


async def test_package_creates_bill_in_queue_and_closing_in_kz(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Из пакета рождаются ДВА документа: счёт к оплате и закрывающий в кредиторке.

    Прежде вложение давало ровно один документ, и им оказывался УПД (маркер «универсальный
    передаточный документ» со второй страницы бьёт маркеры счёта с первой) — счёт терялся."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="СДЭК-Славянск", inn="2370006152")
        intake = _package_intake(cp.id)
        session.add(intake)
        await session.flush()

        status = await materialize_from_intake(session, intake)
        await session.commit()

        # Счёт — в очереди оплат («В банк» доступна именно для linked).
        assert status == "linked"
        bill = await session.get(SupplierInvoice, intake.invoice_id)
        assert bill is not None
        assert bill.doc_kind == "bill"
        assert bill.amount == Decimal("7984.90")

        # Закрывающий — отдельный документ, он и несёт кредиторку.
        assert intake.companion_invoice_id is not None
        closing = await session.get(SupplierInvoice, intake.companion_invoice_id)
        assert closing is not None
        assert closing.doc_kind == "closing"
        assert closing.number == "СКБ-0008640"
        assert closing.operational_scope == "finance"
        assert await _payable(session, cp.id) == Decimal("7984.90")


async def test_package_exclude_restore_and_delete_move_both_documents(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Исключение/возврат/удаление ведут ОБА документа пакета: УПД не остаётся сиротой в КЗ."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="СДЭК-корзина", inn="2370006153")
        intake = _package_intake(cp.id)
        session.add(intake)
        await session.flush()
        await materialize_from_intake(session, intake)
        await session.commit()
        bill_id, closing_id = intake.invoice_id, intake.companion_invoice_id

        await exclude_intake(session, intake)
        await session.commit()
        assert intake.status == "excluded"
        for doc_id in (bill_id, closing_id):
            doc = await session.get(SupplierInvoice, doc_id)
            assert doc is not None and doc.payment_status == "void"
        assert await _payable(session, cp.id) == Decimal("0.00")

        await restore_intake(session, intake)
        await session.commit()
        assert intake.status == "linked"
        for doc_id in (bill_id, closing_id):
            doc = await session.get(SupplierInvoice, doc_id)
            assert doc is not None and doc.payment_status == "unpaid"
        assert await _payable(session, cp.id) == Decimal("7984.90")

        await exclude_intake(session, intake)
        await session.commit()
        await delete_intake_forever(session, intake)
        await session.commit()
        assert await session.get(SupplierInvoice, bill_id) is None
        assert await session.get(SupplierInvoice, closing_id) is None


async def test_free_counterparty_payment_settles_open_kz_and_keeps_change_as_receivable(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Свободный платёж («Новый платёж» → расход по реквизитам) идёт по правилу 1.

    Кейс СДЭК: открытая кредиторка по УПД 7 984,90, платёж 8 000 → КЗ гасится, разница 15,10
    остаётся дебиторкой. Прежде такой платёж не делал ни того, ни другого — он заводил только
    проводку ДДС, и приходящая операция выписки заклеймляла её prebooked-claim'ом мимо канона."""
    async with async_session_factory() as session:
        _account, payer_wallet = await _payer_wallet(session)
        await _safe_wallet(session)
        article = await _free_expense_article(session)
        cp = await make_counterparty(
            session,
            name="СДЭК-платёж",
            inn="2370006154",
            requisites={
                "bankAcnt": "40702810430000013829",
                "bankBik": "040349602",
                "recipientCorrAccountNumber": "30101810100000000602",
            },
            requisites_verified=True,
        )
        intake = _package_intake(cp.id)
        session.add(intake)
        await session.flush()
        await materialize_from_intake(session, intake)
        await session.commit()
        assert await _payable(session, cp.id) == Decimal("7984.90")

        draft = await create_expense_payment_draft(
            session,
            lines=[
                ExpenseLineInput(
                    article_id=article.id,
                    amount=Decimal("8000.00"),
                    purpose="Услуги доставки",
                    counterparty_id=cp.id,
                )
            ],
        )
        assert await apply_payment_status(session, draft=draft, raw_status="executed") == "paid"
        await session.commit()

        # Расход проведён по выбранной статье — это поведение не менялось.
        txn = await session.scalar(
            select(CashflowTransaction).where(
                CashflowTransaction.source_kind == "counterparty_payment",
                CashflowTransaction.source_id == draft.id,
            )
        )
        assert txn is not None
        assert txn.wallet_id == payer_wallet.id
        assert txn.article_id == article.id

        # Канон: кредиторка погашена платежом, излишек — дебиторка.
        assert await _payable(session, cp.id) == Decimal("0.00")
        assert await _receivable(session, cp.id) == Decimal("15.10")


async def test_free_counterparty_payment_without_kz_becomes_receivable(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Платёж поставщику, у которого долга нет, целиком становится дебиторкой (аванс).

    Второй прод-случай той же дыры: платёж ИП Вишневецкому 6 000 не оставил в учёте следа."""
    async with async_session_factory() as session:
        await _payer_wallet(session)
        await _safe_wallet(session)
        article = await _free_expense_article(session)
        cp = await make_counterparty(
            session,
            name="ИП Вишневецкий (свободный платёж)",
            inn="616100000009",
            requisites={
                "bankAcnt": "40802810000000000001",
                "bankBik": "044525225",
                "recipientCorrAccountNumber": "30101810400000000225",
            },
            requisites_verified=True,
        )

        draft = await create_expense_payment_draft(
            session,
            lines=[
                ExpenseLineInput(
                    article_id=article.id,
                    amount=Decimal("6000.00"),
                    purpose="Комиссия агрегатору",
                    counterparty_id=cp.id,
                )
            ],
        )
        await apply_payment_status(session, draft=draft, raw_status="executed")
        await session.commit()

        assert await _receivable(session, cp.id) == Decimal("6000.00")
