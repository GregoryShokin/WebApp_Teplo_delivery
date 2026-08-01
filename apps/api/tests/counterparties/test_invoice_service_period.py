"""Период оказания услуг у накладной вне почты: гард оплаты и ручной ввод периода.

Накладная не из почты (iiko/склад/ручная/касса) рождается со статусом периода
``not_required``. У контрагента с ``service_period_required`` гард оплаты требует ``ready``,
поэтому такая накладная неоплачиваема — а проставить период было нечем (замкнутый круг:
перенос периода требует начисления, начисление создаётся только при ``ready``).
``set_invoice_service_period`` разрывает круг: пишет период, ставит ``ready`` и заводит
начисление. Плюс проверяем честный статус в выдаче (``not_required`` → ``missing``).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from cp_helpers import make_counterparty, make_invoice
from sqlalchemy import func, select

from app.models import (
    CounterpartyPayableProfile,
    SupplierExpenseAccrual,
    SupplierServicePeriodChange,
)
from app.services import counterparty_registry as registry
from app.services.counterparty_payments import (
    CounterpartyPaymentError,
    create_payment_draft_for_invoices,
)
from app.services.counterparty_registry import get_invoice_item
from app.services.supplier_service_periods import (
    ServicePeriodError,
    set_invoice_service_period,
)

pytestmark = pytest.mark.usefixtures("migrated_db")


async def _require_period(session, counterparty_id) -> CounterpartyPayableProfile:
    """Включить у контрагента обязательный период (фабрика этого поля не знает)."""
    profile = await session.scalar(
        select(CounterpartyPayableProfile).where(
            CounterpartyPayableProfile.counterparty_id == counterparty_id
        )
    )
    profile.service_period_required = True
    await session.flush()
    return profile


async def test_guard_blocks_invoice_without_period(async_session_factory):
    """Накладная без периода у контрагента с обязательным периодом не уходит в банк."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="ООО «Аренда»", inn="7700000001")
        await _require_period(session, cp.id)
        invoice = await make_invoice(session, counterparty_id=cp.id, amount="5000.00")
        await session.commit()

        assert invoice.service_period_status == "not_required"
        with pytest.raises(CounterpartyPaymentError, match="период"):
            await create_payment_draft_for_invoices(
                session, invoice_ids=[invoice.id], actor_user_id=None
            )


async def test_set_invoice_service_period_creates_accrual_and_unblocks(async_session_factory):
    """set_invoice_service_period ставит ready, заводит начисление и снимает гард периода."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="ООО «Телеком»", inn="7700000002")
        await _require_period(session, cp.id)
        invoice = await make_invoice(session, counterparty_id=cp.id, amount="4900.00")
        await session.commit()

        await set_invoice_service_period(
            session,
            invoice=invoice,
            start=date(2026, 6, 1),
            end=date(2026, 6, 30),
            actor_user_id=None,
        )

        await session.refresh(invoice)
        assert invoice.service_period_status == "ready"
        assert invoice.service_period_source == "manual"
        assert invoice.service_period_start == date(2026, 6, 1)

        accrual = await session.scalar(
            select(SupplierExpenseAccrual).where(SupplierExpenseAccrual.invoice_id == invoice.id)
        )
        assert accrual is not None
        assert accrual.status == "scheduled"
        assert accrual.service_period_end == date(2026, 6, 30)

        # Гард периода снят: вызов больше не жалуется на период (другие гарды допустимы).
        try:
            await create_payment_draft_for_invoices(
                session, invoice_ids=[invoice.id], actor_user_id=None
            )
        except CounterpartyPaymentError as exc:
            assert "период" not in str(exc)


async def test_set_invoice_service_period_rejects_reversed(async_session_factory):
    """Окончание раньше начала — ошибка, накладная остаётся без периода."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="ООО «Клининг»", inn="7700000003")
        await _require_period(session, cp.id)
        invoice = await make_invoice(session, counterparty_id=cp.id, amount="3000.00")
        await session.commit()

        with pytest.raises(ServicePeriodError, match="раньше начала"):
            await set_invoice_service_period(
                session,
                invoice=invoice,
                start=date(2026, 6, 30),
                end=date(2026, 6, 1),
                actor_user_id=None,
            )
        await session.refresh(invoice)
        assert invoice.service_period_status == "not_required"


async def test_set_invoice_service_period_delegates_audit_when_accrual_exists(
    async_session_factory,
):
    """Повторная правка периода идёт штатным путём переноса и пишет аудит."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="ООО «Хостинг»", inn="7700000004")
        await _require_period(session, cp.id)
        invoice = await make_invoice(session, counterparty_id=cp.id, amount="2500.00")
        await session.commit()

        await set_invoice_service_period(
            session, invoice=invoice, start=date(2026, 6, 1), end=date(2026, 6, 30),
            actor_user_id=None,
        )
        # Первичная установка аудит переноса НЕ пишет.
        first = await session.scalar(select(SupplierServicePeriodChange))
        assert first is None

        await set_invoice_service_period(
            session, invoice=invoice, start=date(2026, 5, 1), end=date(2026, 5, 31),
            actor_user_id=None,
        )
        change = await session.scalar(select(SupplierServicePeriodChange))
        assert change is not None
        assert change.old_period_start == date(2026, 6, 1)
        assert change.new_period_start == date(2026, 5, 1)

        await session.refresh(invoice)
        assert invoice.service_period_end == date(2026, 5, 31)


async def test_get_invoice_item_reports_missing_not_not_required(async_session_factory):
    """В выдаче not_required у контрагента с обязательным периодом честно показан как missing."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="ООО «Связь»", inn="7700000005")
        await _require_period(session, cp.id)
        invoice = await make_invoice(session, counterparty_id=cp.id, amount="1500.00")
        await session.commit()

        item = await get_invoice_item(session, invoice.id)
        assert item is not None
        assert item.service_period_required is True
        assert item.service_period_status == "missing"


async def test_get_invoice_item_keeps_not_required_when_optional(async_session_factory):
    """Без обязательного периода статус остаётся not_required — ничего не выдумываем."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="ООО «Овощи»", inn="7700000006")
        invoice = await make_invoice(session, counterparty_id=cp.id, amount="800.00")
        await session.commit()

        item = await get_invoice_item(session, invoice.id)
        assert item is not None
        assert item.service_period_required is False
        assert item.service_period_status == "not_required"


async def test_bill_with_period_does_not_book_expense(async_session_factory):
    """Счёт с периодом расхода НЕ признаёт — иначе пришедший следом УПД признает его второй раз.

    Период при оплате спрашивают почти у каждого счёта (правка 01.08.2026), и пока начисление
    заводилось и на счёте, схема «счёт оплатили → акт пришёл отдельным письмом» давала двойной
    расход: 13 000 ₽ услуги превращались в 26 000 ₽ в P&L. Деньги при этом сходились — ДЗ по
    счёту гасил тот же акт, — поэтому увидеть расхождение можно было только в прибыли.
    """
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="ООО «Счёт-Период»", inn="7700000042")
        bill = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="13000.00",
            doc_kind="bill",
            invoice_date=date(2026, 7, 1),
        )
        await session.commit()
        await set_invoice_service_period(
            session,
            invoice=bill,
            start=date(2026, 7, 1),
            end=date(2026, 7, 31),
            actor_user_id=None,
        )

        booked = await session.scalar(
            select(SupplierExpenseAccrual).where(SupplierExpenseAccrual.invoice_id == bill.id)
        )
        assert booked is None
        # Период при этом сохранён: он нужен для срока документа и для признания самоактом.
        assert bill.service_period_start == date(2026, 7, 1)
        assert bill.service_period_status == "ready"

        # Закрывающий документ за тот же период расход признаёт — ровно один раз.
        act = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="13000.00",
            doc_kind="closing",
            invoice_date=date(2026, 7, 31),
        )
        await session.commit()
        await set_invoice_service_period(
            session,
            invoice=act,
            start=date(2026, 7, 1),
            end=date(2026, 7, 31),
            actor_user_id=None,
        )
        total = await session.scalar(
            select(func.coalesce(func.sum(SupplierExpenseAccrual.amount), 0)).where(
                SupplierExpenseAccrual.counterparty_id == cp.id,
                SupplierExpenseAccrual.status != "cancelled",
            )
        )
        assert total == Decimal("13000.00")


async def test_billing_mode_drives_period_requirement(async_session_factory):
    """Тип контрагента сам решает, требовать ли период, — отдельного тумблера больше нет.

    «Счёт + УПД» с включённым требованием был тупиком: гард не пускал платёж в банк без
    периода, а период там знает только контрагент. «Счёт за период» без периода не работает
    по определению — требование включается само.
    """
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="ООО «Режим-Период»", inn="7700000043")
        await _require_period(session, cp.id)  # легаси-флаг включён руками
        await session.commit()

        profile = await registry.update_profile(
            session, cp.id, service_billing_mode="per_invoice"
        )
        assert profile.service_period_required is False

        profile = await registry.update_profile(
            session, cp.id, service_billing_mode="fixed_tariff"
        )
        assert profile.service_period_required is True

        # И гард реально работает от типа: счёт без периода в банк не уходит.
        invoice = await make_invoice(session, counterparty_id=cp.id, amount="2000.00")
        await session.commit()
        with pytest.raises(CounterpartyPaymentError, match="период"):
            await create_payment_draft_for_invoices(
                session, invoice_ids=[invoice.id], actor_user_id=None
            )
