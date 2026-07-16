"""RBAC for the «Контрагенты» API + domain-error HTTP mapping.

Permission tiers (migration 0093): read+operate → owner/admin/manager/office_manager,
admin → owner/admin, cashier → no access. Driven through the FastAPI app so the
``require_permission`` guards and the 409 conflict mapping are both exercised.
"""

from __future__ import annotations

import asyncio
import uuid

from cp_helpers import admin_headers, headers_for, make_counterparty, make_invoice
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

BASE = "/api/v1/counterparties"
VERIFIED_REQUISITES = {
    "bankAcnt": "40702810400000012345",
    "bankBik": "044525225",
    "recipientCorrAccountNumber": "30101810400000000225",
}


def _run(coro):
    return asyncio.run(coro)


def _admin(factory) -> dict[str, str]:
    return _run(admin_headers(factory))


def _manager(factory) -> dict[str, str]:
    return _run(headers_for(factory, "cp-manager@test.local", ["manager"]))


def _cashier(factory) -> dict[str, str]:
    return _run(headers_for(factory, "cp-cashier@test.local", ["cashier"]))


async def _seed_supplier_with_invoice(
    factory: async_sessionmaker[AsyncSession], *, verified: bool
) -> tuple[uuid.UUID, uuid.UUID]:
    async with factory() as session:
        cp = await make_counterparty(
            session,
            name="ООО Поставщик",
            inn="7701234567",
            requisites=VERIFIED_REQUISITES,
            requisites_verified=verified,
        )
        invoice = await make_invoice(session, counterparty_id=cp.id, amount="100.00")
        await session.commit()
        return cp.id, invoice.id


def test_read_tier_registry(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    assert client.get(f"{BASE}/registry").status_code == 401  # unauthenticated
    assert client.get(f"{BASE}/registry", headers=_admin(async_session_factory)).status_code == 200
    assert (
        client.get(f"{BASE}/registry", headers=_manager(async_session_factory)).status_code == 200
    )
    # Касса (Фаза 9): /registry открыт кассиру через dual-guard kassa.invoices.create —
    # справочник контрагентов нужен для создания накладной из Кассы.
    assert (
        client.get(f"{BASE}/registry", headers=_cashier(async_session_factory)).status_code == 200
    )


def test_operate_tier_auto_match(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    assert (
        client.post(f"{BASE}/match/auto", headers=_admin(async_session_factory)).status_code == 200
    )
    assert (
        client.post(f"{BASE}/match/auto", headers=_manager(async_session_factory)).status_code
        == 200
    )
    assert (
        client.post(f"{BASE}/match/auto", headers=_cashier(async_session_factory)).status_code
        == 403
    )


def test_admin_tier_create_category(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    def payload() -> dict:
        return {"code": f"cat_{uuid.uuid4().hex[:8]}", "name": "Маркетинг", "sort_order": 1}

    assert (
        client.post(
            f"{BASE}/categories", json=payload(), headers=_cashier(async_session_factory)
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"{BASE}/categories", json=payload(), headers=_manager(async_session_factory)
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"{BASE}/categories", json=payload(), headers=_admin(async_session_factory)
        ).status_code
        == 201
    )


def test_create_counterparty_saves_all_onboarding_tabs(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    admin = _admin(async_session_factory)
    suffix = uuid.uuid4().hex[:8]
    article = client.post(
        "/api/v1/dds/articles",
        json={
            "code": f"onboarding_{suffix}",
            "name": "Оплата поставщикам",
            "movement_type": "outflow",
            "activity_type": "operating",
        },
        headers=admin,
    )
    assert article.status_code == 201, article.text

    response = client.post(
        BASE,
        headers=admin,
        json={
            "name": "ООО Три вкладки",
            "type": "legal_entity",
            "relationship": "official",
            "default_dds_article_id": article.json()["id"],
            "manager_name": "Анна",
            "manager_phone": "+7 999 123-45-67",
            "requisites": {
                "recipientName": "ООО Три вкладки",
                "inn": "7701234567",
                "bankAcnt": "40702810200000012345",
                "bankBik": "044525225",
                "recipientCorrAccountNumber": "30101810400000000225",
            },
            "requisites_verified": True,
        },
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["name"] == "ООО Три вкладки"
    assert body["inn"] == "7701234567"
    assert body["profile"]["relationship"] == "official"
    assert body["profile"]["ledger_category_id"] is None
    assert body["profile"]["default_dds_article_id"] == article.json()["id"]
    assert body["profile"]["manager_name"] == "Анна"
    assert body["profile"]["manager_phone"] == "+7 999 123-45-67"
    assert body["profile"]["requisites"]["bankAcnt"] == "40702810200000012345"
    assert "kpp" not in body["profile"]["requisites"]
    assert body["profile"]["requisites_verified"] is True


def test_create_counterparty_requires_article_or_explicit_exception(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    response = client.post(
        BASE,
        headers=_admin(async_session_factory),
        json={"name": "Без решения по статье", "relationship": "informal"},
    )

    assert response.status_code == 409
    assert "статью ДДС" in response.json()["detail"]


def test_create_official_supplier_requires_complete_bank_requisites(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    response = client.post(
        BASE,
        headers=_admin(async_session_factory),
        json={
            "name": "ООО Неполные реквизиты",
            "relationship": "official",
            "confirm_no_dds_article": True,
            "requisites": {"inn": "7707654321", "bankBik": "044525225"},
        },
    )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "расчётный счёт" in detail
    assert "корреспондентский счёт" in detail


def test_create_counterparty_rejects_invalid_verified_requisites_atomically(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    admin = _admin(async_session_factory)
    response = client.post(
        BASE,
        headers=admin,
        json={
            "name": "ООО Ошибка реквизитов",
            "inn": "7707654321",
            "confirm_no_dds_article": True,
            "requisites": {
                "inn": "7707654321",
                "bankAcnt": "40702810826000036193",
                "bankBik": "044525974",
                "recipientCorrAccountNumber": "30101810400000000974",
            },
            "requisites_verified": True,
        },
    )

    assert response.status_code == 409
    registry = client.get(f"{BASE}/registry", headers=admin)
    assert registry.status_code == 200
    assert all(item["inn"] != "7707654321" for item in registry.json())


def test_create_informal_counterparty_drops_bank_requisites(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    response = client.post(
        BASE,
        headers=_admin(async_session_factory),
        json={
            "name": f"Неофициальный {uuid.uuid4().hex[:8]}",
            "inn": "7707654321",
            "relationship": "informal",
            "confirm_no_dds_article": True,
            "requisites": {
                "recipientName": "Скрытые реквизиты",
                "inn": "7707654321",
                "bankAcnt": "not-a-bank-account",
            },
            "requisites_verified": True,
        },
    )

    assert response.status_code == 201, response.text
    body = response.json()
    # ИНН — ключ идентификации (по нему синки iiko/почты/ЭДО находят карточку), он
    # сохраняется у любого типа отношений; отбрасываются только БАНКОВСКИЕ реквизиты.
    assert body["inn"] == "7707654321"
    assert body["profile"]["relationship"] == "informal"
    assert body["profile"]["requisites"] == {}
    assert body["profile"]["requisites_verified"] is False


def test_post_draft_without_verified_requisites_returns_409(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _cp_id, invoice_id = _run(_seed_supplier_with_invoice(async_session_factory, verified=False))

    response = client.post(
        f"{BASE}/drafts",
        json={"invoice_ids": [str(invoice_id)]},
        headers=_manager(async_session_factory),
    )

    assert response.status_code == 409
    assert "одтвержден" in response.json()["detail"]  # «не подтверждены»


def test_post_draft_happy_path_as_manager_returns_201(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _cp_id, invoice_id = _run(_seed_supplier_with_invoice(async_session_factory, verified=True))

    response = client.post(
        f"{BASE}/drafts",
        json={"invoice_ids": [str(invoice_id)]},
        headers=_manager(async_session_factory),
    )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "created"
    assert body["amount"] == 100.0


async def _seed_supplier(
    factory: async_sessionmaker[AsyncSession], *, name: str, inn: str
) -> uuid.UUID:
    async with factory() as session:
        cp = await make_counterparty(session, name=name, inn=inn)
        await session.commit()
        return cp.id


def test_kassa_enabled_filter_and_toggle(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    cp_id = _run(
        _seed_supplier(async_session_factory, name="ООО Касса-Поставщик", inn="7705550001")
    )
    admin = _admin(async_session_factory)

    # По умолчанию флаг выключен — в kassa_only его нет.
    before = client.get(f"{BASE}/registry", params={"kassa_only": "true"}, headers=admin)
    assert before.status_code == 200
    assert all(row["counterparty_id"] != str(cp_id) for row in before.json())

    # Включаем «Активен в Кассе».
    on = client.post(f"{BASE}/{cp_id}/kassa-enabled", json={"enabled": True}, headers=admin)
    assert on.status_code == 200
    assert on.json()["profile"]["kassa_enabled"] is True

    # Теперь он попадает в kassa_only и помечен флагом.
    after = {
        row["counterparty_id"]: row
        for row in client.get(
            f"{BASE}/registry", params={"kassa_only": "true"}, headers=admin
        ).json()
    }
    assert str(cp_id) in after
    assert after[str(cp_id)]["kassa_enabled"] is True

    # Без фильтра присутствует независимо от флага.
    full = client.get(f"{BASE}/registry", headers=admin)
    assert any(row["counterparty_id"] == str(cp_id) for row in full.json())

    # Выключаем — снова выпадает из kassa_only.
    off = client.post(f"{BASE}/{cp_id}/kassa-enabled", json={"enabled": False}, headers=admin)
    assert off.status_code == 200
    final = client.get(f"{BASE}/registry", params={"kassa_only": "true"}, headers=admin)
    assert all(row["counterparty_id"] != str(cp_id) for row in final.json())


def test_kassa_enabled_requires_operate(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    cp_id = _run(_seed_supplier(async_session_factory, name="ООО Право-Касса", inn="7705550002"))

    # Кассир не может переключать (нужно counterparties.operate).
    assert (
        client.post(
            f"{BASE}/{cp_id}/kassa-enabled",
            json={"enabled": True},
            headers=_cashier(async_session_factory),
        ).status_code
        == 403
    )
    # Менеджер может.
    assert (
        client.post(
            f"{BASE}/{cp_id}/kassa-enabled",
            json={"enabled": True},
            headers=_manager(async_session_factory),
        ).status_code
        == 200
    )
