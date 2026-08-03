"""Единый вход в платёжный контур: три экрана — одна пара прав чтения.

Страницы «Платежи», «Страница на оплату» и реестр ЭДО СБИС живут в ОДНОЙ секции
навигации (``counterparties``), которая во фронте открывается любым из двух прав:
``counterparties.read`` или ``finance.counterparties.read``. Бэкенд обязан принимать
ту же пару — иначе роль с одним лишь финансовым правом видит пункт меню, получает
тихий 403 и читает его как «счетов нет».

Расхождение уже ловили дважды: сначала на ``finance_payments`` (комментарий в
`routes/finance_payments.py`), потом на ``sbis``. Третий экран — ``payment_page`` —
чинится здесь, и тест закрывает все три сразу, чтобы следующий роут контура не завёл
несимметричное право заново.

Отдельно: у ``GET /api/v1/finance/payments`` до этого не было ни одного HTTP-теста,
хотя он питает и страницу, и модалку активных платежей.
"""

from __future__ import annotations

import asyncio
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.security import create_access_token
from app.models import (
    Organization,
    Permission,
    Role,
    RolePermission,
    User,
    UserRole,
)

# Роуты, которые обслуживают один и тот же экран платёжного контура.
CONTOUR_READ_ROUTES = (
    "/api/v1/finance/payments",
    "/api/v1/payment-page/intakes",
    "/api/v1/sbis/documents",
)


def test_payment_contour_read_accepts_both_read_permissions(
    client: TestClient,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Оба права чтения открывают все три роута контура."""
    counterparties_headers = _headers_for_permissions(
        async_session_factory,
        "contour-cp-read@test.local",
        ["counterparties.read"],
    )
    finance_headers = _headers_for_permissions(
        async_session_factory,
        "contour-finance-read@test.local",
        ["finance.counterparties.read"],
    )

    for route in CONTOUR_READ_ROUTES:
        with_counterparties = client.get(route, headers=counterparties_headers)
        with_finance = client.get(route, headers=finance_headers)

        assert with_counterparties.status_code == 200, (
            f"{route} закрыт для counterparties.read: {with_counterparties.text}"
        )
        assert with_finance.status_code == 200, (
            f"{route} закрыт для finance.counterparties.read: {with_finance.text}"
        )


def test_payment_contour_read_denies_unrelated_permission(
    client: TestClient,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Право из чужого контура доступа не даёт — гейт именно на этих двух кодах."""
    foreign_headers = _headers_for_permissions(
        async_session_factory,
        "contour-foreign-read@test.local",
        ["schedule.read"],
    )

    for route in CONTOUR_READ_ROUTES:
        response = client.get(route, headers=foreign_headers)
        assert response.status_code == 403, f"{route} открылся без прав контура"


def test_payments_aggregator_returns_buckets(
    client: TestClient,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Витрина платежей отдаёт корзины и список — контракт, на котором живёт модалка."""
    headers = _headers_for_permissions(
        async_session_factory,
        "contour-aggregator@test.local",
        ["counterparties.read"],
    )

    response = client.get("/api/v1/finance/payments", headers=headers)

    assert response.status_code == 200
    payload = response.json()
    assert payload["scope"] == "active"
    # Корзины отдаются всегда, даже пустые: по ним рисуются счётчики-пилюли.
    assert [bucket["key"] for bucket in payload["buckets"]] == [
        "to_review",
        "to_pay",
        "bank_ready",
        "reserved_safe",
        "reserved_kassa",
    ]
    assert isinstance(payload["items"], list)

    history = client.get("/api/v1/finance/payments", params={"scope": "all"}, headers=headers)
    assert history.status_code == 200
    assert history.json()["scope"] == "all"

    unknown_scope = client.get(
        "/api/v1/finance/payments", params={"scope": "everything"}, headers=headers
    )
    assert unknown_scope.status_code == 422


def _headers_for_permissions(
    session_factory: async_sessionmaker[AsyncSession],
    email: str,
    permission_codes: list[str],
) -> dict[str, str]:
    user_id = asyncio.run(_create_user_with_permissions(session_factory, email, permission_codes))
    return {"Authorization": f"Bearer {create_access_token(str(user_id))}"}


async def _create_user_with_permissions(
    session_factory: async_sessionmaker[AsyncSession],
    email: str,
    permission_codes: list[str],
) -> uuid.UUID:
    async with session_factory() as session:
        existing_user_id = await session.scalar(select(User.id).where(User.email == email))
        if existing_user_id is not None:
            return existing_user_id

        organization_id = await session.scalar(select(Organization.id).limit(1))
        assert organization_id is not None
        permissions = (
            await session.scalars(select(Permission).where(Permission.code.in_(permission_codes)))
        ).all()
        assert {permission.code for permission in permissions} == set(permission_codes)

        role = Role(code=f"test_{uuid.uuid4().hex[:24]}", name=f"Test role {email}")
        user = User(
            email=email,
            full_name=email,
            hashed_password="sha256$unused",
            is_active=True,
        )
        session.add_all([role, user])
        await session.flush()
        for permission in permissions:
            session.add(RolePermission(role_id=role.id, permission_id=permission.id))
        session.add(UserRole(user_id=user.id, role_id=role.id, organization_id=organization_id))
        await session.commit()
        return user.id
