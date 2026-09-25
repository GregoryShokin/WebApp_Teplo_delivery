"""Двери прямой оплаты и аванс правила 1: деньги платежа не числятся дважды.

Воспроизведение со скептиков 25.09 (копия прода): перевод поставщику мимо черновика банка
31.07 — классификатор разбирает его на поставщика, правило 1 складские накладные не подбирает,
и все 12 000 становятся авансом. 01.08 приходит складская накладная, её оплачивают в диалоге
«Оплатить» этой же операцией. До починки аванс оставался открытым, а накладная — оплаченной
теми же деньгами: ДЗ на 01.08 и 31.08 — 12 000 при верных 0.

Решение владельца (25.09): документ гасится ЗАЧЁТОМ из аванса (``source_kind='prepayment'``,
датой вступления документа), сумма аванса не уменьшается — на 31.07 ДЗ 12 000 остаётся.
Уменьшение аванса (первая версия, 134fe3e7) стирало эту ДЗ задним числом. Замок — месяц
документа. Бартерный заём из аванса не гасится: там проверка месяца денег до записи.

Путь — из интерфейса: разбор операции классификатором, кандидаты диалога и маршруты
``/warehouse/match/confirm`` и ``/warehouse/invoices/{id}/pay-split``.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from cp_helpers import (
    admin_headers,
    make_account,
    make_bank_operation,
    make_counterparty,
    make_draft,
    make_expense_article,
    make_invoice,
    make_wallet,
)
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    BankOperation,
    IikoProduct,
    InvoicePaymentAllocation,
    SupplierInvoice,
    SupplierPrepayment,
)
from app.services import accounting_periods
from app.services.banking.classifier import apply_operation_action
from app.services.counterparty_balance_as_of import build_balance_as_of
from app.services.supplier_prepayments import (
    PAYMENT_MATCH_BANK_ORIGIN,
    PAYMENT_MATCH_CASH_ORIGIN,
    RULE1_PREPAYMENT_KIND,
    apply_closing_document,
)

BASE = "/api/v1/warehouse"
PAID_ON = date(2026, 7, 31)
DOC_ON = date(2026, 8, 1)
POSTED = datetime(2026, 7, 31, 15, 0, tzinfo=UTC)  # 18:00 МСК
ISSUED = datetime(2026, 8, 1, 7, 0, tzinfo=UTC)  # 10:00 МСК, 13 часов после платежа
ZERO = Decimal("0.00")


def _run(coro):
    return asyncio.run(coro)


async def _statement_payment(
    session: AsyncSession,
    *,
    counterparty_id: uuid.UUID,
    inn: str,
    amount: str = "12000.00",
    operation_date: date = PAID_ON,
    posted_at: datetime = POSTED,
) -> BankOperation:
    """Перевод поставщику мимо черновика — как его разбирает классификатор: операция Т-Банка,
    «Оплата поставщикам» на контрагента, правило 1 делает из денег аванс."""
    article = await make_expense_article(session)
    account = await make_account(session)
    await make_wallet(session, name=f"Т-Банк {inn}", wallet_type="bank", account_id=account.id)
    operation = await make_bank_operation(
        session,
        amount=amount,
        inn=inn,
        operation_date=operation_date,
        posted_at=posted_at,
        account_id=account.id,
    )
    await apply_operation_action(
        session,
        operation,
        action="set_article",
        article_id=article.id,
        counterparty_id=counterparty_id,
    )
    return operation


async def _warehouse_invoice(
    session: AsyncSession,
    *,
    counterparty_id: uuid.UUID,
    amount: str = "12000.00",
    number: str = "ТН-0801",
    invoice_date: date = DOC_ON,
) -> SupplierInvoice:
    return await make_invoice(
        session,
        counterparty_id=counterparty_id,
        amount=amount,
        number=number,
        operational_scope="warehouse",
        invoice_date=invoice_date,
        issued_at=ISSUED,
    )


async def _seed(
    factory: async_sessionmaker[AsyncSession], *, inn: str
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Платёж 31.07 на 12 000 стал авансом; накладная от 01.08 на 12 000 ждёт оплаты."""
    async with factory() as session:
        cp = await make_counterparty(session, name=f"Поставщик-{inn}", inn=inn)
        operation = await _statement_payment(session, counterparty_id=cp.id, inn=inn)
        invoice = await _warehouse_invoice(session, counterparty_id=cp.id)
        await session.commit()
        advance = await _rule1_advance(session, cp.id)
        assert advance is not None and advance.amount == Decimal("12000.00"), (
            "классификатор должен был сделать из платежа аванс правила 1"
        )
        return cp.id, operation.id, invoice.id


async def _rule1_advance(
    session: AsyncSession, counterparty_id: uuid.UUID
) -> SupplierPrepayment | None:
    return await session.scalar(
        select(SupplierPrepayment).where(
            SupplierPrepayment.counterparty_id == counterparty_id,
            SupplierPrepayment.kind == RULE1_PREPAYMENT_KIND,
        )
    )


async def _balance(
    session: AsyncSession, counterparty_id: uuid.UUID, as_of: date
) -> tuple[Decimal, Decimal]:
    """(ДЗ, КЗ) контрагента на конец дня — как их видит баланс на дату."""
    sheet = await build_balance_as_of(session, as_of=as_of)
    row = next((r for r in sheet.rows if r.counterparty_id == counterparty_id), None)
    return (row.receivable, row.payable) if row is not None else (ZERO, ZERO)


async def _ledger_receivable(session: AsyncSession, counterparty_id: uuid.UUID) -> Decimal:
    """Дебиторка контрагента так, как её считает плитка: открытые остатки предоплат."""
    rows = (
        await session.scalars(
            select(SupplierPrepayment).where(
                SupplierPrepayment.counterparty_id == counterparty_id,
                SupplierPrepayment.status.in_(("open", "partially_settled")),
            )
        )
    ).all()
    return sum((r.amount - r.amount_settled for r in rows), ZERO)


async def _allocations(
    session: AsyncSession, invoice_id: uuid.UUID
) -> list[InvoicePaymentAllocation]:
    return list(
        (
            await session.scalars(
                select(InvoicePaymentAllocation).where(
                    InvoicePaymentAllocation.invoice_id == invoice_id
                )
            )
        ).all()
    )


async def _assert_paid_once(
    factory: async_sessionmaker[AsyncSession],
    counterparty_id: uuid.UUID,
    invoice_id: uuid.UUID,
    *,
    origin: str = PAYMENT_MATCH_BANK_ORIGIN,
) -> None:
    """Деньги платежа 31.07 — дебиторка до 01.08 и оплата накладной с 01.08, ровно один раз."""
    async with factory() as session:
        assert await _balance(session, counterparty_id, date(2026, 7, 31)) == (
            Decimal("12000.00"),
            ZERO,
        ), "31.07 деньги у поставщика, товара нет — ДЗ 12 000 должна остаться"
        assert await _balance(session, counterparty_id, date(2026, 8, 1)) == (ZERO, ZERO), (
            "01.08 ДЗ задвоена: накладная оплачена, а аванс открыт"
        )
        assert await _balance(session, counterparty_id, date(2026, 8, 31)) == (ZERO, ZERO)
        assert await _ledger_receivable(session, counterparty_id) == ZERO

        advance = await _rule1_advance(session, counterparty_id)
        assert advance is not None
        assert advance.amount == Decimal("12000.00"), "сумму аванса дверь не трогает"
        assert advance.amount_settled == Decimal("12000.00")
        assert advance.status == "settled"
        invoice = await session.get(SupplierInvoice, invoice_id)
        assert invoice is not None and invoice.payment_status == "paid"
        [allocation] = await _allocations(session, invoice_id)
        assert allocation.source_kind == "prepayment"
        assert allocation.prepayment_id == advance.id
        assert allocation.origin == origin


# --- путь из интерфейса: диалог «Оплатить» складской накладной ------------------------------


def test_pay_dialog_confirm_settles_the_invoice_from_the_rule1_advance(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Диалог «Оплатить», один кандидат, «обогатить реквизиты» — ``/warehouse/match/confirm``."""
    cp_id, op_id, invoice_id = _run(_seed(async_session_factory, inn="7702000001"))
    headers = _run(admin_headers(async_session_factory))

    suggestions = client.get(f"{BASE}/invoices/{invoice_id}/match-suggestions", headers=headers)
    assert suggestions.status_code == 200, suggestions.text
    assert str(op_id) in {c["bank_operation_id"] for c in suggestions.json()["candidates"]}

    body = {"invoice_id": str(invoice_id), "bank_operation_id": str(op_id), "enrich": True}
    resp = client.post(f"{BASE}/match/confirm", json=body, headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["payment_status"] == "paid"

    _run(_assert_paid_once(async_session_factory, cp_id, invoice_id))


def test_pay_dialog_split_settles_the_invoice_from_the_rule1_advance(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Тот же диалог без «обогащения» уходит в ``pay-split`` — банковская часть та же."""
    cp_id, op_id, invoice_id = _run(_seed(async_session_factory, inn="7702000002"))
    headers = _run(admin_headers(async_session_factory))

    body = {"bank_parts": [{"bank_operation_id": str(op_id)}], "cash_parts": []}
    resp = client.post(f"{BASE}/invoices/{invoice_id}/pay-split", json=body, headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["payment_status"] == "paid"

    _run(_assert_paid_once(async_session_factory, cp_id, invoice_id))


def test_spent_advance_is_neither_offered_nor_accepted_for_the_next_invoice(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Зачёт не метит операцию «использованной» (остаток аванса может оплатить ещё накладную),
    но операцию с исчерпанным авансом диалог не предлагает, а подтверждение отказывает."""
    cp_id, op_id, invoice_id = _run(_seed(async_session_factory, inn="7702000003"))
    headers = _run(admin_headers(async_session_factory))
    body = {"invoice_id": str(invoice_id), "bank_operation_id": str(op_id), "enrich": False}
    assert client.post(f"{BASE}/match/confirm", json=body, headers=headers).status_code == 200

    async def _second() -> uuid.UUID:
        async with async_session_factory() as session:
            second = await _warehouse_invoice(session, counterparty_id=cp_id, number="ТН-0801-2")
            await session.commit()
            return second.id

    second_id = _run(_second())
    suggestions = client.get(f"{BASE}/invoices/{second_id}/match-suggestions", headers=headers)
    assert suggestions.status_code == 200, suggestions.text
    assert str(op_id) not in {c["bank_operation_id"] for c in suggestions.json()["candidates"]}

    body = {"invoice_id": str(second_id), "bank_operation_id": str(op_id), "enrich": False}
    resp = client.post(f"{BASE}/match/confirm", json=body, headers=headers)
    assert resp.status_code == 409, resp.text
    split = {"bank_parts": [{"bank_operation_id": str(op_id)}], "cash_parts": []}
    resp = client.post(f"{BASE}/invoices/{second_id}/pay-split", json=split, headers=headers)
    assert resp.status_code == 409, resp.text

    async def _check() -> None:
        async with async_session_factory() as session:
            assert await _allocations(session, second_id) == []
            # Вторая накладная — честная кредиторка: денег на неё у платежа нет.
            assert await _balance(session, cp_id, date(2026, 8, 1)) == (
                ZERO,
                Decimal("12000.00"),
            )
            assert await _ledger_receivable(session, cp_id) == ZERO

    _run(_check())


def test_one_payment_settles_two_deliveries_from_its_advance(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Один перевод за две поставки (5 000 + 7 000): остаток аванса оплачивает вторую."""

    async def _two() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
        async with async_session_factory() as session:
            cp = await make_counterparty(session, name="Две-поставки", inn="7702000004")
            operation = await _statement_payment(session, counterparty_id=cp.id, inn="7702000004")
            first = await _warehouse_invoice(
                session, counterparty_id=cp.id, amount="5000.00", number="ТН-1"
            )
            second = await _warehouse_invoice(
                session, counterparty_id=cp.id, amount="7000.00", number="ТН-2"
            )
            await session.commit()
            return cp.id, operation.id, first.id, second.id

    cp_id, op_id, first_id, second_id = _run(_two())
    headers = _run(admin_headers(async_session_factory))
    for invoice_id in (first_id, second_id):
        body = {"invoice_id": str(invoice_id), "bank_operation_id": str(op_id), "enrich": False}
        resp = client.post(f"{BASE}/match/confirm", json=body, headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["payment_status"] == "paid"

    async def _check() -> None:
        async with async_session_factory() as session:
            assert await _balance(session, cp_id, date(2026, 7, 31)) == (Decimal("12000.00"), ZERO)
            assert await _balance(session, cp_id, date(2026, 8, 1)) == (ZERO, ZERO)
            assert await _ledger_receivable(session, cp_id) == ZERO

    _run(_check())


def test_touched_advance_pays_only_its_open_rest(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Аванс уже погасил УПД на 5 000 — накладной достаётся открытый остаток 7 000, не 12 000.

    До починки накладная оплачивалась всеми 12 000, а аванс оставался с остатком 7 000:
    денег 12 000, распоряжений — на 24 000."""

    async def _seed_touched() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
        cp_id, op_id, invoice_id = await _seed(async_session_factory, inn="7702000005")
        async with async_session_factory() as session:
            upd = await make_invoice(
                session,
                counterparty_id=cp_id,
                amount="5000.00",
                number="УПД-0805",
                operational_scope="finance",
                invoice_date=date(2026, 8, 5),
            )
            await apply_closing_document(session, upd, as_of=date(2026, 8, 5))
            await session.commit()
            advance = await _rule1_advance(session, cp_id)
            assert advance is not None and advance.amount_settled == Decimal("5000.00")
        return cp_id, op_id, invoice_id

    cp_id, op_id, invoice_id = _run(_seed_touched())
    headers = _run(admin_headers(async_session_factory))
    body = {"invoice_id": str(invoice_id), "bank_operation_id": str(op_id), "enrich": False}
    resp = client.post(f"{BASE}/match/confirm", json=body, headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["payment_status"] == "partially_paid"

    async def _check() -> None:
        async with async_session_factory() as session:
            advance = await _rule1_advance(session, cp_id)
            assert advance is not None
            assert (advance.amount, advance.amount_settled) == (
                Decimal("12000.00"),
                Decimal("12000.00"),
            )
            assert await _balance(session, cp_id, date(2026, 7, 31)) == (Decimal("12000.00"), ZERO)
            assert await _balance(session, cp_id, date(2026, 8, 1)) == (
                Decimal("5000.00"),
                Decimal("5000.00"),
            )
            assert await _balance(session, cp_id, date(2026, 8, 31)) == (ZERO, Decimal("5000.00"))

    _run(_check())


# --- замок месяца ------------------------------------------------------------------------------


def test_closed_document_month_refuses_before_writing(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Зачёт датирован вступлением накладной: закрыт август — 409, и ничего не записано."""
    cp_id, op_id, invoice_id = _run(_seed(async_session_factory, inn="7702000006"))
    headers = _run(admin_headers(async_session_factory))

    async def _close(month: date) -> None:
        async with async_session_factory() as session:
            await accounting_periods.close_month(session, period_month=month, actor_user_id=None)

    _run(_close(date(2026, 8, 1)))
    body = {"invoice_id": str(invoice_id), "bank_operation_id": str(op_id), "enrich": True}
    resp = client.post(f"{BASE}/match/confirm", json=body, headers=headers)
    assert resp.status_code == 409, resp.text
    assert "08.2026 закрыт" in resp.json()["detail"]
    split = {"bank_parts": [{"bank_operation_id": str(op_id)}], "cash_parts": []}
    resp = client.post(f"{BASE}/invoices/{invoice_id}/pay-split", json=split, headers=headers)
    assert resp.status_code == 409, resp.text

    async def _untouched() -> None:
        async with async_session_factory() as session:
            assert await _allocations(session, invoice_id) == []
            invoice = await session.get(SupplierInvoice, invoice_id)
            assert invoice is not None and invoice.payment_status == "unpaid"
            advance = await _rule1_advance(session, cp_id)
            assert advance is not None and advance.amount_settled == ZERO

    _run(_untouched())


def test_closed_money_month_does_not_block_a_document_of_an_open_month(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Июль (месяц денег) закрыт, накладная — августовская: зачёт датирован 01.08, цифры июля
    не меняются — дверь работает, ДЗ на 31.07 та же."""
    cp_id, op_id, invoice_id = _run(_seed(async_session_factory, inn="7702000007"))
    headers = _run(admin_headers(async_session_factory))

    async def _close_july() -> tuple[Decimal, Decimal]:
        async with async_session_factory() as session:
            await accounting_periods.close_month(
                session, period_month=date(2026, 7, 1), actor_user_id=None
            )
            return await _balance(session, cp_id, date(2026, 7, 31))

    july_before = _run(_close_july())
    body = {"invoice_id": str(invoice_id), "bank_operation_id": str(op_id), "enrich": False}
    resp = client.post(f"{BASE}/match/confirm", json=body, headers=headers)
    assert resp.status_code == 200, resp.text

    async def _july_after() -> tuple[Decimal, Decimal]:
        async with async_session_factory() as session:
            return await _balance(session, cp_id, date(2026, 7, 31))

    assert _run(_july_after()) == july_before == (Decimal("12000.00"), ZERO)
    _run(_assert_paid_once(async_session_factory, cp_id, invoice_id))


# --- жизнь зачёта после оплаты: он ведёт себя как сверка операции, которую заменил ---------------


async def _paid_by_the_dialog(
    factory: async_sessionmaker[AsyncSession], *, inn: str
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    from app.services.counterparty_bank_match import confirm_invoice_match

    cp_id, op_id, invoice_id = await _seed(factory, inn=inn)
    async with factory() as session:
        await confirm_invoice_match(
            session,
            invoice_id=invoice_id,
            bank_operation_id=op_id,
            enrich=False,
            actor_user_id=None,
        )
    return cp_id, op_id, invoice_id


async def test_excluding_the_operation_unpays_the_invoice_like_a_bank_match(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """«Исключить операцию»: сверку снимает откат bank-аллокаций, зачёт — тот же откат. Иначе
    аванс, тронутый зачётом, пережил бы снос нетронутых, и накладная осталась бы оплаченной
    деньгами, которых в учёте нет."""
    cp_id, op_id, invoice_id = await _paid_by_the_dialog(async_session_factory, inn="7702000008")
    async with async_session_factory() as session:
        operation = await session.get(BankOperation, op_id)
        await apply_operation_action(session, operation, action="exclude")
        await session.commit()

        invoice = await session.get(SupplierInvoice, invoice_id)
        assert invoice is not None and invoice.payment_status == "unpaid"
        assert await _allocations(session, invoice_id) == []
        assert await _rule1_advance(session, cp_id) is None
        assert await _balance(session, cp_id, date(2026, 7, 31)) == (ZERO, ZERO)
        assert await _balance(session, cp_id, date(2026, 8, 31)) == (ZERO, Decimal("12000.00"))


async def test_resplitting_the_operation_releases_the_settlement_like_a_bank_match(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Переразбор сплитом снимает прежние гашения накладных операцией — и зачёт тоже; новый
    разбор заводит аванс заново, второго на те же деньги нет."""
    from app.services.banking.classifier import OperationSplitLine, apply_operation_split

    cp_id, op_id, invoice_id = await _paid_by_the_dialog(async_session_factory, inn="7702000009")
    async with async_session_factory() as session:
        operation = await session.get(BankOperation, op_id)
        article = await make_expense_article(session)
        await apply_operation_split(
            session,
            operation,
            splits=[
                OperationSplitLine(article_id=article.id, amount=Decimal("8000.00")),
                OperationSplitLine(article_id=article.id, amount=Decimal("4000.00")),
            ],
            counterparty_id=cp_id,
        )
        await session.commit()

        invoice = await session.get(SupplierInvoice, invoice_id)
        assert invoice is not None and invoice.payment_status == "unpaid"
        orphans = (
            await session.scalars(
                select(SupplierPrepayment).where(
                    SupplierPrepayment.counterparty_id == cp_id,
                    SupplierPrepayment.cashflow_transaction_id.is_(None),
                )
            )
        ).all()
        assert orphans == [], "аванс пережил свою проводку"
        assert await _ledger_receivable(session, cp_id) == Decimal("12000.00")
        assert await _balance(session, cp_id, date(2026, 8, 31)) == (
            Decimal("12000.00"),
            Decimal("12000.00"),
        )


async def test_reassigning_the_payment_turns_the_settlement_back_into_the_bank_match(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Платёж перевесили на другого контрагента: деньги больше не аванс поставщика накладной.
    Решение «этот платёж оплатил эту накладную» остаётся — сверкой операции, как было без аванса,
    и новый контрагент аванса на те же деньги не получает."""
    cp_id, op_id, invoice_id = await _paid_by_the_dialog(async_session_factory, inn="7702000010")
    async with async_session_factory() as session:
        other = await make_counterparty(session, name="Перевес-на", inn="7702000011")
        article = await make_expense_article(session)
        operation = await session.get(BankOperation, op_id)
        await apply_operation_action(
            session,
            operation,
            action="set_article",
            article_id=article.id,
            counterparty_id=other.id,
        )
        await session.commit()

        invoice = await session.get(SupplierInvoice, invoice_id)
        assert invoice is not None and invoice.payment_status == "paid"
        [allocation] = await _allocations(session, invoice_id)
        assert (allocation.source_kind, allocation.bank_operation_id) == ("bank", op_id)
        assert await _ledger_receivable(session, cp_id) == ZERO
        assert await _ledger_receivable(session, other.id) == ZERO
        for counterparty_id in (cp_id, other.id):
            assert await _balance(session, counterparty_id, date(2026, 8, 31)) == (ZERO, ZERO)


def test_payment_of_another_counterparty_is_refused(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Платёж разобран на Y — его деньги аванс Y. Накладную X им не оплатить: аванс Y остался бы,
    и деньги числились бы дважды."""

    async def _seed_foreign() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
        async with async_session_factory() as session:
            x = await make_counterparty(session, name="Поставщик-X", inn="7702000012")
            y = await make_counterparty(session, name="Поставщик-Y", inn="7702000013")
            operation = await _statement_payment(session, counterparty_id=y.id, inn="7702000012")
            invoice = await _warehouse_invoice(session, counterparty_id=x.id)
            await session.commit()
            return y.id, operation.id, invoice.id

    y_id, op_id, invoice_id = _run(_seed_foreign())
    headers = _run(admin_headers(async_session_factory))
    body = {"invoice_id": str(invoice_id), "bank_operation_id": str(op_id), "enrich": False}
    resp = client.post(f"{BASE}/match/confirm", json=body, headers=headers)
    assert resp.status_code == 409, resp.text
    assert "другого контрагента" in resp.json()["detail"]

    async def _intact() -> None:
        async with async_session_factory() as session:
            assert await _allocations(session, invoice_id) == []
            assert await _ledger_receivable(session, y_id) == Decimal("12000.00")

    _run(_intact())


# --- двери, доступные через API: ручная оплата проводкой и сверка с черновиком ----------------


async def test_cash_allocation_door_settles_from_the_advance(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``allocate_cash_to_invoice`` с проводкой платежа — та же дверь: зачёт, аванс прежний."""
    from app.services.counterparty_matching import allocate_cash_to_invoice

    cp_id, op_id, invoice_id = await _seed(async_session_factory, inn="7702000014")
    async with async_session_factory() as session:
        operation = await session.get(BankOperation, op_id)
        await allocate_cash_to_invoice(
            session,
            invoice_id=invoice_id,
            amount=Decimal("12000.00"),
            cashflow_transaction_id=operation.cashflow_transaction_id,
        )
    await _assert_paid_once(
        async_session_factory, cp_id, invoice_id, origin=PAYMENT_MATCH_CASH_ORIGIN
    )


async def test_cash_allocation_door_refuses_money_the_advance_already_spent(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Скептик 25.09 (п. 3): аванс уже закрыл УПД — второй раз его деньги не отдать."""
    from app.services.counterparty_matching import CounterpartyMatchError, allocate_cash_to_invoice

    cp_id, op_id, invoice_id = await _seed(async_session_factory, inn="7702000015")
    async with async_session_factory() as session:
        upd = await make_invoice(
            session,
            counterparty_id=cp_id,
            amount="12000.00",
            number="УПД-0801",
            operational_scope="finance",
            invoice_date=date(2026, 8, 1),
        )
        await apply_closing_document(session, upd, as_of=date(2026, 8, 1))
        await session.commit()
        operation = await session.get(BankOperation, op_id)
        with pytest.raises(CounterpartyMatchError):
            await allocate_cash_to_invoice(
                session,
                invoice_id=invoice_id,
                amount=Decimal("1.00"),
                cashflow_transaction_id=operation.cashflow_transaction_id,
            )
        await session.rollback()
        assert await _allocations(session, invoice_id) == []


async def test_draft_allocation_settles_from_the_advance(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Сверка операции с черновиком: акт черновика правило 1 не подбирает, платёж целиком стал
    авансом — акт гасится зачётом, второй черновик на те же деньги не оплачивается."""
    from app.services.counterparty_matching import (
        CounterpartyMatchError,
        allocate_bank_operation_to_draft,
    )

    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Черновик-аванс", inn="7702000016")
        act = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="4000.00",
            number="АКТ-1",
            operational_scope="finance",
            invoice_date=date(2026, 8, 1),
        )
        draft = await make_draft(session, counterparty_id=cp.id, amount="4000.00")
        act.draft_id = draft.id
        operation = await _statement_payment(
            session, counterparty_id=cp.id, inn="7702000016", amount="10000.00"
        )
        await session.commit()
        advance = await _rule1_advance(session, cp.id)
        assert advance is not None and advance.amount == Decimal("10000.00")

        await allocate_bank_operation_to_draft(
            session, bank_operation_id=operation.id, draft_id=draft.id
        )
        await session.refresh(advance)
        assert (await session.get(SupplierInvoice, act.id)).payment_status == "paid"
        assert (advance.amount, advance.amount_settled) == (
            Decimal("10000.00"),
            Decimal("4000.00"),
        )
        assert await _ledger_receivable(session, cp.id) == Decimal("6000.00")
        assert await _balance(session, cp.id, date(2026, 7, 31)) == (Decimal("10000.00"), ZERO)
        assert await _balance(session, cp.id, date(2026, 8, 1)) == (Decimal("6000.00"), ZERO)

        second = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="7000.00",
            number="АКТ-2",
            operational_scope="finance",
            invoice_date=date(2026, 8, 2),
        )
        other_draft = await make_draft(session, counterparty_id=cp.id, amount="7000.00")
        second.draft_id = other_draft.id
        await session.commit()
        second_id, operation_id, other_draft_id = second.id, operation.id, other_draft.id
        with pytest.raises(CounterpartyMatchError):
            await allocate_bank_operation_to_draft(
                session, bank_operation_id=operation_id, draft_id=other_draft_id
            )
        await session.rollback()
        assert await _allocations(session, second_id) == []


# --- бартерный заём: из аванса не гасится, замок — месяц денег, и до записи ---------------------


async def _their_loan(session: AsyncSession, *, inn: str, amount: str = "3000.00"):
    """Их товарный заём от 10.07 (мы должны вернуть) и платёж 31.07, ставший авансом."""
    from app.services.warehouse_invoices import LineInput, create_warehouse_invoice

    cp = await make_counterparty(session, name=f"Бартер-{inn}", inn=inn)
    product = IikoProduct(
        iiko_id=str(uuid.uuid4()),
        name="Моцарелла",
        type="GOODS",
        unit="кг",
        synced_at=datetime.now(UTC),
    )
    session.add(product)
    await session.commit()
    loan = await create_warehouse_invoice(
        session,
        counterparty_id=cp.id,
        issued_at=datetime(2026, 7, 10, 12, 0, tzinfo=UTC),
        mode="loan",
        we_lend=False,
        lines=[
            LineInput(
                name="Моцарелла",
                quantity=Decimal("10"),
                price=Decimal(amount) / 10,
                iiko_product_id=product.id,
            )
        ],
    )
    operation = await _statement_payment(session, counterparty_id=cp.id, inn=inn, amount=amount)
    await session.commit()
    return cp.id, loan.id, operation.id


def test_loan_door_checks_the_money_month_before_writing(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Скептики 25.09 (в): сверка займа коммитилась, а замок пересборки аванса срабатывал
    потом — 409, но заём оплачен, аванс открыт, ДЗ задвоена. Теперь отказ до записи."""

    async def _seed_loan() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
        async with async_session_factory() as session:
            ids = await _their_loan(session, inn="7702000017")
            await accounting_periods.close_month(
                session, period_month=date(2026, 7, 1), actor_user_id=None
            )
            return ids

    cp_id, loan_id, op_id = _run(_seed_loan())
    headers = _run(admin_headers(async_session_factory))
    body = {
        "operation_date": "2026-07-31",
        "bank_operation_id": str(op_id),
        "amount": "3000.00",
    }
    resp = client.post(f"{BASE}/loans/{loan_id}/settle-money", json=body, headers=headers)
    assert resp.status_code == 409, resp.text
    assert "07.2026 закрыт" in resp.json()["detail"]

    async def _nothing_written() -> None:
        async with async_session_factory() as session:
            assert await _allocations(session, loan_id) == [], "сверка займа записана при 409"
            loan = await session.get(SupplierInvoice, loan_id)
            assert loan is not None and loan.payment_status == "unpaid"
            assert await _ledger_receivable(session, cp_id) == Decimal("3000.00")

    _run(_nothing_written())


async def test_loan_paid_by_a_touched_advance_takes_its_money_out_of_the_advance(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Аванс 12 000 уже погасил УПД на 5 000 (тронут) — пересборка правила 1 его не трогает, и
    деньги займа 3 000 оставались в нём: заём оплачен, аванс прежний. Теперь аванс отдаёт их."""
    from app.services.barter_loan_money import pay_payable_loan_with_money

    async with async_session_factory() as session:
        cp_id, loan_id, op_id = await _their_loan(session, inn="7702000018", amount="12000.00")
        upd = await make_invoice(
            session,
            counterparty_id=cp_id,
            amount="5000.00",
            number="УПД-0805",
            operational_scope="finance",
            invoice_date=date(2026, 8, 5),
        )
        await apply_closing_document(session, upd, as_of=date(2026, 8, 5))
        await session.commit()

        await pay_payable_loan_with_money(
            session,
            loan_id=loan_id,
            operation_date=PAID_ON,
            amount=Decimal("3000.00"),
            bank_operation_id=op_id,
        )
        advance = await _rule1_advance(session, cp_id)
        assert advance is not None
        assert (advance.amount, advance.amount_settled) == (
            Decimal("9000.00"),
            Decimal("5000.00"),
        )
        assert await _ledger_receivable(session, cp_id) == Decimal("4000.00"), (
            "12 000 денег: 5 000 закрыли УПД, 3 000 — заём, дебиторки 4 000"
        )
        assert await _balance(session, cp_id, date(2026, 8, 31)) == (Decimal("4000.00"), ZERO)
