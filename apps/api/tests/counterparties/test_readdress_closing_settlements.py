"""Ремонтный скрипт: адресное перегашение закрывающего документа указанным авансом.

Кейс Лемы (прод, 24.09.2026). УПД 32108 за август погасил аванс за СЕНТЯБРЬ по равной сумме, а
августовский остался открытым. Лестницу исправили, но уже погашенный документ сам не
переподбирается — его перегашает скрипт. Перекрёст здесь заводится напрямую аллокацией, ровно
как он лежит в базе прода, а не прогоном старой лестницы.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from cp_helpers import make_counterparty, make_wallet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    AccountingPeriodClose,
    CashflowTransaction,
    DdsArticle,
    InvoicePaymentAllocation,
    Location,
    LocationLease,
    Organization,
    SupplierInvoice,
    SupplierPrepayment,
    UtilityAccount,
)
from app.scripts import readdress_closing_settlements as script
from app.services import supplier_prepayments, utility_charges

AUGUST = (date(2026, 8, 1), date(2026, 8, 31))
SEPTEMBER = (date(2026, 9, 1), date(2026, 9, 30))


async def _bill(
    session: AsyncSession, *, counterparty_id: uuid.UUID, number: str, on: date, period
) -> SupplierInvoice:
    bill = SupplierInvoice(
        counterparty_id=counterparty_id,
        source="email",
        direction="payable",
        doc_kind="bill",
        operational_scope="finance",
        number=number,
        invoice_date=on,
        amount=Decimal("3700.00"),
        payment_status="paid",
        service_period_start=period[0],
        service_period_end=period[1],
        service_period_status="ready",
    )
    session.add(bill)
    await session.flush()
    return bill


async def _prepaid(
    session: AsyncSession,
    *,
    counterparty_id: uuid.UUID,
    bill: SupplierInvoice,
    paid_on: date,
    wallet_code: str,
    period,
    amount: str = "3700.00",
) -> SupplierPrepayment:
    wallet = await make_wallet(session, code=wallet_code, name=f"Кошелёк {wallet_code}")
    tx = CashflowTransaction(
        wallet_id=wallet.id,
        direction="out",
        amount=Decimal(amount),
        operation_date=paid_on,
        counterparty_id=counterparty_id,
        source_kind="bank_feed",
    )
    session.add(tx)
    await session.flush()
    prepayment = SupplierPrepayment(
        counterparty_id=counterparty_id,
        kind="prepaid_bill",
        wallet_id=wallet.id,
        amount=Decimal(amount),
        amount_settled=Decimal("0.00"),
        status="open",
        cashflow_transaction_id=tx.id,
        bill_invoice_id=bill.id,
        service_period_start=period[0],
        service_period_end=period[1],
        service_period_status="ready",
    )
    session.add(prepayment)
    await session.flush()
    return prepayment


async def _crossed_lema(session: AsyncSession, *, tag: str, august_amount: str = "3700.00"):
    """Лема как на проде: УПД за август висит на сентябрьском авансе рангом ``amount``."""
    cp = await make_counterparty(
        session, name=f"Лема-{tag}", inn=f"77{uuid.uuid4().int % 10**8:08d}"
    )
    august_bill = await _bill(
        session, counterparty_id=cp.id, number="70221/1/У", on=date(2026, 7, 8), period=AUGUST
    )
    september_bill = await _bill(
        session, counterparty_id=cp.id, number="73163/1/У", on=date(2026, 8, 8), period=SEPTEMBER
    )
    august_money = await _prepaid(
        session,
        counterparty_id=cp.id,
        bill=august_bill,
        paid_on=date(2026, 7, 10),
        wallet_code=f"{tag}-aug",
        period=AUGUST,
        amount=august_amount,
    )
    september_money = await _prepaid(
        session,
        counterparty_id=cp.id,
        bill=september_bill,
        paid_on=date(2026, 8, 10),
        wallet_code=f"{tag}-sep",
        period=SEPTEMBER,
    )
    act = SupplierInvoice(
        counterparty_id=cp.id,
        source="sbis",
        direction="payable",
        doc_kind="closing",
        operational_scope="finance",
        number="32108",
        invoice_date=date(2026, 8, 31),
        amount=Decimal("3700.00"),
        payment_status="paid",
        activation_status="active",
        service_period_start=AUGUST[0],
        service_period_end=AUGUST[1],
        service_period_status="ready",
    )
    session.add(act)
    await session.flush()
    session.add(
        InvoicePaymentAllocation(
            invoice_id=act.id,
            source_kind="prepayment",
            prepayment_id=september_money.id,
            amount=Decimal("3700.00"),
            match_basis="amount",
        )
    )
    september_money.amount_settled = Decimal("3700.00")
    september_money.status = "settled"
    await session.flush()
    await session.commit()
    return cp, act, august_money, september_money


async def _prepayment_trail(session: AsyncSession, invoice_id: uuid.UUID) -> list[uuid.UUID | None]:
    return list(
        (
            await session.scalars(
                select(InvoicePaymentAllocation.prepayment_id).where(
                    InvoicePaymentAllocation.invoice_id == invoice_id,
                    InvoicePaymentAllocation.source_kind == "prepayment",
                )
            )
        ).all()
    )


async def test_readdress_moves_lema_act_to_its_own_month(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """УПД за август переезжает на августовский аванс, сентябрьский снова открыт, нетто то же."""
    async with async_session_factory() as session:
        cp, act, august_money, september_money = await _crossed_lema(session, tag="ok")
        net_before = await script.counterparty_net(session, cp.id)

        lines: list[str] = []
        result = await script.readdress_closing(
            session, closing_id=act.id, prepayment_id=august_money.id, out=lines.append
        )
        await session.commit()

        assert result.changed
        assert result.released == Decimal("3700.00")
        assert result.settled == Decimal("3700.00")
        assert await _prepayment_trail(session, act.id) == [august_money.id]
        await session.refresh(act)
        await session.refresh(august_money)
        await session.refresh(september_money)
        assert act.payment_status == "paid"
        assert (act.service_period_start, act.service_period_end) == AUGUST
        assert (august_money.status, august_money.amount_settled) == ("settled", Decimal("3700.00"))
        assert (september_money.status, september_money.amount_settled) == (
            "open",
            Decimal("0.00"),
        )
        assert await script.counterparty_net(session, cp.id) == net_before
        assert any("[до]" in line for line in lines) and any("[после]" in line for line in lines)

        # Повторный запуск той же пары — не ошибка и не второе движение.
        again = await script.readdress_closing(
            session, closing_id=act.id, prepayment_id=august_money.id, out=lines.append
        )
        assert not again.changed
        assert await _prepayment_trail(session, act.id) == [august_money.id]


async def _assert_untouched(
    session: AsyncSession, act: SupplierInvoice, september_money: SupplierPrepayment
) -> None:
    """Отказ ничего не записал: документ висит на прежнем авансе, тот по-прежнему погашен."""
    act_id, september_id = act.id, september_money.id
    await session.rollback()
    assert await _prepayment_trail(session, act_id) == [september_id]
    refreshed = await session.get(SupplierPrepayment, september_id)
    assert refreshed is not None and refreshed.status == "settled"


async def test_refuses_prepayment_of_another_counterparty(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        _, act, _, september_money = await _crossed_lema(session, tag="own")
        _, _, stranger_money, _ = await _crossed_lema(session, tag="stranger")

        with pytest.raises(script.ReaddressRefused, match="другого контрагента"):
            await script.readdress_closing(
                session, closing_id=act.id, prepayment_id=stranger_money.id, out=lambda _: None
            )
        await _assert_untouched(session, act, september_money)


async def test_refuses_when_target_prepayment_is_too_small(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        _, act, august_money, september_money = await _crossed_lema(
            session, tag="short", august_amount="3000.00"
        )

        with pytest.raises(script.ReaddressRefused, match="свободно 3000.00"):
            await script.readdress_closing(
                session, closing_id=act.id, prepayment_id=august_money.id, out=lambda _: None
            )
        await _assert_untouched(session, act, september_money)


async def test_refuses_in_closed_month(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Август закрыт замком — перегашение его документа уходит к человеку, а не проходит молча."""
    async with async_session_factory() as session:
        _, act, august_money, september_money = await _crossed_lema(session, tag="locked")
        session.add(AccountingPeriodClose(period_month=date(2026, 8, 1)))
        await session.commit()

        with pytest.raises(script.ReaddressRefused, match="закрыт"):
            await script.readdress_closing(
                session, closing_id=act.id, prepayment_id=august_money.id, out=lambda _: None
            )
        await _assert_untouched(session, act, september_money)


async def test_refuses_prepayment_of_unrelated_period(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Скрипт исправляет угаданное на доказуемое, а не наоборот: сентябрь августу не пара."""
    async with async_session_factory() as session:
        cp, act, august_money, september_money = await _crossed_lema(session, tag="period")
        # Вернём документ на августовский аванс и попробуем перегасить его обратно на сентябрь.
        await script.readdress_closing(
            session, closing_id=act.id, prepayment_id=august_money.id, out=lambda _: None
        )
        await session.commit()

        with pytest.raises(script.ReaddressRefused, match="не относится"):
            await script.readdress_closing(
                session, closing_id=act.id, prepayment_id=september_money.id, out=lambda _: None
            )
        act_id, august_id = act.id, august_money.id
        await session.rollback()
        assert await _prepayment_trail(session, act_id) == [august_id]


async def test_readdress_returns_rent_money_and_takes_water_bill_money(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Вода Станислава Юрьевича (прод, 01.09.2026): акт бота висит на аренде вперёд.

    Акт коммуналки погасился арендным авансом хронологией, а ДЗ его собственного счёта,
    оплаченного из Сейфа, осталась открытой. Счёт и акт пары бота несут один номер — это и
    есть счёт-основание. После перегашения аренда снова свободна, водяная ДЗ закрыта."""
    async with async_session_factory() as session:
        cp = await make_counterparty(
            session, name="Станислав-вода", inn=f"50{uuid.uuid4().int % 10**8:08d}"
        )
        water_bill = SupplierInvoice(
            counterparty_id=cp.id,
            source="utility",
            direction="payable",
            doc_kind="bill",
            operational_scope="finance",
            number="ВОДА-08.2026",
            invoice_date=date(2026, 9, 1),
            amount=Decimal("9429.75"),
            payment_status="paid",
            service_period_start=AUGUST[0],
            service_period_end=AUGUST[1],
            service_period_status="ready",
        )
        session.add(water_bill)
        await session.flush()
        water_money = await _prepaid(
            session,
            counterparty_id=cp.id,
            bill=water_bill,
            paid_on=date(2026, 9, 1),
            wallet_code="water-safe",
            period=AUGUST,
            amount="9429.75",
        )
        rent_money = SupplierPrepayment(
            counterparty_id=cp.id,
            kind="subscription",
            amount=Decimal("50000.00"),
            amount_settled=Decimal("9429.75"),
            status="partially_settled",
            service_period_status="missing",
        )
        session.add(rent_money)
        act = SupplierInvoice(
            counterparty_id=cp.id,
            source="utility",
            direction="payable",
            doc_kind="closing",
            operational_scope="finance",
            number="ВОДА-08.2026",
            invoice_date=date(2026, 8, 31),
            amount=Decimal("9429.75"),
            payment_status="paid",
            activation_status="active",
            service_period_start=AUGUST[0],
            service_period_end=AUGUST[1],
            service_period_status="ready",
        )
        session.add(act)
        await session.flush()
        session.add(
            InvoicePaymentAllocation(
                invoice_id=act.id,
                source_kind="prepayment",
                prepayment_id=rent_money.id,
                amount=Decimal("9429.75"),
                match_basis="chronology",
            )
        )
        await session.commit()
        net_before = await script.counterparty_net(session, cp.id)

        result = await script.readdress_closing(
            session, closing_id=act.id, prepayment_id=water_money.id, out=lambda _: None
        )
        await session.commit()

        assert result.changed
        assert await _prepayment_trail(session, act.id) == [water_money.id]
        await session.refresh(rent_money)
        await session.refresh(water_money)
        assert (rent_money.status, rent_money.amount_settled) == ("open", Decimal("0.00"))
        assert (water_money.status, water_money.amount_settled) == ("settled", Decimal("9429.75"))
        assert await script.counterparty_net(session, cp.id) == net_before


async def _landlord_water_on_rent(session: AsyncSession):
    """Станислав Юрьевич как на проде 01.09.2026: пара бота за август, акт висит на аренде.

    Всё собрано штатными дверями, кроме двух шагов, которые воспроизводят СОСТОЯНИЕ базы прода:
    зачёт акта «Арендой вперёд» (его делала старая лестница хронологией — исправленная так уже
    не сделает) и ДЗ оплаченного из Сейфа счёта воды, заведённая без обратного порядка (он
    теперь сам перенёс бы акт на свои деньги — а на проде перенос уже не случится)."""
    organization_id = await session.scalar(select(Organization.id).limit(1))
    if organization_id is None:
        organization = Organization(id=uuid.uuid4(), name="Тест-организация")
        session.add(organization)
        await session.flush()
        organization_id = organization.id
    location = Location(
        id=uuid.uuid4(), organization_id=organization_id, name=f"Черникова {uuid.uuid4().hex[:4]}"
    )
    session.add(location)
    landlord = await make_counterparty(
        session,
        name=f"Станислав Юрьевич {uuid.uuid4().hex[:4]}",
        inn=f"6143{uuid.uuid4().int % 10**8:08d}",
        cp_type="individual",
        relationship="informal",
    )
    rent_article, water_article = (
        DdsArticle(
            id=uuid.uuid4(),
            code=f"art_{uuid.uuid4().hex[:8]}",
            name=name,
            movement_type="outflow",
            activity_type="operating",
            location_required=True,
            lease_bound=lease_bound,
        )
        for name, lease_bound in (("Аренда", True), ("Вода", False))
    )
    session.add_all([rent_article, water_article])
    await session.flush()
    lease = LocationLease(
        id=uuid.uuid4(),
        location_id=location.id,
        counterparty_id=landlord.id,
        monthly_amount=Decimal("50000.00"),
        started_on=date(2026, 1, 1),
        dds_article_id=rent_article.id,
        payment_mode="prepaid",
        documents_mode="informal",
        accrual_enabled=False,
    )
    water = UtilityAccount(
        location_id=location.id,
        counterparty_id=landlord.id,
        kind="water",
        dds_article_id=water_article.id,
        started_on=date(2026, 1, 1),
        is_active=True,
    )
    session.add_all([lease, water])
    await session.flush()

    safe = await make_wallet(session, code=f"safe-{uuid.uuid4().hex[:6]}", name="Сейф")
    rent_tx = CashflowTransaction(
        wallet_id=safe.id,
        direction="out",
        amount=Decimal("50000.00"),
        operation_date=date(2026, 8, 25),
        counterparty_id=landlord.id,
        article_id=rent_article.id,
        lease_id=lease.id,
        source_kind="safe_payout",
    )
    session.add(rent_tx)
    await session.flush()
    rent_money = SupplierPrepayment(
        counterparty_id=landlord.id,
        kind="subscription",
        wallet_id=safe.id,
        amount=Decimal("50000.00"),
        amount_settled=Decimal("0.00"),
        status="open",
        cashflow_transaction_id=rent_tx.id,
        article_id=rent_article.id,
    )
    session.add(rent_money)
    await session.flush()

    water_bill, water_act = await utility_charges.build_utility_documents(
        session,
        water,
        period_start=AUGUST[0],
        period_end=AUGUST[1],
        expense_amount=Decimal("9429.75"),
        payable_amount=Decimal("9429.75"),
        as_of=date(2026, 9, 1),
    )
    assert water_act is not None
    await supplier_prepayments._allocate_invoice_from_prepayment(
        session,
        invoice=water_act,
        prepayment=rent_money,
        amount=Decimal("9429.75"),
        actor_user_id=None,
        match_basis="chronology",
    )
    await supplier_prepayments._recompute_status(session, water_act)

    water_tx = CashflowTransaction(
        wallet_id=safe.id,
        direction="out",
        amount=Decimal("9429.75"),
        operation_date=date(2026, 9, 1),
        counterparty_id=landlord.id,
        article_id=water_article.id,
        source_kind="safe_payout",
    )
    session.add(water_tx)
    await session.flush()
    session.add(
        InvoicePaymentAllocation(
            invoice_id=water_bill.id,
            source_kind="bank",
            cashflow_transaction_id=water_tx.id,
            amount=Decimal("9429.75"),
        )
    )
    water_bill.payment_status = "paid"
    water_money = SupplierPrepayment(
        counterparty_id=landlord.id,
        kind="prepaid_bill",
        wallet_id=safe.id,
        amount=Decimal("9429.75"),
        amount_settled=Decimal("0.00"),
        status="open",
        cashflow_transaction_id=water_tx.id,
        article_id=water_article.id,
        bill_invoice_id=water_bill.id,
        service_period_start=AUGUST[0],
        service_period_end=AUGUST[1],
        service_period_status="ready",
    )
    session.add(water_money)
    await session.commit()
    return landlord, water_act, water_money, rent_money


async def test_readdress_landlord_water_act_from_rent_to_its_own_bill(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Прод-пара 42d6dfe5 → 3321f346 в том виде, в каком она лежит в базе.

    Акт бота несёт ключ потока, аванс — ДЗ счёта того же потока и месяца: пара проходит и
    проверку адресности скрипта, и фильтр потоков арендодателя в авто-зачёте. После перегашения
    «Аренда вперёд» свободна целиком — 01.10 её честно заберёт «Аренда 09.2026», — а водяная ДЗ
    закрыта своим актом. Обратное направление — вода на аренду — скрипт не пропускает."""
    async with async_session_factory() as session:
        landlord, act, water_money, rent_money = await _landlord_water_on_rent(session)
        assert await _prepayment_trail(session, act.id) == [rent_money.id]
        net_before = await script.counterparty_net(session, landlord.id)

        result = await script.readdress_closing(
            session, closing_id=act.id, prepayment_id=water_money.id, out=lambda _: None
        )
        await session.commit()

        assert result.changed
        assert await _prepayment_trail(session, act.id) == [water_money.id]
        await session.refresh(act)
        await session.refresh(rent_money)
        await session.refresh(water_money)
        assert act.payment_status == "paid"
        assert (rent_money.status, rent_money.amount_settled) == ("open", Decimal("0.00"))
        assert (water_money.status, water_money.amount_settled) == ("settled", Decimal("9429.75"))
        assert await script.counterparty_net(session, landlord.id) == net_before

        with pytest.raises(script.ReaddressRefused, match="не относится"):
            await script.readdress_closing(
                session, closing_id=act.id, prepayment_id=rent_money.id, out=lambda _: None
            )
        act_id, water_id = act.id, water_money.id
        await session.rollback()
        assert await _prepayment_trail(session, act_id) == [water_id]


async def _unbound_closing(session: AsyncSession, *, flag: str):
    """Октябрьский документ с октябрьским авансом: будущий (``pending``) или справочный."""
    cp = await make_counterparty(
        session, name=f"Не-долг-{flag}", inn=f"77{uuid.uuid4().int % 10**8:08d}"
    )
    october = (date(2026, 10, 1), date(2026, 10, 31))
    wallet = await make_wallet(session, code=f"{flag}-{uuid.uuid4().hex[:6]}", name="Банк")
    tx = CashflowTransaction(
        wallet_id=wallet.id,
        direction="out",
        amount=Decimal("3700.00"),
        operation_date=date(2026, 9, 10),
        counterparty_id=cp.id,
        source_kind="bank_feed",
    )
    session.add(tx)
    await session.flush()
    money = SupplierPrepayment(
        counterparty_id=cp.id,
        kind="subscription",
        wallet_id=wallet.id,
        amount=Decimal("3700.00"),
        amount_settled=Decimal("0.00"),
        status="open",
        cashflow_transaction_id=tx.id,
        service_period_start=october[0],
        service_period_end=october[1],
        service_period_status="ready",
    )
    act = SupplierInvoice(
        counterparty_id=cp.id,
        source="sbis",
        direction="payable",
        doc_kind="closing",
        operational_scope="finance",
        number=f"UPD-{flag}",
        invoice_date=date(2026, 10, 31),
        amount=Decimal("3700.00"),
        payment_status="unpaid",
        activation_status="pending" if flag == "pending" else "active",
        informational=flag == "informational",
        service_period_start=october[0],
        service_period_end=october[1],
        service_period_status="ready",
    )
    session.add_all([money, act])
    await session.commit()
    return cp, act, money


@pytest.mark.parametrize(
    ("flag", "reason"),
    [("pending", "не вступил в силу"), ("informational", "справочный")],
)
async def test_refuses_document_that_does_not_bind_settlement(
    async_session_factory: async_sessionmaker[AsyncSession], flag: str, reason: str
) -> None:
    """Скептик S7: будущий и справочный документ ДЗ не гасят — ни штатно, ни скриптом.

    Пара проходит все прочие проверки (тот же контрагент, периоды совпадают, денег хватает),
    поэтому отказ здесь — именно канон, а не случайный соседний фильтр. И нетто не видит их
    суммы как КЗ: иначе мнимый долг маскировал бы зачёт, которого быть не должно."""
    async with async_session_factory() as session:
        cp, act, money = await _unbound_closing(session, flag=flag)
        assert await script.counterparty_net(session, cp.id) == Decimal("3700.00")

        with pytest.raises(script.ReaddressRefused, match=reason):
            await script.readdress_closing(
                session, closing_id=act.id, prepayment_id=money.id, out=lambda _: None
            )
        act_id, money_id = act.id, money.id
        await session.rollback()
        assert await _prepayment_trail(session, act_id) == []
        refreshed = await session.get(SupplierPrepayment, money_id)
        assert refreshed is not None
        assert (refreshed.status, refreshed.amount_settled) == ("open", Decimal("0.00"))
