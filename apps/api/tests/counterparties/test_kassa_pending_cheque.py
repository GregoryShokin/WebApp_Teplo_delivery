"""Ручной чек Кассы с отложенным матчем: ввод суммы вручную до прихода выписки, статус
«ожидает подтверждения банком», авто-слияние с card-операцией и эскалация «банк не передал».
Прогоняется на ``teplo_test``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from cp_helpers import (
    make_bank_operation,
    make_counterparty,
    make_expense_article,
)
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    BankOperation,
    CashflowTransaction,
    InvoicePaymentAllocation,
    ReconciliationCase,
    SupplierInvoice,
    Wallet,
)
from app.services.banking.classifier import run_classification_rules
from app.services.kassa.cheque import (
    ChequeLineInput,
    create_cheque,
    escalate_overdue_pending_cheques,
    match_pending_cheque_operations,
)

ISSUED = datetime(2026, 6, 17, 12, 0, tzinfo=UTC)  # день покупки — 17.06 (МСК)
PURCHASE_AUTH = "2026-06-17T08:00:00Z"  # authorizationDate той же датой покупки


async def _txns(session: AsyncSession, invoice_id) -> list[CashflowTransaction]:
    return list(
        (
            await session.scalars(
                select(CashflowTransaction).where(CashflowTransaction.source_id == invoice_id)
            )
        ).all()
    )


async def _allocs(session: AsyncSession, invoice_id) -> list[InvoicePaymentAllocation]:
    return list(
        (
            await session.scalars(
                select(InvoicePaymentAllocation).where(
                    InvoicePaymentAllocation.invoice_id == invoice_id
                )
            )
        ).all()
    )


async def _tbank_main(session: AsyncSession) -> Wallet:
    wallet = await session.scalar(select(Wallet).where(Wallet.code == "tbank_main"))
    assert wallet is not None, "tbank_main засеян миграцией 0115"
    return wallet


async def _make_pending_cheque(
    session: AsyncSession, *, amount: str, lines: list[ChequeLineInput], article_id=None
) -> SupplierInvoice:
    cp = await make_counterparty(session, name="Местный закуп")
    await session.commit()
    return await create_cheque(
        session,
        counterparty_id=cp.id,
        article_id=article_id,
        issued_at=ISSUED,
        pending_card_amount=Decimal(amount),
        track_nomenclature=True,
        lines=lines,
    )


async def _incoming_card_op(session: AsyncSession, *, amount: str, account_id) -> BankOperation:
    op = await make_bank_operation(
        session,
        amount=amount,
        operation_date=date(2026, 6, 19),  # проводка позже покупки (settlement-лаг)
        posted_at=datetime(2026, 6, 19, 0, 0, tzinfo=UTC),
        category="cardOperation",
        raw_payload={"authorizationDate": PURCHASE_AUTH},
        account_id=account_id,
    )
    await session.commit()
    return op


async def test_pending_cheque_creates_awaiting_placeholder(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        article = await make_expense_article(
            session, code="sod", name="Содержание торговых точек"
        )
        tbank = await _tbank_main(session)
        cheque = await _make_pending_cheque(
            session,
            amount="500.00",
            article_id=article.id,
            lines=[
                ChequeLineInput(
                    name="Мыло",
                    quantity=Decimal("1"),
                    price=Decimal("500.00"),
                    dds_article_id=article.id,
                )
            ],
        )

        # Чек считается оплаченным (не висит в кредиторке), но в балансе банка не сидит.
        assert cheque.payment_status == "paid"
        assert cheque.amount == Decimal("500.00")

        txns = await _txns(session, cheque.id)
        assert len(txns) == 1
        placeholder = txns[0]
        assert placeholder.quality_status == "awaiting_bank"
        assert placeholder.wallet_id == tbank.id
        assert placeholder.amount == Decimal("500.00")
        assert placeholder.article_id is None  # разнесём по статьям при матче

        allocs = await _allocs(session, cheque.id)
        assert len(allocs) == 1
        assert allocs[0].source_kind == "card_pending"
        assert allocs[0].cashflow_transaction_id == placeholder.id
        assert allocs[0].bank_operation_id is None


async def test_pending_cheque_matches_incoming_card_operation(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        veg = await make_expense_article(
            session, code="pit", name="Расходы на питание персонала"
        )
        house = await make_expense_article(session, code="sod", name="Содержание торговых точек")
        tbank = await _tbank_main(session)
        cheque = await _make_pending_cheque(
            session,
            amount="500.00",
            lines=[
                ChequeLineInput(
                    name="Помидоры",
                    quantity=Decimal("2"),
                    price=Decimal("150.00"),
                    dds_article_id=veg.id,
                ),  # 300
                ChequeLineInput(
                    name="Мыло",
                    quantity=Decimal("1"),
                    price=Decimal("200.00"),
                    dds_article_id=house.id,
                ),  # 200
            ],
        )
        op = await _incoming_card_op(session, amount="500.00", account_id=tbank.account_id)

        matched = await match_pending_cheque_operations(session)
        await session.commit()
        assert matched == 1

        # Плейсхолдер заменён двумя разнесёнными по статьям проводками (final), без задвоения.
        txns = await _txns(session, cheque.id)
        assert len(txns) == 2
        assert {t.quality_status for t in txns} == {"final"}
        by_article = {t.article_id: t.amount for t in txns}
        assert by_article[veg.id] == Decimal("300.00")
        assert by_article[house.id] == Decimal("200.00")

        # Операция привязана и классифицирована (расход в ДДС один раз).
        op_after = await session.get(BankOperation, op.id)
        assert op_after.cashflow_transaction_id in {t.id for t in txns}
        assert op_after.classification_status == "classified"

        # Аллокация card_pending заменена на bank.
        allocs = await _allocs(session, cheque.id)
        assert [a.source_kind for a in allocs] == ["bank"]
        assert allocs[0].bank_operation_id == op.id

        invoice = await session.get(SupplierInvoice, cheque.id)
        assert invoice.payment_status == "paid"


async def test_generic_classifier_does_not_grab_pending_placeholder(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Защита от двойного учёта: общий prebooked-механизм НЕ привязывает пендинг-плейсхолдер
    # (его сводит только чек-реконсилер) — иначе операция привязалась бы без разнесения.
    async with async_session_factory() as session:
        article = await make_expense_article(session, code="sod", name="Содержание торговых точек")
        tbank = await _tbank_main(session)
        cheque = await _make_pending_cheque(
            session,
            amount="500.00",
            article_id=article.id,
            lines=[
                ChequeLineInput(
                    name="Мыло",
                    quantity=Decimal("1"),
                    price=Decimal("500.00"),
                    dds_article_id=article.id,
                )
            ],
        )
        op = await _incoming_card_op(session, amount="500.00", account_id=tbank.account_id)

        # Общая классификация не должна привязать операцию к плейсхолдеру.
        await run_classification_rules(session, [op])
        await session.commit()
        op_after = await session.get(BankOperation, op.id)
        assert op_after.cashflow_transaction_id is None

        placeholder = (await _txns(session, cheque.id))[0]
        assert placeholder.quality_status == "awaiting_bank"

        # Чек-реконсилер сводит и закрывает заведённый общей классификацией review-кейс.
        matched = await match_pending_cheque_operations(session)
        await session.commit()
        assert matched == 1
        open_op_case = await session.scalar(
            select(ReconciliationCase).where(
                ReconciliationCase.bank_operation_id == op.id,
                ReconciliationCase.status == "pending",
            )
        )
        assert open_op_case is None


async def test_pending_cheque_escalates_when_bank_silent(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        article = await make_expense_article(session, code="sod", name="Содержание торговых точек")
        cheque = await _make_pending_cheque(
            session,
            amount="500.00",
            article_id=article.id,
            lines=[
                ChequeLineInput(
                    name="Мыло",
                    quantity=Decimal("1"),
                    price=Decimal("500.00"),
                    dds_article_id=article.id,
                )
            ],
        )
        placeholder = (await _txns(session, cheque.id))[0]
        # Состарим плейсхолдер за порог (4 дня из настройки по умолчанию).
        await session.execute(
            update(CashflowTransaction)
            .where(CashflowTransaction.id == placeholder.id)
            .values(created_at=datetime(2026, 6, 1, tzinfo=UTC))
        )
        await session.commit()

        now = datetime(2026, 6, 17, tzinfo=UTC)
        created = await escalate_overdue_pending_cheques(session, now=now)
        await session.commit()
        assert created == 1
        case = await session.scalar(
            select(ReconciliationCase).where(
                ReconciliationCase.kind == "unconfirmed_cheque",
                ReconciliationCase.status == "pending",
            )
        )
        assert case is not None
        assert case.payload["invoice_id"] == str(cheque.id)

        # Идемпотентно: повторный прогон новых кейсов не плодит.
        again = await escalate_overdue_pending_cheques(session, now=now)
        await session.commit()
        assert again == 0
