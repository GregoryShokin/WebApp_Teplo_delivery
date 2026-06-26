"""Уникальность номера накладной Кассы.

iiko ключует документ по «номер + дата» и отвергает второй такой же за день. Поэтому
автоподсказка номера считается по ВСЕМ накладным, а при создании kassa/manual номер
делается уникальным на дату чека (авто-суффикс «N-2»).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from cp_helpers import make_counterparty, make_invoice
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.services.warehouse_invoices import (
    LineInput,
    create_warehouse_invoice,
    next_invoice_number,
)


def _goods() -> list[LineInput]:
    return [LineInput(name="Товар", quantity=Decimal("1"), price=Decimal("100"))]


async def _kassa_invoice(
    session: AsyncSession, *, cp_id: uuid.UUID, number: str, when: datetime
):
    return await create_warehouse_invoice(
        session,
        counterparty_id=cp_id,
        issued_at=when,
        lines=_goods(),
        number=number,
        source="kassa_invoice",
    )


async def test_next_number_considers_all_sources(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Раньше next_invoice_number смотрел только source='manual' и для накладной Кассы
    подсказывал уже занятый «4». Теперь учитывает все накладные."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="П1", inn="7700001001")
        await _kassa_invoice(
            session, cp_id=cp.id, number="4", when=datetime(2026, 6, 25, 14, tzinfo=UTC)
        )
        await session.commit()
        assert await next_invoice_number(session) == "5"


async def test_next_number_ignores_non_digit_and_foreign_sources(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Считаем только НАШУ серию (manual/kassa_invoice) и только чисто-цифровые номера:
    iiko-номера поставщиков и не-цифровые ручные номера подсказку не двигают/не регрессируют."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="П2", inn="7700001010")
        await _kassa_invoice(
            session, cp_id=cp.id, number="7", when=datetime(2026, 6, 25, 14, tzinfo=UTC)
        )
        # Не-цифровой ручной номер — игнор (иначе старая loose-логика дала бы регрессию).
        await make_invoice(
            session, counterparty_id=cp.id, amount="10", number="INV-99", source="manual"
        )
        # iiko-номер поставщика «500» — вне нашей серии, не должен раздувать счётчик до 501.
        await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="10",
            number="500",
            source="iiko",
            external_id="ext-500",
        )
        await session.commit()
        assert await next_invoice_number(session) == "8"


async def test_kassa_number_uniquified_same_date(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Два разных поставщика с номером «4» в один день → второй получает «4-2»."""
    async with async_session_factory() as session:
        cp1 = await make_counterparty(session, name="Поставщик A", inn="7700001002")
        cp2 = await make_counterparty(session, name="Поставщик B", inn="7700001003")
        when = datetime(2026, 6, 25, 14, tzinfo=UTC)
        a = await _kassa_invoice(session, cp_id=cp1.id, number="4", when=when)
        b = await _kassa_invoice(session, cp_id=cp2.id, number="4", when=when)
        await session.commit()
        assert a.number == "4"
        assert b.number == "4-2"


async def test_kassa_number_kept_across_dates(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Тот же номер «4» в РАЗНЫЕ дни конфликта в iiko не даёт → номер сохраняется."""
    async with async_session_factory() as session:
        cp1 = await make_counterparty(session, name="Поставщик C", inn="7700001004")
        cp2 = await make_counterparty(session, name="Поставщик D", inn="7700001005")
        a = await _kassa_invoice(
            session, cp_id=cp1.id, number="4", when=datetime(2026, 6, 23, 12, tzinfo=UTC)
        )
        b = await _kassa_invoice(
            session, cp_id=cp2.id, number="4", when=datetime(2026, 6, 25, 12, tzinfo=UTC)
        )
        await session.commit()
        assert a.number == "4"
        assert b.number == "4"
