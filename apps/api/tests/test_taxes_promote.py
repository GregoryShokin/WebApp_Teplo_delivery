"""Продвижение распознанных платёжек в плановые обязательства.

Гарды тут важнее happy-path: нулевая заглушка и смешанный ЕНП НЕ должны становиться
обязательствами, а повторный документ обязан обновлять план, а не плодить строки.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.tax import TaxDocumentIntake, TaxPayment
from app.services.taxes.promote import (
    PromotionError,
    promote_intake,
    promote_ready_intakes,
)


def _intake(
    *,
    tax_kind: str = "usn_advance",
    amount: str | None = "478376",
    period: str | None = "h1",
    due: str | None = "2026-07-28",
    status: str = "parsed",
    document_type: str = "payment_order",
    received: datetime | None = None,
    filename: str = "УСН 2 кв до 28.07.docx",
) -> TaxDocumentIntake:
    return TaxDocumentIntake(
        id=uuid.uuid4(),
        mailbox="corporate",
        from_addr="askad02@mail.ru",
        attachment_sha256=uuid.uuid4().hex * 2,
        received_at=received or datetime(2026, 7, 23, tzinfo=UTC),
        filename=filename,
        document_type=document_type,
        status=status,
        recognition={
            "tax_kind": tax_kind,
            "amount": amount,
            "period_hint": period,
            "due_date": due,
        },
    )


async def test_promotes_payment_order_to_planned_obligation(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        intake = _intake()
        session.add(intake)
        await session.flush()

        result = await promote_intake(session, intake)
        await session.commit()

        assert result.action == "created"
        payment = (await session.execute(select(TaxPayment))).scalar_one()
        assert payment.status == "planned"
        assert payment.kind == "usn_advance"
        assert payment.amount == Decimal("478376")
        assert payment.for_year == 2026
        assert payment.for_period == "h1"
        assert payment.paid_on == date(2026, 7, 28)  # срок уплаты
        assert payment.source_kind == "tax_notice"
        assert intake.status == "promoted"
        assert intake.tax_payment_bundle_id == payment.bundle_id


async def test_zero_stub_is_not_promotable(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Нулевая платёжка — несформированный документ, а не обязательство на 0 ₽."""
    async with async_session_factory() as session:
        intake = _intake(amount="0")
        session.add(intake)
        await session.flush()

        with pytest.raises(PromotionError, match="нулевая"):
            await promote_intake(session, intake)

        assert intake.status == "parsed"  # статус не съехал
        assert await session.scalar(select(func.count()).select_from(TaxPayment)) == 0


async def test_mixed_enp_is_not_promotable(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Зарплатный ЕНП без разноса продвигать нельзя — вид платежа неизвестен."""
    async with async_session_factory() as session:
        intake = _intake(tax_kind="enp_payroll", amount="14902.30", period=None)
        session.add(intake)
        await session.flush()

        with pytest.raises(PromotionError, match="разнос"):
            await promote_intake(session, intake)


async def test_needs_review_is_not_promoted_automatically(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        intake = _intake(status="needs_review")
        session.add(intake)
        await session.flush()

        with pytest.raises(PromotionError, match="parsed"):
            await promote_intake(session, intake)


async def test_second_document_updates_plan_not_duplicates(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Исправленная платёжка обновляет плановую строку, а не создаёт вторую."""
    async with async_session_factory() as session:
        first = _intake(amount="400000", received=datetime(2026, 7, 22, tzinfo=UTC))
        session.add(first)
        await session.flush()
        await promote_intake(session, first)

        second = _intake(amount="478376", received=datetime(2026, 7, 23, tzinfo=UTC))
        session.add(second)
        await session.flush()
        result = await promote_intake(session, second)
        await session.commit()

        assert result.action == "updated"
        payments = (await session.execute(select(TaxPayment))).scalars().all()
        assert len(payments) == 1
        assert payments[0].amount == Decimal("478376")


async def test_year_end_payment_belongs_to_previous_tax_year(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Годовой УСН платится в апреле СЛЕДУЮЩЕГО года — налоговый год предыдущий."""
    async with async_session_factory() as session:
        intake = _intake(period="year", due="2027-04-28", amount="100000")
        session.add(intake)
        await session.flush()

        await promote_intake(session, intake)
        await session.commit()

        payment = (await session.execute(select(TaxPayment))).scalar_one()
        assert payment.for_year == 2026


async def test_bulk_promotion_skips_unpromotable_without_failing(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Пакетное продвижение: годное продвигается, негодное пропускается с причиной."""
    async with async_session_factory() as session:
        session.add(_intake(amount="478376"))
        session.add(_intake(amount="0", filename="УСН нулевая.docx"))
        session.add(
            _intake(tax_kind="enp_payroll", amount="14902.30", filename="ЕНП.docx")
        )
        await session.flush()

        results = await promote_ready_intakes(session)
        await session.commit()

        actions = sorted(r.action for r in results)
        assert actions == ["created", "skipped", "skipped"]
        assert await session.scalar(select(func.count()).select_from(TaxPayment)) == 1
