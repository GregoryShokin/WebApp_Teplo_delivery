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

from app.models.tax import TaxDocumentIntake, TaxPayment, TaxPayrollLedger
from app.services.taxes.promote import (
    PromotionError,
    promote_intake,
    promote_ready_intakes,
    promote_turnover_intake,
)


def _turnover_intake(
    *,
    year: int = 2026,
    month: int = 7,
    status: str = "parsed",
    filename: str = "ОБОРОТКА 07.xls",
    rows: list[dict] | None = None,
) -> TaxDocumentIntake:
    rows = rows or [
        {
            "tab_number": "206", "employee": "ИВАНОВА И.И.", "oklad": "50000.00",
            "days": "23", "accrued": "50000.00", "ndfl": "6318.00", "advance": "20986.00",
            "contributions": "13595.93", "injury": "100.00", "deduction": "1400",
            "to_pay": "22696.00",
        }
    ]
    return TaxDocumentIntake(
        id=uuid.uuid4(),
        mailbox="corporate",
        from_addr="askad02@mail.ru",
        attachment_sha256=uuid.uuid4().hex * 2,
        received_at=datetime(2026, 7, 24, tzinfo=UTC),
        filename=filename,
        document_type="turnover_statement",
        status=status,
        recognition={
            "year": year,
            "month": month,
            "period_hint": f"{year}-{month:02d}",
            "rows": rows,
        },
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
        # Годная УСН → created; нулевая-заглушка → skipped. ЕНП-платёжка пропускается МОЛЧА
        # (её разнос делает оборотка), поэтому в результатах её нет вовсе.
        assert actions == ["created", "skipped"]
        assert await session.scalar(select(func.count()).select_from(TaxPayment)) == 1


# ── оборотки → канон tax_payroll_ledger ──────────────────────────────────────


async def test_promotes_turnover_to_payroll_ledger(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Оборотка становится строкой раскладки, а не платежом: НДФЛ и взносы разделены."""
    async with async_session_factory() as session:
        intake = _turnover_intake()
        session.add(intake)
        await session.flush()

        result = await promote_turnover_intake(session, intake)
        await session.commit()

        assert result.action == "created"
        row = (await session.execute(select(TaxPayrollLedger))).scalar_one()
        assert (row.year, row.month, row.tab_number) == (2026, 7, "206")
        assert row.employee == "ИВАНОВА И.И."
        assert row.ndfl == Decimal("6318.00")
        assert row.contributions == Decimal("13595.93")
        assert row.injury == Decimal("100.00")
        assert row.to_pay == Decimal("22696.00")
        assert row.intake_id == intake.id
        assert intake.status == "promoted"
        # Оборотка НЕ создаёт платёж.
        assert await session.scalar(select(func.count()).select_from(TaxPayment)) == 0


async def test_turnover_reload_updates_ledger_not_duplicates(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Повторная оборотка того же месяца обновляет строку сотрудника, а не плодит вторую."""
    async with async_session_factory() as session:
        first = _turnover_intake()
        session.add(first)
        await session.flush()
        await promote_turnover_intake(session, first)

        corrected = _turnover_intake(
            filename="ОБОРОТКА 07 испр.xls",
            rows=[
                {
                    "tab_number": "206", "employee": "ИВАНОВА И.И.", "oklad": "50000.00",
                    "days": "23", "accrued": "50000.00", "ndfl": "6400.00",
                    "advance": "20986.00", "contributions": "13595.93", "injury": "100.00",
                    "deduction": "1400", "to_pay": "22614.00",
                }
            ],
        )
        session.add(corrected)
        await session.flush()
        result = await promote_turnover_intake(session, corrected)
        await session.commit()

        assert result.action == "updated"
        rows = (await session.execute(select(TaxPayrollLedger))).scalars().all()
        assert len(rows) == 1
        assert rows[0].ndfl == Decimal("6400.00")


async def test_bulk_promotion_routes_turnover_and_payment_separately(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """В одной пачке платёжка идёт в обязательства, оборотка — в раскладку И в разнос ЕНП."""
    async with async_session_factory() as session:
        session.add(_intake(amount="478376"))
        session.add(_turnover_intake())
        await session.flush()

        results = await promote_ready_intakes(session)
        await session.commit()

        assert sorted(r.action for r in results) == ["created", "created"]
        # Оборотка → 1 строка раскладки.
        assert await session.scalar(
            select(func.count()).select_from(TaxPayrollLedger)
        ) == 1
        # tax_payment: УСН-обязательство + разнос ЕНП (взносы + НДФЛ) из оборотки.
        kinds = set(
            (await session.execute(select(TaxPayment.kind))).scalars().all()
        )
        assert kinds == {"usn_advance", "contrib_employees", "ndfl"}


async def test_injury_without_due_gets_legal_deadline(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Травматизм без срока в документе получает законный срок — 15 число следующего месяца.

    В «Форме ПД (налог)» срока нет (поле «Дата» — для отметки банка), и плановая строка брала
    дату письма. Обязательство назавтра краснело просрочкой, хотя по 125-ФЗ (ст. 22) взнос за
    июль платится до 15 августа (вопрос владельца 27.07.2026).
    """
    async with async_session_factory() as session:
        intake = _intake(
            tax_kind="contrib_injury",
            amount="100",
            period="year",
            due=None,
            received=datetime(2026, 7, 22, tzinfo=UTC),
            filename="0,2 %.xls",
        )
        session.add(intake)
        await session.commit()

        await promote_intake(session, intake)
        await session.commit()

        row = (
            await session.scalars(
                select(TaxPayment).where(TaxPayment.kind == "contrib_injury")
            )
        ).one()

    assert row.paid_on == date(2026, 8, 15)
    assert row.recipient == "sfr"  # отдельные реквизиты СФР, не ЕНП


async def test_month_period_defines_tax_year_not_due_date(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Декабрьский травматизм остаётся в 2025 году, хотя срок у него — 15 января.

    Год брался из срока, поэтому платёжка за декабрь уезжала в 2026-й и утаскивала за собой
    факт уплаты: в сводке 2026 года «уплачено» показывало лишние 499,27 ₽ (найдено на проде).
    """
    async with async_session_factory() as session:
        intake = _intake(
            tax_kind="contrib_injury",
            amount="499.27",
            period="2025-12",
            due="2026-01-15",
            received=datetime(2025, 12, 23, tzinfo=UTC),
            filename="0,2%.xls",
        )
        session.add(intake)
        await session.commit()

        await promote_intake(session, intake)
        await session.commit()

        row = (
            await session.scalars(
                select(TaxPayment).where(TaxPayment.kind == "contrib_injury")
            )
        ).one()

    assert row.for_year == 2025
    assert row.paid_on == date(2026, 1, 15)  # срок остаётся законным
