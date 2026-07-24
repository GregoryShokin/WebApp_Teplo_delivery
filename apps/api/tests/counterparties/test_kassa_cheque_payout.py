"""Проводка прочих расходов чека в iiko изъятием (addPayOut): резолв типа по структуре,
разнос по источникам (карта/наличные), идемпотентность, обработка ошибок. iiko HTTP
замокан (monkeypatch имён в модуле cheque_payout_push). Прогон на ``teplo_test``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from cp_helpers import (
    make_account,
    make_bank_operation,
    make_counterparty,
    make_expense_article,
    make_invoice,
    make_wallet,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.services.kassa.cheque_payout_push as payout
from app.models import ChequeIikoPayout, InvoiceLineItem, InvoicePaymentAllocation, Wallet
from app.services.iiko_location import get_department_id
from app.services.kassa.cheque import ChequeBankPart, ChequeLineInput, create_cheque
from app.services.kassa.cheque_payout_push import (
    _build_payout_type_index,
    _resolve_pay_out_type,
    _split_by_source,
    post_kassa_payment_to_iiko,
)

ISSUED = datetime(2026, 6, 17, 12, 0, tzinfo=UTC)
OP_DATE = date(2026, 6, 17)

# Синтетический план счетов iiko (id → имя) и 6 типов изъятий (3 статьи × 2 источника),
# как на боевом: «Затраты на персонал» несёт две статьи (cfc 34 / 3.1).
ACCOUNTS = {
    "id_kassa": "Главная касса",
    "id_ekv": "Денежные средства, эквайринг",
    "id_pers": "Затраты на персонал",
    "id_tt": "Содержание торговых точек",
    "id_sup": "Задолженность перед поставщиками",
}
PAYOUT_TYPES = [
    {
        "id": "T_SUP_CASH",
        "transactionType": "PAYOUT",
        "chiefAccount": "id_kassa",
        "account": "id_sup",
        "cashFlowCategory": {"code": "3"},
    },
    {
        "id": "T_SUP_CARD",
        "transactionType": "PAYOUT",
        "chiefAccount": "id_ekv",
        "account": "id_sup",
        "cashFlowCategory": {"code": "3"},
    },
    {
        "id": "T_FOOD_CASH",
        "transactionType": "PAYOUT",
        "chiefAccount": "id_kassa",
        "account": "id_pers",
        "cashFlowCategory": {"code": "34"},
    },
    {
        "id": "T_FOOD_CARD",
        "transactionType": "PAYOUT",
        "chiefAccount": "id_ekv",
        "account": "id_pers",
        "cashFlowCategory": {"code": "34"},
    },
    {
        "id": "T_PERS_CASH",
        "transactionType": "PAYOUT",
        "chiefAccount": "id_kassa",
        "account": "id_pers",
        "cashFlowCategory": {"code": "3.1"},
    },
    {
        "id": "T_PERS_CARD",
        "transactionType": "PAYOUT",
        "chiefAccount": "id_ekv",
        "account": "id_pers",
        "cashFlowCategory": {"code": "3.1"},
    },
    {
        "id": "T_TT_CASH",
        "transactionType": "PAYOUT",
        "chiefAccount": "id_kassa",
        "account": "id_tt",
        "cashFlowCategory": {"code": "36"},
    },
    {
        "id": "T_TT_CARD",
        "transactionType": "PAYOUT",
        "chiefAccount": "id_ekv",
        "account": "id_tt",
        "cashFlowCategory": {"code": "36"},
    },
    # PAYIN-тип на тот же счёт — должен игнорироваться индексом.
    {
        "id": "T_IGNORE",
        "transactionType": "PAYIN",
        "chiefAccount": "id_kassa",
        "account": "id_pers",
        "cashFlowCategory": {"code": "34"},
    },
]

FOOD = "Расходы на питание персонала"
TT = "Содержание торговых точек"


def _mock_iiko(monkeypatch, calls: list, *, fail: bool = False) -> None:
    monkeypatch.setattr(payout, "_auth_token", lambda: "TOKEN")
    monkeypatch.setattr(payout, "_fetch_accounts_map", lambda token: dict(ACCOUNTS))
    monkeypatch.setattr(
        payout,
        "_iiko_get",
        lambda token, path, params: list(PAYOUT_TYPES) if "payInOutTypes" in path else [],
    )

    def fake_post(token, path, body):
        calls.append(body)
        if fail:
            raise RuntimeError("iiko 500")
        return {"result": "SUCCESS"}

    monkeypatch.setattr(payout, "_iiko_post", fake_post)


async def _card_op(session: AsyncSession, *, amount: str):
    account = await make_account(session)
    await make_wallet(session, name="Тинькофф карта", wallet_type="bank", account_id=account.id)
    op = await make_bank_operation(
        session,
        amount=amount,
        operation_date=OP_DATE,
        posted_at=ISSUED,
        category="cardOperation",
        account_id=account.id,
    )
    return op


async def _payouts(session: AsyncSession, invoice_id) -> list[ChequeIikoPayout]:
    return list(
        (
            await session.scalars(
                select(ChequeIikoPayout).where(ChequeIikoPayout.invoice_id == invoice_id)
            )
        ).all()
    )


# --- чистые функции резолва/разнесения --------------------------------------------


def test_resolve_pay_out_type_by_structure() -> None:
    index = _build_payout_type_index(ACCOUNTS, PAYOUT_TYPES)
    assert _resolve_pay_out_type(index, FOOD, "cash") == "T_FOOD_CASH"
    assert _resolve_pay_out_type(index, FOOD, "card") == "T_FOOD_CARD"
    assert _resolve_pay_out_type(index, "Расходы на персонал", "card") == "T_PERS_CARD"
    assert _resolve_pay_out_type(index, TT, "cash") == "T_TT_CASH"
    # PAYIN-тип в индекс не попал.
    assert "T_IGNORE" not in index.values()
    # Товарная статья «Оплата поставщикам» резолвится в тип на корсчёт поставщиков.
    assert _resolve_pay_out_type(index, "Оплата поставщикам", "cash") == "T_SUP_CASH"
    assert _resolve_pay_out_type(index, "Оплата поставщикам", "card") == "T_SUP_CARD"
    # Неизвестная статья → None.
    assert _resolve_pay_out_type(index, "Неведомая статья", "cash") is None


def test_split_by_source() -> None:
    assert dict(_split_by_source(Decimal("100"), Decimal("60"), Decimal("100"))) == {
        "card": Decimal("60.00"),
        "cash": Decimal("40.00"),
    }
    # Чисто наличный — всё в cash.
    assert dict(_split_by_source(Decimal("100"), Decimal("0"), Decimal("100"))) == {
        "card": Decimal("0.00"),
        "cash": Decimal("100.00"),
    }


# --- интеграция (БД + замоканный iiko) ---------------------------------------------


async def test_posts_card_only_expense(
    async_session_factory: async_sessionmaker[AsyncSession], monkeypatch
) -> None:
    async with async_session_factory() as session:
        food = await make_expense_article(session, code="food", name=FOOD)
        cp = await make_counterparty(session, name="Местный закуп")
        op = await _card_op(session, amount="300.00")
        await session.commit()
        cheque = await create_cheque(
            session,
            counterparty_id=cp.id,
            issued_at=ISSUED,
            bank_parts=[ChequeBankPart(bank_operation_id=op.id)],
            track_nomenclature=True,
            lines=[
                ChequeLineInput(
                    name="Обеды",
                    quantity=Decimal("1"),
                    price=Decimal("300.00"),
                    dds_article_id=food.id,
                )
            ],
        )

        calls: list = []
        _mock_iiko(monkeypatch, calls)
        report = await post_kassa_payment_to_iiko(session, cheque.id)
        await session.commit()

        assert report.posted == 1 and report.failed == 0
        assert len(calls) == 1
        assert calls[0]["payOutTypeId"] == "T_FOOD_CARD"
        assert calls[0]["departmentSumMap"][get_department_id()] == 300.0
        rows = await _payouts(session, cheque.id)
        assert len(rows) == 1
        assert rows[0].status == "posted" and rows[0].source == "card"
        assert rows[0].amount == Decimal("300.00")


async def test_splits_expense_between_card_and_cash(
    async_session_factory: async_sessionmaker[AsyncSession], monkeypatch
) -> None:
    async with async_session_factory() as session:
        food = await make_expense_article(session, code="food2", name=FOOD)
        cp = await make_counterparty(session, name="Местный закуп")
        op = await _card_op(session, amount="300.00")
        assert await session.scalar(select(Wallet).where(Wallet.code == "tk_chernikova"))
        await session.commit()
        cheque = await create_cheque(
            session,
            counterparty_id=cp.id,
            issued_at=ISSUED,
            bank_parts=[ChequeBankPart(bank_operation_id=op.id)],
            cash_amount=Decimal("200.00"),
            track_nomenclature=True,
            lines=[
                ChequeLineInput(
                    name="Обеды",
                    quantity=Decimal("1"),
                    price=Decimal("500.00"),
                    dds_article_id=food.id,
                )
            ],
        )

        calls: list = []
        _mock_iiko(monkeypatch, calls)
        report = await post_kassa_payment_to_iiko(session, cheque.id)
        await session.commit()

        assert report.posted == 2
        by_type = {
            c["payOutTypeId"]: c["departmentSumMap"][get_department_id()] for c in calls
        }
        assert by_type == {"T_FOOD_CARD": 300.0, "T_FOOD_CASH": 200.0}
        rows = {(r.source, r.status) for r in await _payouts(session, cheque.id)}
        assert rows == {("card", "posted"), ("cash", "posted")}


async def test_supplier_line_posted_with_counteragent(
    async_session_factory: async_sessionmaker[AsyncSession], monkeypatch
) -> None:
    # Товарная статья «Оплата поставщикам» теперь проводится изъятием (эквайринг) с
    # контрагентом-поставщиком — корсчёт «Задолженность перед поставщиками».
    async with async_session_factory() as session:
        supplier = await make_expense_article(session, code="sup", name="Оплата поставщикам")
        cp = await make_counterparty(session, name="Местный закуп", iiko_guid="SUP-GUID")
        op = await _card_op(session, amount="250.00")
        await session.commit()
        cheque = await create_cheque(
            session,
            counterparty_id=cp.id,
            issued_at=ISSUED,
            bank_parts=[ChequeBankPart(bank_operation_id=op.id)],
            track_nomenclature=True,
            lines=[
                ChequeLineInput(
                    name="Помидоры",
                    quantity=Decimal("1"),
                    price=Decimal("250.00"),
                    dds_article_id=supplier.id,
                )
            ],
        )

        calls: list = []
        _mock_iiko(monkeypatch, calls)
        report = await post_kassa_payment_to_iiko(session, cheque.id)
        await session.commit()

        assert report.posted == 1 and report.failed == 0
        assert len(calls) == 1
        assert calls[0]["payOutTypeId"] == "T_SUP_CARD"
        assert calls[0]["counteragent"] == "SUP-GUID"
        rows = await _payouts(session, cheque.id)
        assert len(rows) == 1 and rows[0].status == "posted" and rows[0].source == "card"


async def test_idempotent_no_double_post(
    async_session_factory: async_sessionmaker[AsyncSession], monkeypatch
) -> None:
    async with async_session_factory() as session:
        food = await make_expense_article(session, code="food3", name=FOOD)
        cp = await make_counterparty(session, name="Местный закуп")
        op = await _card_op(session, amount="300.00")
        await session.commit()
        cheque = await create_cheque(
            session,
            counterparty_id=cp.id,
            issued_at=ISSUED,
            bank_parts=[ChequeBankPart(bank_operation_id=op.id)],
            track_nomenclature=True,
            lines=[
                ChequeLineInput(
                    name="Обеды",
                    quantity=Decimal("1"),
                    price=Decimal("300.00"),
                    dds_article_id=food.id,
                )
            ],
        )

        calls: list = []
        _mock_iiko(monkeypatch, calls)
        await post_kassa_payment_to_iiko(session, cheque.id)
        await session.commit()
        # Повторный прогон не должен задвоить уже проведённое изъятие.
        report2 = await post_kassa_payment_to_iiko(session, cheque.id)
        await session.commit()

        assert report2.posted == 0 and report2.skipped == 1
        assert len(calls) == 1  # addPayOut вызван ровно один раз за оба прогона
        assert len(await _payouts(session, cheque.id)) == 1


async def test_iiko_error_marks_failed_without_raising(
    async_session_factory: async_sessionmaker[AsyncSession], monkeypatch
) -> None:
    async with async_session_factory() as session:
        food = await make_expense_article(session, code="food4", name=FOOD)
        cp = await make_counterparty(session, name="Местный закуп")
        op = await _card_op(session, amount="300.00")
        await session.commit()
        cheque = await create_cheque(
            session,
            counterparty_id=cp.id,
            issued_at=ISSUED,
            bank_parts=[ChequeBankPart(bank_operation_id=op.id)],
            track_nomenclature=True,
            lines=[
                ChequeLineInput(
                    name="Обеды",
                    quantity=Decimal("1"),
                    price=Decimal("300.00"),
                    dds_article_id=food.id,
                )
            ],
        )

        calls: list = []
        _mock_iiko(monkeypatch, calls, fail=True)
        report = await post_kassa_payment_to_iiko(session, cheque.id)
        await session.commit()

        assert report.failed == 1 and report.posted == 0
        rows = await _payouts(session, cheque.id)
        assert len(rows) == 1
        assert rows[0].status == "failed"
        assert rows[0].error is not None


async def test_invoice_goods_and_staff_split_by_article(
    async_session_factory: async_sessionmaker[AsyncSession], monkeypatch
) -> None:
    # Накладная Кассы (kassa_invoice), оплата картой: товарная строка (без явной статьи) →
    # «Оплата поставщикам» с контрагентом, персональная → своя статья; всё по эквайрингу.
    async with async_session_factory() as session:
        food = await make_expense_article(session, code="finv", name=FOOD)
        await make_expense_article(session, code="supinv", name="Оплата поставщикам")
        cp = await make_counterparty(session, name="Местный закуп", iiko_guid="SUP-G")
        op = await _card_op(session, amount="300.00")
        inv = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="300.00",
            staff_amount="100.00",
            number="ИНВ-1",
            source="kassa_invoice",
            issued_at=ISSUED,
        )
        session.add(
            InvoiceLineItem(
                invoice_id=inv.id,
                name="Огурцы",
                quantity=Decimal("1"),
                price=Decimal("200.00"),
                sum=Decimal("200.00"),
                is_staff=False,
                product_guid="P-1",
            )
        )
        session.add(
            InvoiceLineItem(
                invoice_id=inv.id,
                name="молоко",
                quantity=Decimal("1"),
                price=Decimal("100.00"),
                sum=Decimal("100.00"),
                is_staff=True,
                dds_article_id=food.id,
            )
        )
        session.add(
            InvoicePaymentAllocation(
                invoice_id=inv.id,
                source_kind="bank",
                bank_operation_id=op.id,
                amount=Decimal("300.00"),
            )
        )
        await session.commit()

        calls: list = []
        _mock_iiko(monkeypatch, calls)
        report = await post_kassa_payment_to_iiko(session, inv.id)
        await session.commit()

        assert report.posted == 2 and report.failed == 0
        by_type = {
            c["payOutTypeId"]: c["departmentSumMap"][get_department_id()] for c in calls
        }
        # товар → оплата поставщикам (эквайринг), персонал → своя статья (эквайринг)
        assert by_type == {"T_SUP_CARD": 200.0, "T_FOOD_CARD": 100.0}
        sup_call = next(c for c in calls if c["payOutTypeId"] == "T_SUP_CARD")
        assert sup_call["counteragent"] == "SUP-G"
