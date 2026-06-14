"""Barter loan settlement: auto-net by exact sum + номенклатура, manual confirm.

Returns are recorded at the same ruble sum as the loan (possibly batched into one
invoice), so matching is by amount (1:1 or subset-sum) disambiguated by product set.
Auto only on a unique 100% match; otherwise the manager confirms.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from cp_helpers import make_counterparty, make_invoice
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import SupplierInvoice
from app.services.counterparty_barter_match import (
    BarterMatchError,
    auto_settle_barter,
    barter_detail,
    confirm_barter_settlement,
    settled_roles,
)


def items(*product_ids: str) -> list[dict[str, str]]:
    return [{"product_id": pid, "name": pid, "article": pid} for pid in product_ids]


async def _barter_cp(session: AsyncSession, inn: str):
    return await make_counterparty(session, name="Барт", inn=inn, relationship="barter")


async def _settled_ids(session: AsyncSession, cp_id) -> set:
    rows = (
        (
            await session.execute(
                select(SupplierInvoice).where(SupplierInvoice.counterparty_id == cp_id)
            )
        )
        .scalars()
        .all()
    )
    return {row.id for row in rows if row.barter_settlement_id is not None}


async def test_auto_settle_1to1_exact(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await _barter_cp(session, "7710000001")
        p = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="2030.00",
            direction="payable",
            line_items=items("A"),
        )
        r = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="2030.00",
            direction="receivable",
            line_items=items("A"),
        )
        await session.commit()

        assert await auto_settle_barter(session, cp.id) == 1
        assert await _settled_ids(session, cp.id) == {p.id, r.id}


async def test_auto_settle_batched_return(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """One return invoice = sum of two loans; products = union → auto-settle."""
    async with async_session_factory() as session:
        cp = await _barter_cp(session, "7710000002")
        p1 = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="1643.98",
            direction="payable",
            line_items=items("chili"),
        )
        p2 = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="1078.00",
            direction="payable",
            line_items=items("fries"),
        )
        r = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="2721.98",
            direction="receivable",
            line_items=items("chili", "fries"),
        )
        await session.commit()

        assert await auto_settle_barter(session, cp.id) == 1
        assert await _settled_ids(session, cp.id) == {p1.id, p2.id, r.id}


async def test_no_auto_when_products_differ(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await _barter_cp(session, "7710000003")
        await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="1000.00",
            direction="payable",
            line_items=items("A"),
        )
        await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="1000.00",
            direction="receivable",
            line_items=items("B"),
        )
        await session.commit()

        assert await auto_settle_barter(session, cp.id) == 0
        assert await _settled_ids(session, cp.id) == set()


async def test_no_auto_without_line_items(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await _barter_cp(session, "7710000004")
        await make_invoice(session, counterparty_id=cp.id, amount="1000.00", direction="payable")
        await make_invoice(session, counterparty_id=cp.id, amount="1000.00", direction="receivable")
        await session.commit()

        assert await auto_settle_barter(session, cp.id) == 0  # no products → cannot verify


async def test_no_auto_when_ambiguous(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two identical payables + two identical receivables → no unique pairing → manual."""
    async with async_session_factory() as session:
        cp = await _barter_cp(session, "7710000005")
        for _ in range(2):
            await make_invoice(
                session,
                counterparty_id=cp.id,
                amount="1000.00",
                direction="payable",
                line_items=items("A"),
            )
            await make_invoice(
                session,
                counterparty_id=cp.id,
                amount="1000.00",
                direction="receivable",
                line_items=items("A"),
            )
        await session.commit()

        assert await auto_settle_barter(session, cp.id) == 0


async def test_confirm_manual_settles(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await _barter_cp(session, "7710000006")
        p = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="465.00",
            direction="payable",
            line_items=items("A"),
        )
        r = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="465.00",
            direction="receivable",
            line_items=items("A"),
        )
        await session.commit()

        settlement = await confirm_barter_settlement(
            session, cp.id, payable_ids=[p.id], receivable_ids=[r.id]
        )
        assert settlement.amount == Decimal("465.00")
        assert settlement.is_auto is False
        assert await _settled_ids(session, cp.id) == {p.id, r.id}


async def test_confirm_rejects_unequal_sums(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await _barter_cp(session, "7710000007")
        p = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="1000.00",
            direction="payable",
            line_items=items("A"),
        )
        r = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="500.00",
            direction="receivable",
            line_items=items("A"),
        )
        await session.commit()

        with pytest.raises(BarterMatchError, match="не совпадают"):
            await confirm_barter_settlement(
                session, cp.id, payable_ids=[p.id], receivable_ids=[r.id]
            )


async def test_barter_detail_excludes_settled(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await _barter_cp(session, "7710000008")
        await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="1000.00",
            direction="payable",
            line_items=items("A"),
        )
        await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="1000.00",
            direction="receivable",
            line_items=items("A"),
        )
        await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="300.00",
            direction="receivable",
            line_items=items("B"),
        )
        await session.commit()

        await auto_settle_barter(session, cp.id)  # settles the 1000 pair
        detail = await barter_detail(session, cp.id)

        assert detail.relationship_balance == Decimal("-300.00")  # they owe us 300
        assert len(detail.open_payables) == 0
        assert len(detail.open_receivables) == 1
        assert len(detail.settlements) == 1
        assert detail.settlements[0].is_auto is True


async def test_auto_settle_reverse_we_lent(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Ёбидоёби-кейс: МЫ заняли (ранний расход) → партнёр вернул (поздний приход).

    Старая логика «приход=заём нам, расход=мы вернули» этот случай не сводила (требовала
    расход-возврат ПОСЛЕ прихода-займа). Хронология сводит: ранняя накладная — заём,
    поздняя — возврат, независимо от направления.
    """
    async with async_session_factory() as session:
        cp = await _barter_cp(session, "7710000030")
        loan = await make_invoice(
            session, counterparty_id=cp.id, amount="700.00", direction="receivable",
            number="AR-2", line_items=items("SAUCE"), invoice_date=date(2026, 6, 2),
        )
        ret = await make_invoice(
            session, counterparty_id=cp.id, amount="700.00", direction="payable",
            number="AP-5", line_items=items("SAUCE"), invoice_date=date(2026, 6, 5),
        )
        await session.commit()

        assert await auto_settle_barter(session, cp.id) == 1
        assert await _settled_ids(session, cp.id) == {loan.id, ret.id}

        roles = await settled_roles(session, [loan.barter_settlement_id])
        assert roles[loan.id] == "loan"  # ранний расход = наш заём
        assert roles[ret.id] == "return"  # поздний приход = возврат партнёра
        detail = await barter_detail(session, cp.id)
        assert detail.settlements[0].we_lent is True
        assert detail.settlements[0].loan_numbers == ["AR-2"]
        assert detail.settlements[0].return_numbers == ["AP-5"]


async def test_auto_settle_fifo_closes_earliest_loan(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Два одинаковых наших займа + два одинаковых возврата: ранний возврат гасит ранний
    заём (FIFO). Без дат это было бы неоднозначно (остаётся ручным); с датами — авто."""
    async with async_session_factory() as session:
        cp = await _barter_cp(session, "7710000031")
        l1 = await make_invoice(
            session, counterparty_id=cp.id, amount="100.00", direction="receivable",
            number="L1", line_items=items("A"), invoice_date=date(2026, 6, 1),
        )
        l2 = await make_invoice(
            session, counterparty_id=cp.id, amount="100.00", direction="receivable",
            number="L2", line_items=items("A"), invoice_date=date(2026, 6, 2),
        )
        r1 = await make_invoice(
            session, counterparty_id=cp.id, amount="100.00", direction="payable",
            number="R1", line_items=items("A"), invoice_date=date(2026, 6, 3),
        )
        r2 = await make_invoice(
            session, counterparty_id=cp.id, amount="100.00", direction="payable",
            number="R2", line_items=items("A"), invoice_date=date(2026, 6, 4),
        )
        await session.commit()

        assert await auto_settle_barter(session, cp.id) == 2
        for inv in (l1, l2, r1, r2):
            await session.refresh(inv)
        # FIFO: l1↔r1, l2↔r2; the two settlements are distinct.
        assert l1.barter_settlement_id == r1.barter_settlement_id
        assert l2.barter_settlement_id == r2.barter_settlement_id
        assert l1.barter_settlement_id != l2.barter_settlement_id


async def test_role_tie_break_by_number_same_date(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Заём и возврат в один день: больший номер — более поздняя накладная (возврат)."""
    async with async_session_factory() as session:
        cp = await _barter_cp(session, "7710000032")
        a = await make_invoice(
            session, counterparty_id=cp.id, amount="500.00", direction="receivable",
            number="N-001", line_items=items("A"), invoice_date=date(2026, 6, 7),
        )
        b = await make_invoice(
            session, counterparty_id=cp.id, amount="500.00", direction="payable",
            number="N-002", line_items=items("A"), invoice_date=date(2026, 6, 7),
        )
        await session.commit()

        assert await auto_settle_barter(session, cp.id) == 1
        roles = await settled_roles(session, [a.barter_settlement_id])
        assert roles[a.id] == "loan"  # N-001 раньше → заём (расход → мы заняли)
        assert roles[b.id] == "return"  # N-002 позже → возврат


async def test_list_invoices_exposes_barter_role(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Инбокс отдаёт роль (loan/return) у сведённых бартер-накладных, не только direction."""
    from app.services.counterparty_registry import list_invoices

    async with async_session_factory() as session:
        cp = await _barter_cp(session, "7710000033")
        loan = await make_invoice(
            session, counterparty_id=cp.id, amount="700.00", direction="receivable",
            number="AR-9", line_items=items("S"), invoice_date=date(2026, 6, 2),
        )
        ret = await make_invoice(
            session, counterparty_id=cp.id, amount="700.00", direction="payable",
            number="AP-9", line_items=items("S"), invoice_date=date(2026, 6, 5),
        )
        await session.commit()
        await auto_settle_barter(session, cp.id)

        by_id = {item.id: item for item in await list_invoices(session, counterparty_id=cp.id)}
        assert by_id[loan.id].barter_role == "loan"
        assert by_id[ret.id].barter_role == "return"
