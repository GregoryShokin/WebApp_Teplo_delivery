"""Кросс-канальный дедуп «почта/ручной ввод → СБИС» (обратная сторона test_sbis_sync).

Симптом сверки ДЗ/КЗ 17.07: поставщик присылает СЧЁТ письмом и УПД через СБИС на ту же
поставку — номера и даты не совпадают («0000-002629» с почты vs «2653» из ЭДО), строгий
дедуп (сумма+дата+номер) пару не видит, и в системе встают ДВА обязательства: одно
оплачивается, второе висит фантомной кредиторкой. Здесь проверяем направление «СБИС уже
материализован, счёт приходит почтой/руками позже»: окно контрагент+сумма ±7 дней.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from cp_helpers import make_counterparty, make_invoice
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import EmailInvoiceIntake, SupplierInvoice
from app.services.counterparty_registry import (
    CounterpartyRegistryError,
    create_manual_invoice,
)
from app.services.email_invoice_ingest import (
    _find_duplicate_email_invoice,
    materialize_from_intake,
)
from app.services.invoice_recognition import RecognizedInvoice

SUPPLIER_INN = "6168118525"


async def _invoice_count(session: AsyncSession) -> int:
    return await session.scalar(select(func.count()).select_from(SupplierInvoice))


def _make_intake(recognition: dict) -> EmailInvoiceIntake:
    return EmailInvoiceIntake(
        mailbox="corporate",
        from_addr="billing@lema.ru",
        subject="Счёт на оплату",
        attachment_sha256=uuid.uuid4().hex + uuid.uuid4().hex,  # 64 hex-символа
        recognition=recognition,
        status="needs_review",
    )


async def test_email_ingest_links_to_existing_sbis_bill(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Тот же СЧЁТ пришёл и через СБИС (СчетВх=bill), и письмом позже с другим номером/датой —
    окно контрагент+сумма+РОЛЬ ±7 дней связывает их, второй счёт «к оплате» не создаётся.
    Дедуп теперь по одной роли (канон): счёт дедупится со счётом, а не с закрывающим УПД."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Лема", inn=SUPPLIER_INN)
        sbis_invoice = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="80455.00",
            number="2653",
            source="sbis",
            doc_kind="bill",
            invoice_date=date(2026, 6, 30),
        )
        intake = _make_intake(
            {
                "amount": "80455.00",
                "invoice_number": "0000-002629",
                "invoice_date": "2026-07-02",
                "document_kind": "invoice",
            }
        )
        session.add(intake)
        await session.flush()

        status = await materialize_from_intake(session, intake, counterparty_id=cp.id)
        await session.commit()

        assert status == "duplicate"
        assert intake.invoice_id == sbis_invoice.id
        assert await _invoice_count(session) == 1


async def test_email_bill_and_sbis_closing_coexist(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Канон: счёт (bill) с почты и закрывающий УПД (closing) из СБИС той же поставки —
    РАЗНЫЕ роли, дедуп их НЕ схлопывает. Оба живут: счёт в очереди оплат, УПД в балансе."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Лема", inn=SUPPLIER_INN)
        await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="3700.00",
            number="УПД-24234",
            source="sbis",
            doc_kind="closing",
            invoice_date=date(2026, 6, 30),
        )
        intake = _make_intake(
            {
                "amount": "3700.00",
                "invoice_number": "СЧ-70221",
                "invoice_date": "2026-07-02",
                "document_kind": "invoice",
            }
        )
        session.add(intake)
        await session.flush()

        status = await materialize_from_intake(session, intake, counterparty_id=cp.id)
        await session.commit()

        assert status == "linked"  # счёт не схлопнулся на УПД
        assert await _invoice_count(session) == 2


async def test_email_ingest_outside_window_creates_new_invoice(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Месячный шаг (Лема 3 700 ₽/мес): прошломесячная СБИС-накладная не съедает новый
    почтовый счёт — окно ±7 дней уже месячного шага."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Лема", inn=SUPPLIER_INN)
        await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="3700.00",
            number="24234",
            source="sbis",
            doc_kind="bill",
            invoice_date=date(2026, 5, 31),
        )
        intake = _make_intake(
            {
                "amount": "3700.00",
                "invoice_number": "25001",
                "invoice_date": "2026-06-30",
                "document_kind": "invoice",
            }
        )
        session.add(intake)
        await session.flush()

        status = await materialize_from_intake(session, intake, counterparty_id=cp.id)
        await session.commit()

        assert status == "linked"
        assert await _invoice_count(session) == 2


async def test_email_duplicate_enriches_sbis_period(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Из названий ЭДО-документа период не распознался (missing), а в PDF-счёте он есть:
    письмо-дубль дозаполняет период СБИС-накладной и создаёт начисление признания."""
    from app.models import SupplierExpenseAccrual

    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Лема", inn=SUPPLIER_INN)
        sbis_invoice = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="15580.00",
            number="УПД-31",
            source="sbis",
            doc_kind="closing",
            invoice_date=date(2026, 7, 1),
        )
        sbis_invoice.service_period_status = "missing"
        # Тот же закрывающий УПД пришёл и письмом — период из PDF дозаполняет СБИС-накладную.
        intake = _make_intake(
            {
                "amount": "15580.00",
                "invoice_number": "0006634309",
                "invoice_date": "2026-07-03",
                "service_period_start": "2026-06-01",
                "service_period_end": "2026-06-30",
                "service_period_source": "regex",
                "document_kind": "upd",
            }
        )
        session.add(intake)
        await session.flush()

        status = await materialize_from_intake(session, intake, counterparty_id=cp.id)
        await session.commit()

        assert status == "duplicate"
        await session.refresh(sbis_invoice)
        assert sbis_invoice.service_period_status == "ready"
        assert sbis_invoice.service_period_start == date(2026, 6, 1)
        assert sbis_invoice.service_period_end == date(2026, 6, 30)
        accrual = (
            await session.execute(
                select(SupplierExpenseAccrual).where(
                    SupplierExpenseAccrual.invoice_id == sbis_invoice.id
                )
            )
        ).scalar_one_or_none()
        assert accrual is not None


async def test_email_window_ignores_void_and_ready_period_not_overwritten(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Вычищенный вручную дубль (void) не участвует в окне; заполненный период живой
    накладной письмо-дубль не перетирает."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Лема", inn=SUPPLIER_INN)
        await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="3700.00",
            number="24234",
            source="sbis",
            invoice_date=date(2026, 6, 30),
            payment_status="void",
        )
        live = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="3700.00",
            number="2653",
            source="sbis",
            invoice_date=date(2026, 7, 1),
        )
        live.service_period_start = date(2026, 6, 1)
        live.service_period_end = date(2026, 6, 30)
        live.service_period_status = "ready"
        live.service_period_source = "sbis_regex"
        await session.flush()

        probe = RecognizedInvoice(
            amount=Decimal("3700.00"),
            invoice_number="25001",
            invoice_date=date(2026, 7, 2),
            service_period_start=date(2026, 5, 1),
            service_period_end=date(2026, 5, 31),
        )
        dup = await _find_duplicate_email_invoice(session, cp.id, probe, doc_kind="closing")
        assert dup is not None
        assert dup.id == live.id  # void-строка пропущена, взята живая

        intake = _make_intake(
            {
                "amount": "3700.00",
                "invoice_number": "25001",
                "invoice_date": "2026-07-02",
                "service_period_start": "2026-05-01",
                "service_period_end": "2026-05-31",
                "document_kind": "upd",
            }
        )
        session.add(intake)
        await session.flush()
        status = await materialize_from_intake(session, intake, counterparty_id=cp.id)
        await session.commit()

        assert status == "duplicate"
        await session.refresh(live)
        # Период НЕ перетёрт майскими датами из письма.
        assert live.service_period_start == date(2026, 6, 1)
        assert live.service_period_end == date(2026, 6, 30)


async def test_manual_entry_blocked_by_sbis_invoice_in_window(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Ручной ввод при живой СБИС-накладной той же суммы в окне — понятная ошибка,
    второй счёт не создаётся. Вне окна — создаётся штатно."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="ДОКСИНБОКС", inn=SUPPLIER_INN)
        cp_id = cp.id  # после rollback ORM-объект протухает — id держим в переменной
        await make_invoice(
            session,
            counterparty_id=cp_id,
            amount="15580.00",
            number="2653",
            source="sbis",
            invoice_date=date(2026, 6, 30),
        )
        await session.commit()

        with pytest.raises(CounterpartyRegistryError, match="через СБИС"):
            await create_manual_invoice(
                session,
                counterparty_id=cp_id,
                amount=Decimal("15580.00"),
                number="0000-002629",
                invoice_date=date(2026, 7, 2),
                due_date=None,
                note=None,
                actor_user_id=None,
            )
        await session.rollback()
        assert await _invoice_count(session) == 1

        # Тот же счёт, но месяцем позже — легитимный новый, создаётся.
        created = await create_manual_invoice(
            session,
            counterparty_id=cp_id,
            amount=Decimal("15580.00"),
            number="0000-002700",
            invoice_date=date(2026, 7, 31),
            due_date=None,
            note=None,
            actor_user_id=None,
        )
        assert created.id is not None
        assert await _invoice_count(session) == 2
