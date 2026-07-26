"""Факт-слой: списания в ФНС/СФР из выписки → строки ``tax_payment``.

Критичный для прода контур: без него вычет УСН пуст и налог завышен. Проверяем
слои доверия (черновик → план → платёжка → эвристика), обязательный разнос ЕНП
по оборотке, «дозревание» нераспознанных платежей и идемпотентность.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.dds import BankOperation
from app.models.tax import TaxBankDraft, TaxDocumentIntake, TaxPayment, TaxPayrollLedger
from app.services.taxes.bank_facts import sync_tax_facts_from_bank

FNS_INN = "7727406020"
SFR_INN = "6163013494"


def _operation(
    amount: str,
    op_date: date,
    *,
    inn: str = FNS_INN,
    direction: str = "out",
    purpose: str = "Единый налоговый платеж",
    doc_number: str | None = "123456",
) -> BankOperation:
    return BankOperation(
        id=uuid.uuid4(),
        provider="tbank",
        provider_operation_id=uuid.uuid4().hex,
        operation_date=op_date,
        direction=direction,
        amount=Decimal(amount),
        currency="RUB",
        counterparty_name_raw="Казначейство России (ФНС России)",
        counterparty_inn_raw=inn,
        payment_purpose=purpose,
        document_number=doc_number,
        raw_payload={},
        classification_status="pending",
    )


def _draft(
    kind: str, amount: str, *, period: str | None, due: date, title: str = ""
) -> TaxBankDraft:
    return TaxBankDraft(
        id=uuid.uuid4(),
        tax_kind=kind,
        for_year=2026,
        for_period=period,
        title=title or kind,
        amount=Decimal(amount),
        purpose="Единый налоговый платеж",
        due_date=due,
        status="in_bank",
        bank_provider="tbank",
    )


def _planned(kind: str, amount: str, due: date, period: str | None) -> TaxPayment:
    recipient = "sfr" if kind == "contrib_injury" else "fns"
    return TaxPayment(
        id=uuid.uuid4(),
        bundle_id=uuid.uuid4(),
        paid_on=due,
        kind=kind,
        amount=Decimal(amount),
        recipient=recipient,
        for_year=2026,
        for_period=period,
        status="planned",
        source_kind="tax_notice",
        quality_status="confirmed",
    )


def _intake(tax_kind: str, amount: str, due: date) -> TaxDocumentIntake:
    return TaxDocumentIntake(
        id=uuid.uuid4(),
        mailbox="corporate",
        from_addr="Бухгалтер <a@b.c>",
        attachment_sha256=(uuid.uuid4().hex + uuid.uuid4().hex),
        received_at=datetime(2026, 7, 1, tzinfo=UTC),
        filename="платёжка.docx",
        document_type="payment_order",
        status="parsed",
        recognition={
            "tax_kind": tax_kind,
            "amount": amount,
            "due_date": due.isoformat(),
        },
    )


def _ledger(month: int, contributions: str, ndfl: str = "0") -> TaxPayrollLedger:
    return TaxPayrollLedger(
        id=uuid.uuid4(),
        year=2026,
        month=month,
        tab_number="206",
        employee="ИВАНОВА И.И.",
        contributions=Decimal(contributions),
        ndfl=Decimal(ndfl),
    )


async def _facts(session: AsyncSession) -> list[TaxPayment]:
    return list(
        (
            await session.scalars(
                select(TaxPayment).where(TaxPayment.source_kind == "bank_statement")
            )
        ).all()
    )


async def test_draft_match_creates_fact_and_pays_draft(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Списание суммой нашего черновика: факт с видом/периодом черновика, черновик → paid."""
    async with async_session_factory() as session:
        draft = _draft("usn_advance", "478376.00", period="h1", due=date(2026, 7, 28))
        session.add(draft)
        session.add(_operation("478376.00", date(2026, 7, 27)))
        await session.commit()

        report = await sync_tax_facts_from_bank(session)
        await session.commit()

        facts = await _facts(session)
        assert report.bundles_created == 1
        assert report.drafts_paid == 1
        assert len(facts) == 1
        fact = facts[0]
        assert (fact.kind, fact.for_period, fact.for_year) == ("usn_advance", "h1", 2026)
        assert fact.quality_status == "confirmed"
        assert fact.paid_on == date(2026, 7, 27)
        assert fact.bank_operation_id is not None
        refreshed = await session.get(TaxBankDraft, draft.id)
        assert refreshed is not None and refreshed.status == "paid"


async def test_enp_operation_splits_by_turnover_ledger(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """ЕНП 14 902,30 по платёжке бухгалтера: взносы июня из оборотки + НДФЛ остатком."""
    async with async_session_factory() as session:
        session.add(_intake("enp_payroll", "14902.30", date(2026, 7, 28)))
        session.add(_ledger(6, "8571.30", "3532"))
        session.add(_operation("14902.30", date(2026, 7, 28)))
        await session.commit()

        report = await sync_tax_facts_from_bank(session)
        await session.commit()

        facts = sorted(await _facts(session), key=lambda f: f.kind)
        assert report.bundles_created == 1
        assert [(f.kind, str(f.amount)) for f in facts] == [
            ("contrib_employees", "8571.30"),
            ("ndfl", "6331.00"),
        ]
        assert {f.for_period for f in facts} == {"2026-06"}
        assert {f.quality_status for f in facts} == {"reconstructed"}
        assert len({f.bundle_id for f in facts}) == 1  # один перевод — один bundle


async def test_enp_without_ledger_waits_then_ripens(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Без оборотки ЕНП ждёт в other/requires_review; пришла оборотка — дозрел."""
    async with async_session_factory() as session:
        session.add(_intake("enp_payroll", "14902.30", date(2026, 7, 28)))
        session.add(_operation("14902.30", date(2026, 7, 28)))
        await session.commit()

        report = await sync_tax_facts_from_bank(session)
        await session.commit()
        facts = await _facts(session)
        assert report.review_pending == 1
        assert [(f.kind, f.quality_status) for f in facts] == [("other", "requires_review")]

        session.add(_ledger(6, "8571.30", "3532"))
        await session.commit()

        report2 = await sync_tax_facts_from_bank(session)
        await session.commit()
        facts2 = sorted(await _facts(session), key=lambda f: f.kind)
        assert report2.bundles_ripened == 1
        assert [(f.kind, str(f.amount)) for f in facts2] == [
            ("contrib_employees", "8571.30"),
            ("ndfl", "6331.00"),
        ]


async def test_sfr_heuristic_and_unknown_fns_review(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """СФР без документа — травматизм по эвристике; неизвестный ФНС — на проверку."""
    async with async_session_factory() as session:
        session.add(_operation("57.14", date(2026, 6, 23), inn=SFR_INN))
        session.add(_operation("39029.00", date(2026, 6, 23)))
        await session.commit()

        report = await sync_tax_facts_from_bank(session)
        await session.commit()

        facts = {f.kind: f for f in await _facts(session)}
        assert report.bundles_created == 2
        assert facts["contrib_injury"].recipient == "sfr"
        assert facts["contrib_injury"].quality_status == "reconstructed"
        assert facts["other"].quality_status == "requires_review"
        assert facts["other"].recipient == "fns"
        assert report.review_pending == 1


async def test_planned_match_sets_kind_and_period(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Списание суммой планового обязательства: вид/период из плана (допвзнос 1%)."""
    async with async_session_factory() as session:
        session.add(_planned("contrib_extra_1pct", "105628.00", date(2026, 9, 25), "h1"))
        session.add(_operation("105628.00", date(2026, 9, 20)))
        await session.commit()

        await sync_tax_facts_from_bank(session)
        await session.commit()

        facts = await _facts(session)
        assert [(f.kind, f.for_period, f.quality_status) for f in facts] == [
            ("contrib_extra_1pct", "h1", "confirmed")
        ]


async def test_sync_is_idempotent_and_ignores_foreign_operations(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Повторный прогон не плодит фактов; чужие/входящие операции не трогаются."""
    async with async_session_factory() as session:
        session.add(_operation("57.14", date(2026, 6, 23), inn=SFR_INN))
        # Поставщик и входящая операция — не налоговый контур.
        session.add(_operation("5000.00", date(2026, 6, 23), inn="6167110428"))
        session.add(_operation("70000.00", date(2026, 6, 23), direction="in"))
        await session.commit()

        first = await sync_tax_facts_from_bank(session)
        await session.commit()
        second = await sync_tax_facts_from_bank(session)
        await session.commit()

        assert first.bundles_created == 1
        assert second.bundles_created == 0
        assert second.bundles_ripened == 0
        assert len(await _facts(session)) == 1


async def test_split_enp_feeds_deduction_with_contributions_only(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Вычет двигается ТОЛЬКО взносовой частью разнесённого ЕНП, не НДФЛ."""
    from app.services.taxes.repository import load_tax_inputs

    async with async_session_factory() as session:
        session.add(_intake("enp_payroll", "14902.30", date(2026, 7, 28)))
        session.add(_ledger(6, "8571.30", "3532"))
        session.add(_operation("14902.30", date(2026, 7, 28)))
        await session.commit()

        await sync_tax_facts_from_bank(session)
        await session.commit()

        inputs = await load_tax_inputs(session, as_of=date(2026, 8, 1))

    assert inputs.employees_paid == Decimal("8571.30")
