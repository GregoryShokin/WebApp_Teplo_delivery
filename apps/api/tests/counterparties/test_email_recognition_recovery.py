"""Сверенный пакет восстанавливает счёт, сохраняя ID акта и договорное признание расхода."""

from __future__ import annotations

import copy
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from cp_helpers import make_counterparty
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    CounterpartyPayableProfile,
    CounterpartyServiceAgreement,
    DdsArticle,
    EmailInvoiceIntake,
    SupplierExpenseAccrual,
    SupplierInvoice,
)
from app.services.email_invoice_ingest import (
    confirm_intake_with_review,
    materialize_from_intake,
)
from app.services.email_invoice_recovery import recover_intake_recognition

ACTUAL_DATE = date.today() - timedelta(days=1)
PERIOD_END = ACTUAL_DATE.replace(day=1) - timedelta(days=1)
PERIOD_START = PERIOD_END.replace(day=1)


def _verified() -> dict:
    closing = {
        "document_kind": "act",
        "invoice_number": "130",
        "invoice_date": ACTUAL_DATE.isoformat(),
        "amount": "9000",
        "service_period_start": PERIOD_START.isoformat(),
        "service_period_end": PERIOD_END.isoformat(),
        "service_period_source": "document",
        "service_period_confidence": 1,
        "vat_mode": "none",
    }
    return {**closing, "document_kind": "invoice", "companion": closing}


async def _damaged(session: AsyncSession):
    cp = await make_counterparty(session, name="Поставщик пакета", inn="7701111111")
    old_date = ACTUAL_DATE + timedelta(days=365)
    rec = {
        "document_kind": "act",
        "invoice_number": "130",
        "invoice_date": old_date.isoformat(),
        "amount": "9000",
    }
    sha = uuid.uuid4().hex + uuid.uuid4().hex
    closing = SupplierInvoice(
        counterparty_id=cp.id,
        source="email",
        direction="payable",
        doc_kind="closing",
        operational_scope="finance",
        external_id=sha,
        number="130",
        invoice_date=old_date,
        amount=Decimal("9000"),
        payment_status="unpaid",
        activation_status="pending",
        raw_payload={"recognition": copy.deepcopy(rec)},
    )
    session.add(closing)
    await session.flush()
    intake = EmailInvoiceIntake(
        mailbox="corporate",
        attachment_sha256=sha,
        attachment_filename="package.pdf",
        counterparty_id=cp.id,
        invoice_id=closing.id,
        status="closing",
        recognition=rec,
    )
    session.add(intake)
    await session.commit()
    return cp, intake, closing


async def test_recovery_preserves_act_and_restores_one_bill_without_duplicate_expense(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp, intake, closing = await _damaged(session)
        closing_id = closing.id
        article = await session.scalar(
            select(DdsArticle).where(DdsArticle.movement_type == "outflow").limit(1)
        )
        session.add(
            CounterpartyServiceAgreement(
                counterparty_id=cp.id,
                title="Услуга по договору",
                monthly_amount=Decimal("3000"),
                dds_article_id=article.id,
                documents_mode="informal",
                accrual_enabled=True,
                started_on=PERIOD_START,
            )
        )
        await session.commit()
        snapshot = copy.deepcopy(intake.recognition)
        result = await recover_intake_recognition(
            session,
            intake,
            recognition=_verified(),
            expected_recognition=snapshot,
            reason="Проверено по исходному PDF",
        )
        await session.commit()
        await session.refresh(closing)
        bill = await session.get(SupplierInvoice, intake.invoice_id)
        assert result["companion_invoice_id"] == str(closing_id)
        assert intake.companion_invoice_id == closing_id
        assert bill.id != closing_id
        assert bill.doc_kind == "bill" and bill.amount == Decimal("9000")
        assert bill.invoice_date == closing.invoice_date == ACTUAL_DATE
        assert bill.service_period_start == closing.service_period_start == PERIOD_START
        assert bill.service_period_end == closing.service_period_end == PERIOD_END
        assert bill.payment_status == "unpaid" and bill.draft_id is None
        assert intake.status == "linked"
        assert closing.doc_kind == "closing" and closing.activation_status == "active"
        assert closing.informational is True
        assert await session.scalar(select(func.count()).select_from(SupplierExpenseAccrual)) == 0
        assert intake.recognition["recovery_history"][-1]["before"]["recognition"] == snapshot
        bill_id = bill.id

        # Повторное применение с новым снимком не создаёт ни новый счёт, ни новый акт.
        await recover_intake_recognition(
            session,
            intake,
            recognition=_verified(),
            expected_recognition=copy.deepcopy(intake.recognition),
            reason="Повторная сверка",
        )
        await session.commit()
        assert intake.invoice_id == bill_id and intake.companion_invoice_id == closing_id
        assert await session.scalar(select(func.count()).select_from(SupplierInvoice)) == 2


async def test_date_only_recovery_keeps_document_identity(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        _, intake, closing = await _damaged(session)
        old_id = closing.id
        verified = {k: v for k, v in _verified()["companion"].items()}
        await recover_intake_recognition(
            session,
            intake,
            recognition=verified,
            expected_recognition=copy.deepcopy(intake.recognition),
            reason="Сверена дата",
        )
        await session.commit()
        assert intake.invoice_id == old_id and intake.companion_invoice_id is None
        assert intake.status == "closing"
        assert closing.invoice_date == ACTUAL_DATE
        assert closing.activation_status == "active"
        assert await session.scalar(select(func.count()).select_from(SupplierInvoice)) == 1


async def test_recovery_rejects_stale_recognition_without_partial_changes(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        _, intake, closing = await _damaged(session)
        snapshot = copy.deepcopy(intake.recognition)
        stale = {**snapshot, "amount": "1"}
        with pytest.raises(ValueError, match="изменилось"):
            await recover_intake_recognition(
                session,
                intake,
                recognition=_verified(),
                expected_recognition=stale,
                reason="Сверка",
            )
        await session.commit()
        await session.refresh(intake)
        assert intake.recognition == snapshot
        assert intake.invoice_id == closing.id and intake.companion_invoice_id is None
        assert await session.scalar(select(func.count()).select_from(SupplierInvoice)) == 1


async def test_recovery_keeps_period_guard_and_rolls_back(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp, intake, closing = await _damaged(session)
        profile = await session.scalar(
            select(CounterpartyPayableProfile).where(
                CounterpartyPayableProfile.counterparty_id == cp.id
            )
        )
        profile.service_period_required = True
        await session.commit()
        verified = _verified()
        verified.pop("service_period_start")
        verified.pop("service_period_end")
        snapshot = copy.deepcopy(intake.recognition)
        with pytest.raises(ValueError, match="начало и окончание"):
            await recover_intake_recognition(
                session,
                intake,
                recognition=verified,
                expected_recognition=snapshot,
                reason="Сверка неполного документа",
            )
        await session.commit()
        await session.refresh(intake)
        assert intake.status == "closing" and intake.invoice_id == closing.id
        assert intake.companion_invoice_id is None and intake.recognition == snapshot


@pytest.mark.parametrize("status", ["paid", "partially_paid", "void"])
async def test_recovery_cannot_rewrite_settled_documents(
    async_session_factory: async_sessionmaker[AsyncSession],
    status: str,
) -> None:
    async with async_session_factory() as session:
        _, intake, closing = await _damaged(session)
        closing.payment_status = status
        await session.commit()
        snapshot = copy.deepcopy(intake.recognition)
        with pytest.raises(ValueError, match="в банке, оплачен"):
            await recover_intake_recognition(
                session,
                intake,
                recognition=_verified(),
                expected_recognition=snapshot,
                reason="Проверка замороженного документа",
            )
        await session.commit()
        await session.refresh(intake)
        assert intake.status == "closing" and intake.recognition == snapshot
        assert await session.scalar(select(func.count()).select_from(SupplierInvoice)) == 1


@pytest.mark.parametrize("operation", ["materialize", "confirm"])
async def test_ambiguous_package_cannot_be_confirmed_by_confidence_only(
    async_session_factory: async_sessionmaker[AsyncSession],
    operation: str,
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Неполный пакет")
        intake = EmailInvoiceIntake(
            mailbox="corporate",
            attachment_sha256=uuid.uuid4().hex + uuid.uuid4().hex,
            counterparty_id=cp.id,
            status="needs_review",
            recognition={**_verified(), "package_ambiguous": True, "confidence": 1},
        )
        session.add(intake)
        await session.flush()
        with pytest.raises(ValueError, match="полный состав"):
            if operation == "materialize":
                await materialize_from_intake(session, intake)
            else:
                await confirm_intake_with_review(session, intake, actor_user_id=None)
        assert await session.scalar(select(func.count()).select_from(SupplierInvoice)) == 0
