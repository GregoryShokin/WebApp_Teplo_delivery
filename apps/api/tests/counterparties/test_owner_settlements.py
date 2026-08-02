"""Расчёты с собственниками на «Странице ДЗ/КЗ»: входящий остаток плюс выданный займ.

СЦЕНАРИЙ — НАСТОЯЩИЙ (владелец, 02.08.2026). На 01.07.2026 собственники должны бизнесу:
Григорий 1 020 000 ₽, Павел 200 000 ₽. Четырнадцатого июля Павлу выдан займ ещё на 30 000 ₽
банковским переводом, и в проводке не был указан контрагент — механизма собственников тогда не
существовало. После указания контрагента у Павла обязано стать 230 000 ₽, у Григория —
1 020 000 ₽, и обе суммы обязаны быть видны на «Странице ДЗ/КЗ».

ЧТО ЗДЕСЬ ПРОВЕРЯЕТСЯ ПО СУЩЕСТВУ:

* направление долга. В наших книгах это ДЕБИТОРКА: деньги ушли из бизнеса, и должны нам. Знак
  здесь легко перепутать — владелец называет тот же факт «кредиторской задолженностью перед
  бизнесом», глядя со стороны собственника, — а перепутанный знак меняет баланс на два долга;
* займ становится долгом САМ, простым указанием контрагента: правило 1 канона превращает
  свободный платёж в дебиторку. Никакой отдельной кнопки «оформить займ» для этого не нужно;
* входящий остаток НЕ создаёт проводки: деньги ушли до внедрения системы, и вторая проводка
  задвоила бы расход в ДДС.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date
from decimal import Decimal

from cp_helpers import make_counterparty
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import BusinessOwner, CashflowTransaction, CounterpartyRole, DdsArticle, Wallet
from app.services import supplier_prepayments
from app.services.owner_analytics import OWNER_ROLE

HEADERS = {"X-User-Role": "admin"}

# Числа владельца на 01.07.2026.
OPENING = {"Григорий": Decimal("1020000.00"), "Павел": Decimal("200000.00")}
LOAN_AMOUNT = Decimal("30000.00")


def _run(coro):
    return asyncio.run(coro)


async def _owner(session: AsyncSession, name: str, opening: Decimal) -> uuid.UUID:
    person = await make_counterparty(
        session, name=name, inn=None, cp_type="individual", relationship="informal"
    )
    session.add(CounterpartyRole(counterparty_id=person.id, role=OWNER_ROLE))
    session.add(
        BusinessOwner(
            counterparty_id=person.id,
            share_percent=Decimal("50"),
            started_on=date(2026, 1, 1),
        )
    )
    await session.flush()
    await supplier_prepayments.create_opening_prepayment(
        session,
        counterparty_id=person.id,
        amount=opening,
        kind="owner_loan",
        note="Входящий остаток на 01.07.2026",
    )
    return person.id


def _seed(factory: async_sessionmaker[AsyncSession]) -> dict[str, str]:
    async def go() -> dict[str, str]:
        async with factory() as session:
            ids = {name: await _owner(session, name, amount) for name, amount in OPENING.items()}
            wallet = Wallet(code="owner_loan_bank", name="Т-Банк", type="bank_account")
            article = DdsArticle(
                code="owner_loan_issue",
                name="Выдача кредитов и займов",
                movement_type="outflow",
                activity_type="investing",
            )
            session.add_all([wallet, article])
            await session.flush()
            # Ровно та проводка, что лежит на проде: банковская выписка, контрагент не указан.
            txn = CashflowTransaction(
                wallet_id=wallet.id,
                direction="out",
                amount=LOAN_AMOUNT,
                operation_date=date(2026, 7, 14),
                article_id=article.id,
                source_kind="bank_operation",
                payment_purpose="Перевод собственных средств на карту ИП! З Павел",
                quality_status="owner_review",
            )
            session.add(txn)
            await session.commit()
            return {
                "pavel": str(ids["Павел"]),
                "grigoriy": str(ids["Григорий"]),
                "txn": str(txn.id),
                "article": str(article.id),
            }

    return _run(go())


def _receivable(client: TestClient, counterparty_id: str) -> Decimal:
    response = client.get("/api/v1/accounting/suppliers/balances", headers=HEADERS)
    assert response.status_code == 200, response.text
    for item in response.json()["items"]:
        if item["counterparty_id"] == counterparty_id:
            return Decimal(str(item["receivable"]))
    return Decimal("0")


def test_owner_debt_shows_up_and_grows_with_the_loan(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Входящий остаток виден сразу; указание контрагента на займе добавляет 30 000 Павлу."""
    seeded = _seed(async_session_factory)

    assert _receivable(client, seeded["grigoriy"]) == Decimal("1020000.00")
    assert _receivable(client, seeded["pavel"]) == Decimal("200000.00")

    response = client.patch(
        f"/api/v1/dds/transactions/{seeded['txn']}",
        json={"article_id": seeded["article"], "counterparty_id": seeded["pavel"]},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text

    # 200 000 входящего остатка + 30 000 июльского займа. Долг вырос сам: правило 1 превращает
    # свободный платёж контрагенту в дебиторку, отдельной кнопки «оформить займ» не нужно.
    assert _receivable(client, seeded["pavel"]) == Decimal("230000.00")
    assert _receivable(client, seeded["grigoriy"]) == Decimal("1020000.00")


def test_opening_balance_does_not_create_a_cashflow(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Входящий остаток — не платёж. Проводки под него нет и быть не должно.

    Деньги ушли до внедрения системы. Заведи под остаток проводку — и расход в ДДС задвоится:
    один раз исторически, второй раз сегодняшним числом.
    """
    seeded = _seed(async_session_factory)

    async def check() -> tuple[int, bool]:
        async with async_session_factory() as session:
            rows = (
                await session.scalars(
                    select(CashflowTransaction).where(
                        CashflowTransaction.counterparty_id == uuid.UUID(seeded["grigoriy"])
                    )
                )
            ).all()
            prepayment = await session.scalar(
                select(supplier_prepayments.SupplierPrepayment).where(
                    supplier_prepayments.SupplierPrepayment.counterparty_id
                    == uuid.UUID(seeded["grigoriy"])
                )
            )
            return len(rows), bool(prepayment and prepayment.opening)

    transactions, is_opening = _run(check())
    assert transactions == 0, "входящий остаток не двигает деньги"
    assert is_opening, "остаток обязан быть помечен входящим — иначе сверка сочтёт его платежом"
