"""Возвраты карт-покупок в чеках Кассы.

Чек вводится gross (все позиции как на бумаге, сверка с операцией копейка в копейку),
возвращённые позиции помечаются ``is_return`` и не проводятся: ДДС/аллокации/iiko = net.

⚠️ Надёжного пер-транзакционного ключа «покупка↔возврат» банк НЕ даёт (боевой Ч-22,
2026-07-06: у возврата свой ``rrn``, ``authCode`` отсутствует). Поэтому возврат
сопоставляется с ЧЕКОМ по декларации кассира: та же карта (``cardNumber``) + остаток
ожидания (Σ ``is_return`` − привязанные возвраты) == сумме возврата. Прогон на ``teplo_test``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from cp_helpers import (
    make_account,
    make_bank_operation,
    make_counterparty,
    make_expense_article,
    make_wallet,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    BankOperation,
    CashflowTransaction,
    DdsArticle,
    InvoiceLineItem,
    InvoicePaymentAllocation,
    ReconciliationCase,
)
from app.services.kassa.cheque import (
    CARD_REFUND_CASE_KIND,
    EXPENSE_REFUND_ARTICLE_CODE,
    REFUND_MISSING_CASE_KIND,
    ChequeBankPart,
    ChequeLineInput,
    KassaChequeError,
    apply_card_refund_case,
    create_cheque,
    escalate_missing_cheque_refunds,
    list_card_transactions,
    match_card_refund_operations,
)
from app.services.kassa.cheque_payout_push import compute_kassa_goods_split

ISSUED = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)
OP_DATE = date(2026, 7, 4)
REFUND_DATE = date(2026, 7, 6)
# Боевые реквизиты Ч-22: у покупки и возврата РАЗНЫЕ rrn, у возврата НЕТ authCode — общий
# только cardNumber. Это ровно то, на чём падал старый ключ rrn+authCode.
CARD = "220070******4426"
PURCHASE_RRN = "618525666673"
PURCHASE_AUTH = "449672"
REFUND_RRN = "618544314930"  # свой rrn у возврата!


async def _purchase_op(
    session: AsyncSession,
    *,
    amount: str,
    card: str = CARD,
    account=None,
    wallet=None,
):
    """Счёт + карт-кошелёк + карт-покупка (cardNumber + свой rrn/authCode, как в выписке)."""
    if account is None:
        account = await make_account(session)
    if wallet is None:
        wallet = await make_wallet(
            session, name="Тинькофф карта", wallet_type="bank", account_id=account.id
        )
    op = await make_bank_operation(
        session,
        amount=amount,
        operation_date=OP_DATE,
        posted_at=ISSUED,
        category="cardOperation",
        account_id=account.id,
        raw_payload={"cardNumber": card, "rrn": PURCHASE_RRN, "authCode": PURCHASE_AUTH},
    )
    return account, wallet, op


_refund_doc_seq = 0


async def _refund_op(
    session: AsyncSession,
    account_id,
    *,
    amount: str,
    card: str = CARD,
) -> BankOperation:
    """Настоящий возврат refundIn: свой rrn, БЕЗ authCode, тот же cardNumber, свой
    documentNumber (у боевого возврата он есть → ingest-дедуп по stable_key работает)."""
    global _refund_doc_seq
    _refund_doc_seq += 1
    return await make_bank_operation(
        session,
        amount=amount,
        direction="in",
        operation_date=REFUND_DATE,
        category="refundIn",
        account_id=account_id,
        raw_payload={"cardNumber": card, "rrn": REFUND_RRN, "documentNumber": f"D{_refund_doc_seq}"},
        classification_status="pending",
    )


async def _net_cheque(session: AsyncSession, op, *, article, ret: str = "500.00", keep: str = "5500.00"):
    """Чек gross: позиция keep + возвращённая позиция ret → проводится net (keep)."""
    cp = await make_counterparty(session, name="Местный закуп")
    return await create_cheque(
        session,
        counterparty_id=cp.id,
        article_id=None,
        issued_at=ISSUED,
        bank_parts=[ChequeBankPart(bank_operation_id=op.id)],
        track_nomenclature=True,
        lines=[
            ChequeLineInput(
                name="Товар", quantity=Decimal("1"), price=Decimal(keep), dds_article_id=article.id
            ),
            ChequeLineInput(
                name="Возвращённый товар",
                quantity=Decimal("1"),
                price=Decimal(ret),
                dds_article_id=article.id,
                is_return=True,
            ),
        ],
    )


async def _gross_cheque(session: AsyncSession, op, *, article):
    """Чек 6000 без пометок возврата — проведён полностью (возврата не ждёт)."""
    cp = await make_counterparty(session, name="Местный закуп")
    return await create_cheque(
        session,
        counterparty_id=cp.id,
        article_id=None,
        issued_at=ISSUED,
        bank_parts=[ChequeBankPart(bank_operation_id=op.id)],
        track_nomenclature=True,
        lines=[
            ChequeLineInput(
                name="Товар", quantity=Decimal("1"), price=Decimal("6000.00"), dds_article_id=article.id
            )
        ],
    )


async def _pending_cases(session: AsyncSession, kind: str) -> list[ReconciliationCase]:
    return list(
        (
            await session.scalars(
                select(ReconciliationCase).where(
                    ReconciliationCase.kind == kind, ReconciliationCase.status == "pending"
                )
            )
        ).all()
    )


# --- Ввод чека с возвратом: net-проводка --------------------------------------------------


async def test_cheque_with_returned_line_books_net(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        article = await make_expense_article(session, code="personal", name="Расходы на персонал")
        _, wallet, op = await _purchase_op(session, amount="6000.00")
        await session.commit()

        cheque = await _net_cheque(session, op, article=article)

        assert cheque.amount == Decimal("5500.00")
        assert cheque.payment_status == "paid"
        allocations = (
            await session.scalars(
                select(InvoicePaymentAllocation).where(
                    InvoicePaymentAllocation.invoice_id == cheque.id
                )
            )
        ).all()
        assert [a.amount for a in allocations] == [Decimal("5500.00")]
        txns = (
            await session.scalars(
                select(CashflowTransaction).where(CashflowTransaction.source_id == cheque.id)
            )
        ).all()
        assert sum(t.amount for t in txns) == Decimal("5500.00")
        assert all(t.wallet_id == wallet.id for t in txns)
        lines = (
            await session.scalars(
                select(InvoiceLineItem)
                .where(InvoiceLineItem.invoice_id == cheque.id)
                .order_by(InvoiceLineItem.sort_order)
            )
        ).all()
        assert [line.is_return for line in lines] == [False, True]


async def test_cheque_return_gross_must_match_operation(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        article = await make_expense_article(session, code="personal", name="Расходы на персонал")
        cp = await make_counterparty(session, name="Местный закуп")
        _, _, op = await _purchase_op(session, amount="6000.00")
        await session.commit()
        with pytest.raises(KassaChequeError, match="не совпадает"):
            await create_cheque(
                session,
                counterparty_id=cp.id,
                article_id=None,
                issued_at=ISSUED,
                bank_parts=[ChequeBankPart(bank_operation_id=op.id)],
                track_nomenclature=True,
                lines=[
                    ChequeLineInput(
                        name="Товар", quantity=Decimal("1"), price=Decimal("5400.00"),
                        dds_article_id=article.id,
                    ),
                    ChequeLineInput(
                        name="Возврат", quantity=Decimal("1"), price=Decimal("500.00"),
                        dds_article_id=article.id, is_return=True,
                    ),
                ],
            )


async def test_cheque_return_guards(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        article = await make_expense_article(session, code="personal", name="Расходы на персонал")
        cp = await make_counterparty(session, name="Местный закуп")
        _, _, op = await _purchase_op(session, amount="6000.00")
        await session.commit()
        returned_line = ChequeLineInput(
            name="Возврат", quantity=Decimal("1"), price=Decimal("500.00"),
            dds_article_id=article.id, is_return=True,
        )
        base = {"counterparty_id": cp.id, "article_id": None, "issued_at": ISSUED,
                "track_nomenclature": True}
        with pytest.raises(KassaChequeError, match="без наличных"):
            await create_cheque(session, **base, bank_parts=[ChequeBankPart(bank_operation_id=op.id)],
                                 cash_amount=Decimal("100.00"), lines=[returned_line])
        with pytest.raises(KassaChequeError, match="одной карт-операции"):
            await create_cheque(session, **base, bank_parts=[], cash_amount=Decimal("500.00"),
                                 lines=[returned_line])
        with pytest.raises(KassaChequeError, match="ручным вводом"):
            await create_cheque(session, **base, bank_parts=[],
                                 pending_card_amount=Decimal("500.00"), lines=[returned_line])
        with pytest.raises(KassaChequeError, match="не меньше суммы операции"):
            await create_cheque(session, **base, bank_parts=[ChequeBankPart(bank_operation_id=op.id)],
                                 lines=[ChequeLineInput(name="Возврат всего", quantity=Decimal("1"),
                                        price=Decimal("6000.00"), dds_article_id=article.id, is_return=True)])


# --- Матчинг возврата к чеку по карте + сумме ---------------------------------------------


async def test_refund_matches_by_card_when_rrn_differs(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """РЕГРЕСС Ч-22: возврат со СВОИМ rrn и БЕЗ authCode привязывается к чеку по карте+сумме."""
    async with async_session_factory() as session:
        article = await make_expense_article(session, code="personal", name="Расходы на персонал")
        account, _, op = await _purchase_op(session, amount="6637.22")
        await session.commit()

        cheque = await _net_cheque(session, op, article=article, ret="1267.07", keep="5370.15")
        refund = await _refund_op(session, account.id, amount="1267.07")
        await session.flush()
        # rrn РАЗНЫЕ, у возврата нет authCode — старый ключ бы не сматчил.
        assert (refund.raw_payload or {}).get("rrn") != (op.raw_payload or {}).get("rrn")
        assert "authCode" not in (refund.raw_payload or {})

        assert await match_card_refund_operations(session) == 1
        await session.flush()
        await session.refresh(refund)
        await session.refresh(op)
        assert refund.cashflow_transaction_id == op.cashflow_transaction_id
        assert refund.classification_status == "classified"
        _ = cheque
        # Идемпотентность.
        assert await match_card_refund_operations(session) == 0


async def test_refund_attached_at_creation_when_already_in_statement(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        article = await make_expense_article(session, code="personal", name="Расходы на персонал")
        account, _, op = await _purchase_op(session, amount="6000.00")
        refund = await _refund_op(session, account.id, amount="500.00")
        await session.commit()

        await _net_cheque(session, op, article=article)
        await session.refresh(refund)
        await session.refresh(op)
        assert refund.cashflow_transaction_id == op.cashflow_transaction_id
        assert refund.classification_status == "classified"


async def test_refund_not_attached_to_other_card(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Возврат по ДРУГОЙ карте не привязывается, даже если сумма совпадает.
    async with async_session_factory() as session:
        article = await make_expense_article(session, code="personal", name="Расходы на персонал")
        account, _, op = await _purchase_op(session, amount="6000.00", card=CARD)
        await session.commit()
        await _net_cheque(session, op, article=article)  # ждёт 500 по карте CARD
        refund = await _refund_op(session, account.id, amount="500.00", card="220070******3867")
        await session.flush()
        assert await match_card_refund_operations(session) == 0
        await session.refresh(refund)
        assert refund.cashflow_transaction_id is None


async def test_ambiguous_refund_escalates_and_apply_links_to_chosen_cheque(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Два чека на одной карте ждут по 500 → возврат 500 неоднозначен; эскалация ambiguous с
    # кандидатами. «Учесть возврат» с выбором чека A ПРИВЯЗЫВАЕТ возврат к A (гасит ожидание A),
    # закрывает missing-кейс A; B не тронут.
    async with async_session_factory() as session:
        article = await make_expense_article(session, code="personal", name="Расходы на персонал")
        account, wallet, op1 = await _purchase_op(session, amount="1000.00")
        _, _, op2 = await _purchase_op(session, amount="1000.00", account=account, wallet=wallet)
        await session.commit()
        cheque_a = await _net_cheque(session, op1, article=article, ret="500.00", keep="500.00")
        await _net_cheque(session, op2, article=article, ret="500.00", keep="500.00")
        refund = await _refund_op(session, account.id, amount="500.00")
        await session.flush()

        assert await match_card_refund_operations(session) == 0
        await session.refresh(refund)
        assert refund.cashflow_transaction_id is None

        future = datetime.now(UTC) + timedelta(days=10)
        await escalate_missing_cheque_refunds(session, now=future)
        await session.flush()
        cases = await _pending_cases(session, CARD_REFUND_CASE_KIND)
        assert len(cases) == 1
        case = cases[0]
        assert case.payload["reason"] == "ambiguous"
        assert len(case.payload["candidates"]) == 2
        # missing-кейсы подняты по обоим чекам.
        assert len(await _pending_cases(session, REFUND_MISSING_CASE_KIND)) == 2

        result = await apply_card_refund_case(session, case, invoice_id=cheque_a.id)
        await session.flush()
        assert result["linked_invoice_id"] == str(cheque_a.id)
        await session.refresh(refund)
        await session.refresh(op1)
        # Привязан к проводке чека A (НЕ отдельная inflow-проводка) → ожидание A погашено.
        assert refund.cashflow_transaction_id == op1.cashflow_transaction_id
        assert refund.classification_status == "classified"
        assert case.status == "resolved"
        # missing-кейс A закрыт; B (реально ещё ждёт) остаётся.
        remaining = await _pending_cases(session, REFUND_MISSING_CASE_KIND)
        assert len(remaining) == 1
        assert remaining[0].payload["invoice_id"] != str(cheque_a.id)


async def test_apply_wrong_invoice_rejected(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # «Учесть возврат» нельзя привязать к чеку, которого нет среди кандидатов кейса.
    async with async_session_factory() as session:
        article = await make_expense_article(session, code="personal", name="Расходы на персонал")
        account, wallet, op1 = await _purchase_op(session, amount="1000.00")
        _, _, op2 = await _purchase_op(session, amount="1000.00", account=account, wallet=wallet)
        await session.commit()
        await _net_cheque(session, op1, article=article, ret="500.00", keep="500.00")
        await _net_cheque(session, op2, article=article, ret="500.00", keep="500.00")
        # чужой чек на другой карте
        acc2, _, op3 = await _purchase_op(session, amount="6000.00", card="220070******3867")
        other = await _net_cheque(session, op3, article=article, ret="500.00", keep="5500.00")
        refund = await _refund_op(session, account.id, amount="500.00")
        await session.flush()
        future = datetime.now(UTC) + timedelta(days=10)
        await escalate_missing_cheque_refunds(session, now=future)
        await session.flush()
        case = (await _pending_cases(session, CARD_REFUND_CASE_KIND))[0]
        with pytest.raises(KassaChequeError, match="не относится"):
            await apply_card_refund_case(session, case, invoice_id=other.id)


async def test_split_refund_attaches_partial(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Банк прислал возврат двумя операциями (300+200) на ожидание 500 у ОДНОГО чека →
    # каждая привязывается как частичная; ожидание гасится в ноль; missing-кейса не остаётся.
    async with async_session_factory() as session:
        article = await make_expense_article(session, code="personal", name="Расходы на персонал")
        account, _, op = await _purchase_op(session, amount="6000.00")
        await session.commit()
        await _net_cheque(session, op, article=article)  # ждёт 500

        r1 = await _refund_op(session, account.id, amount="300.00")
        await session.flush()
        assert await match_card_refund_operations(session) == 1  # 300 вписалось (уникальный чек)
        await session.flush()
        await session.refresh(r1)
        await session.refresh(op)
        assert r1.cashflow_transaction_id == op.cashflow_transaction_id

        r2 = await _refund_op(session, account.id, amount="200.00")
        await session.flush()
        assert await match_card_refund_operations(session) == 1  # 200 добило остаток
        await session.flush()
        await session.refresh(r2)
        assert r2.cashflow_transaction_id == op.cashflow_transaction_id

        # Ожидание погашено (300+200==500) → эскалация не поднимает missing.
        future = datetime.now(UTC) + timedelta(days=10)
        await escalate_missing_cheque_refunds(session, now=future)
        await session.flush()
        assert await _pending_cases(session, REFUND_MISSING_CASE_KIND) == []


async def test_unmatched_refund_escalates_and_apply_books_inflow(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Чек проведён полностью (возврата не ждали) → refundIn ничей → эскалация no_matching_cheque;
    # «Учесть возврат» книжит входящую проводку «Возврат расходов».
    async with async_session_factory() as session:
        article = await make_expense_article(session, code="personal", name="Расходы на персонал")
        account, wallet, op = await _purchase_op(session, amount="6000.00")
        await session.commit()
        await _gross_cheque(session, op, article=article)
        refund = await _refund_op(session, account.id, amount="500.00")
        await session.flush()

        assert await match_card_refund_operations(session) == 0  # не привязан
        future = datetime.now(UTC) + timedelta(days=10)
        assert await escalate_missing_cheque_refunds(session, now=future) >= 1
        cases = await _pending_cases(session, CARD_REFUND_CASE_KIND)
        assert len(cases) == 1
        case = cases[0]
        assert case.bank_operation_id == refund.id
        assert case.payload["reason"] == "no_matching_cheque"

        result = await apply_card_refund_case(session, case)
        await session.flush()
        assert result["already_linked"] is False
        await session.refresh(refund)
        await session.refresh(case)
        assert case.status == "resolved"
        assert refund.classification_status == "classified"
        txn = await session.get(CashflowTransaction, refund.cashflow_transaction_id)
        assert txn.direction == "in"
        assert txn.amount == Decimal("500.00")
        assert txn.wallet_id == wallet.id
        assert txn.source_kind == "kassa_cheque_refund"
        refund_article = await session.get(DdsArticle, txn.article_id)
        assert refund_article.code == EXPENSE_REFUND_ARTICLE_CODE
        assert refund_article.movement_type == "inflow"


async def test_escalate_missing_then_resolved_on_arrival(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Чек ждёт возврат дольше порога → кейс missing; приход возврата закрывает его.
    async with async_session_factory() as session:
        article = await make_expense_article(session, code="personal", name="Расходы на персонал")
        account, _, op = await _purchase_op(session, amount="6000.00")
        await session.commit()
        cheque = await _net_cheque(session, op, article=article)  # ждёт 500

        assert await escalate_missing_cheque_refunds(session) == 0  # свежий чек — до порога
        future = datetime.now(UTC) + timedelta(days=10)
        assert await escalate_missing_cheque_refunds(session, now=future) == 1
        cases = await _pending_cases(session, REFUND_MISSING_CASE_KIND)
        assert len(cases) == 1 and cases[0].payload["invoice_id"] == str(cheque.id)
        assert await escalate_missing_cheque_refunds(session, now=future) == 0  # дедуп

        await _refund_op(session, account.id, amount="500.00")
        await session.flush()
        assert await match_card_refund_operations(session) == 1
        await session.flush()
        assert await _pending_cases(session, REFUND_MISSING_CASE_KIND) == []


async def test_refund_does_not_attach_to_manual_classification(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Покупка классифицирована НЕ чеком (ручная разметка) — возврат не приманивается.
    async with async_session_factory() as session:
        article = await make_expense_article(session, code="personal", name="Расходы на персонал")
        account, wallet, op = await _purchase_op(session, amount="6000.00")
        txn = CashflowTransaction(
            wallet_id=wallet.id, direction="out", amount=Decimal("6000.00"),
            operation_date=OP_DATE, article_id=article.id, source_kind="bank_import",
            quality_status="final",
        )
        session.add(txn)
        await session.flush()
        op.cashflow_transaction_id = txn.id
        op.classification_status = "classified"
        refund = await _refund_op(session, account.id, amount="500.00")
        await session.commit()
        assert await match_card_refund_operations(session) == 0
        await session.refresh(refund)
        assert refund.cashflow_transaction_id is None


async def test_card_transactions_carry_refund_hint(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Пикер: у покупки на карте с непривязанным возвратом — card-scoped refund_amount.
    async with async_session_factory() as session:
        account, _, op = await _purchase_op(session, amount="6000.00")
        await _refund_op(session, account.id, amount="500.00")
        await session.commit()
        candidates = await list_card_transactions(session, date_from=OP_DATE, date_to=OP_DATE)
        by_id = {c.bank_operation_id: c for c in candidates}
        assert by_id[op.id].refund_amount == Decimal("500.00")
        assert by_id[op.id].refund_count == 1


async def test_goods_split_excludes_returned_lines(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        supplier = await make_expense_article(session, code="sup", name="Оплата поставщикам")
        cp = await make_counterparty(session, name="Местный закуп")
        _, _, op = await _purchase_op(session, amount="6000.00")
        await session.commit()
        cheque = await create_cheque(
            session, counterparty_id=cp.id, article_id=None, issued_at=ISSUED,
            bank_parts=[ChequeBankPart(bank_operation_id=op.id)], track_nomenclature=True,
            lines=[
                ChequeLineInput(name="Товар", quantity=Decimal("1"), price=Decimal("5500.00"),
                                dds_article_id=supplier.id),
                ChequeLineInput(name="Возвращённый товар", quantity=Decimal("1"),
                                price=Decimal("500.00"), dds_article_id=supplier.id, is_return=True),
            ],
        )
        lines = (
            await session.scalars(
                select(InvoiceLineItem).where(InvoiceLineItem.invoice_id == cheque.id)
            )
        ).all()
        for line in lines:
            line.product_guid = "guid-товара"
        await session.commit()
        split = await compute_kassa_goods_split(session, cheque.id)
        assert split is not None
        card_share, cash_share = split
        assert card_share == Decimal("5500.00")
        assert cash_share == Decimal("0.00")
