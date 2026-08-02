"""Загрузка документа на «Страницу на оплату»: HTTP-слой.

Отдельно от сервисных тестов, потому что ломается здесь другое: разбор multipart (до этой
ветки приложение файлов не принимало вовсе и ``python-multipart`` в образе не было), проверка
типа ПО СОДЕРЖИМОМУ и права. Ответ — список: один файл несёт столько документов, сколько в нём
есть, и фронт обязан получить их все.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date
from pathlib import Path

from cp_helpers import admin_headers, make_counterparty
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    CounterpartyPayableProfile,
    DdsArticle,
    Location,
    Organization,
    UtilityAccount,
)
from app.services import utility_ocr

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "utility"
JPEG = b"\xff\xd8\xff" + b"stand-photo"
SVG_PAYLOAD = b"<svg xmlns='http://www.w3.org/2000/svg'></svg>"


def _headers(session_factory: async_sessionmaker[AsyncSession]) -> dict[str, str]:
    return asyncio.run(admin_headers(session_factory))


async def _seed_flow(session: AsyncSession) -> UtilityAccount:
    organization = await session.scalar(select(Organization).limit(1))
    if organization is None:
        organization = Organization(id=uuid.uuid4(), name="Тепло")
        session.add(organization)
        await session.flush()
    location = Location(id=uuid.uuid4(), organization_id=organization.id, name="Черникова")
    article = DdsArticle(
        id=uuid.uuid4(),
        code=f"art_{uuid.uuid4().hex[:8]}",
        name="Коммунальные платежи",
        movement_type="outflow",
        activity_type="operating",
        location_required=True,
    )
    session.add_all([location, article])
    await session.flush()
    landlord = await make_counterparty(
        session, name="Гордеев Виталий Анатольевич", inn="614314309921", cp_type="individual"
    )
    profile = await session.scalar(
        select(CounterpartyPayableProfile).where(
            CounterpartyPayableProfile.counterparty_id == landlord.id
        )
    )
    if profile is not None:
        profile.relationship = "informal"
    account = UtilityAccount(
        location_id=location.id,
        counterparty_id=landlord.id,
        kind="electricity",
        dds_article_id=article.id,
        started_on=date(2026, 1, 1),
    )
    session.add(account)
    await session.commit()
    return account


def test_upload_creates_payable_row(
    client: TestClient,
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    """Снимок акта за факт → строка очереди оплат с обеими суммами в ответе."""
    text = (FIXTURE_ROOT / "electricity_real_20260717_actual.txt").read_text(encoding="utf-8")

    async def fake_extract(content, *, mime, settings):  # noqa: ANN001, ARG001
        return text, "vision"

    monkeypatch.setattr(utility_ocr, "extract_text", fake_extract)

    async def seed() -> None:
        async with async_session_factory() as session:
            await _seed_flow(session)

    asyncio.run(seed())
    headers = _headers(async_session_factory)

    response = client.post(
        "/api/v1/payment-page/intakes/upload",
        headers=headers,
        files={"file": ("IMG_0001.jpg", JPEG, "image/jpeg")},
    )

    assert response.status_code == 201, response.text
    rows = response.json()
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "linked"
    assert row["amount"] == "30402.00"
    assert row["utility_expense_amount"] == "95402.00"
    assert row["utility_kind_label"] == "Электричество"
    assert row["utility_act_kind"] == "actual"
    # Тип вложения едет наружу: без него фронт покажет снимок пустым PDF-фреймом.
    assert row["attachment_mime"] == "image/jpeg"
    assert row["utility_hints"], "подсказка про парный акт обязана дойти до экрана"


def test_upload_rejects_foreign_format(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Тип определяется по содержимому: SVG со скриптом под видом PNG не пройдёт.

    Заголовку клиента верить нельзя — принятый и отданный обратно inline SVG исполнился бы
    в origin приложения, с его куками и токеном.
    """
    headers = _headers(async_session_factory)

    response = client.post(
        "/api/v1/payment-page/intakes/upload",
        headers=headers,
        files={"file": ("evil.png", SVG_PAYLOAD, "image/png")},
    )

    assert response.status_code == 422
    assert "формат" in response.json()["detail"].lower()


def test_upload_requires_authorization(client: TestClient) -> None:
    response = client.post(
        "/api/v1/payment-page/intakes/upload", files={"file": ("a.jpg", JPEG, "image/jpeg")}
    )
    assert response.status_code in (401, 403)
