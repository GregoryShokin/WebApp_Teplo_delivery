"""День записи предоплаты — день ПО МОСКВЕ, одинаковый во всех зеркалах.

У входящего остатка (``create_opening_prepayment``) денежного факта нет: ни проводки, ни оплат
счёта. Датой его денег служит день записи, и считают её пять мест — баланс на дату (SQL),
очередь гашения ``_settlement_order``, сверка с бегущим остатком, реестр платежей и очередь
«Ждём документ» (Python). Пока SQL брал голый ``date(created_at)`` — день в зоне сессии, на проде
``Etc/UTC``, — а Python ``created_at.date()`` (asyncpg отдаёт UTC), все пять сходились друг с
другом, но не с Москвой: займы собственникам 1 020 000 и 200 000 ₽, записанные 03.08.2026 в
00:42 МСК (02.08 21:42 UTC), жили с 02.08. Перевести на Москву одно зеркало — и баланс со
сверкой ставят одну запись в разные сутки.

Сценарий везде один: запись 02.08 в 21:42 UTC. Срез 02.08 её не видит, срез 03.08 — видит.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from cp_helpers import admin_headers, make_counterparty, make_invoice, make_wallet
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import CashflowTransaction, SupplierPrepayment
from app.services import supplier_prepayments
from app.services.counterparty_balance_as_of import _prepayment_money_date, build_balance_as_of
from app.services.counterparty_settlement_ledger import build_ledger

BASE = "/api/v1/accounting/suppliers"
# 03.08.2026 00:42 по Москве — вторые сутки по UTC ещё не кончились.
RECORDED_AT = datetime(2026, 8, 2, 21, 42, tzinfo=UTC)
UTC_DAY = date(2026, 8, 2)
MOSCOW_DAY = date(2026, 8, 3)


async def _opening(
    session: AsyncSession, *, counterparty_id: uuid.UUID, amount: str, kind: str = "other"
) -> SupplierPrepayment:
    prepayment = SupplierPrepayment(
        counterparty_id=counterparty_id,
        kind=kind,
        amount=Decimal(amount),
        amount_settled=Decimal("0.00"),
        status="open",
        opening=True,
        created_at=RECORDED_AT,
        note="Входящий остаток на 01.07.2026",
    )
    session.add(prepayment)
    await session.flush()
    return prepayment


async def _session_time_zone(session: AsyncSession, zone: str) -> None:
    # SET LOCAL живёт до конца транзакции и в пул не утекает.
    await session.execute(text(f"SET LOCAL TIME ZONE '{zone}'"))


@pytest.mark.parametrize("zone", ["UTC", "Europe/Moscow", "America/New_York"])
async def test_balance_counts_opening_from_its_moscow_record_day(
    async_session_factory: async_sessionmaker[AsyncSession], zone: str
) -> None:
    """Баланс на дату: срез 02.08 остатка не видит, срез 03.08 — видит, в любой зоне сессии.

    ``UTC`` — прод. В ``America/New_York`` голый ``date()`` давал бы 02.08 (17:42 по Нью-Йорку),
    в ``Europe/Moscow`` — случайно верный 03.08: ответ не должен зависеть от настроек сервера."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Остаток ночью", inn="6155000901")
        await _opening(session, counterparty_id=cp.id, amount="1020000.00")
        await session.commit()

        await _session_time_zone(session, zone)
        same_utc_day = await build_balance_as_of(session, as_of=UTC_DAY)
        assert same_utc_day.receivable_total == Decimal("0.00"), (
            "остаток, записанный 03.08 по Москве, попал в срез 02.08"
        )
        next_day = await build_balance_as_of(session, as_of=MOSCOW_DAY)
        assert next_day.receivable_total == Decimal("1020000.00")


async def test_settlement_order_dates_opening_as_the_balance_does(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Очередь гашения ставит остаток в тот же день, что баланс, — и это меняет порядок.

    Документ от 02.08 сначала закрывает деньги, которые к его дате уже были. Аванс со своей
    проводкой 02.08 был; остаток, появившийся в балансе 03.08, — ещё нет. По UTC оба выходили
    вторым августа, и ничью решала дата записи: раньше записанный остаток шёл первым и
    документ гасил деньги, которых на его дату в балансе ещё не существовало."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Очередь ночью", inn="6155000902")
        wallet = await make_wallet(session, code="tbank-msk-order", name="Т-Банк")
        tx = CashflowTransaction(
            counterparty_id=cp.id,
            wallet_id=wallet.id,
            direction="out",
            amount=Decimal("5000.00"),
            operation_date=UTC_DAY,
            source_kind="manual",
            quality_status="auto",
        )
        session.add(tx)
        await session.flush()
        advance = SupplierPrepayment(
            counterparty_id=cp.id,
            kind="other",
            wallet_id=wallet.id,
            amount=Decimal("5000.00"),
            amount_settled=Decimal("0.00"),
            status="open",
            cashflow_transaction_id=tx.id,
            # Выписку разобрали позже остатка: по дате записи аванс проигрывал бы ничью.
            created_at=datetime(2026, 8, 5, 9, 0, tzinfo=UTC),
        )
        session.add(advance)
        opening = await _opening(session, counterparty_id=cp.id, amount="3000.00")
        document = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="4000.00",
            number="АКТ-0208",
            doc_kind="closing",
            operational_scope="finance",
            invoice_date=UTC_DAY,
        )
        await session.commit()

        await _session_time_zone(session, "UTC")
        order = await supplier_prepayments._settlement_order(session, document)
        assert [prepayment.id for prepayment, _ in order] == [advance.id, opening.id]
        money_on = {prepayment.id: day for prepayment, day in order}
        balance_day = await session.scalar(
            select(_prepayment_money_date.c.money_date).where(
                _prepayment_money_date.c.prepayment_id == opening.id
            )
        )
        assert money_on[opening.id] == balance_day == MOSCOW_DAY


async def test_ledger_dates_opening_by_moscow_day(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Сверка с бегущим остатком ставит строку «Входящий остаток» тем же днём, что баланс."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Сверка ночью", inn="6155000903")
        await _opening(session, counterparty_id=cp.id, amount="200000.00")
        await session.commit()

        ledger = await build_ledger(session, cp.id, today=date(2026, 9, 1))
        assert [(row.title, row.row_date) for row in ledger.rows] == [
            ("Входящий остаток", MOSCOW_DAY)
        ]


def test_payments_register_filters_and_dates_opening_by_moscow_day(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Реестр платежей: фильтр по датам и дата строки — один и тот же московский день.

    Фильтр считался в SQL по зоне сессии, дата строки — в Python по UTC. Переведи на Москву
    одно без другого — и строка с датой 03.08 отбиралась бы фильтром «по 02.08»."""

    async def seed() -> uuid.UUID:
        async with async_session_factory() as session:
            cp = await make_counterparty(session, name="Реестр ночью", inn="6155000904")
            await _opening(session, counterparty_id=cp.id, amount="15862.24", kind="ad")
            await session.commit()
            return cp.id

    cp_id = asyncio.run(seed())
    headers = asyncio.run(admin_headers(async_session_factory))

    def openings(**params: str) -> list[dict]:
        response = client.get(
            f"{BASE}/payments", params={"counterparty_id": str(cp_id), **params}, headers=headers
        )
        assert response.status_code == 200
        return [row for row in response.json()["items"] if row["row_kind"] == "opening_prepayment"]

    assert openings(date_to=UTC_DAY.isoformat()) == []
    [row] = openings(date_from=MOSCOW_DAY.isoformat(), date_to=MOSCOW_DAY.isoformat())
    assert row["operation_date"] == MOSCOW_DAY.isoformat()


def test_waiting_document_queue_dates_opening_by_moscow_day(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Очередь «Ждём документ»: дата денег входящего остатка — московский день записи."""

    async def seed() -> uuid.UUID:
        async with async_session_factory() as session:
            cp = await make_counterparty(session, name="Очередь признания ночью", inn="6155000905")
            await _opening(session, counterparty_id=cp.id, amount="15862.24", kind="ad")
            await session.commit()
            return cp.id

    cp_id = asyncio.run(seed())
    response = client.get(
        f"{BASE}?view=all", headers=asyncio.run(admin_headers(async_session_factory))
    )
    assert response.status_code == 200
    [item] = [row for row in response.json()["items"] if row["counterparty_id"] == str(cp_id)]
    assert item["payment_date"] == MOSCOW_DAY.isoformat()
