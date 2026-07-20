"""Оплата бартерного займа безналом и выбор банка-плательщика в черновике.

Правило владельца (19.07.2026): «мы должны» деньгами не гасится привязкой чужой входящей
операции выписки — Ёбидоёби нам не заплатит за то, что мы должны им. Наш долг закрывает НАШ
платёж: наличная выдача либо ЧЕРНОВИК в банк (Т-Банк или Сбер), подпись в банке, гашение
придёт выпиской. Раньше бартер в банк не пускало право ``invoices.normal.pay``.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence
from decimal import Decimal

from cp_helpers import make_counterparty, make_invoice, token_headers
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    CounterpartyPaymentDraft,
    Organization,
    Permission,
    Role,
    RolePermission,
    SupplierInvoice,
    User,
    UserRole,
)
from app.services.counterparty_payments import create_payment_draft_for_invoices

CP_BASE = "/api/v1/counterparties"
VERIFIED_REQUISITES = {
    "bankAcnt": "40702810400000012345",
    "bankBik": "044525225",
    "recipientCorrAccountNumber": "30101810400000000225",
}


def _run(coro):
    return asyncio.run(coro)


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


def _headers(
    factory: async_sessionmaker[AsyncSession], email: str, codes: Sequence[str]
) -> dict[str, str]:
    return token_headers(_run(_user_with_perms(factory, email, codes)))


async def _loan_invoice(
    session: AsyncSession, *, inn: str, barter_role: str | None = "loan"
) -> SupplierInvoice:
    """Приходная накладная поставщика с верифицированными реквизитами (можно слать в банк)."""
    cp = await make_counterparty(
        session,
        name=f"Партнёр-{inn}",
        inn=inn,
        requisites=VERIFIED_REQUISITES,
        requisites_verified=True,
    )
    invoice = await make_invoice(
        session,
        counterparty_id=cp.id,
        amount="1223.24",
        doc_kind="closing",
        number=f"ЗМ-{inn}",
        barter_role=barter_role,
    )
    if barter_role == "loan":
        invoice.barter_return_status = "open"
    await session.flush()
    return invoice


async def test_draft_defaults_to_tbank(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Без явного канала черновик выписывается на Т-Банк — прежнее поведение не изменилось."""
    async with async_session_factory() as session:
        invoice = await _loan_invoice(session, inn="7710000001", barter_role=None)
        await session.commit()

        draft = await create_payment_draft_for_invoices(
            session, invoice_ids=[invoice.id], actor_user_id=None
        )
        assert draft.bank_provider == "tbank"
        assert Decimal(str(draft.amount)) == Decimal("1223.24")


async def test_draft_channel_selects_sber(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Канал ``bank_draft_sber`` переводит черновик на Сбер — провайдер запоминается."""
    async with async_session_factory() as session:
        invoice = await _loan_invoice(session, inn="7710000002", barter_role=None)
        await session.commit()

        draft = await create_payment_draft_for_invoices(
            session,
            invoice_ids=[invoice.id],
            actor_user_id=None,
            channel="bank_draft_sber",
        )
        assert draft.bank_provider == "sber"


async def test_barter_loan_can_be_sent_to_bank(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Бартерный заём «мы должны» уходит в банк черновиком — как обычная поставка.

    Накладная получает ``draft_id``: пока платёж в пути, авто-зачёт её не тронет (гард
    ``auto_settle`` по draft_id), иначе поставщику заплатили бы дважды.
    """
    async with async_session_factory() as session:
        loan = await _loan_invoice(session, inn="7710000003")
        await session.commit()
        loan_id = loan.id

        draft = await create_payment_draft_for_invoices(
            session, invoice_ids=[loan_id], actor_user_id=None
        )
        await session.commit()

        refreshed = await session.get(SupplierInvoice, loan_id)
        assert refreshed.draft_id == draft.id
        assert refreshed.barter_role == "loan", "роль займа не теряется при отправке в банк"
        assert refreshed.payment_status == "unpaid", "черновик — ещё не оплата"


def test_barter_loan_draft_requires_barter_permission(
    client,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Право разделено: бартерный заём в банк шлёт тот, кто ведёт бартер.

    Раньше ручка требовала ``invoices.normal.pay`` и бартер в банк не пускала вовсе.
    """

    async def _seed() -> uuid.UUID:
        async with async_session_factory() as session:
            loan = await _loan_invoice(session, inn="7710000004")
            await session.commit()
            return loan.id

    loan_id = _run(_seed())

    # Оплачивающий обычные накладные к бартерному займу не допускается.
    normal_only = _headers(async_session_factory, "normal-pay@t.local", ["invoices.normal.pay"])
    denied = client.post(
        f"{CP_BASE}/drafts", json={"invoice_ids": [str(loan_id)]}, headers=normal_only
    )
    assert denied.status_code == 403, denied.text

    # Ведущий бартер — допускается.
    barter = _headers(async_session_factory, "barter-create@t.local", ["invoices.barter.create"])
    allowed = client.post(
        f"{CP_BASE}/drafts", json={"invoice_ids": [str(loan_id)]}, headers=barter
    )
    assert allowed.status_code == 201, allowed.text


def test_ordinary_invoice_still_requires_normal_permission(
    client,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Регресс-гард: обычную накладную по-прежнему шлёт только ``invoices.normal.pay``."""

    async def _seed() -> uuid.UUID:
        async with async_session_factory() as session:
            invoice = await _loan_invoice(session, inn="7710000005", barter_role=None)
            await session.commit()
            return invoice.id

    invoice_id = _run(_seed())

    barter_only = _headers(
        async_session_factory, "barter-only@t.local", ["invoices.barter.create"]
    )
    denied = client.post(
        f"{CP_BASE}/drafts", json={"invoice_ids": [str(invoice_id)]}, headers=barter_only
    )
    assert denied.status_code == 403, denied.text


def test_draft_channel_passes_through_http(
    client,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Выбор банка доезжает от окна гашения до черновика."""

    async def _seed() -> uuid.UUID:
        async with async_session_factory() as session:
            loan = await _loan_invoice(session, inn="7710000006")
            await session.commit()
            return loan.id

    loan_id = _run(_seed())
    headers = _headers(async_session_factory, "barter-sber@t.local", ["invoices.barter.create"])
    response = client.post(
        f"{CP_BASE}/drafts",
        json={"invoice_ids": [str(loan_id)], "channel": "bank_draft_sber"},
        headers=headers,
    )
    assert response.status_code == 201, response.text

    async def _provider() -> str:
        async with async_session_factory() as session:
            draft = await session.get(
                CounterpartyPaymentDraft, uuid.UUID(response.json()["id"])
            )
            return draft.bank_provider

    assert _run(_provider()) == "sber"
