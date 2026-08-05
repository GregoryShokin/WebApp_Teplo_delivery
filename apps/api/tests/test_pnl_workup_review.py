"""Контур подтверждения проработки: вопрос человеку и акт списания в iiko.

Расход товара проработки признаётся АКТОМ (решение владельца 05.08.2026). Значит цена ошибки
здесь несимметрична: лишний акт — это лишний документ в боевой iiko и задвоенное списание со
склада, а потерянный ответ — расход, не попавший в прибыль вовсе. Тесты закрепляют оба края.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models import User
from app.models.pnl import (
    PnlIikoProductObservation,
    PnlProductMonthlyDecision,
    PnlWorkupReview,
)
from app.services.pnl import workup_review

JULY = date(2026, 7, 1)


def test_writeoff_moment_is_midday_not_now() -> None:
    """iiko сдвигает время на −3 часа: вечерний акт уехал бы на предыдущие сутки."""
    moment = workup_review.writeoff_moment(JULY)
    assert moment.hour == 12
    assert moment.date() == JULY
    # Даже после сдвига на три часа назад документ остаётся в своём дне и месяце.
    assert (moment.hour - 3) > 0

    formatted = workup_review.format_iiko_datetime(moment)
    assert formatted.startswith("2026-07-01T12:00:00.000")
    assert formatted.endswith("+03:00")


def test_writeoff_body_carries_the_fields_api_silently_requires() -> None:
    """``amountUnit`` и ``containerId`` схема зовёт необязательными, а create без них даёт 500."""
    body = workup_review.build_writeoff_body(
        organization_id="org",
        product_guid="product",
        unit_guid="unit",
        quantity=Decimal("2.500"),
        moment=workup_review.writeoff_moment(JULY),
        comment="Проработка: Контейнер",
    )
    assert body["organizationId"] == "org"
    assert body["storeFrom"] == workup_review.DEFAULT_STORE
    assert body["expenseAccount"] == workup_review.DEFAULT_EXPENSE_ACCOUNT
    item = body["items"][0]
    assert item["product"] == "product"
    assert item["amount"] == 2.5
    assert item["amountUnit"] == "unit"
    assert item["containerId"] == workup_review.ZERO_GUID


async def _seed_workup(session, guid: str, name: str, amount: str) -> None:
    session.add(
        PnlIikoProductObservation(
            period_month=JULY,
            source_kind="incoming_invoice",
            iiko_product_guid=guid,
            product_name=name,
            amount=Decimal(amount),
            rows_count=1,
        )
    )
    session.add(
        PnlProductMonthlyDecision(
            period_month=JULY,
            iiko_product_guid=guid,
            source_kind="incoming_invoice",
            decision_kind="workup",
        )
    )


def test_queue_is_built_once_and_keeps_the_answer(async_session_factory) -> None:
    """Повторная сборка не плодит вопросы и не стирает уже данный ответ."""

    async def scenario() -> None:
        async with async_session_factory() as session:
            await _seed_workup(session, "queue-guid", "Контейнер 300мл", "656.00")
            await session.commit()

            assert await workup_review.build_review_queue(session, JULY) == 1
            await session.commit()

            review = await session.scalar(
                select(PnlWorkupReview).where(PnlWorkupReview.iiko_product_guid == "queue-guid")
            )
            assert review is not None
            assert review.status == "pending"
            assert review.purchase_amount == Decimal("656.00")

            # Человек ответил, а ночной синк пришёл снова.
            review.status = "confirmed"
            await session.commit()
            assert await workup_review.build_review_queue(session, JULY) == 0
            await session.commit()
            await session.refresh(review)
            assert review.status == "confirmed"

    asyncio.run(scenario())


def _fake_cloud(
    calls: list,
    *,
    status: int = 201,
    response: dict | None = None,
    post_status: int = 200,
):
    """Заглушка Cloud: create и post — РАЗНЫЕ вызовы, и падать они умеют по отдельности."""

    def call(path: str, body: dict, **_kwargs):
        calls.append((path, body))
        if path == workup_review.POST_PATH:
            return post_status, {"message": "posted"}
        return status, (
            response
            if response is not None
            else {
                "message": "Writeoff document saved successfully",
                "documentId": "doc-1",
                "documentNumber": "0459",
            }
        )

    return call


def test_confirm_creates_the_act_once(async_session_factory, monkeypatch) -> None:
    """Подтверждение списывает товар актом, а повтор второй акт НЕ создаёт.

    Cloud ``create`` не идемпотентен: каждый вызов плодит документ в боевой iiko. Гейт —
    сохранённый ``writeoff_document_id``, и это единственная защита от двойного клика.
    """

    async def scenario() -> None:
        calls: list = []
        monkeypatch.setattr(workup_review, "iiko_cloud_call", _fake_cloud(calls))
        monkeypatch.setattr(workup_review, "_product_unit_guid", _unit("unit-guid"))

        async with async_session_factory() as session:
            await _seed_workup(session, "confirm-guid", "Контейнер 300мл", "656.00")
            await session.commit()
            await workup_review.build_review_queue(session, JULY)
            review = await session.scalar(
                select(PnlWorkupReview).where(PnlWorkupReview.iiko_product_guid == "confirm-guid")
            )
            review.quantity = Decimal("50.000")
            await session.commit()

            row = await workup_review.confirm_workup(session, review.id, user_id=None)
            assert row.status == "confirmed"
            assert row.writeoff_document_id == "doc-1"
            assert row.writeoff_number == "0459"
            assert row.writeoff_posted is True, "акт проводится сразу — иначе расхода нет"
            assert row.writeoff_error is None
            assert [path for path, _ in calls] == [
                workup_review.CREATE_PATH,
                workup_review.POST_PATH,
            ]
            _, create_body = calls[0]
            assert create_body["items"][0]["amount"] == 50.0
            assert create_body["date"].startswith("2026-07-01T12:00:00")
            _, post_body = calls[1]
            assert post_body["documentId"] == "doc-1"

            # Второй клик по той же строке.
            row = await workup_review.confirm_workup(session, review.id, user_id=None)
            assert row.writeoff_document_id == "doc-1"
            assert len(calls) == 2, (
                "повтор не должен ни создавать второй акт, ни проводить уже проведённый"
            )

    asyncio.run(scenario())


def _unit(value: str | None):
    async def resolver(_session, _guid):
        return value

    return resolver


def test_iiko_failure_keeps_the_answer_and_reports_it(async_session_factory, monkeypatch) -> None:
    """Отказ iiko не отменяет решение человека и не теряется: он виден и повторяем."""

    async def scenario() -> None:
        calls: list = []
        monkeypatch.setattr(
            workup_review,
            "iiko_cloud_call",
            _fake_cloud(calls, status=500, response={"message": "deserialization failed"}),
        )
        monkeypatch.setattr(workup_review, "_product_unit_guid", _unit("unit-guid"))

        async with async_session_factory() as session:
            await _seed_workup(session, "fail-guid", "Крышка", "550.00")
            await session.commit()
            await workup_review.build_review_queue(session, JULY)
            review = await session.scalar(
                select(PnlWorkupReview).where(PnlWorkupReview.iiko_product_guid == "fail-guid")
            )
            review.quantity = Decimal("10.000")
            await session.commit()

            row = await workup_review.confirm_workup(session, review.id, user_id=None)
            assert row.status == "confirmed", "ответ человека сохраняется даже при отказе iiko"
            assert row.writeoff_document_id is None
            assert row.writeoff_posted is False
            assert "HTTP 500" in (row.writeoff_error or "")

            # Повтор — новая попытка, потому что акта всё ещё нет.
            monkeypatch.setattr(workup_review, "iiko_cloud_call", _fake_cloud(calls))
            row = await workup_review.confirm_workup(session, review.id, user_id=None)
            assert row.writeoff_document_id == "doc-1"
            assert row.writeoff_posted is True
            assert row.writeoff_error is None

    asyncio.run(scenario())


def test_unknown_unit_is_explained_not_swallowed(async_session_factory, monkeypatch) -> None:
    """Без единицы измерения акт не создаётся — человек должен узнать причину, а не тишину."""

    async def scenario() -> None:
        calls: list = []
        monkeypatch.setattr(workup_review, "iiko_cloud_call", _fake_cloud(calls))
        monkeypatch.setattr(workup_review, "_product_unit_guid", _unit(None))

        async with async_session_factory() as session:
            await _seed_workup(session, "nounit-guid", "Странный товар", "100.00")
            await session.commit()
            await workup_review.build_review_queue(session, JULY)
            review = await session.scalar(
                select(PnlWorkupReview).where(PnlWorkupReview.iiko_product_guid == "nounit-guid")
            )
            review.quantity = Decimal("1.000")
            await session.commit()

            row = await workup_review.confirm_workup(session, review.id, user_id=None)
            assert row.writeoff_document_id is None
            assert "единиц" in (row.writeoff_error or "")
            assert not calls, "в iiko без единицы не ходим — там гарантированный 500"

    asyncio.run(scenario())


def test_reject_returns_product_to_normal_classification(async_session_factory) -> None:
    """«Не проработка» снимает месячное решение — иначе товар навсегда выпал бы из разметки."""

    async def scenario() -> None:
        async with async_session_factory() as session:
            await _seed_workup(session, "reject-guid", "Обычный товар", "300.00")
            await session.commit()
            await workup_review.build_review_queue(session, JULY)
            review = await session.scalar(
                select(PnlWorkupReview).where(PnlWorkupReview.iiko_product_guid == "reject-guid")
            )
            await session.commit()

            row = await workup_review.reject_workup(session, review.id, user_id=None)
            assert row.status == "rejected"
            assert (
                await session.scalar(
                    select(PnlProductMonthlyDecision).where(
                        PnlProductMonthlyDecision.iiko_product_guid == "reject-guid"
                    )
                )
                is None
            )

    asyncio.run(scenario())


def test_reject_is_refused_when_the_act_already_exists(async_session_factory) -> None:
    """Передумать после созданного акта нельзя молча: документ в iiko уже живёт."""

    async def scenario() -> None:
        async with async_session_factory() as session:
            owner = User(
                id=uuid.uuid4(),
                email=f"workup-{uuid.uuid4()}@test.local",
                hashed_password="hashed",
                full_name="Владелец",
                is_active=True,
            )
            session.add(owner)
            await _seed_workup(session, "locked-guid", "Списанный товар", "400.00")
            await session.commit()
            await workup_review.build_review_queue(session, JULY)
            review = await session.scalar(
                select(PnlWorkupReview).where(PnlWorkupReview.iiko_product_guid == "locked-guid")
            )
            review.status = "confirmed"
            review.writeoff_document_id = "doc-existing"
            review.writeoff_number = "0460"
            await session.commit()

            with pytest.raises(workup_review.WorkupReviewError) as error:
                await workup_review.reject_workup(session, review.id, user_id=owner.id)
            assert "0460" in str(error.value)

    asyncio.run(scenario())


def test_created_but_unposted_act_is_finished_not_duplicated(
    async_session_factory, monkeypatch
) -> None:
    """Сбой ПРОВЕДЕНИЯ — самый коварный случай: документ уже есть, а расхода ещё нет.

    Повтор обязан довести до конца именно этот акт. Создать второй значило бы списать товар
    со склада дважды; счесть дело сделанным — оставить проработку без расхода в прибыли.
    """

    async def scenario() -> None:
        calls: list = []
        monkeypatch.setattr(workup_review, "iiko_cloud_call", _fake_cloud(calls, post_status=429))
        monkeypatch.setattr(workup_review, "_product_unit_guid", _unit("unit-guid"))

        async with async_session_factory() as session:
            await _seed_workup(session, "unposted-guid", "Контейнер", "656.00")
            await session.commit()
            await workup_review.build_review_queue(session, JULY)
            review = await session.scalar(
                select(PnlWorkupReview).where(PnlWorkupReview.iiko_product_guid == "unposted-guid")
            )
            review.quantity = Decimal("5.000")
            await session.commit()

            row = await workup_review.confirm_workup(session, review.id, user_id=None)
            assert row.writeoff_document_id == "doc-1", "документ создан"
            assert row.writeoff_posted is False, "но не проведён"
            assert "не проведён" in (row.writeoff_error or "")
            assert row.writeoff_number in (row.writeoff_error or "")

            # Повтор: iiko снова отвечает, проведение доходит до конца.
            monkeypatch.setattr(workup_review, "iiko_cloud_call", _fake_cloud(calls))
            row = await workup_review.confirm_workup(session, review.id, user_id=None)
            assert row.writeoff_posted is True
            assert row.writeoff_error is None
            created = [path for path, _ in calls if path == workup_review.CREATE_PATH]
            assert len(created) == 1, "второй документ создавать нельзя — товар спишется дважды"

    asyncio.run(scenario())
