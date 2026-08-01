"""Ручное признание расхода по зависшему платежу: «за этот период израсходовано столько-то».

Платёж ушёл, закрывающего документа не будет, и без решения человека деньги висят дебиторкой
вечно, а расход не попадает ни в один месяц — на 01.08.2026 так стояло 311 969 ₽. Действие
разрывает это: указали период — расход разложился по месяцам и сразу попал в P&L.

Главное здесь не «сохранилось», а ТРИ ПУТИ К ДВОЙНОМУ РАСХОДУ, каждый из которых закрыт
отказом с объяснением: договор услуги (месяц начисляет ночная джоба), уже созданные самоакты
(повторный период лёг бы поверх) и разовое начисление по строке платежа (9 000 + 3×3 000 =
18 000 ₽ — ровно этот дефект уже был на проде у Наумченко).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date
from decimal import Decimal

import pytest
from cp_helpers import (
    admin_headers,
    make_counterparty,
    make_draft,
    make_expense_article,
    make_invoice,
    make_wallet,
)
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    CounterpartyServiceAgreement,
    ExpenseDraftLine,
    SupplierExpenseAccrual,
    SupplierInvoice,
    SupplierPrepayment,
)
from app.services import supplier_service_periods as periods

BASE = "/api/v1/accounting/suppliers"

pytestmark = pytest.mark.usefixtures("migrated_db")


def _admin(factory) -> dict[str, str]:
    return asyncio.run(admin_headers(factory))


async def _stuck_prepayment(
    session: AsyncSession,
    *,
    counterparty_id: uuid.UUID,
    amount: str,
    article_id: uuid.UUID | None,
    kind: str = "subscription",
) -> SupplierPrepayment:
    """Дебиторка без периода — то, чем платёж становится сам по себе."""
    prepayment = SupplierPrepayment(
        counterparty_id=counterparty_id,
        kind=kind,
        amount=Decimal(amount),
        amount_settled=Decimal("0.00"),
        status="open",
        article_id=article_id,
    )
    session.add(prepayment)
    await session.flush()
    return prepayment


def test_recognition_splits_payment_across_months_and_hits_pl(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """9 000 ₽ за апрель-июнь → три расхода по 3 000 в своих месяцах, дебиторка закрыта."""

    async def seed() -> uuid.UUID:
        async with async_session_factory() as session:
            cp = await make_counterparty(session, name="Признание Три", inn="6155000501")
            article = await make_expense_article(session, name="Признание Услуги Три")
            prepayment = await _stuck_prepayment(
                session, counterparty_id=cp.id, amount="9000.00", article_id=article.id
            )
            await session.commit()
            return prepayment.id

    prepayment_id = asyncio.run(seed())
    response = client.post(
        f"{BASE}/prepayments/{prepayment_id}/recognize",
        headers=_admin(async_session_factory),
        json={"service_period_start": "2026-04-01", "service_period_end": "2026-06-30"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["period_months"] == 3
    assert payload["months_recognized"] == 3
    assert payload["amount_recognized"] == 9000.0

    async def check() -> None:
        async with async_session_factory() as session:
            accruals = (
                await session.scalars(
                    select(SupplierExpenseAccrual)
                    .join(
                        SupplierInvoice,
                        SupplierInvoice.id == SupplierExpenseAccrual.invoice_id,
                    )
                    .where(SupplierInvoice.external_id.like(f"self:{prepayment_id}:%"))
                )
            ).all()
            assert len(accruals) == 3
            # Расход признан в своих месяцах, а не одним куском в последнем.
            assert {row.recognition_month for row in accruals} == {
                date(2026, 4, 1),
                date(2026, 5, 1),
                date(2026, 6, 1),
            }
            assert sum(row.amount for row in accruals) == Decimal("9000.00")
            assert all(row.status == "recognized" for row in accruals)

            prepayment = await session.get(SupplierPrepayment, prepayment_id)
            assert prepayment.status == "settled"
            assert prepayment.amount_settled == Decimal("9000.00")

    asyncio.run(check())


def test_current_month_is_not_recognized_early(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Незакончившийся месяц в расход не уходит, и ответ честно говорит, сколько признано."""

    async def seed() -> uuid.UUID:
        async with async_session_factory() as session:
            cp = await make_counterparty(session, name="Признание Хвост", inn="6155000502")
            article = await make_expense_article(session, name="Признание Услуги Хвост")
            prepayment = await _stuck_prepayment(
                session, counterparty_id=cp.id, amount="6000.00", article_id=article.id
            )
            await session.commit()
            return prepayment.id

    prepayment_id = asyncio.run(seed())
    today = date.today()
    # Период: прошлый месяц + текущий. Текущий ещё идёт — признать его нельзя.
    first_of_current = today.replace(day=1)
    previous = date(
        first_of_current.year - (1 if first_of_current.month == 1 else 0),
        12 if first_of_current.month == 1 else first_of_current.month - 1,
        1,
    )
    response = client.post(
        f"{BASE}/prepayments/{prepayment_id}/recognize",
        headers=_admin(async_session_factory),
        json={
            "service_period_start": previous.isoformat(),
            "service_period_end": today.isoformat(),
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["period_months"] == 2
    assert payload["months_recognized"] == 1
    assert payload["amount_recognized"] == 3000.0


def test_refuses_when_agreement_already_accrues_the_month(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Договор услуги уже начисляет месяц сам — второе признание удвоило бы расход."""

    async def seed() -> uuid.UUID:
        async with async_session_factory() as session:
            cp = await make_counterparty(session, name="Признание Договор", inn="6155000503")
            article = await make_expense_article(session, name="Признание Услуги Договор")
            session.add(
                CounterpartyServiceAgreement(
                    counterparty_id=cp.id,
                    title="Обслуживание",
                    monthly_amount=Decimal("3000.00"),
                    dds_article_id=article.id,
                    documents_mode="informal",
                    started_on=date(2026, 1, 1),
                    accrual_enabled=True,
                )
            )
            prepayment = await _stuck_prepayment(
                session, counterparty_id=cp.id, amount="9000.00", article_id=article.id
            )
            await session.commit()
            return prepayment.id

    prepayment_id = asyncio.run(seed())
    response = client.post(
        f"{BASE}/prepayments/{prepayment_id}/recognize",
        headers=_admin(async_session_factory),
        json={"service_period_start": "2026-04-01", "service_period_end": "2026-06-30"},
    )
    assert response.status_code == 409
    assert "договор" in response.json()["detail"].lower()


def test_refuses_when_draft_line_already_recognized_the_whole_sum(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Расход по строке платежа уже в P&L — помесячное признание сложилось бы с ним."""

    async def seed() -> uuid.UUID:
        async with async_session_factory() as session:
            cp = await make_counterparty(session, name="Признание Строка", inn="6155000504")
            article = await make_expense_article(session, name="Признание Услуги Строка")
            draft = await make_draft(session, counterparty_id=cp.id, amount="9000.00")
            line = ExpenseDraftLine(
                draft_id=draft.id,
                position=0,
                counterparty_id=cp.id,
                article_id=article.id,
                amount=Decimal("9000.00"),
                purpose="Обслуживание апрель-июнь",
                service_period_start=date(2026, 4, 1),
                service_period_end=date(2026, 6, 30),
            )
            session.add(line)
            await session.flush()
            accrual = await periods.sync_expense_line_accrual(session, line)
            accrual.status = "recognized"
            accrual.recognition_month = date(2026, 6, 1)

            prepayment = await _stuck_prepayment(
                session, counterparty_id=cp.id, amount="9000.00", article_id=article.id
            )
            # Связь предоплаты со строкой идёт через ДДС-проводку черновика.
            wallet = await make_wallet(session, name="Признание Кошелёк Строка")
            from app.models import CashflowTransaction

            tx = CashflowTransaction(
                wallet_id=wallet.id,
                direction="out",
                amount=Decimal("9000.00"),
                operation_date=date(2026, 4, 5),
                counterparty_id=cp.id,
                source_kind="counterparty_payment",
                source_id=draft.id,
            )
            session.add(tx)
            await session.flush()
            prepayment.cashflow_transaction_id = tx.id
            await session.commit()
            return prepayment.id

    prepayment_id = asyncio.run(seed())
    response = client.post(
        f"{BASE}/prepayments/{prepayment_id}/recognize",
        headers=_admin(async_session_factory),
        json={"service_period_start": "2026-04-01", "service_period_end": "2026-06-30"},
    )
    assert response.status_code == 409
    assert "уже признан" in response.json()["detail"]


def test_refuses_prepaid_bill_and_missing_article(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """ДЗ по оплаченному счёту гасит УПД, а без статьи ДДС расход некуда отнести."""

    async def seed() -> tuple[uuid.UUID, uuid.UUID]:
        async with async_session_factory() as session:
            cp = await make_counterparty(session, name="Признание Отказы", inn="6155000505")
            article = await make_expense_article(session, name="Признание Услуги Отказы")
            bill = await make_invoice(
                session, counterparty_id=cp.id, amount="4000.00", doc_kind="bill"
            )
            paid_bill = await _stuck_prepayment(
                session,
                counterparty_id=cp.id,
                amount="4000.00",
                article_id=article.id,
                kind="prepaid_bill",
            )
            paid_bill.source_invoice_id = bill.id
            no_article = await _stuck_prepayment(
                session, counterparty_id=cp.id, amount="2000.00", article_id=None
            )
            await session.commit()
            return paid_bill.id, no_article.id

    paid_bill_id, no_article_id = asyncio.run(seed())
    headers = _admin(async_session_factory)
    body = {"service_period_start": "2026-06-01", "service_period_end": "2026-06-30"}

    bill_response = client.post(
        f"{BASE}/prepayments/{paid_bill_id}/recognize", headers=headers, json=body
    )
    assert bill_response.status_code == 409
    assert "документом" in bill_response.json()["detail"]

    article_response = client.post(
        f"{BASE}/prepayments/{no_article_id}/recognize", headers=headers, json=body
    )
    assert article_response.status_code == 409
    assert "стать" in article_response.json()["detail"].lower()


def test_recognition_does_not_touch_other_counterparties(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Кнопка по одному платежу не утаскивает в P&L созревшие расходы чужих контрагентов.

    Признать их всё равно придётся, и ночная джоба это сделает. Но человек нажимал кнопку по
    конкретному платежу, и месяц закрытия чужого расхода не должен зависеть от того, в какой
    день кто-то разобрал соседнюю строку.
    """

    async def seed() -> tuple[uuid.UUID, uuid.UUID]:
        async with async_session_factory() as session:
            mine = await make_counterparty(session, name="Признание Своё", inn="6155000508")
            article = await make_expense_article(session, name="Признание Услуги Своё")
            prepayment = await _stuck_prepayment(
                session, counterparty_id=mine.id, amount="3000.00", article_id=article.id
            )

            # Чужое начисление, у которого период давно закончился: джоба признает его ночью.
            other = await make_counterparty(session, name="Признание Чужое", inn="6155000509")
            invoice = await make_invoice(
                session,
                counterparty_id=other.id,
                amount="5000.00",
                invoice_date=date(2026, 5, 31),
            )
            await session.commit()
            await periods.set_invoice_service_period(
                session,
                invoice=invoice,
                start=date(2026, 5, 1),
                end=date(2026, 5, 31),
                actor_user_id=None,
            )
            foreign = await session.scalar(
                select(SupplierExpenseAccrual).where(
                    SupplierExpenseAccrual.invoice_id == invoice.id
                )
            )
            assert foreign.status == "scheduled"
            return prepayment.id, foreign.id

    prepayment_id, foreign_id = asyncio.run(seed())
    response = client.post(
        f"{BASE}/prepayments/{prepayment_id}/recognize",
        headers=_admin(async_session_factory),
        json={"service_period_start": "2026-06-01", "service_period_end": "2026-06-30"},
    )
    assert response.status_code == 200, response.text

    async def check() -> None:
        async with async_session_factory() as session:
            foreign = await session.get(SupplierExpenseAccrual, foreign_id)
            assert foreign.status == "scheduled"

    asyncio.run(check())


def test_article_can_be_chosen_at_recognition(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Платежу из выписки статью выбирают прямо здесь — иначе действие недоступно вовсе.

    В банковской выписке статья ДДС не проставлена почти никогда, а без неё расход некуда
    отнести. Отправлять человека размечать проводку в другом разделе и возвращаться — способ
    сделать так, чтобы кнопкой не пользовались.
    """

    async def seed() -> tuple[uuid.UUID, uuid.UUID]:
        async with async_session_factory() as session:
            cp = await make_counterparty(session, name="Признание Статья", inn="6155000507")
            article = await make_expense_article(session, name="Признание Услуги Статья")
            prepayment = await _stuck_prepayment(
                session, counterparty_id=cp.id, amount="4000.00", article_id=None
            )
            await session.commit()
            return prepayment.id, article.id

    prepayment_id, article_id = asyncio.run(seed())
    response = client.post(
        f"{BASE}/prepayments/{prepayment_id}/recognize",
        headers=_admin(async_session_factory),
        json={
            "service_period_start": "2026-06-01",
            "service_period_end": "2026-06-30",
            "dds_article_id": str(article_id),
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["months_recognized"] == 1

    async def check() -> None:
        async with async_session_factory() as session:
            prepayment = await session.get(SupplierPrepayment, prepayment_id)
            assert prepayment.article_id == article_id
            # Статья доехала до самоакта: без неё расход лёг бы в P&L без разреза.
            invoice = await session.scalar(
                select(SupplierInvoice).where(
                    SupplierInvoice.external_id.like(f"self:{prepayment_id}:%")
                )
            )
            assert invoice.dds_article_id == article_id

    asyncio.run(check())


def test_second_recognition_is_refused_not_doubled(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Повторный запрос по тому же платежу не добавляет месяцы поверх уже признанных."""

    async def seed() -> uuid.UUID:
        async with async_session_factory() as session:
            cp = await make_counterparty(session, name="Признание Повтор", inn="6155000506")
            article = await make_expense_article(session, name="Признание Услуги Повтор")
            prepayment = await _stuck_prepayment(
                session, counterparty_id=cp.id, amount="9000.00", article_id=article.id
            )
            await session.commit()
            return prepayment.id

    prepayment_id = asyncio.run(seed())
    headers = _admin(async_session_factory)
    first = client.post(
        f"{BASE}/prepayments/{prepayment_id}/recognize",
        headers=headers,
        json={"service_period_start": "2026-04-01", "service_period_end": "2026-06-30"},
    )
    assert first.status_code == 200
    second = client.post(
        f"{BASE}/prepayments/{prepayment_id}/recognize",
        headers=headers,
        json={"service_period_start": "2026-01-01", "service_period_end": "2026-03-31"},
    )
    assert second.status_code == 409

    async def check_total() -> None:
        async with async_session_factory() as session:
            total = await session.scalar(
                select(SupplierExpenseAccrual.amount)
                .join(
                    SupplierInvoice, SupplierInvoice.id == SupplierExpenseAccrual.invoice_id
                )
                .where(SupplierInvoice.external_id.like(f"self:{prepayment_id}:%"))
            )
            assert total == Decimal("3000.00")

    asyncio.run(check_total())
