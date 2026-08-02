"""Очередь признания расходов на языке владельца: четыре состояния и кто делает шаг.

Бухгалтерские ``receivable/payable/scheduled`` человеку ничего не говорили: увидев «Дебиторка ·
период не указан», он не понимал, ждут от него действия или нет. ``stage`` отвечает ровно на
это, и главное различие — между ``needs_period`` (период знает человек, он же его укажет) и
``waiting_document`` (сумму расхода принесёт УПД, спрашивать период бессмысленно).

Второй тест — регресс на форму ответа PATCH: ``stage`` обязателен в схеме, и роут, собиравший
ответ вручную, отдавал 500 на валидации. Ни один тест этот эндпоинт не дёргал, поэтому падение
дожило бы до продакшена — а это единственное действие всего экрана.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date
from decimal import Decimal

from cp_helpers import admin_headers, make_counterparty, make_invoice, make_wallet
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    CashflowTransaction,
    CounterpartyPayableProfile,
    SupplierExpenseAccrual,
    SupplierPrepayment,
)
from app.services.supplier_service_periods import set_invoice_service_period

BASE = "/api/v1/accounting/suppliers"

# Период заведомо не закончился ни в один день прогона тестов: stage считается от реального
# «сегодня» сервера, и дата вроде «конец текущего месяца» переключила бы состояние сама собой.
FAR_FUTURE_END = date(2030, 12, 31)


def _admin(factory) -> dict[str, str]:
    return asyncio.run(admin_headers(factory))


async def _open_prepayment(
    session: AsyncSession,
    *,
    counterparty_id: uuid.UUID,
    amount: str,
    paid_on: date | None = None,
) -> SupplierPrepayment:
    """Предоплата без периода — ровно то, чем становится платёж из банковской выписки."""
    prepayment = SupplierPrepayment(
        counterparty_id=counterparty_id,
        kind="subscription",
        amount=Decimal(amount),
        amount_settled=Decimal("0.00"),
        status="open",
    )
    session.add(prepayment)
    await session.flush()
    if paid_on is not None:
        # Дата платежа живёт на ДДС-проводке: от неё считается месяц-фолбэк и срок документа.
        wallet = await make_wallet(session, name=f"Стадии Кошелёк {paid_on:%m-%d} {amount}")
        tx = CashflowTransaction(
            wallet_id=wallet.id,
            direction="out",
            amount=Decimal(amount),
            operation_date=paid_on,
            counterparty_id=counterparty_id,
            source_kind="bank_feed",
        )
        session.add(tx)
        await session.flush()
        prepayment.cashflow_transaction_id = tx.id
        await session.flush()
    return prepayment


async def _set_billing_mode(session: AsyncSession, counterparty_id: uuid.UUID, mode: str) -> None:
    profile = await session.scalar(
        select(CounterpartyPayableProfile).where(
            CounterpartyPayableProfile.counterparty_id == counterparty_id
        )
    )
    profile.service_billing_mode = mode
    await session.flush()


def test_stages_split_queue_by_who_makes_the_step(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Период идёт — ждать; документ ожидается — ждать его; документа не будет — решать человеку.

    По умолчанию документ ЖДЁТСЯ: так считала сводка разрывов, и это единственное честное
    предположение, пока режимы контрагентам не проставлены. Человека дёргаем только там, где
    известно, что бумаги не будет, — иначе весь экран стал бы одной очередью «нужен период».
    """

    async def seed() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
        async with async_session_factory() as session:
            running = await make_counterparty(session, name="Стадии Период", inn="6155000401")
            invoice = await make_invoice(
                session,
                counterparty_id=running.id,
                amount="12000.00",
                invoice_date=date(2026, 7, 1),
            )
            await session.commit()
            await set_invoice_service_period(
                session,
                invoice=invoice,
                start=date(2026, 7, 1),
                end=FAR_FUTURE_END,
                actor_user_id=None,
            )

            # Разовые работы: документа не ждут, значит расход не признается сам никогда.
            asks_human = await make_counterparty(session, name="Стадии Человек", inn="6155000402")
            await _set_billing_mode(session, asks_human.id, "one_off")
            await _open_prepayment(session, counterparty_id=asks_human.id, amount="9000.00")

            waits_doc = await make_counterparty(session, name="Стадии Документ", inn="6155000403")
            await _open_prepayment(
                session,
                counterparty_id=waits_doc.id,
                amount="5000.00",
                paid_on=date(2026, 5, 20),
            )
            await session.commit()
            return running.id, asks_human.id, waits_doc.id

    running_id, human_id, doc_id = asyncio.run(seed())
    response = client.get(f"{BASE}?view=all", headers=_admin(async_session_factory))
    assert response.status_code == 200
    payload = response.json()
    by_cp = {item["counterparty_id"]: item for item in payload["items"]}

    assert by_cp[str(running_id)]["stage"] == "period_running"
    assert by_cp[str(human_id)]["stage"] == "needs_period"

    # Разрывы переехали сюда: срок документа считается от периода, а период без разметки
    # берётся по месяцу платежа — и строка честно об этом говорит.
    waiting = by_cp[str(doc_id)]
    assert waiting["stage"] == "waiting_document"
    assert waiting["period_assumed"] is True
    assert waiting["payment_date"] == "2026-05-20"
    assert waiting["expected_by"] == "2026-05-31"
    # А вот ПРОСРОЧКИ на выдуманном периоде нет, и это решение владельца 02.08.2026. Срок
    # отсчитывался от месяца платежа, но для предоплаты гипотеза неверна ровно наоборот:
    # платёж 29.06 — это платёж ЗА ИЮЛЬ, документ по нему ждут в августе. Экран показывал
    # «нет 33 дн» там, где ждать ещё рано, и красное на выдуманном сроке обесценивало
    # настоящую просрочку: на него переставали смотреть.
    assert waiting["days_overdue"] == 0
    # Порядок: сначала просроченное, потом по величине зависших денег. Раз просрочки на
    # выдуманном периоде больше нет, наверх выходит самая крупная строка — и это верно:
    # без настоящих просрочек сортировать очередь по деньгам единственно осмысленно.
    assert payload["items"][0]["balance_amount"] >= payload["items"][-1]["balance_amount"]
    assert str(doc_id) in {item["counterparty_id"] for item in payload["items"]}

    # Плитки очередей считают зависшие деньги — то, что уплачено, но расходом ещё не стало.
    assert payload["period_running"]["amount"] >= 12000.0
    assert payload["needs_period"]["amount"] >= 9000.0
    assert payload["waiting_document"]["amount"] >= 5000.0
    assert payload["needs_period"]["count"] >= 1


    # Фильтр по состоянию оставляет только свою очередь.
    filtered = client.get(
        f"{BASE}?view=all&stage=needs_period", headers=_admin(async_session_factory)
    )
    assert filtered.status_code == 200
    stages = {item["stage"] for item in filtered.json()["items"]}
    assert stages == {"needs_period"}


def test_goods_supplier_stays_out_of_the_recognition_queue(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Товарный аванс в очередь признания не попадает: его гасит накладная, а не УПД.

    Расход по сырью идёт фудкостом, и держать поставщика продуктов в очереди «чего ждём»
    значило бы звать человека решать то, что решится приходом товара.
    """

    async def seed() -> uuid.UUID:
        async with async_session_factory() as session:
            cp = await make_counterparty(session, name="Стадии Товар", inn="6155000406")
            await make_invoice(
                session,
                counterparty_id=cp.id,
                amount="40000.00",
                operational_scope="warehouse",
                invoice_date=date(2026, 7, 5),
            )
            await _open_prepayment(session, counterparty_id=cp.id, amount="15000.00")
            await session.commit()
            return cp.id

    cp_id = asyncio.run(seed())
    response = client.get(f"{BASE}?view=all", headers=_admin(async_session_factory))
    assert response.status_code == 200
    assert str(cp_id) not in {item["counterparty_id"] for item in response.json()["items"]}


def test_stage_totals_ignore_the_view_filter(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Отфильтровав очередь, человек всё равно видит, сколько висит в остальных состояниях.

    Иначе фильтр «на ручной разбор» обнулял бы соседние плитки, и экран сообщал бы, что
    признавать больше нечего.
    """

    async def seed() -> uuid.UUID:
        async with async_session_factory() as session:
            cp = await make_counterparty(session, name="Стадии Фильтр", inn="6155000404")
            invoice = await make_invoice(
                session,
                counterparty_id=cp.id,
                amount="7000.00",
                invoice_date=date(2026, 7, 1),
            )
            await session.commit()
            await set_invoice_service_period(
                session,
                invoice=invoice,
                start=date(2026, 7, 1),
                end=FAR_FUTURE_END,
                actor_user_id=None,
            )
            return cp.id

    cp_id = asyncio.run(seed())
    response = client.get(f"{BASE}?view=needs_review", headers=_admin(async_session_factory))
    assert response.status_code == 200
    payload = response.json()

    # Строки идущего периода отфильтрованы, а его плитка — нет.
    assert str(cp_id) not in {item["counterparty_id"] for item in payload["items"]}
    assert payload["period_running"]["amount"] >= 7000.0


def test_patch_service_period_answers_with_full_item(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Перенос периода отвечает валидной строкой — регресс на 500 из-за неполного ответа."""

    async def seed() -> uuid.UUID:
        async with async_session_factory() as session:
            cp = await make_counterparty(session, name="Стадии Правка", inn="6155000405")
            invoice = await make_invoice(
                session,
                counterparty_id=cp.id,
                amount="3000.00",
                invoice_date=date(2026, 7, 1),
            )
            await session.commit()
            await set_invoice_service_period(
                session,
                invoice=invoice,
                start=date(2026, 7, 1),
                end=FAR_FUTURE_END,
                actor_user_id=None,
            )
            accrual = await session.scalar(
                select(SupplierExpenseAccrual).where(
                    SupplierExpenseAccrual.invoice_id == invoice.id
                )
            )
            return accrual.id

    accrual_id = asyncio.run(seed())
    response = client.patch(
        f"{BASE}/service-periods/{accrual_id}",
        headers=_admin(async_session_factory),
        json={"service_period_start": "2026-08-01", "service_period_end": "2030-11-30"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["stage"] == "period_running"
    assert payload["service_period_start"] == "2026-08-01"
    assert payload["service_period_end"] == "2030-11-30"
