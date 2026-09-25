"""Дивиденды — выплата собственнику, а не его долг бизнесу (решение владельца 25.09.2026).

РИСК, РАДИ КОТОРОГО ФАЙЛ. Правило 1 канона превращает свободный платёж контрагенту с
payable-профилем в дебиторку и по статье не смотрит. У собственников Григория и Павла профиль
есть — через него растёт их настоящий долг: заём (``vydacha_kreditov_i_zaimov``) обязан стать ДЗ
простым указанием контрагента (``test_owner_settlements``). Но той же дверью проходят и
дивиденды. На проде 25.09 лежат три выплаты дивидендов без контрагента (статья требует
собственника, проводки в ``owner_review``): банковская 24.06 на 80 000 ₽, импорт шаблона 24.06
на 30 000 ₽ и выдача из Сейфа 16.07 на 80 000 ₽. Стоит человеку назвать в первых двух
собственника — и правило 1 заведёт ему «долг» на 110 000 ₽: баланс на дату и «Остатки» покажут
распределение прибыли дебиторкой. Сейф долгом не станет и сегодня (адресный контур, у
собственников нет режима услуг), но гейт по контуру денег — случайность, а не правило.

Отбор по СТАТЬЕ, а не по контрагенту: собственник бывает бизнесу и арендодателем, и
подрядчиком (``owner_analytics``), и его аренда обязана гасить свою кредиторку, а переплата —
становиться дебиторкой, как у всех.

Каждый тест проходит отдельную дверь правила 1; все они сходятся в
``supplier_prepayments._sync_rule1_distribution``, и гейт стоит там, а не в каждой двери.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date
from decimal import Decimal

import pytest
from cp_helpers import make_account, make_bank_operation, make_counterparty, make_invoice
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    BusinessOwner,
    CashflowTransaction,
    CounterpartyPayableProfile,
    CounterpartyRole,
    DdsArticle,
    InvoicePaymentAllocation,
    SupplierInvoice,
    SupplierPrepayment,
    Wallet,
)
from app.services.banking.cashflow_classify import CashflowSplitLine, apply_cashflow_split
from app.services.owner_analytics import DIVIDENDS_ARTICLE_CODE, OWNER_ROLE

HEADERS = {"X-User-Role": "admin"}

DIVIDENDS = Decimal("80000.00")
LOAN = Decimal("30000.00")
RENT_ACT = Decimal("20000.00")


def _run(coro):
    return asyncio.run(coro)


async def _owner(session: AsyncSession, name: str) -> uuid.UUID:
    # Как на проде: у собственника есть payable-профиль (make_counterparty его заводит) — через
    # него правило 1 и растит его заём.
    person = await make_counterparty(
        session, name=name, inn=None, cp_type="individual", relationship="informal"
    )
    session.add(CounterpartyRole(counterparty_id=person.id, role=OWNER_ROLE))
    session.add(
        BusinessOwner(
            counterparty_id=person.id, share_percent=Decimal("50"), started_on=date(2026, 1, 1)
        )
    )
    await session.flush()
    return person.id


def _seed(factory: async_sessionmaker[AsyncSession]) -> dict[str, uuid.UUID]:
    async def go() -> dict[str, uuid.UUID]:
        async with factory() as session:
            account = await make_account(session)
            bank = Wallet(
                code="owner_div_bank", name="Т-Банк", type="bank_account", account_id=account.id
            )
            safe = Wallet(code="owner_div_safe", name="Сейф", type="cash_safe")
            # Статьи собственника — из каталога, который заводят миграции (0114, 0247, 0249), как
            # на проде: признак дивидендов живёт в коде статьи.
            dividends = await session.scalar(
                select(DdsArticle).where(DdsArticle.code == DIVIDENDS_ARTICLE_CODE)
            )
            loan = await session.scalar(
                select(DdsArticle).where(DdsArticle.code == "vydacha_kreditov_i_zaimov")
            )
            assert dividends is not None and dividends.owner_required
            assert loan is not None and loan.owner_required
            rent = DdsArticle(
                code="owner_div_rent",
                name="Аренда",
                movement_type="outflow",
                activity_type="operating",
            )
            session.add_all([bank, safe, rent])
            await session.flush()
            ids = {
                "account": account.id,
                "bank": bank.id,
                "safe": safe.id,
                "dividends": dividends.id,
                "loan": loan.id,
                "rent": rent.id,
                "grigoriy": await _owner(session, "Григорий"),
                "pavel": await _owner(session, "Павел"),
            }
            await session.commit()
            return ids

    return _run(go())


def _add_txn(
    factory: async_sessionmaker[AsyncSession],
    *,
    wallet_id: uuid.UUID,
    article_id: uuid.UUID,
    amount: Decimal,
    source_kind: str,
    operation_date: date,
    counterparty_id: uuid.UUID | None = None,
) -> uuid.UUID:
    async def go() -> uuid.UUID:
        async with factory() as session:
            txn = CashflowTransaction(
                wallet_id=wallet_id,
                direction="out",
                amount=amount,
                operation_date=operation_date,
                article_id=article_id,
                counterparty_id=counterparty_id,
                source_kind=source_kind,
                payment_purpose="Выплата собственнику",
                # Как на проде: статья требует собственника, а его нет — проводка ждёт разбора.
                quality_status="owner_review" if counterparty_id is None else "auto",
            )
            session.add(txn)
            await session.commit()
            return txn.id

    return _run(go())


def _prepayments(
    factory: async_sessionmaker[AsyncSession], counterparty_id: uuid.UUID
) -> list[tuple[str, Decimal]]:
    async def go() -> list[tuple[str, Decimal]]:
        async with factory() as session:
            rows = (
                await session.scalars(
                    select(SupplierPrepayment).where(
                        SupplierPrepayment.counterparty_id == counterparty_id
                    )
                )
            ).all()
            return sorted((row.kind, Decimal(row.amount)) for row in rows)

    return _run(go())


def _receivable(client: TestClient, counterparty_id: uuid.UUID) -> Decimal:
    response = client.get("/api/v1/accounting/suppliers/balances", headers=HEADERS)
    assert response.status_code == 200, response.text
    for item in response.json()["items"]:
        if item["counterparty_id"] == str(counterparty_id):
            return Decimal(str(item["receivable"]))
    return Decimal("0")


def _set_service_billing(
    factory: async_sessionmaker[AsyncSession], *counterparty_ids: uuid.UUID
) -> None:
    async def go() -> None:
        async with factory() as session:
            for profile in (
                await session.scalars(
                    select(CounterpartyPayableProfile).where(
                        CounterpartyPayableProfile.counterparty_id.in_(counterparty_ids)
                    )
                )
            ).all():
                profile.service_billing_mode = "fixed_tariff"
            await session.commit()

    _run(go())


def _patch(
    client: TestClient, txn_id: uuid.UUID, *, article_id: uuid.UUID, counterparty_id: uuid.UUID
) -> None:
    response = client.patch(
        f"/api/v1/dds/transactions/{txn_id}",
        json={"article_id": str(article_id), "counterparty_id": str(counterparty_id)},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text


# --- Сценарий прода: собственника называют в проводке, которая лежит без него ---------------


@pytest.mark.parametrize(
    "source_kind",
    [
        # Банковская выплата 24.06 на 80 000 ₽: правило 1 идёт прямым путём.
        "bank_operation",
        # Импорт шаблона 24.06 на 30 000 ₽: ручная дверь, деньги «свободны» — не адресный контур.
        "template_import",
        # Ручная проводка — та же ручная дверь.
        "manual",
        # Выдача из Сейфа: адресный контур, и дебиторкой она становится, лишь когда у
        # контрагента размечен режим услуг. У собственников его сегодня нет — поэтому 80 000 ₽
        # из Сейфа 16.07 и не станут долгом. Размечаем: гейт обязан держать и тогда.
        "safe_payout",
    ],
)
def test_named_owner_on_dividends_is_not_a_debt(
    client: TestClient,
    async_session_factory: async_sessionmaker[AsyncSession],
    source_kind: str,
) -> None:
    """Указали собственника на выплате дивидендов — дебиторки нет, баланс собственника пуст.

    Контроль в том же тесте: заём Павлу той же дверью по-прежнему становится долгом. Без него
    тест прошёл бы и на правиле 1, которое вообще перестало работать для собственников.
    """
    seeded = _seed(async_session_factory)
    if source_kind == "safe_payout":
        _set_service_billing(async_session_factory, seeded["grigoriy"], seeded["pavel"])
    wallet = seeded["bank"] if source_kind == "bank_operation" else seeded["safe"]
    dividends_txn = _add_txn(
        async_session_factory,
        wallet_id=wallet,
        article_id=seeded["dividends"],
        amount=DIVIDENDS,
        source_kind=source_kind,
        operation_date=date(2026, 7, 24),
    )
    loan_txn = _add_txn(
        async_session_factory,
        wallet_id=wallet,
        article_id=seeded["loan"],
        amount=LOAN,
        source_kind=source_kind,
        operation_date=date(2026, 7, 14),
    )

    _patch(
        client, dividends_txn, article_id=seeded["dividends"], counterparty_id=seeded["grigoriy"]
    )
    _patch(client, loan_txn, article_id=seeded["loan"], counterparty_id=seeded["pavel"])

    assert _prepayments(async_session_factory, seeded["grigoriy"]) == [], (
        "дивиденды стали дебиторкой собственника"
    )
    assert _receivable(client, seeded["grigoriy"]) == Decimal("0")
    # Заём — долг, как и прежде: гейт по статье, а не по собственнику.
    assert _prepayments(async_session_factory, seeded["pavel"]) == [("subscription", LOAN)]
    assert _receivable(client, seeded["pavel"]) == LOAN


# --- Переразметка: статья меняется туда и обратно, собственник — ещё и арендодатель ----------


def _act_is_paid(state: tuple[str, Decimal]) -> bool:
    status, paid = state
    return status == "paid" and paid == RENT_ACT


@pytest.mark.parametrize("source_kind", ["bank_operation", "manual"])
def test_dividends_do_not_pay_owners_rent_and_reclassification_unwinds(
    client: TestClient,
    async_session_factory: async_sessionmaker[AsyncSession],
    source_kind: str,
) -> None:
    """Правило 1 не распоряжается дивидендами вовсе — ни дебиторкой, ни зачётом кредиторки.

    Григорий сдаёт бизнесу помещение, и у него открыт акт аренды на 20 000 ₽. Платёж ему на
    50 000 ₽ со статьёй аренды гасит акт и даёт 30 000 ₽ переплаты в ДЗ — это правило 1 как
    есть. Переразметили тот же платёж в «Дивиденды» — акт снова ждёт оплаты, переплаты нет:
    распределение прибыли не оплачивает аренду. Вернули статью аренды — всё возвращается,
    пересборка обратима.

    Ручная дверь здесь важна отдельно: она сначала спрашивает, свободны ли деньги
    (``manual_payment_money_is_free``), и гейт, поставленный туда, промолчал бы — дебиторка,
    заведённая до переразметки, осталась бы висеть.
    """
    seeded = _seed(async_session_factory)
    grigoriy = seeded["grigoriy"]
    payment = Decimal("50000.00")

    async def rent_act() -> uuid.UUID:
        async with async_session_factory() as session:
            act = await make_invoice(
                session,
                counterparty_id=grigoriy,
                amount=RENT_ACT,
                invoice_date=date(2026, 7, 1),
                operational_scope="finance",
            )
            await session.commit()
            return act.id

    act_id = _run(rent_act())
    txn_id = _add_txn(
        async_session_factory,
        wallet_id=seeded["bank"] if source_kind == "bank_operation" else seeded["safe"],
        article_id=seeded["rent"],
        amount=payment,
        source_kind=source_kind,
        operation_date=date(2026, 7, 20),
    )

    async def act_state() -> tuple[str, Decimal]:
        async with async_session_factory() as session:
            act = await session.get(SupplierInvoice, act_id)
            assert act is not None
            paid = await session.scalar(
                select(InvoicePaymentAllocation.amount).where(
                    InvoicePaymentAllocation.invoice_id == act_id,
                    InvoicePaymentAllocation.cashflow_transaction_id == txn_id,
                )
            )
            return act.payment_status, Decimal(paid or 0)

    _patch(client, txn_id, article_id=seeded["rent"], counterparty_id=grigoriy)
    assert _act_is_paid(_run(act_state()))
    assert _prepayments(async_session_factory, grigoriy) == [("subscription", payment - RENT_ACT)]

    _patch(client, txn_id, article_id=seeded["dividends"], counterparty_id=grigoriy)
    status, paid = _run(act_state())
    assert paid == Decimal("0"), "дивиденды оплатили аренду собственника"
    assert status == "unpaid"
    assert _prepayments(async_session_factory, grigoriy) == []
    assert _receivable(client, grigoriy) == Decimal("0")

    _patch(client, txn_id, article_id=seeded["rent"], counterparty_id=grigoriy)
    assert _act_is_paid(_run(act_state()))
    assert _prepayments(async_session_factory, grigoriy) == [("subscription", payment - RENT_ACT)]


# --- Разбор по статьям: одна проводка, дивиденды и заём долями --------------------------------


def test_manual_split_keeps_dividends_share_off_receivables(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Ручной разбор: доля «Дивиденды» долгом не становится, доля займа — становится."""
    seeded = _seed(async_session_factory)
    pavel = seeded["pavel"]
    txn_id = _add_txn(
        async_session_factory,
        wallet_id=seeded["safe"],
        article_id=seeded["dividends"],
        amount=DIVIDENDS + LOAN,
        source_kind="manual",
        operation_date=date(2026, 8, 19),
    )

    async def split() -> None:
        async with async_session_factory() as session:
            txn = await session.get(CashflowTransaction, txn_id)
            assert txn is not None
            await apply_cashflow_split(
                session,
                txn,
                splits=[
                    CashflowSplitLine(seeded["dividends"], DIVIDENDS, counterparty_id=pavel),
                    CashflowSplitLine(seeded["loan"], LOAN, counterparty_id=pavel),
                ],
            )
            await session.commit()

    _run(split())
    assert _prepayments(async_session_factory, pavel) == [("subscription", LOAN)]


def test_bank_operation_split_keeps_dividends_share_off_receivables(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Разбор операции выписки (классификатор): та же граница, что и у ручного разбора."""
    seeded = _seed(async_session_factory)
    pavel = seeded["pavel"]

    async def operation() -> uuid.UUID:
        async with async_session_factory() as session:
            op = await make_bank_operation(
                session,
                amount=DIVIDENDS + LOAN,
                direction="out",
                account_id=seeded["account"],
                name="Павел",
                operation_date=date(2026, 8, 19),
            )
            await session.commit()
            return op.id

    op_id = _run(operation())
    response = client.post(
        f"/api/v1/dds/operations/{op_id}/classify",
        json={
            "action": "split",
            "splits": [
                {
                    "article_id": str(seeded["dividends"]),
                    "amount": str(DIVIDENDS),
                    "counterparty_id": str(pavel),
                },
                {
                    "article_id": str(seeded["loan"]),
                    "amount": str(LOAN),
                    "counterparty_id": str(pavel),
                },
            ],
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    assert _prepayments(async_session_factory, pavel) == [("subscription", LOAN)]
    assert _receivable(client, pavel) == LOAN
