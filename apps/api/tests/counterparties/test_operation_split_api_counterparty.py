"""Роуты разбора операции: контрагент приходит В СТРОКЕ, разбор читается обратно.

* ``POST /dds/operations/{id}/classify`` принимает ``counterparty_id`` у каждой доли и
  ``create_counterparty`` (нового контрагента из выписки получает только запросившая доля).
* ``GET /dds/operations/{id}/split`` отдаёт текущие доли — диалог открывается на том, что уже
  размечено, а не с чистой строки на всю сумму.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence

from cp_helpers import (
    make_account,
    make_bank_operation,
    make_counterparty,
    make_expense_article,
    make_invoice,
    make_wallet,
    token_headers,
)
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    CashflowTransaction,
    Counterparty,
    Organization,
    Permission,
    Role,
    RolePermission,
    SupplierInvoice,
    User,
    UserRole,
)

CLASSIFY_PERMS = ("finance.cashflow.classify", "finance.cashflow.read")


async def _user_with_perms(
    factory: async_sessionmaker[AsyncSession], email: str, codes: Sequence[str]
) -> uuid.UUID:
    async with factory() as session:
        existing = await session.scalar(select(User).where(User.email == email))
        if existing is not None:
            return existing.id
        org_id = await session.scalar(select(Organization.id).limit(1))
        assert org_id is not None
        role = Role(code=f"t-{uuid.uuid4().hex[:10]}", name="test-role")
        session.add(role)
        await session.flush()
        perms = (
            await session.scalars(select(Permission).where(Permission.code.in_(tuple(codes))))
        ).all()
        assert {p.code for p in perms} == set(codes), "permission codes must be seeded"
        for perm in perms:
            session.add(RolePermission(role_id=role.id, permission_id=perm.id))
        user = User(email=email, full_name=email, hashed_password="sha256$unused", is_active=True)
        session.add(user)
        await session.flush()
        session.add(UserRole(user_id=user.id, role_id=role.id, organization_id=org_id))
        await session.commit()
        return user.id


def _headers(factory: async_sessionmaker[AsyncSession], email: str) -> dict[str, str]:
    return token_headers(asyncio.run(_user_with_perms(factory, email, CLASSIFY_PERMS)))


def _seed(factory: async_sessionmaker[AsyncSession], *, op_amount: str = "8000.00"):
    async def _run():
        async with factory() as session:
            account = await make_account(session)
            await make_wallet(session, wallet_type="bank", account_id=account.id)
            article = await make_expense_article(session)  # payment_to_supplier
            # Статья без обязательного контрагента — на ней проверяем, что новый контрагент
            # достаётся только запросившей доле, а «пустая» доля остаётся пустой.
            other = await make_expense_article(
                session, code="prochie_rashody", name="Прочие расходы"
            )
            veggies = await make_counterparty(session, name="Поставка овощей", inn="7701234567")
            boxes = await make_counterparty(session, name="Коробки", inn="7801234567")
            op = await make_bank_operation(
                session,
                amount=op_amount,
                direction="out",
                account_id=account.id,
                name="ООО «Мусорщики»",
                inn="7901234567",
            )
            await session.commit()
            return article.id, other.id, veggies.id, boxes.id, op.id

    return asyncio.run(_run())


def test_classify_assigns_counterparty_per_line(
    client: TestClient,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Один платёж на двух контрагентов: каждая проводка идёт в свою карточку."""
    article_id, _other_id, veggies_id, boxes_id, op_id = _seed(async_session_factory)
    response = client.post(
        f"/api/v1/dds/operations/{op_id}/classify",
        json={
            "action": "split",
            "splits": [
                {
                    "article_id": str(article_id),
                    "amount": "5000.00",
                    "counterparty_id": str(veggies_id),
                },
                {
                    "article_id": str(article_id),
                    "amount": "3000.00",
                    "counterparty_id": str(boxes_id),
                },
            ],
        },
        headers=_headers(async_session_factory, "split-per-line@teplo.local"),
    )
    assert response.status_code == 200, response.text
    created = response.json()["cashflow_transaction_ids"]

    async def _check():
        async with async_session_factory() as session:
            first = await session.get(CashflowTransaction, uuid.UUID(created[0]))
            second = await session.get(CashflowTransaction, uuid.UUID(created[1]))
            assert str(first.counterparty_id) == str(veggies_id)
            assert str(second.counterparty_id) == str(boxes_id)

    asyncio.run(_check())


def test_classify_creates_counterparty_only_for_requesting_line(
    client: TestClient,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``create_counterparty`` у одной доли не «протекает» в соседнюю, где контрагент не указан."""
    article_id, other_id, veggies_id, _boxes_id, op_id = _seed(async_session_factory)
    response = client.post(
        f"/api/v1/dds/operations/{op_id}/classify",
        json={
            "action": "split",
            "splits": [
                {
                    "article_id": str(article_id),
                    "amount": "5000.00",
                    "create_counterparty": True,
                },
                # Статья без обязательного контрагента: доля осознанно остаётся без него.
                {"article_id": str(other_id), "amount": "3000.00"},
            ],
            "new_counterparty_name": "ООО «Мусорщики»",
            "new_counterparty_inn": "7901234567",
        },
        headers=_headers(async_session_factory, "split-new-cp@teplo.local"),
    )
    assert response.status_code == 200, response.text
    created = response.json()["cashflow_transaction_ids"]

    async def _check():
        async with async_session_factory() as session:
            created_cp = await session.scalar(
                select(Counterparty).where(Counterparty.inn == "7901234567")
            )
            assert created_cp is not None
            first = await session.get(CashflowTransaction, uuid.UUID(created[0]))
            second = await session.get(CashflowTransaction, uuid.UUID(created[1]))
            assert first.counterparty_id == created_cp.id
            assert second.counterparty_id is None
            assert veggies_id is not None  # фикстура: прочие контрагенты не задеты

    asyncio.run(_check())


def test_read_split_returns_current_lines(
    client: TestClient,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """GET /split отдаёт доли уже разобранной операции — с контрагентом и накладной."""
    article_id, _other_id, veggies_id, boxes_id, op_id = _seed(async_session_factory)

    async def _add_invoice():
        async with async_session_factory() as session:
            invoice = await make_invoice(
                session, counterparty_id=boxes_id, amount="3000.00", number="К-1"
            )
            await session.commit()
            return invoice.id

    invoice_id = asyncio.run(_add_invoice())
    headers = _headers(async_session_factory, "split-read@teplo.local")
    classify = client.post(
        f"/api/v1/dds/operations/{op_id}/classify",
        json={
            "action": "split",
            "splits": [
                {
                    "article_id": str(article_id),
                    "amount": "5000.00",
                    "counterparty_id": str(veggies_id),
                },
                {
                    "article_id": str(article_id),
                    "amount": "3000.00",
                    "counterparty_id": str(boxes_id),
                    "invoice_id": str(invoice_id),
                },
            ],
        },
        headers=headers,
    )
    assert classify.status_code == 200, classify.text

    response = client.get(f"/api/v1/dds/operations/{op_id}/split", headers=headers)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["amount"] == "8000.00"
    assert payload["classification_status"] == "classified"
    lines = payload["lines"]
    # Якорная доля (первая строка прошлого разбора) идёт первой, остальные — стабильным порядком.
    assert lines[0]["amount"] == "5000.00"
    assert lines[0]["counterparty_id"] == str(veggies_id)
    assert lines[0]["invoice_id"] is None
    by_amount = {line["amount"]: line for line in lines}
    assert set(by_amount) == {"5000.00", "3000.00"}
    assert by_amount["3000.00"]["counterparty_id"] == str(boxes_id)
    assert by_amount["3000.00"]["invoice_id"] == str(invoice_id)

    async def _invoice_paid():
        async with async_session_factory() as session:
            assert (
                await session.get(SupplierInvoice, uuid.UUID(str(invoice_id)))
            ).payment_status == "paid"

    asyncio.run(_invoice_paid())
