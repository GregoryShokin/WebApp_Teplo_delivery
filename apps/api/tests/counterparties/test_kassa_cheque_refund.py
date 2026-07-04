"""Возвраты карт-покупок в чеках Кассы.

Чек вводится gross (все позиции как на бумаге, сверка с операцией копейка в копейку),
возвращённые позиции помечаются ``is_return`` и не проводятся: ДДС/аллокации/iiko = net.
Разница ``|операция| − аллокация`` = ожидаемый возврат; пришедший ``refundIn`` (тот же
``rrn``/``authCode`` — боевой контракт T-Банка) привязывается уборщиком молча.
Прогоняется на ``teplo_test``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
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

ISSUED = datetime(2026, 5, 21, 12, 0, tzinfo=UTC)
OP_DATE = date(2026, 5, 21)
REFUND_DATE = date(2026, 5, 24)
# Боевой контракт: возврат несёт rrn/authCode исходной покупки (см. прод-кейс Магнита).
RRN = "614137578458"
AUTH = "962021"


async def _purchase_op(
    session: AsyncSession,
    *,
    amount: str,
    rrn: str = RRN,
    auth: str = AUTH,
):
    """Счёт + карт-кошелёк + карт-покупка с rrn/authCode (как в выписке T-Банка)."""
    account = await make_account(session)
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
        raw_payload={"rrn": rrn, "authCode": auth},
    )
    return account, wallet, op


async def _refund_op(
    session: AsyncSession,
    account_id,
    *,
    amount: str,
    rrn: str = RRN,
    auth: str = AUTH,
) -> BankOperation:
    """Возврат покупки: входящая операция refundIn с теми же rrn/authCode."""
    return await make_bank_operation(
        session,
        amount=amount,
        direction="in",
        operation_date=REFUND_DATE,
        category="refundIn",
        account_id=account_id,
        raw_payload={"rrn": rrn, "authCode": auth},
        classification_status="pending",
    )


async def _net_cheque(session: AsyncSession, op, *, article) -> object:
    """Чек 6000 gross: позиция 5500 + возвращённая позиция 500 → проводится net 5500."""
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
                name="Товар",
                quantity=Decimal("1"),
                price=Decimal("5500.00"),
                dds_article_id=article.id,
            ),
            ChequeLineInput(
                name="Возвращённый товар",
                quantity=Decimal("1"),
                price=Decimal("500.00"),
                dds_article_id=article.id,
                is_return=True,
            ),
        ],
    )


async def test_cheque_with_returned_line_books_net(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        article = await make_expense_article(session, code="personal", name="Расходы на персонал")
        _, wallet, op = await _purchase_op(session, amount="6000.00")
        await session.commit()

        cheque = await _net_cheque(session, op, article=article)

        # Проведено net, статус paid (аллокации == amount).
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

        # Обе строки сохранены (gross-след), возвратная помечена.
        lines = (
            await session.scalars(
                select(InvoiceLineItem)
                .where(InvoiceLineItem.invoice_id == cheque.id)
                .order_by(InvoiceLineItem.sort_order)
            )
        ).all()
        assert [line.sum for line in lines] == [Decimal("5500.00"), Decimal("500.00")]
        assert [line.is_return for line in lines] == [False, True]

        # Операция классифицирована чеком; недоиспользованные 500 = ожидаемый возврат.
        await session.refresh(op)
        assert op.cashflow_transaction_id is not None
        assert op.classification_status == "classified"


async def test_cheque_return_gross_must_match_operation(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Сверка «как на бумаге»: позиции с возвратами должны сойтись с операцией копейка в копейку.
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
                        name="Товар",
                        quantity=Decimal("1"),
                        price=Decimal("5400.00"),
                        dds_article_id=article.id,
                    ),
                    ChequeLineInput(
                        name="Возврат",
                        quantity=Decimal("1"),
                        price=Decimal("500.00"),
                        dds_article_id=article.id,
                        is_return=True,
                    ),
                ],
            )


async def test_cheque_return_guards(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # MVP-контур: возврат только при одной карт-операции, без наличных/пендинга/ручной суммы.
    async with async_session_factory() as session:
        article = await make_expense_article(session, code="personal", name="Расходы на персонал")
        cp = await make_counterparty(session, name="Местный закуп")
        _, _, op = await _purchase_op(session, amount="6000.00")
        await session.commit()

        returned_line = ChequeLineInput(
            name="Возврат",
            quantity=Decimal("1"),
            price=Decimal("500.00"),
            dds_article_id=article.id,
            is_return=True,
        )
        base = {
            "counterparty_id": cp.id,
            "article_id": None,
            "issued_at": ISSUED,
            "track_nomenclature": True,
        }

        with pytest.raises(KassaChequeError, match="без наличных"):
            await create_cheque(
                session,
                **base,
                bank_parts=[ChequeBankPart(bank_operation_id=op.id)],
                cash_amount=Decimal("100.00"),
                lines=[returned_line],
            )
        # Наличный чек с помеченным возвратом: возврат требует карт-операцию.
        with pytest.raises(KassaChequeError, match="одной карт-операции"):
            await create_cheque(
                session,
                **base,
                bank_parts=[],
                cash_amount=Decimal("500.00"),
                lines=[returned_line],
            )
        with pytest.raises(KassaChequeError, match="ручным вводом"):
            await create_cheque(
                session,
                **base,
                bank_parts=[],
                pending_card_amount=Decimal("500.00"),
                lines=[returned_line],
            )
        with pytest.raises(KassaChequeError, match="автоматически"):
            await create_cheque(
                session,
                **base,
                bank_parts=[ChequeBankPart(bank_operation_id=op.id, amount=Decimal("100.00"))],
                lines=[returned_line],
            )
        # Возвраты не меньше всей операции — нечего проводить.
        with pytest.raises(KassaChequeError, match="не меньше суммы операции"):
            await create_cheque(
                session,
                **base,
                bank_parts=[ChequeBankPart(bank_operation_id=op.id)],
                lines=[
                    ChequeLineInput(
                        name="Возврат всего",
                        quantity=Decimal("1"),
                        price=Decimal("6000.00"),
                        dds_article_id=article.id,
                        is_return=True,
                    )
                ],
            )


async def test_refund_attached_at_creation_when_already_in_statement(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Поздний ввод чека: возврат уже в выписке → привязка сразу при создании.
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


async def test_match_card_refund_operations_late_arrival(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Главный кейс: чек введён net, возврат приезжает через дни → уборщик привязывает молча.
    async with async_session_factory() as session:
        article = await make_expense_article(session, code="personal", name="Расходы на персонал")
        account, _, op = await _purchase_op(session, amount="6000.00")
        await session.commit()

        await _net_cheque(session, op, article=article)
        refund = await _refund_op(session, account.id, amount="500.00")
        await session.flush()

        assert await match_card_refund_operations(session) == 1
        # Как в проде: ingest_operations делает flush сразу после матчеров.
        await session.flush()
        await session.refresh(refund)
        await session.refresh(op)
        assert refund.cashflow_transaction_id == op.cashflow_transaction_id
        assert refund.classification_status == "classified"

        # Идемпотентность: второй прогон ничего не находит.
        assert await match_card_refund_operations(session) == 0


async def test_match_skips_unexpected_refunds(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        article = await make_expense_article(session, code="personal", name="Расходы на персонал")
        account, _, op = await _purchase_op(session, amount="6000.00")
        await session.commit()

        await _net_cheque(session, op, article=article)

        # Возврат больше ожидания (ждали 500) — не привязывается, остаётся в разборе.
        oversized = await _refund_op(session, account.id, amount="600.00")
        # Возврат по чужой покупке (другой rrn, покупка без чека) — не привязывается.
        orphan = await _refund_op(session, account.id, amount="500.00", rrn="999", auth="000")
        await session.flush()

        assert await match_card_refund_operations(session) == 0
        await session.refresh(oversized)
        await session.refresh(orphan)
        assert oversized.cashflow_transaction_id is None
        assert orphan.cashflow_transaction_id is None


async def test_refund_does_not_attach_to_manual_classification(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Покупка классифицирована НЕ чеком (ручная разметка, без аллокаций) — возврат не приманивается.
    async with async_session_factory() as session:
        article = await make_expense_article(session, code="personal", name="Расходы на персонал")
        account, wallet, op = await _purchase_op(session, amount="6000.00")
        txn = CashflowTransaction(
            wallet_id=wallet.id,
            direction="out",
            amount=Decimal("6000.00"),
            operation_date=OP_DATE,
            article_id=article.id,
            source_kind="bank_import",
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
    # Пикер: у покупки с пришедшим возвратом — refund_amount для подсветки в UI.
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
    # Зеркало add_payment: товарная сумма — без возвращённых позиций.
    async with async_session_factory() as session:
        supplier = await make_expense_article(session, code="sup", name="Оплата поставщикам")
        cp = await make_counterparty(session, name="Местный закуп")
        _, _, op = await _purchase_op(session, amount="6000.00")
        await session.commit()

        cheque = await create_cheque(
            session,
            counterparty_id=cp.id,
            article_id=None,
            issued_at=ISSUED,
            bank_parts=[ChequeBankPart(bank_operation_id=op.id)],
            track_nomenclature=True,
            lines=[
                ChequeLineInput(
                    name="Товар",
                    quantity=Decimal("1"),
                    price=Decimal("5500.00"),
                    dds_article_id=supplier.id,
                ),
                ChequeLineInput(
                    name="Возвращённый товар",
                    quantity=Decimal("1"),
                    price=Decimal("500.00"),
                    dds_article_id=supplier.id,
                    is_return=True,
                ),
            ],
        )
        # product_guid у строк чека — снапшот из номенклатуры; для сплита проставим вручную
        # (goods-фильтр требует product_guid NOT NULL, как у prepare_push).
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


# --- Фаза 2: поздний возврат (чек не ждал) и эскалация «возврат не пришёл» --------------


async def _gross_cheque(session: AsyncSession, op, *, article) -> object:
    """Чек 6000 без возвратных пометок — проведён полностью (возврата не ждёт)."""
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
                name="Товар",
                quantity=Decimal("1"),
                price=Decimal("6000.00"),
                dds_article_id=article.id,
            )
        ],
    )


async def _pending_cases(session: AsyncSession, kind: str) -> list[ReconciliationCase]:
    return list(
        (
            await session.scalars(
                select(ReconciliationCase).where(
                    ReconciliationCase.kind == kind,
                    ReconciliationCase.status == "pending",
                )
            )
        ).all()
    )


async def test_late_refund_creates_case(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Чек проведён полностью (возврата не ждали) → пришедший refundIn не привязывается,
    # а поднимает кейс «возврат по проведённому чеку». Дедуп по pending-кейсу.
    async with async_session_factory() as session:
        article = await make_expense_article(session, code="personal", name="Расходы на персонал")
        account, _, op = await _purchase_op(session, amount="6000.00")
        await session.commit()

        cheque = await _gross_cheque(session, op, article=article)
        refund = await _refund_op(session, account.id, amount="500.00")
        await session.flush()

        assert await match_card_refund_operations(session) == 0
        await session.flush()
        cases = await _pending_cases(session, CARD_REFUND_CASE_KIND)
        assert len(cases) == 1
        case = cases[0]
        assert case.bank_operation_id == refund.id
        assert case.payload["invoice_id"] == str(cheque.id)
        assert case.payload["reason"] == "cheque_did_not_expect"

        # Повторный прогон не плодит дублей.
        assert await match_card_refund_operations(session) == 0
        await session.flush()
        assert len(await _pending_cases(session, CARD_REFUND_CASE_KIND)) == 1


async def test_oversized_refund_creates_case(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Возврат больше остатка ожидания: не привязывается, кейс с reason=exceeds_expected.
    async with async_session_factory() as session:
        article = await make_expense_article(session, code="personal", name="Расходы на персонал")
        account, _, op = await _purchase_op(session, amount="6000.00")
        await session.commit()

        await _net_cheque(session, op, article=article)  # ждём 500
        refund = await _refund_op(session, account.id, amount="600.00")
        await session.flush()

        assert await match_card_refund_operations(session) == 0
        await session.flush()
        cases = await _pending_cases(session, CARD_REFUND_CASE_KIND)
        assert len(cases) == 1
        assert cases[0].bank_operation_id == refund.id
        assert cases[0].payload["reason"] == "exceeds_expected"


async def test_apply_card_refund_case_books_inflow(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # «Учесть возврат»: входящая проводка «Возврат расходов», операция classified, кейс resolved.
    async with async_session_factory() as session:
        article = await make_expense_article(session, code="personal", name="Расходы на персонал")
        account, wallet, op = await _purchase_op(session, amount="6000.00")
        await session.commit()

        cheque = await _gross_cheque(session, op, article=article)
        refund = await _refund_op(session, account.id, amount="500.00")
        await session.flush()
        await match_card_refund_operations(session)
        await session.flush()
        case = (await _pending_cases(session, CARD_REFUND_CASE_KIND))[0]

        result = await apply_card_refund_case(session, case)
        await session.flush()
        assert result["already_linked"] is False

        await session.refresh(refund)
        await session.refresh(case)
        assert case.status == "resolved"
        assert refund.classification_status == "classified"
        txn = await session.get(CashflowTransaction, refund.cashflow_transaction_id)
        assert txn is not None
        assert txn.direction == "in"
        assert txn.amount == Decimal("500.00")
        assert txn.wallet_id == wallet.id
        assert txn.source_kind == "kassa_cheque_refund"
        assert txn.source_id == cheque.id
        refund_article = await session.get(DdsArticle, txn.article_id)
        assert refund_article.code == EXPENSE_REFUND_ARTICLE_CODE
        assert refund_article.movement_type == "inflow"


async def test_escalate_missing_cheque_refunds(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Чек ждёт возврат дольше порога → кейс; привязка возврата закрывает кейс.
    from datetime import timedelta

    async with async_session_factory() as session:
        article = await make_expense_article(session, code="personal", name="Расходы на персонал")
        account, _, op = await _purchase_op(session, amount="6000.00")
        await session.commit()

        cheque = await _net_cheque(session, op, article=article)  # ждём 500

        # Свежий чек — до порога кейса нет.
        assert await escalate_missing_cheque_refunds(session) == 0

        # Смотрим «из будущего», за порогом (7 дней по умолчанию).
        future = datetime.now(UTC) + timedelta(days=8)
        assert await escalate_missing_cheque_refunds(session, now=future) == 1
        cases = await _pending_cases(session, REFUND_MISSING_CASE_KIND)
        assert len(cases) == 1
        assert cases[0].bank_operation_id == op.id
        assert cases[0].payload["invoice_id"] == str(cheque.id)
        # Дедуп.
        assert await escalate_missing_cheque_refunds(session, now=future) == 0

        # Возврат пришёл и привязался → кейс закрыт автоматически.
        await _refund_op(session, account.id, amount="500.00")
        await session.flush()
        assert await match_card_refund_operations(session) == 1
        await session.flush()
        assert await _pending_cases(session, REFUND_MISSING_CASE_KIND) == []
