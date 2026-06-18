"""Чеки модуля «Касса»: создание (карта / сплит карта+нал / номенклатура), подбор
card-операций и анти-дубль. Прогоняется на ``teplo_test``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from cp_helpers import (
    headers_for,
    make_account,
    make_bank_operation,
    make_counterparty,
    make_expense_article,
    make_wallet,
)
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    AppSetting,
    BankOperation,
    CashflowTransaction,
    IikoProduct,
    InvoiceLineItem,
    InvoicePaymentAllocation,
    Wallet,
)
from app.services.kassa.cheque import (
    ChequeBankPart,
    ChequeLineInput,
    KassaChequeError,
    create_cheque,
    list_card_transactions,
)
from app.services.warehouse_invoice_push import prepare_push

ISSUED = datetime(2026, 6, 17, 12, 0, tzinfo=UTC)
OP_DATE = date(2026, 6, 17)


async def _card_op(session: AsyncSession, *, amount: str, posted_at: datetime | None = ISSUED):
    """Account + card wallet hanging off it + a card-purchase bank op on that account."""
    account = await make_account(session)
    wallet = await make_wallet(
        session, name="Тинькофф карта", wallet_type="bank", account_id=account.id
    )
    op = await make_bank_operation(
        session,
        amount=amount,
        operation_date=OP_DATE,
        posted_at=posted_at,
        category="cardOperation",
        account_id=account.id,
    )
    return wallet, op


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


async def _txns(session: AsyncSession, invoice_id) -> list[CashflowTransaction]:
    return list(
        (
            await session.scalars(
                select(CashflowTransaction).where(CashflowTransaction.source_id == invoice_id)
            )
        ).all()
    )


async def test_create_cheque_card_only_is_paid_and_booked(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        article = await make_expense_article(
            session, code="courier_payout", name="Курьерская служба -"
        )
        cp = await make_counterparty(session, name="Магазин")
        wallet, op = await _card_op(session, amount="1500.00")
        await session.commit()

        cheque = await create_cheque(
            session,
            counterparty_id=cp.id,
            article_id=article.id,
            issued_at=ISSUED,
            bank_parts=[ChequeBankPart(bank_operation_id=op.id)],
        )

        assert cheque.source == "kassa_cheque"
        assert cheque.payment_status == "paid"
        assert cheque.amount == Decimal("1500.00")
        assert (cheque.number or "").startswith("Ч-")

        allocs = await _allocs(session, cheque.id)
        assert len(allocs) == 1
        assert allocs[0].source_kind == "bank"
        assert allocs[0].bank_operation_id == op.id

        txns = await _txns(session, cheque.id)
        assert len(txns) == 1
        assert txns[0].source_kind == "kassa_cheque"
        assert txns[0].wallet_id == wallet.id
        assert txns[0].direction == "out"
        assert txns[0].article_id == article.id

        # The card op is now linked to the DDS movement (classified — no double expense).
        op_after = await session.get(BankOperation, op.id)
        assert op_after.cashflow_transaction_id == txns[0].id


async def test_create_cheque_split_card_plus_cash(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        article = await make_expense_article(session, code="cleaning", name="Расходники")
        cp = await make_counterparty(session, name="Магазин")
        card_wallet, op = await _card_op(session, amount="600.00")
        # tk_chernikova засеян миграцией 0115 — сервис находит его по коду.
        cash_wallet = await session.scalar(select(Wallet).where(Wallet.code == "tk_chernikova"))
        assert cash_wallet is not None
        await session.commit()

        cheque = await create_cheque(
            session,
            counterparty_id=cp.id,
            article_id=article.id,
            issued_at=ISSUED,
            bank_parts=[ChequeBankPart(bank_operation_id=op.id)],
            cash_amount=Decimal("400.00"),
        )

        assert cheque.payment_status == "paid"
        assert cheque.amount == Decimal("1000.00")

        allocs = await _allocs(session, cheque.id)
        assert sorted(a.source_kind for a in allocs) == ["bank", "cash"]

        txns = await _txns(session, cheque.id)
        assert len(txns) == 2
        assert {t.wallet_id for t in txns} == {card_wallet.id, cash_wallet.id}


async def test_create_cheque_with_nomenclature(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        article = await make_expense_article(session, code="goods", name="Сырьё")
        cp = await make_counterparty(session, name="Магазин")
        _, op = await _card_op(session, amount="500.00")
        await session.commit()

        cheque = await create_cheque(
            session,
            counterparty_id=cp.id,
            article_id=article.id,
            issued_at=ISSUED,
            bank_parts=[ChequeBankPart(bank_operation_id=op.id)],
            track_nomenclature=True,
            lines=[
                ChequeLineInput(
                    name="Лук", quantity=Decimal("5"), unit="кг", price=Decimal("100.00")
                )
            ],
        )

        assert cheque.payment_status == "paid"
        lines = (
            await session.scalars(
                select(InvoiceLineItem).where(InvoiceLineItem.invoice_id == cheque.id)
            )
        ).all()
        assert len(lines) == 1
        assert lines[0].name == "Лук"
        assert lines[0].unit == "кг"
        assert lines[0].sum == Decimal("500.00")


async def test_cheque_books_dds_per_line_article(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Местный закуп: позиции с разными статьями ДДС → отдельные движения по статьям.
    # Статьи — из белого списка чека (CHEQUE_ARTICLE_NAMES).
    async with async_session_factory() as session:
        veg = await make_expense_article(
            session, code="pitanie", name="Расходы на питание персонала"
        )
        household = await make_expense_article(
            session, code="tochki", name="Содержание торговых точек"
        )
        cp = await make_counterparty(session, name="Местный закуп")
        _, op = await _card_op(session, amount="500.00")
        await session.commit()

        cheque = await create_cheque(
            session,
            counterparty_id=cp.id,
            article_id=None,  # статья теперь на позициях, не на чеке
            issued_at=ISSUED,
            bank_parts=[ChequeBankPart(bank_operation_id=op.id)],
            track_nomenclature=True,
            lines=[
                ChequeLineInput(
                    name="Помидоры",
                    quantity=Decimal("2"),
                    price=Decimal("150.00"),
                    dds_article_id=veg.id,
                ),
                ChequeLineInput(
                    name="Мыло",
                    quantity=Decimal("1"),
                    price=Decimal("200.00"),
                    dds_article_id=household.id,
                ),
            ],
        )

        assert cheque.payment_status == "paid"
        # Движения ДДС разнесены по статьям позиций: 300 ₽ овощи + 200 ₽ хозтовары.
        by_article = {t.article_id: t.amount for t in await _txns(session, cheque.id)}
        assert by_article[veg.id] == Decimal("300.00")
        assert by_article[household.id] == Decimal("200.00")
        # Каждая позиция сохранила свою статью.
        lines = (
            await session.scalars(
                select(InvoiceLineItem).where(InvoiceLineItem.invoice_id == cheque.id)
            )
        ).all()
        assert {line.dds_article_id for line in lines} == {veg.id, household.id}


async def test_cheque_pushes_only_store_lines_to_iiko(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Складская позиция (с товаром) уходит в iiko; прочий расход (без товара) — нет.
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Местный закуп", iiko_guid="LP-GUID")
        product = IikoProduct(
            iiko_id="PROD-X", name="Помидоры", type="GOODS", unit="кг", main_unit_guid="U-KG"
        )
        session.add(product)
        session.add(
            AppSetting(
                key="iiko.default_store_guid",
                value={"guid": "ST-1"},
                value_type="json",
                category="iiko",
                display_name="Склад",
                widget_type="json",
            )
        )
        supplier = await make_expense_article(session, code="sup", name="Оплата поставщикам")
        other = await make_expense_article(session, code="sod", name="Содержание торговых точек")
        _, op = await _card_op(session, amount="700.00")
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
                    name="Помидоры",
                    quantity=Decimal("2"),
                    price=Decimal("250.00"),
                    dds_article_id=supplier.id,
                    iiko_product_id=product.id,
                ),
                ChequeLineInput(
                    name="Губки",
                    quantity=Decimal("1"),
                    price=Decimal("200.00"),
                    dds_article_id=other.id,
                ),
            ],
        )

        lines = (
            await session.scalars(
                select(InvoiceLineItem)
                .where(InvoiceLineItem.invoice_id == cheque.id)
                .order_by(InvoiceLineItem.sort_order)
            )
        ).all()
        assert lines[0].product_guid == "PROD-X"  # складская — с iiko-GUID
        assert lines[1].product_guid is None  # прочий расход — без товара

        prepared = await prepare_push(session, cheque)
        assert prepared.xml is not None
        assert "PROD-X" in prepared.xml  # складская уходит в iiko
        assert "Губки" not in prepared.xml  # прочий расход в iiko НЕ уходит


async def test_nomenclature_total_mismatch_rejected(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        article = await make_expense_article(session, code="goods2", name="Сырьё2")
        cp = await make_counterparty(session, name="Магазин")
        _, op = await _card_op(session, amount="500.00")
        await session.commit()

        with pytest.raises(KassaChequeError):
            await create_cheque(
                session,
                counterparty_id=cp.id,
                article_id=article.id,
                issued_at=ISSUED,
                bank_parts=[ChequeBankPart(bank_operation_id=op.id)],
                track_nomenclature=True,
                lines=[ChequeLineInput(name="Лук", quantity=Decimal("1"), price=Decimal("100.00"))],
            )


async def test_non_card_operation_rejected(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        article = await make_expense_article(session, code="x", name="X")
        cp = await make_counterparty(session, name="Магазин")
        account = await make_account(session)
        await make_wallet(session, wallet_type="bank", account_id=account.id)
        # Supplier payment: has INN + account, no cardOperation → not a card purchase.
        op = await make_bank_operation(
            session,
            amount="500.00",
            operation_date=OP_DATE,
            inn="7712345678",
            account="40702810900000000001",
            account_id=account.id,
        )
        await session.commit()

        with pytest.raises(KassaChequeError):
            await create_cheque(
                session,
                counterparty_id=cp.id,
                article_id=article.id,
                issued_at=ISSUED,
                bank_parts=[ChequeBankPart(bank_operation_id=op.id)],
            )


async def test_operation_reuse_rejected(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        article = await make_expense_article(session, code="y", name="Y")
        cp = await make_counterparty(session, name="Магазин")
        _, op = await _card_op(session, amount="700.00")
        await session.commit()

        await create_cheque(
            session,
            counterparty_id=cp.id,
            article_id=article.id,
            issued_at=ISSUED,
            bank_parts=[ChequeBankPart(bank_operation_id=op.id)],
        )
        with pytest.raises(KassaChequeError):
            await create_cheque(
                session,
                counterparty_id=cp.id,
                article_id=article.id,
                issued_at=ISSUED,
                bank_parts=[ChequeBankPart(bank_operation_id=op.id)],
            )


async def test_list_card_transactions_excludes_non_card(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        account = await make_account(session)
        await make_wallet(session, wallet_type="bank", account_id=account.id)
        card = await make_bank_operation(
            session,
            amount="300.00",
            operation_date=OP_DATE,
            posted_at=ISSUED,
            category="cardOperation",
            account_id=account.id,
        )
        # Supplier payment (has requisites, not a card op) — must be excluded.
        await make_bank_operation(
            session,
            amount="900.00",
            operation_date=OP_DATE,
            posted_at=ISSUED,
            inn="7712345678",
            account="40702810900000000002",
            account_id=account.id,
        )
        await session.commit()

        candidates = await list_card_transactions(session, issued_at=ISSUED)
        assert [c.bank_operation_id for c in candidates] == [card.id]
        assert candidates[0].tier == 1


async def test_card_purchase_with_tbank_processing_requisites_is_recognized(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Боевой формат T-Bank: у card-покупки получатель = процессинг АО «ТБанк»
    # (ИНН 7710140679 из BANK_NOISE_INNS), реальный магазин — в ``merch``. Раньше такая
    # операция ложно отсекалась BANK_NOISE-фильтром (проверка шла до cardOperation) —
    # регресс-тест на боевую покупку «Магнит».
    async with async_session_factory() as session:
        account = await make_account(session)
        await make_wallet(session, wallet_type="bank", account_id=account.id)
        op = await make_bank_operation(
            session,
            amount="3025.83",
            operation_date=OP_DATE,
            posted_at=ISSUED,
            category="cardOperation",
            name='АО "ТБанк"',
            inn="7710140679",
            account="30232810600010000002",
            receiver={"inn": "7710140679", "name": 'АО "ТБанк"', "acct": "30232810600010000002"},
            raw_payload={"merch": {"name": "MAGNIT MM BEREGOVOJ", "city": "Volgodonsk"}},
            account_id=account.id,
        )
        await session.commit()

        candidates = await list_card_transactions(session, issued_at=ISSUED)
        assert [c.bank_operation_id for c in candidates] == [op.id]
        # В дропдауне — реальный магазин, а не «АО ТБанк».
        assert candidates[0].counterparty_name_raw == "MAGNIT MM BEREGOVOJ (Volgodonsk)"


async def test_list_card_transactions_filters_by_purchase_day_not_settlement(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Банк проводит карт-покупку пачкой позже (settlement): operation_date/posted_at =
    # день ПРОВОДКИ, authorizationDate = реальный день ПОКУПКИ. Фильтр — по дню покупки.
    async with async_session_factory() as session:
        account = await make_account(session)
        await make_wallet(session, wallet_type="bank", account_id=account.id)
        op = await make_bank_operation(
            session,
            amount="216.73",
            operation_date=date(2026, 6, 15),  # проведено 15-го
            posted_at=datetime(2026, 6, 15, 0, 0, tzinfo=UTC),
            category="cardOperation",
            raw_payload={
                "authorizationDate": "2026-06-13T08:41:00Z",  # куплено 13-го
                "merch": {"name": "PYATEROCHKA"},
            },
            account_id=account.id,
        )
        await session.commit()

        # Дата чека 13.06 (день покупки) — операция найдена, хотя проведена 15-го.
        purchase_day = await list_card_transactions(
            session, issued_at=datetime(2026, 6, 13, 12, 0)
        )
        assert [c.bank_operation_id for c in purchase_day] == [op.id]

        # Дата чека 15.06 (день проводки) — НЕ найдена: покупка была не в этот день.
        settlement_day = await list_card_transactions(
            session, issued_at=datetime(2026, 6, 15, 12, 0)
        )
        assert settlement_day == []


# --- HTTP-слой (FastAPI): право, маппинг тела, сериализация ChequeRead, 409 --------

KASSA_BASE = "/api/v1/kassa"


def _run(coro):
    return asyncio.run(coro)


async def _seed_route(factory: async_sessionmaker[AsyncSession]):
    async with factory() as session:
        article = await make_expense_article(
            session, code="courier_payout", name="Курьерская служба -"
        )
        cp = await make_counterparty(session, name="Магазин")
        account = await make_account(session)
        await make_wallet(session, wallet_type="bank", account_id=account.id)
        op = await make_bank_operation(
            session,
            amount="1500.00",
            operation_date=OP_DATE,
            posted_at=ISSUED,
            category="cardOperation",
            account_id=account.id,
        )
        await session.commit()
        return cp.id, article.id, op.id


def _cheque_body(cp_id, article_id, op_id) -> dict:
    return {
        "counterparty_id": str(cp_id),
        "article_id": str(article_id),
        "issued_at": ISSUED.isoformat(),
        "bank_parts": [{"bank_operation_id": str(op_id)}],
    }


def test_post_cheque_creates_paid_and_serializes(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    cp_id, article_id, op_id = _run(_seed_route(async_session_factory))
    headers = _run(headers_for(async_session_factory, "kassa-cashier@test.local", ["cashier"]))
    body = _cheque_body(cp_id, article_id, op_id)

    resp = client.post(f"{KASSA_BASE}/cheques", json=body, headers=headers)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["payment_status"] == "paid"
    assert data["amount"] == 1500.0
    assert data["number"].startswith("Ч-")
    assert data["article_name"] == "Курьерская служба -"
    assert len(data["allocations"]) == 1

    got = client.get(f"{KASSA_BASE}/cheques/{data['id']}", headers=headers)
    assert got.status_code == 200
    assert got.json()["id"] == data["id"]

    # Та же операция повторно → 409 (анти-дубль на HTTP-слое).
    repeat = client.post(f"{KASSA_BASE}/cheques", json=body, headers=headers)
    assert repeat.status_code == 409


def test_post_cheque_forbidden_without_permission(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    cp_id, article_id, op_id = _run(_seed_route(async_session_factory))
    headers = _run(
        headers_for(async_session_factory, "kassa-courier@test.local", ["senior_courier"])
    )
    resp = client.post(
        f"{KASSA_BASE}/cheques", json=_cheque_body(cp_id, article_id, op_id), headers=headers
    )
    assert resp.status_code == 403
