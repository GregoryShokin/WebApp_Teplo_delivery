"""Защита от двойного клика: гонки на слотах ловит БД, код их переживает.

Тесты гоняют НАСТОЯЩИЕ параллельные сессии (две транзакции к одной БД) — только так
видно то, что не видно в однопоточном тесте: «select → пусто → insert» у обоих.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.tax import TaxBankDraft, TaxDocumentIntake, TaxPayment, TaxPayrollLedger
from app.services.taxes.bank_draft import create_tax_payment_draft
from app.services.taxes.enp_split import rebuild_payroll_enp_split
from app.services.taxes.promote import PromotionError, promote_intake


def _intake(*, sha: str | None = None) -> TaxDocumentIntake:
    return TaxDocumentIntake(
        id=uuid.uuid4(),
        mailbox="corporate",
        from_addr="askad02@mail.ru",
        attachment_sha256=sha or uuid.uuid4().hex * 2,
        received_at=datetime(2026, 7, 23, tzinfo=UTC),
        filename="УСН 2 кв до 28.07.docx",
        document_type="payment_order",
        status="parsed",
        recognition={
            "tax_kind": "usn_advance",
            "amount": "478376",
            "period_hint": "h1",
            "due_date": "2026-07-28",
        },
    )


def _planned(kind: str = "usn_advance", period: str | None = "h1") -> TaxPayment:
    return TaxPayment(
        id=uuid.uuid4(),
        bundle_id=uuid.uuid4(),
        paid_on=date(2026, 7, 28),
        kind=kind,
        amount=Decimal("478376.00"),
        recipient="fns",
        for_year=2026,
        for_period=period,
        status="planned",
        source_kind="tax_notice",
        quality_status="confirmed",
    )


# ── БД действительно не даёт задвоить слот ───────────────────────────────────


async def test_planned_slot_unique_across_transactions(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Два параллельных запроса не создадут два обязательства на один слот."""
    async with async_session_factory() as first, async_session_factory() as second:
        first.add(_planned())
        await first.commit()

        second.add(_planned())
        with pytest.raises(IntegrityError):
            await second.commit()


async def test_planned_slot_unique_with_null_period(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """NULL-период тоже слот: без NULLS NOT DISTINCT индекс бы его не защитил."""
    async with async_session_factory() as first, async_session_factory() as second:
        first.add(_planned(kind="contrib_fixed", period=None))
        await first.commit()

        second.add(_planned(kind="contrib_fixed", period=None))
        with pytest.raises(IntegrityError):
            await second.commit()


async def test_paid_facts_are_not_constrained(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Ограничение — только на ПЛАНОВЫХ: два реальных списания одного вида законны."""
    async with async_session_factory() as session:
        for _ in range(2):
            session.add(
                TaxPayment(
                    id=uuid.uuid4(),
                    bundle_id=uuid.uuid4(),
                    paid_on=date(2026, 7, 28),
                    kind="contrib_injury",
                    amount=Decimal("100.00"),
                    recipient="sfr",
                    for_year=2026,
                    for_period="year",
                    status="paid",
                    source_kind="bank_statement",
                    quality_status="confirmed",
                )
            )
        await session.commit()  # не падает: факты не ограничены слотом

        total = await session.scalar(
            select(func.count()).select_from(TaxPayment).where(TaxPayment.status == "paid")
        )
    assert total == 2


async def test_bank_operation_kind_slot_unique(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Одна операция — одна строка каждого вида (вебхук и поллинг одновременно)."""
    from app.models.dds import BankOperation

    op_id = uuid.uuid4()
    async with async_session_factory() as session:
        session.add(
            BankOperation(
                id=op_id,
                provider="tbank",
                provider_operation_id=uuid.uuid4().hex,
                direction="out",
                amount=Decimal("14902.30"),
                operation_date=date(2026, 7, 28),
                counterparty_inn_raw="7727406020",
                raw_payload={},
            )
        )
        await session.commit()

    def _fact(kind: str) -> TaxPayment:
        return TaxPayment(
            id=uuid.uuid4(),
            bundle_id=uuid.uuid4(),
            paid_on=date(2026, 7, 28),
            kind=kind,
            amount=Decimal("8571.30"),
            recipient="fns",
            for_year=2026,
            for_period="2026-06",
            status="paid",
            source_kind="bank_statement",
            quality_status="reconstructed",
            bank_operation_id=op_id,
        )

    async with async_session_factory() as first, async_session_factory() as second:
        # Разнос ЕНП даёт ДВЕ строки на операцию (взносы + НДФЛ) — это законно.
        first.add(_fact("contrib_employees"))
        first.add(_fact("ndfl"))
        await first.commit()

        second.add(_fact("contrib_employees"))  # дубль того же вида — нет
        with pytest.raises(IntegrityError):
            await second.commit()


# ── ручки переживают проигранную гонку ───────────────────────────────────────


async def test_double_promote_of_same_document(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Двойной клик по «Продвинуть готовые»: второй запрос видит «уже продвинут».

    Блокировка строки документа сериализует запросы — работа не делается дважды.
    """
    intake = _intake()
    async with async_session_factory() as setup:
        setup.add(intake)
        await setup.commit()
        intake_id = intake.id

    async with async_session_factory() as first, async_session_factory() as second:
        first_intake = await first.get(TaxDocumentIntake, intake_id)
        second_intake = await second.get(TaxDocumentIntake, intake_id)
        assert first_intake.status == second_intake.status == "parsed"  # оба видят «готов»

        await promote_intake(first, first_intake)
        await first.commit()

        with pytest.raises(PromotionError):
            await promote_intake(second, second_intake)

    async with async_session_factory() as check:
        total = await check.scalar(
            select(func.count()).select_from(TaxPayment).where(TaxPayment.status == "planned")
        )
    assert total == 1  # обязательство ровно одно


async def test_two_documents_same_slot_do_not_duplicate(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Две платёжки одного обязательства из разных запросов → одно обязательство.

    Проигравший гонку запрос не падает 500-й, а обновляет выигравшую строку.
    """
    first_doc, second_doc = _intake(), _intake()
    async with async_session_factory() as setup:
        setup.add(first_doc)
        setup.add(second_doc)
        await setup.commit()
        first_id, second_id = first_doc.id, second_doc.id

    async with async_session_factory() as first, async_session_factory() as second:
        doc_a = await first.get(TaxDocumentIntake, first_id)
        doc_b = await second.get(TaxDocumentIntake, second_id)

        await promote_intake(first, doc_a)
        await first.commit()

        result = await promote_intake(second, doc_b)  # гонка на слоте — не падаем
        await second.commit()

    assert result.action == "updated"
    async with async_session_factory() as check:
        total = await check.scalar(
            select(func.count()).select_from(TaxPayment).where(TaxPayment.status == "planned")
        )
    assert total == 1


async def test_double_draft_creation_keeps_one(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Двойной клик «Отправить в банк» не готовит вторую платёжку на ту же сумму."""
    async with async_session_factory() as first, async_session_factory() as second:
        await create_tax_payment_draft(
            first,
            tax_kind="usn_advance",
            amount=Decimal("478376.00"),
            for_year=2026,
            for_period="h1",
        )
        await first.commit()

        draft = await create_tax_payment_draft(
            second,
            tax_kind="usn_advance",
            amount=Decimal("478376.00"),
            for_year=2026,
            for_period="h1",
        )
        await second.commit()

    assert draft.status == "ready_to_send"
    async with async_session_factory() as check:
        total = await check.scalar(select(func.count()).select_from(TaxBankDraft))
    assert total == 1


async def test_parallel_enp_split_keeps_one_row_per_slot(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Кнопка и фоновый джоб одновременно: разнос ЕНП не задваивается."""
    async with async_session_factory() as setup:
        setup.add(
            TaxPayrollLedger(
                id=uuid.uuid4(),
                year=2026,
                month=6,
                tab_number="206",
                employee="ВОДОЛАЗОВА В.С.",
                contributions=Decimal("8571.30"),
                ndfl=Decimal("3532.00"),
            )
        )
        await setup.commit()

    async with async_session_factory() as first, async_session_factory() as second:
        await rebuild_payroll_enp_split(first, year=2026)
        await first.commit()

        await rebuild_payroll_enp_split(second, year=2026)  # проигранная гонка
        await second.commit()

    async with async_session_factory() as check:
        rows = (
            await check.scalars(
                select(TaxPayment).where(TaxPayment.for_period == "2026-06")
            )
        ).all()
    kinds = sorted(r.kind for r in rows)
    assert kinds == ["contrib_employees", "ndfl"]  # по одной строке на вид
