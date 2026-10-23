"""Цепочка исправления оплаченной накладной: сторно → «(испр.)» → оплата на неё без второй траты.

Решение владельца 23.10.2026: «…делает сторно на исправленную накладную ровно на ту же сумму…
эта оплата обнуляется… создаётся ещё одна уже исправленная конечная накладная, и на неё тоже
накладывается оплата… Никто руками ничего не делает». Пример — чек Ч-117 «Местного закупа»:
исходная 3 253,91 оплачена картой, исправление перенесло 284,97 из товара в питание персонала,
«(испр.)» — 2 968,94. iiko подменена своей: проверяем, что и сколько цепочка ставит, и где она
обязана остановиться, чтобы не потратить деньги второй раз.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from cp_helpers import make_counterparty, make_invoice
from sqlalchemy import select

import app.services.counterparty_iiko_payment as cip
import app.services.warehouse_invoice_push as wip
from app.core.config import get_settings
from app.models import (
    ChequeIikoPayout,
    DdsArticle,
    IikoInvoicePaymentPush,
    InvoiceLineItem,
    SupplierInvoice,
)
from app.services.iiko_correction_chain import (
    ADVANCES_ACCOUNT_NAME,
    OFFSET_KEY_PREFIX,
    ExpenseShare,
    build_payment_plan,
    cheque_expense_shares,
    complete_correction_payment,
)
from app.services.iiko_payment_watch import IikoDocument, LedgerPayment
from app.services.warehouse_invoice_push import _CloudPushOutcome, book_correction_in_iiko

SUPPLIER = "SUP-MZ"
ACCOUNTS = {
    ADVANCES_ACCOUNT_NAME: "ACC-ADVANCES",
    "Затраты на персонал": "ACC-STAFF",
    "Содержание торговых точек": "ACC-POINTS",
}
FOOD = "Расходы на питание персонала"
POINTS = "Содержание торговых точек"
GOODS = "Оплата поставщикам"


# --- план: из чего складывается оплата исправленной ------------------------------------------


def test_plan_ch117_offsets_correction_and_moves_difference_to_food():
    """Ч-117: сторно = исходная 3 253,91; «(испр.)» 2 968,94 платится зачётом целиком; разница
    284,97 — ровно прирост питания персонала. Кредита у поставщика не остаётся."""
    plan = build_payment_plan(
        storno_total=Decimal("3253.91"),
        paid_for_original=Decimal("3253.91"),
        correction_total=Decimal("2968.94"),
        expense_shares=[ExpenseShare(FOOD, "Затраты на персонал", Decimal("284.97"))],
        share_problems=[],
        is_cheque=True,
    )
    assert plan.problems == []
    assert plan.offset_amount == Decimal("2968.94")
    assert plan.expense_total == Decimal("284.97")
    assert plan.leftover_credit == Decimal("0.00")


def test_plan_accepts_only_the_old_storno_kopeck_drift():
    """Живой Ч-117: старое сторно 0004 = 3 254,04 при оплате исходной 3 253,91. Перекос 0,13 —
    единственный объяснимый остаток; он остаётся кредитом поставщика и не стопорит цепочку."""
    plan = build_payment_plan(
        storno_total=Decimal("3254.04"),
        paid_for_original=Decimal("3253.91"),
        correction_total=Decimal("2968.94"),
        expense_shares=[ExpenseShare(FOOD, "Затраты на персонал", Decimal("284.97"))],
        share_problems=[],
        is_cheque=True,
    )
    assert plan.problems == []
    assert plan.leftover_credit == Decimal("0.13")


@pytest.mark.parametrize(
    ("correction", "shares", "words"),
    [
        ("3300.00", [], "дороже исходной"),
        ("2968.94", [ExpenseShare(FOOD, "Затраты на персонал", Decimal("200.00"))], "не сходится"),
    ],
)
def test_plan_stops_when_money_would_not_balance(correction, shares, words):
    """Доплата деньгами и разница, которая не равна приросту расходов, — не угадываем."""
    plan = build_payment_plan(
        storno_total=Decimal("3253.91"),
        paid_for_original=Decimal("3253.91"),
        correction_total=Decimal(correction),
        expense_shares=shares,
        share_problems=[],
        is_cheque=True,
    )
    assert any(words in problem for problem in plan.problems)


def test_plan_bank_invoice_keeps_overpayment_as_supplier_credit():
    """Банковская накладная (АЛЬЯНС ЮГ): разница — настоящая переплата, она остаётся кредитом."""
    plan = build_payment_plan(
        storno_total=Decimal("33982.80"),
        paid_for_original=Decimal("33982.80"),
        correction_total=Decimal("10252.80"),
        expense_shares=[],
        share_problems=[],
        is_cheque=False,
    )
    assert plan.problems == []
    assert plan.offset_amount == Decimal("10252.80")
    assert plan.leftover_credit == Decimal("23730.00")


# --- живая часть: подменённая iiko -------------------------------------------------------------


@dataclass
class FakeChainIiko:
    """iiko для шагов 3–4: документы, проводки оплат, план счетов, акты услуг."""

    docs: dict[str, IikoDocument] = field(default_factory=dict)
    returns: dict[str, IikoDocument] = field(default_factory=dict)
    payments: list[LedgerPayment] = field(default_factory=list)
    acts: list[dict] = field(default_factory=list)
    act_posted: bool = True

    def incoming(self, document_id):
        return self.docs.get(document_id, IikoDocument(document_id, found=False))

    def returned(self, document_id):
        return self.returns.get(document_id, IikoDocument(document_id, found=False))

    def ledger(self, date_from, date_to):
        return list(self.payments)

    def account_id(self, name):
        return ACCOUNTS.get(name)

    def create_service_act(self, body):
        self.acts.append(body)
        return f"ACT-{len(self.acts)}", self.act_posted, None if self.act_posted else "post 500"

    def post_service_act(self, document_id):
        return True, None


def _doc(document_id, number, total, *, links_to=None):
    return IikoDocument(
        document_id=document_id,
        found=True,
        number=number,
        status="PROCESSED",
        counteragent_id=SUPPLIER,
        incoming_invoice_id=links_to,
        total=Decimal(total),
    )


def _pay(number, amount):
    return LedgerPayment(number, SUPPLIER, "Денежные средства, эквайринг", Decimal(amount), None)


def _ch117_iiko():
    """iiko Ч-117 после шагов 1–2: X оплачена Тепло, сторно на X, «(испр.)» без оплаты."""
    return FakeChainIiko(
        docs={
            "X117": _doc("X117", "Ч-117", "3253.91"),
            "Y117": _doc("Y117", "Ч-117 (испр.)", "2968.94"),
        },
        returns={"R117": _doc("R117", "0004", "3253.91", links_to="X117")},
        payments=[_pay("Ч-117", "3253.91")],
    )


async def _article(session, name):
    return await session.scalar(select(DdsArticle).where(DdsArticle.name == name))


async def _ch117_invoice(session):
    """Чек Ч-117 в Тепле после исправления: товар 2 968,94, питание 1 369,88, содержание 243,94;
    изъятия в iiko проведены ещё на старые суммы — питание 1 084,91, содержание 243,94."""
    cp = await make_counterparty(session, name="Местный закуп", iiko_guid=SUPPLIER)
    inv = await make_invoice(
        session,
        counterparty_id=cp.id,
        amount="4582.76",
        number="Ч-117",
        payment_status="paid",
        source="kassa_cheque",
        external_id="Y117",
        issued_at=datetime(2026, 9, 2, 8, 11, tzinfo=UTC),
        invoice_date=date(2026, 9, 2),
    )
    inv.iiko_correction_new_external_id = "Y117"
    inv.iiko_return_external_id = "R117"
    inv.iiko_return_status = "booked"
    for order, (name, amount) in enumerate(
        ((GOODS, "2968.94"), (FOOD, "1369.88"), (POINTS, "243.94"))
    ):
        article = await _article(session, name)
        session.add(
            InvoiceLineItem(
                invoice_id=inv.id,
                name=name,
                quantity=Decimal("1"),
                price=Decimal(amount),
                sum=Decimal(amount),
                dds_article_id=article.id,
                sort_order=order,
            )
        )
    for name, amount in ((FOOD, "1084.91"), (POINTS, "243.94")):
        article = await _article(session, name)
        session.add(
            ChequeIikoPayout(
                invoice_id=inv.id,
                dds_article_id=article.id,
                source="card",
                pay_out_type_id="PT",
                amount=Decimal(amount),
                status="posted",
            )
        )
    # Оплата X, которую Тепло поставило 02.09 (ключ товарной доли чека).
    session.add(
        IikoInvoicePaymentPush(
            idempotency_key=f"kassa_goods:{inv.id}:card",
            invoice_id=inv.id,
            external_id="X117",
            amount=Decimal("3253.91"),
            account_to="ACQ",
            status="ok",
            attempts=1,
        )
    )
    await session.commit()
    return inv.id


@pytest.fixture
def add_payment_calls(monkeypatch):
    calls: list[dict] = []

    def fake(payload):
        calls.append(payload)
        return 201, {"accountingTransactionId": f"T-{len(calls)}"}

    monkeypatch.setattr(cip, "_call_add_payment", fake)

    async def no_creds(session):
        return None

    monkeypatch.setattr("app.services.iiko_sync._load_source_credential_env", no_creds)
    return calls


@pytest.fixture
def service_product(monkeypatch):
    monkeypatch.setattr(get_settings(), "iiko_correction_service_product_id", "SVC-FOOD")


async def test_cheque_shares_are_expense_growth_over_posted_payouts(async_session_factory):
    """Прирост статей — новые суммы чека минус проведённые изъятия; товар в расход не идёт."""
    async with async_session_factory() as session:
        inv_id = await _ch117_invoice(session)
        shares, problems = await cheque_expense_shares(
            session, await session.get(SupplierInvoice, inv_id)
        )
    assert problems == []
    assert shares == [ExpenseShare(FOOD, "Затраты на персонал", Decimal("284.97"))]


async def test_chain_disabled_sends_nothing(async_session_factory, add_payment_calls):
    """Настройка выключена — «(испр.)» остаётся без оплаты, как было; в iiko ничего не уходит."""
    iiko = _ch117_iiko()
    async with async_session_factory() as session:
        inv_id = await _ch117_invoice(session)
        outcome = await complete_correction_payment(session, inv_id, iiko=iiko, enabled=False)
    assert outcome == "disabled"
    assert add_payment_calls == [] and iiko.acts == []


async def test_chain_pays_correction_by_offset_and_books_difference_as_food(
    async_session_factory, add_payment_calls, service_product
):
    """Ч-117 целиком: «(испр.)» оплачена зачётом 2 968,94 со счёта «Авансы поставщиков», а
    284,97 питания — актом от того же поставщика на «Затраты на персонал». Денег не тратим."""
    iiko = _ch117_iiko()
    async with async_session_factory() as session:
        inv_id = await _ch117_invoice(session)
        outcome = await complete_correction_payment(session, inv_id, iiko=iiko, enabled=True)
        inv = await session.get(SupplierInvoice, inv_id)
        offset = await session.scalar(
            select(IikoInvoicePaymentPush).where(
                IikoInvoicePaymentPush.idempotency_key == f"{OFFSET_KEY_PREFIX}{inv_id}"
            )
        )

    assert outcome == "done"
    assert inv.iiko_return_status == "booked" and inv.iiko_correction_act_external_id == "ACT-1"
    [payment] = add_payment_calls
    assert payment["documentId"] == "Y117"
    assert payment["accountId"] == "ACC-ADVANCES"  # кредит сторно, не эквайринг
    assert payment["amount"] == 2968.94
    assert payment["paymentDate"].startswith("2026-09-02")
    assert offset is not None and offset.status == "ok" and offset.external_id == "Y117"
    [act] = iiko.acts
    assert act["counteragent"] == SUPPLIER
    assert [(i["revenueAccount"], i["sum"], i["product"]) for i in act["items"]] == [
        ("ACC-STAFF", 284.97, "SVC-FOOD")
    ]


async def test_chain_rerun_does_not_pay_or_book_twice(
    async_session_factory, add_payment_calls, service_product
):
    """Повторный запуск (ретрай, кнопка) не ставит второй зачёт и не заводит второй акт."""
    iiko = _ch117_iiko()
    async with async_session_factory() as session:
        inv_id = await _ch117_invoice(session)
        await complete_correction_payment(session, inv_id, iiko=iiko, enabled=True)
        again = await complete_correction_payment(session, inv_id, iiko=iiko, enabled=True)
    assert again == "done"
    assert len(add_payment_calls) == 1 and len(iiko.acts) == 1


async def test_chain_refuses_when_correction_already_paid_by_hand(
    async_session_factory, add_payment_calls, service_product
):
    """«(испр.)» уже оплатили помимо Тепла (так было у всех трёх пар к 06.10) — зачёт сверху
    задвоил бы оплату. Стоп, причина словами, в iiko ничего."""
    iiko = _ch117_iiko()
    iiko.payments.append(_pay("Ч-117 (испр.)", "2968.94"))
    async with async_session_factory() as session:
        inv_id = await _ch117_invoice(session)
        outcome = await complete_correction_payment(session, inv_id, iiko=iiko, enabled=True)
        inv = await session.get(SupplierInvoice, inv_id)
    assert outcome == "blocked"
    assert "уже оплачена" in inv.iiko_return_error
    assert add_payment_calls == [] and iiko.acts == []


async def test_chain_refuses_when_original_payment_is_gone(
    async_session_factory, add_payment_calls, service_product
):
    """Оплату исходной сняли (Ч-116, Ч-123 13.10) — кредита сторно за ней нет, зачёт был бы из
    пустоты. Стоп."""
    iiko = _ch117_iiko()
    iiko.payments.clear()
    async with async_session_factory() as session:
        inv_id = await _ch117_invoice(session)
        outcome = await complete_correction_payment(session, inv_id, iiko=iiko, enabled=True)
        inv = await session.get(SupplierInvoice, inv_id)
    assert outcome == "blocked"
    assert "Оплата исходной" in inv.iiko_return_error
    assert add_payment_calls == []


async def test_chain_waits_for_service_item_then_books_the_act(
    async_session_factory, add_payment_calls, monkeypatch
):
    """Услуги для акта в iiko ещё нет: зачёт стоит, акт ждёт с причиной. Услугу завели — ретрай
    проводит акт и второй зачёт не ставит."""
    monkeypatch.setattr(get_settings(), "iiko_correction_service_product_id", None)
    iiko = _ch117_iiko()
    async with async_session_factory() as session:
        inv_id = await _ch117_invoice(session)
        first = await complete_correction_payment(session, inv_id, iiko=iiko, enabled=True)
        inv = await session.get(SupplierInvoice, inv_id)
        assert first == "blocked" and "услуга" in inv.iiko_return_error
        assert len(add_payment_calls) == 1 and iiko.acts == []

        monkeypatch.setattr(get_settings(), "iiko_correction_service_product_id", "SVC-FOOD")
        second = await complete_correction_payment(session, inv_id, iiko=iiko, enabled=True)
    assert second == "done"
    assert len(add_payment_calls) == 1 and len(iiko.acts) == 1


async def test_chain_stops_on_unbalanced_difference(
    async_session_factory, add_payment_calls, service_product
):
    """Разница не сходится с приростом расходов (кто-то поменял цену) — ничего не ставим."""
    iiko = _ch117_iiko()
    iiko.docs["Y117"] = _doc("Y117", "Ч-117 (испр.)", "2900.00")
    async with async_session_factory() as session:
        inv_id = await _ch117_invoice(session)
        outcome = await complete_correction_payment(session, inv_id, iiko=iiko, enabled=True)
        inv = await session.get(SupplierInvoice, inv_id)
    assert outcome == "blocked" and "не сходится" in inv.iiko_return_error
    assert add_payment_calls == [] and iiko.acts == []


# --- шаг 1: сторно ровно на сумму исходной ----------------------------------------------------


async def test_storno_takes_lines_from_original_in_iiko(async_session_factory, monkeypatch):
    """Сторно строится из строк самой X в iiko — сумма совпадает с X до копейки (раньше сага
    пересчитывала суммы из снимка цен Тепла и промахивалась: 0004 = 3 254,04 при Ч-117 3 253,91)."""
    from test_adjust_paid_invoice import _setup_pending_contour_invoice

    calls: dict = {}
    monkeypatch.setattr(
        wip,
        "_cloud_original_items",
        lambda org, doc: [
            {
                "num": 1,
                "product": "P-1",
                "store": "ST-X",
                "amount": 1.044,
                "price": 308.75,
                "sum": 322.34,
            },
        ],
    )

    def fake_return(org, body):
        calls["return"] = body
        return _CloudPushOutcome("RET-DOC-1", posted=True)

    monkeypatch.setattr(wip, "_cloud_create_and_post_return", fake_return)
    monkeypatch.setattr(
        wip,
        "_cloud_create_and_post",
        lambda direction, org, body: _CloudPushOutcome("NEW-DOC-Y", posted=True, created=True),
    )
    async with async_session_factory() as session:
        inv_id = await _setup_pending_contour_invoice(session, number=f"S{uuid.uuid4().hex[:4]}")
        await book_correction_in_iiko(session, inv_id)

    [item] = calls["return"]["items"]
    assert (item["product"], item["amount"], item["sum"], item["store"]) == (
        "P-1",
        1.044,
        322.34,
        "ST-X",
    )


async def test_saga_hands_over_to_chain_after_new_invoice(async_session_factory, monkeypatch):
    """После «(испр.)» сага сама зовёт шаги 3–4 — человек ничего не нажимает."""
    from test_adjust_paid_invoice import _setup_pending_contour_invoice, _wire_contour_mocks

    seen: list[uuid.UUID] = []

    async def fake_chain(session, invoice_id, **kwargs):
        seen.append(invoice_id)
        return "done"

    monkeypatch.setattr(
        "app.services.iiko_correction_chain.complete_correction_payment", fake_chain
    )
    _wire_contour_mocks(monkeypatch, {})
    async with async_session_factory() as session:
        inv_id = await _setup_pending_contour_invoice(session, number=f"H{uuid.uuid4().hex[:4]}")
        await book_correction_in_iiko(session, inv_id)
    assert seen == [inv_id]


# --- сверка и сторож знают новую схему ---------------------------------------------------------


async def _offset_push(session, inv_id, amount="2968.94", *, age_minutes=60):
    from datetime import timedelta

    row = IikoInvoicePaymentPush(
        idempotency_key=f"{OFFSET_KEY_PREFIX}{inv_id}",
        invoice_id=inv_id,
        external_id="Y117",
        amount=Decimal(amount),
        account_to="ACC-ADVANCES",
        status="ok",
        attempts=1,
    )
    session.add(row)
    await session.flush()
    row.created_at = datetime.now(UTC) - timedelta(minutes=age_minutes)
    await session.commit()
    return row


@pytest.mark.parametrize(
    ("olap", "verified"),
    [([("Ч-117 (испр.)", Decimal("2968.94"))], True), ([("Ч-117", Decimal("3253.91"))], False)],
)
async def test_verify_checks_offset_under_correction_number(
    async_session_factory, monkeypatch, olap, verified
):
    """Зачёт на «(испр.)» сверка ищет под номером «(испр.)»: оплата исходной с тем же нашим
    номером его подтверждать не должна."""
    from app.services import iiko_payment_verify as verify_mod

    monkeypatch.setattr(verify_mod, "fetch_invoice_payment_transactions", lambda a, b: olap)
    async with async_session_factory() as session:
        inv_id = await _ch117_invoice(session)
        row = await _offset_push(session, inv_id)
        await verify_mod.verify_mirrored_payments(session)
        await session.refresh(row)
    assert (row.verified_at is not None) is verified


@pytest.mark.parametrize(
    ("y_payments", "code"),
    [
        (["2968.94"], None),  # «(испр.)» оплачена ровно зачётом Тепла — по схеме
        (["2968.94", "2968.94"], "correction_paid"),  # сверх зачёта кто-то оплатил — задвоение
        ([], "correction_offset_removed"),  # зачёт сняли в iiko
    ],
)
async def test_watch_follows_new_scheme_when_teplo_paid_correction(
    async_session_factory, y_payments, code
):
    """Пара по новой схеме: «(испр.)» оплачена зачётом Тепла — это норма; лишняя оплата сверх
    него — задвоение; пропавший зачёт — снятая оплата."""
    from test_iiko_payment_watch import FakeIiko

    from app.services.iiko_payment_watch import CASE_KIND, watch_iiko_payments

    iiko = FakeIiko(
        payments=[_pay("Ч-117", "3253.91"), *(_pay("Ч-117 (испр.)", a) for a in y_payments)],
        incoming_docs={
            "X117": _doc("X117", "Ч-117", "3253.91"),
            "Y117": _doc("Y117", "Ч-117 (испр.)", "2968.94"),
        },
        returned_docs={"R117": _doc("R117", "0004", "3253.91", links_to="X117")},
    )
    async with async_session_factory() as session:
        inv_id = await _ch117_invoice(session)
        await _offset_push(session, inv_id, age_minutes=60 * 24 * 40)
        await watch_iiko_payments(session, source=iiko)
        from app.models import ReconciliationCase

        cases = (
            await session.scalars(
                select(ReconciliationCase).where(ReconciliationCase.kind == CASE_KIND)
            )
        ).all()
    if code is None:
        assert cases == []
    else:
        [case] = cases
        assert [d["code"] for d in case.payload["deviations"]] == [code]
