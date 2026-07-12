"""Контроль ошибочных цен накладных по скользящему среднему.

Товар со средней ценой ~200 ₽ и достаточной историей: цена, отклонившаяся вверх на +10% /
вниз на −15%, помечает накладную «подозрительной» (flagged) → оплата и отправка в банк
заблокированы, пока цену не подтвердят («ОК, всё верно»). Пороги ассиметричны. Без истории
(< 3 закупок) не флагаем. Правка сбрасывает подтверждение.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from cp_helpers import make_counterparty, make_iiko_product
from sqlalchemy import select

from app.models import InvoiceLineItem, SupplierInvoice
from app.services.counterparty_payments import (
    CounterpartyPaymentError,
    create_payment_draft_for_invoices,
)
from app.services.invoice_price_control import (
    PriceCheckLine,
    PriceControlError,
    apply_price_control,
    assert_price_cleared,
    confirm_prices,
    evaluate_lines,
    load_config,
    price_stats_for_product,
)
from app.services.warehouse_invoices import (
    LineInput,
    create_warehouse_invoice,
    update_warehouse_invoice,
)

pytestmark = pytest.mark.usefixtures("migrated_db")


async def _add_history(session, *, cp_id, product, prices, days_ago=5):
    """Насыпать историю закупок товара: по одной payable-накладной со строкой на каждую цену."""
    when = datetime.now(tz=UTC) - timedelta(days=days_ago)
    for price in prices:
        inv = SupplierInvoice(
            counterparty_id=cp_id,
            source="manual",
            direction="payable",
            amount=Decimal(str(price)),
            payment_status="unpaid",
            issued_at=when,
            invoice_date=when.date(),
        )
        session.add(inv)
        await session.flush()
        session.add(
            InvoiceLineItem(
                invoice_id=inv.id,
                iiko_product_id=product.id,
                product_guid=product.iiko_id,
                name=product.name,
                unit=product.unit,
                quantity=Decimal("1"),
                price=Decimal(str(price)),
                sum=Decimal(str(price)),
            )
        )
    await session.commit()


def _check_line(product, price):
    return PriceCheckLine(
        name=product.name,
        product_guid=product.iiko_id,
        unit=product.unit,
        price=Decimal(str(price)),
    )


# --- низкоуровневая математика отклонения ------------------------------------


@pytest.mark.parametrize(
    "new_price, expect_flag, direction",
    [
        (200, False, None),  # ровно среднее
        (210, False, None),  # +5% — в пределах
        (220, False, None),  # +10% ровно — граница НЕ флагает
        (221, True, "high"),  # +10.5% — переплата
        (230, True, "high"),  # +15%
        (185, False, None),  # −7.5% — в пределах
        (170, False, None),  # −15% ровно — граница НЕ флагает
        (169, True, "low"),  # −15.5% — подозрительно дёшево
        (160, True, "low"),  # −20%
    ],
)
async def test_deviation_thresholds(async_session_factory, new_price, expect_flag, direction):
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Поставщик", inn="7700000001")
        product = await make_iiko_product(session, name="Картошка фри", unit="кг")
        await _add_history(session, cp_id=cp.id, product=product, prices=[200, 200, 200])
        cfg = await load_config(session)
        assert (cfg.upper_pct, cfg.lower_pct, cfg.min_samples) == (
            Decimal("10"),
            Decimal("15"),
            3,
        )
        anomalies = await evaluate_lines(
            session,
            lines=[_check_line(product, new_price)],
            as_of=datetime.now(tz=UTC).date(),
            cfg=cfg,
            exclude_invoice_id=None,
        )
        assert bool(anomalies) is expect_flag
        if expect_flag:
            assert anomalies[0]["direction"] == direction
            assert anomalies[0]["avg_price"] == 200.0
            assert anomalies[0]["sample_count"] == 3


async def test_insufficient_history_not_flagged(async_session_factory):
    """Меньше min_samples (3) закупок → базы нет, аномалию не поднимаем даже при цене ×2."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Поставщик", inn="7700000002")
        product = await make_iiko_product(session, name="Редкий товар", unit="шт")
        await _add_history(session, cp_id=cp.id, product=product, prices=[100, 100])  # только 2
        cfg = await load_config(session)
        anomalies = await evaluate_lines(
            session,
            lines=[_check_line(product, 400)],
            as_of=datetime.now(tz=UTC).date(),
            cfg=cfg,
            exclude_invoice_id=None,
        )
        assert anomalies == []


async def test_different_unit_not_compared(async_session_factory):
    """Разная единица измерения (кг vs шт) → база не смешивается, флага нет."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Поставщик", inn="7700000003")
        product = await make_iiko_product(session, name="Масло", unit="кг")
        await _add_history(session, cp_id=cp.id, product=product, prices=[200, 200, 200])
        cfg = await load_config(session)
        line = PriceCheckLine(
            name=product.name, product_guid=product.iiko_id, unit="шт", price=Decimal("500")
        )
        anomalies = await evaluate_lines(
            session,
            lines=[line],
            as_of=datetime.now(tz=UTC).date(),
            cfg=cfg,
            exclude_invoice_id=None,
        )
        assert anomalies == []


# --- сквозной поток: создание, блокировка, подтверждение, сброс ---------------


async def _create(session, cp_id, product, price):
    return await create_warehouse_invoice(
        session,
        counterparty_id=cp_id,
        issued_at=datetime.now(tz=UTC),
        lines=[LineInput(name=product.name, quantity=Decimal("1"), price=Decimal(str(price)),
                         iiko_product_id=product.id)],
    )


async def test_create_flags_and_blocks_payment(async_session_factory):
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Поставщик", inn="7700000004")
        product = await make_iiko_product(session, name="Картошка фри", unit="кг")
        await _add_history(session, cp_id=cp.id, product=product, prices=[200, 200, 200])

        invoice = await _create(session, cp.id, product, 300)  # +50% — явная аномалия
        assert invoice.price_control_status == "flagged"
        assert len(invoice.price_anomalies) == 1
        assert invoice.price_anomalies[0]["direction"] == "high"

        # Оплата подозрительной заблокирована.
        with pytest.raises(PriceControlError):
            assert_price_cleared(invoice)

        # Отправка в банк заблокирована.
        with pytest.raises(CounterpartyPaymentError, match="подозрительными ценами"):
            await create_payment_draft_for_invoices(
                session, invoice_ids=[invoice.id], actor_user_id=None
            )


async def test_confirm_unblocks(async_session_factory):
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Поставщик", inn="7700000005")
        product = await make_iiko_product(session, name="Картошка фри", unit="кг")
        await _add_history(session, cp_id=cp.id, product=product, prices=[200, 200, 200])
        invoice = await _create(session, cp.id, product, 300)

        await confirm_prices(session, invoice, actor_user_id=None)
        assert invoice.price_control_status == "confirmed"
        assert invoice.price_confirmed_at is not None
        # После подтверждения оплата разблокирована.
        assert_price_cleared(invoice)  # не бросает

        # Повторное подтверждение — ошибка.
        with pytest.raises(PriceControlError, match="уже подтверждены"):
            await confirm_prices(session, invoice, actor_user_id=None)


async def test_clean_invoice_not_flagged(async_session_factory):
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Поставщик", inn="7700000006")
        product = await make_iiko_product(session, name="Картошка фри", unit="кг")
        await _add_history(session, cp_id=cp.id, product=product, prices=[200, 200, 200])
        invoice = await _create(session, cp.id, product, 205)  # +2.5% — норма
        assert invoice.price_control_status == "clean"
        assert invoice.price_anomalies == []
        assert_price_cleared(invoice)


async def test_edit_resets_confirmation(async_session_factory):
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Поставщик", inn="7700000007")
        product = await make_iiko_product(session, name="Картошка фри", unit="кг")
        await _add_history(session, cp_id=cp.id, product=product, prices=[200, 200, 200])
        invoice = await _create(session, cp.id, product, 300)
        await confirm_prices(session, invoice, actor_user_id=None)
        assert invoice.price_control_status == "confirmed"

        # Правка на нормальную цену → пересчёт, статус clean, подтверждение снято.
        await update_warehouse_invoice(
            session,
            invoice,
            lines=[LineInput(name=product.name, quantity=Decimal("1"), price=Decimal("205"),
                             iiko_product_id=product.id)],
        )
        await session.refresh(invoice)
        assert invoice.price_control_status == "clean"
        assert invoice.price_confirmed_at is None

        # Правка обратно на аномальную → снова flagged (и по-прежнему без подтверждения).
        await update_warehouse_invoice(
            session,
            invoice,
            lines=[LineInput(name=product.name, quantity=Decimal("1"), price=Decimal("300"),
                             iiko_product_id=product.id)],
        )
        await session.refresh(invoice)
        assert invoice.price_control_status == "flagged"
        assert invoice.price_confirmed_at is None


async def test_barter_not_controlled(async_session_factory):
    """Бартерная накладная контролю цен не подлежит — всегда clean."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Партнёр", inn="7700000008", role="supplier")
        product = await make_iiko_product(session, name="Картошка фри", unit="кг")
        await _add_history(session, cp_id=cp.id, product=product, prices=[200, 200, 200])
        invoice = await create_warehouse_invoice(
            session,
            counterparty_id=cp.id,
            issued_at=datetime.now(tz=UTC),
            mode="loan",
            we_lend=True,
            lines=[LineInput(name=product.name, quantity=Decimal("1"), price=Decimal("300"),
                             iiko_product_id=product.id)],
        )
        assert invoice.price_control_status == "clean"


async def test_price_stats_endpoint_helper(async_session_factory):
    """price_stats_for_product отдаёт среднее и пороги для живой подсветки в форме."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Поставщик", inn="7700000009")
        product = await make_iiko_product(session, name="Картошка фри", unit="кг")
        await _add_history(session, cp_id=cp.id, product=product, prices=[200, 200, 200])
        stats = await price_stats_for_product(session, product_id=product.id)
        assert stats["avg_price"] == 200.0
        assert stats["sample_count"] == 3
        assert stats["upper_pct"] == 10.0
        assert stats["lower_pct"] == 15.0
