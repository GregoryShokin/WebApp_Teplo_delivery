"""Ручная привязка карт-оплаты к накладной + атомарность разноса ДДС.

Контур: местный закуп часто оплачивается КАРТОЙ. В банке получатель такой операции — эквайер
(``_is_card_noise``), поэтому авто-подбор её к накладной не делает, а guard режет привязку.
Оператор должен иметь возможность привязать оплату к накладной ВРУЧНУЮ, явно подтвердив это
(``allow_card``). Проверяем:

* без ``allow_card`` карт-привязка отклоняется, и разнос НЕ применяется даже частично (атомарность);
* с ``allow_card`` привязка проходит: аллокация создаётся, накладная гасится, но контрагент
  реквизитами эквайера НЕ обогащается;
* авто-подсказки (``suggest_invoice_matches``) карт-шум по-прежнему исключают;
* роут разноса и ``/warehouse/match/confirm`` принимают ``allow_card`` и проверяют право оплаты
  накладной (``invoices.{normal|barter}.pay``); карт-привязка не запоминается правилом.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence
from decimal import Decimal

import pytest
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
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    BankOperation,
    CashflowTransaction,
    ClassificationRule,
    Counterparty,
    InvoicePaymentAllocation,
    Organization,
    Permission,
    ReconciliationCase,
    Role,
    RolePermission,
    SupplierInvoice,
    User,
    UserRole,
)
from app.services.banking.classifier import apply_operation_split
from app.services.counterparty_bank_match import confirm_invoice_match, suggest_invoice_matches
from app.services.counterparty_matching import CounterpartyMatchError

ACQUIRER_RECEIVER = {"name": "АО ТБанк (эквайер)", "inn": "7710140679"}


async def _card_fixture(
    session: AsyncSession,
    *,
    op_amount: str = "1750.00",
    inv_amount: str = "1750.00",
    with_case: bool = False,
    cp_name: str = "Местный закуп",
    cp_inn: str = "7701234567",
):
    """Карт-операция (category=cardOperation) + неоплаченная накладная контрагента + кошелёк."""
    account = await make_account(session)
    await make_wallet(session, wallet_type="bank", account_id=account.id)
    article = await make_expense_article(session)  # code=payment_to_supplier
    cp = await make_counterparty(session, name=cp_name, inn=cp_inn)
    invoice = await make_invoice(
        session, counterparty_id=cp.id, amount=inv_amount, number="78787706"
    )
    op = await make_bank_operation(
        session,
        amount=op_amount,
        direction="out",
        account_id=account.id,
        category="cardOperation",
        receiver=ACQUIRER_RECEIVER,
        classification_status="needs_review",
    )
    if with_case:
        session.add(
            ReconciliationCase(
                kind="unclassified_operation",
                status="pending",
                provider="tbank",
                bank_operation_id=op.id,
            )
        )
        await session.flush()
    return article, cp, invoice, op


async def _alloc_count(session: AsyncSession, operation_id) -> int:
    return await session.scalar(
        select(func.count())
        .select_from(InvoicePaymentAllocation)
        .where(InvoicePaymentAllocation.bank_operation_id == operation_id)
    )


async def _op_cashflow_count(session: AsyncSession, operation_id) -> int:
    return await session.scalar(
        select(func.count())
        .select_from(CashflowTransaction)
        .where(
            CashflowTransaction.source_kind == "bank_operation",
            CashflowTransaction.source_id == operation_id,
        )
    )


# --- сервис: apply_operation_split -------------------------------------------


async def test_split_card_without_allow_card_rejected_and_no_writes(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Атомарность: карт-операция + накладная без allow_card → отказ И ни одной записи
    (проводка не создана, аллокации нет, накладная unpaid, статус операции не тронут)."""
    async with async_session_factory() as session:
        article, cp, invoice, op = await _card_fixture(session)
        await session.commit()
        inv_id, op_id = invoice.id, op.id

        with pytest.raises(ValueError, match="исходящим платежом поставщику"):
            await apply_operation_split(
                session,
                op,
                splits=[(article.id, Decimal("1750.00"), None, inv_id)],
                counterparty_id=cp.id,
            )

        assert await _op_cashflow_count(session, op_id) == 0
        assert await _alloc_count(session, op_id) == 0
        assert (await session.get(SupplierInvoice, inv_id)).payment_status == "unpaid"
        assert (await session.get(BankOperation, op_id)).classification_status == "needs_review"


async def test_split_card_with_allow_card_pays_invoice(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """allow_card=True: карт-оплата привязывается к накладной, накладная гасится, операция
    classified. Контрагент при этом НЕ трогается (split не обогащает реквизитами эквайера)."""
    async with async_session_factory() as session:
        article, cp, invoice, op = await _card_fixture(session)
        await session.commit()
        inv_id, cp_id = invoice.id, cp.id

        await apply_operation_split(
            session,
            op,
            splits=[(article.id, Decimal("1750.00"), None, inv_id)],
            counterparty_id=cp.id,
            allow_card=True,
        )
        await session.commit()

        assert (await session.get(SupplierInvoice, inv_id)).payment_status == "paid"
        assert op.classification_status == "classified"
        assert await _alloc_count(session, op.id) == 1
        cp_after = await session.get(Counterparty, cp_id)
        assert cp_after.name == "Местный закуп"
        assert cp_after.inn == "7701234567"


# --- сервис: confirm_invoice_match -------------------------------------------


async def test_confirm_match_card_without_allow_card_rejected(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        _article, _cp, invoice, op = await _card_fixture(session)
        await session.commit()

        with pytest.raises(CounterpartyMatchError, match="исходящим платежом поставщику"):
            await confirm_invoice_match(
                session,
                invoice_id=invoice.id,
                bank_operation_id=op.id,
                enrich=True,
                actor_user_id=None,
            )


async def test_confirm_match_card_with_allow_card_allocates_without_enrich(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """allow_card=True: привязка проходит и гасит накладную, но контрагент реквизитами эквайера
    НЕ обогащается (enriched=False), даже при enrich=True."""
    async with async_session_factory() as session:
        _article, cp, invoice, op = await _card_fixture(session)
        await session.commit()
        cp_id, inv_id = cp.id, invoice.id

        result = await confirm_invoice_match(
            session,
            invoice_id=inv_id,
            bank_operation_id=op.id,
            enrich=True,
            actor_user_id=None,
            allow_card=True,
        )

        assert result["enriched"] is False
        assert (await session.get(SupplierInvoice, inv_id)).payment_status == "paid"
        cp_after = await session.get(Counterparty, cp_id)
        assert cp_after.name == "Местный закуп"
        assert cp_after.inn == "7701234567"


# --- сервис: авто-подсказки по-прежнему исключают карт-шум --------------------


async def test_suggest_still_excludes_card_noise(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """suggest_invoice_matches не предлагает карт-операцию, даже если сумма совпадает: карт-шум
    исключён на уровне подсказок, автопривязки карт нет ни при каком флаге."""
    async with async_session_factory() as session:
        await make_expense_article(session)
        cp = await make_counterparty(session, name="Местный закуп", inn="7701234567")
        await make_invoice(session, counterparty_id=cp.id, amount="1750.00", number="78787706")
        # Карт-операция с точной суммой — но это шум, в подсказки попадать не должна.
        await make_bank_operation(
            session, amount="1750.00", direction="out", category="cardOperation"
        )
        await session.commit()

        suggestions = await suggest_invoice_matches(session, counterparty_id=cp.id)
        assert suggestions == []


# --- API: роут разноса /dds/operations/{id}/classify -------------------------


async def _user_with_perms(
    factory: async_sessionmaker[AsyncSession], email: str, codes: Sequence[str]
) -> uuid.UUID:
    """Пользователь с точным набором прав через временную роль (готовых гранулярных ролей нет)."""
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


def _headers(
    factory: async_sessionmaker[AsyncSession], email: str, codes: Sequence[str]
) -> dict[str, str]:
    return token_headers(asyncio.run(_user_with_perms(factory, email, codes)))


def _seed_card(factory: async_sessionmaker[AsyncSession], *, with_case: bool = True):
    async def _seed():
        async with factory() as session:
            article, cp, invoice, op = await _card_fixture(session, with_case=with_case)
            await session.commit()
            return article.id, cp.id, invoice.id, op.id

    return asyncio.run(_seed())


def _classify_url(op_id) -> str:
    return f"/api/v1/dds/operations/{op_id}/classify"


def test_classify_card_invoice_without_allow_card_rolls_back(
    client: TestClient,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Атомарность через роут: разнос карт-операции с накладной без allow_card → 400 и полный
    откат (операция needs_review, проводки нет, аллокации нет, кейс не resolved)."""
    article_id, cp_id, inv_id, op_id = _seed_card(async_session_factory, with_case=True)
    headers = _headers(
        async_session_factory,
        "mgr-atomic@teplo.local",
        ["finance.cashflow.classify", "invoices.normal.pay"],
    )
    resp = client.post(
        _classify_url(op_id),
        json={
            "action": "split",
            "splits": [
                {"article_id": str(article_id), "amount": "1750.00", "invoice_id": str(inv_id)}
            ],
            "counterparty_id": str(cp_id),
        },
        headers=headers,
    )
    assert resp.status_code == 400

    async def _check():
        async with async_session_factory() as session:
            op = await session.get(BankOperation, op_id)
            assert op.classification_status == "needs_review"
            assert await _op_cashflow_count(session, op_id) == 0
            assert await _alloc_count(session, op_id) == 0
            assert (await session.get(SupplierInvoice, inv_id)).payment_status == "unpaid"
            case_status = await session.scalar(
                select(ReconciliationCase.status).where(
                    ReconciliationCase.bank_operation_id == op_id
                )
            )
            assert case_status == "pending"

    asyncio.run(_check())


def test_classify_card_invoice_with_allow_card_ok(
    client: TestClient,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """allow_card=true + право оплаты накладной → 200: накладная гасится, операция classified,
    кейс resolved."""
    article_id, cp_id, inv_id, op_id = _seed_card(async_session_factory, with_case=True)
    headers = _headers(
        async_session_factory,
        "mgr-ok@teplo.local",
        ["finance.cashflow.classify", "invoices.normal.pay"],
    )
    resp = client.post(
        _classify_url(op_id),
        json={
            "action": "split",
            "splits": [
                {"article_id": str(article_id), "amount": "1750.00", "invoice_id": str(inv_id)}
            ],
            "counterparty_id": str(cp_id),
            "allow_card": True,
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text

    async def _check():
        async with async_session_factory() as session:
            assert (await session.get(SupplierInvoice, inv_id)).payment_status == "paid"
            assert (await session.get(BankOperation, op_id)).classification_status == "classified"
            assert await _alloc_count(session, op_id) == 1
            case_status = await session.scalar(
                select(ReconciliationCase.status).where(
                    ReconciliationCase.bank_operation_id == op_id
                )
            )
            assert case_status == "resolved"

    asyncio.run(_check())


def test_classify_card_allow_card_forbidden_without_pay_permission(
    client: TestClient,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """RBAC: allow_card-привязка требует invoices.{kind}.pay. Классификатор без права оплаты
    накладной → 403, и ничего не записано (полный откат)."""
    article_id, cp_id, inv_id, op_id = _seed_card(async_session_factory, with_case=True)
    headers = _headers(
        async_session_factory,
        "classify-only@teplo.local",
        ["finance.cashflow.classify"],
    )
    resp = client.post(
        _classify_url(op_id),
        json={
            "action": "split",
            "splits": [
                {"article_id": str(article_id), "amount": "1750.00", "invoice_id": str(inv_id)}
            ],
            "counterparty_id": str(cp_id),
            "allow_card": True,
        },
        headers=headers,
    )
    assert resp.status_code == 403

    async def _check():
        async with async_session_factory() as session:
            assert await _op_cashflow_count(session, op_id) == 0
            assert await _alloc_count(session, op_id) == 0
            assert (await session.get(SupplierInvoice, inv_id)).payment_status == "unpaid"

    asyncio.run(_check())


def test_classify_card_allow_card_does_not_remember_rule(
    client: TestClient,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Карт-привязку правилом не запоминаем — иначе оно авто-матчило бы будущие карт-операции."""
    article_id, cp_id, inv_id, op_id = _seed_card(async_session_factory, with_case=True)
    headers = _headers(
        async_session_factory,
        "mgr-rule@teplo.local",
        ["finance.cashflow.classify", "invoices.normal.pay"],
    )

    async def _rules_count() -> int:
        async with async_session_factory() as session:
            return await session.scalar(select(func.count()).select_from(ClassificationRule))

    # База может содержать сид-правила — сравниваем с числом ДО запроса, а не с нулём.
    before = asyncio.run(_rules_count())
    resp = client.post(
        _classify_url(op_id),
        json={
            "action": "split",
            "splits": [
                {"article_id": str(article_id), "amount": "1750.00", "invoice_id": str(inv_id)}
            ],
            "counterparty_id": str(cp_id),
            "allow_card": True,
            "remember_as_rule": True,
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["rule_id"] is None
    assert asyncio.run(_rules_count()) == before


# --- API: /warehouse/match/confirm -------------------------------------------


def test_match_confirm_card_requires_allow_card(
    client: TestClient,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """/match/confirm без allow_card режет карт-операцию (409); с allow_card — гасит накладную
    и не обогащает контрагента."""
    _article_id, _cp_id, inv_id, op_id = _seed_card(async_session_factory, with_case=False)
    headers = _headers(
        async_session_factory,
        "mgr-mc@teplo.local",
        ["finance.cashflow.classify", "invoices.normal.pay"],
    )
    url = "/api/v1/warehouse/match/confirm"
    body = {"invoice_id": str(inv_id), "bank_operation_id": str(op_id), "enrich": True}

    rejected = client.post(url, json=body, headers=headers)
    assert rejected.status_code == 409

    ok = client.post(url, json={**body, "allow_card": True}, headers=headers)
    assert ok.status_code == 200, ok.text
    assert ok.json()["enriched"] is False

    async def _check():
        async with async_session_factory() as session:
            assert (await session.get(SupplierInvoice, inv_id)).payment_status == "paid"

    asyncio.run(_check())
