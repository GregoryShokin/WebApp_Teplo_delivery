"""Деньги потоков арендодателя не смешиваются: аренда, вода и свет гасят только свои документы.

У арендодателя (Станислав Юрьевич, Виталий — Черникова) под одним контрагентом живут несколько
потоков денег: аренда (закрывающие ``source='lease'`` «Аренда MM.YYYY», наличные вперёд
«Аренда вперёд») и коммуналка — вода, свет, газ (пары «счёт + акт» ``source='utility'`` из
бота, ДЗ оплаченного счёта ``prepaid_bill``). Лестница адресности — это ПОРЯДОК: без
адресного признака документ гасится любым открытым авансом контрагента по хронологии. У
обычного поставщика так и надо, у арендодателя это перекрёст: прод, 01.09.2026 — акт воды за
август закрылся «Арендой вперёд», свой счёт оплатили через час, и его ДЗ повисла открытой;
01.10 «Аренда 09.2026» доедала бы водяные деньги.

Поэтому поток — ФИЛЬТР в ядре зачёта, а не параметр одной двери: акт коммуналки не гасится
арендой и деньгами другого потока, арендный акт — деньгами коммуналки, в любой двери зачёта из
авансов — приём ботом, активация 1-го числа, обратный порядок при оплате чужого счёта. Фильтр —
чёрный список: отвергается только ЯВНО чужое, деньги неизвестного назначения годятся, как на
main. Акт без своих денег остаётся честной КЗ, чужая ДЗ — открытой. Мимо фильтра идёт только
правило 1 — банковские деньги прямо на открытую КЗ (известное ограничение, как на main).

Плюс три стыка переноса угаданного зачёта на деньги своего счёта: своя ДЗ сначала своему
акту; пара электричества (авансовый и фактический счёт с одинаковым номером) связывается по
``external_id``; закрытый замком месяц перенос не переписывает.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from cp_helpers import make_counterparty, make_wallet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    AccountingPeriodClose,
    CashflowTransaction,
    Counterparty,
    DdsArticle,
    InvoicePaymentAllocation,
    Location,
    LocationLease,
    Organization,
    SupplierExpenseAccrual,
    SupplierInvoice,
    SupplierPrepayment,
    UtilityAccount,
)
from app.services import supplier_prepayments, utility_charges
from app.services.counterparty_balance_as_of import build_balance_as_of
from app.services.lease_accruals import ensure_lease_invoice, settle_lease_invoice_from_cash
from app.services.pnl.sources.waiting import build_waiting_layer

AUGUST = (date(2026, 8, 1), date(2026, 8, 31))
SEPTEMBER = (date(2026, 9, 1), date(2026, 9, 30))


async def _article(session: AsyncSession, *, name: str, lease_bound: bool = False) -> DdsArticle:
    article = DdsArticle(
        id=uuid.uuid4(),
        code=f"art_{uuid.uuid4().hex[:8]}",
        name=name,
        movement_type="outflow",
        activity_type="operating",
        location_required=True,
        lease_bound=lease_bound,
    )
    session.add(article)
    await session.flush()
    return article


async def _location(session: AsyncSession) -> Location:
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
    await session.flush()
    return location


async def _landlord(session: AsyncSession) -> tuple[Counterparty, Location]:
    landlord = await make_counterparty(
        session,
        name=f"Арендодатель {uuid.uuid4().hex[:4]}",
        inn=f"6143{uuid.uuid4().int % 10**8:08d}",
        cp_type="individual",
        relationship="informal",
    )
    return landlord, await _location(session)


async def _stream(
    session: AsyncSession, landlord: Counterparty, location: Location, *, kind: str, article: str
) -> UtilityAccount:
    account = UtilityAccount(
        location_id=location.id,
        counterparty_id=landlord.id,
        kind=kind,
        dds_article_id=(await _article(session, name=article)).id,
        started_on=date(2026, 1, 1),
        is_active=True,
    )
    session.add(account)
    await session.flush()
    return account


async def _lease(
    session: AsyncSession,
    landlord: Counterparty,
    location: Location,
    *,
    article_id: uuid.UUID | None,
    accrual_enabled: bool = True,
) -> LocationLease:
    lease = LocationLease(
        id=uuid.uuid4(),
        location_id=location.id,
        counterparty_id=landlord.id,
        monthly_amount=Decimal("50000.00"),
        started_on=date(2026, 1, 1),
        dds_article_id=article_id,
        payment_mode="prepaid",
        documents_mode="informal",
        accrual_enabled=accrual_enabled,
    )
    session.add(lease)
    await session.flush()
    return lease


async def _rent_paid_forward(
    session: AsyncSession, lease: LocationLease, *, on: date
) -> SupplierPrepayment:
    """«Аренда вперёд» штатной дверью — наличная выдача по договору, пока акта месяца нет."""
    wallet = await make_wallet(session, name="Сейф")
    tx = CashflowTransaction(
        wallet_id=wallet.id,
        direction="out",
        amount=Decimal("50000.00"),
        operation_date=on,
        counterparty_id=lease.counterparty_id,
        article_id=lease.dds_article_id,
        lease_id=lease.id,
        source_kind="safe_payout",
        quality_status="final",
    )
    session.add(tx)
    await session.flush()
    await settle_lease_invoice_from_cash(
        session,
        lease_id=lease.id,
        transaction_id=tx.id,
        amount=Decimal("50000.00"),
        wallet_id=wallet.id,
    )
    rent = await session.scalar(
        select(SupplierPrepayment).where(SupplierPrepayment.cashflow_transaction_id == tx.id)
    )
    assert rent is not None
    return rent


async def _bare_rent_money(
    session: AsyncSession, landlord: Counterparty, *, on: date
) -> SupplierPrepayment:
    """Арендные деньги так, как они лежат на проде: свободный аванс со статьёй аренды."""
    rent_article = await _article(session, name="Аренда торговых точек", lease_bound=True)
    wallet = await make_wallet(session, name="Сейф")
    tx = CashflowTransaction(
        wallet_id=wallet.id,
        direction="out",
        amount=Decimal("50000.00"),
        operation_date=on,
        counterparty_id=landlord.id,
        article_id=rent_article.id,
        source_kind="safe_payout",
        quality_status="final",
    )
    session.add(tx)
    await session.flush()
    rent = SupplierPrepayment(
        counterparty_id=landlord.id,
        kind=supplier_prepayments.RULE1_PREPAYMENT_KIND,
        wallet_id=wallet.id,
        amount=Decimal("50000.00"),
        amount_settled=Decimal("0.00"),
        status="open",
        cashflow_transaction_id=tx.id,
        article_id=rent_article.id,
        note="Аренда вперёд: обязательство ещё не вступило в силу",
    )
    session.add(rent)
    await session.flush()
    return rent


async def _bot_pair(
    session: AsyncSession,
    account: UtilityAccount,
    *,
    amount: str,
    period: tuple[date, date] = AUGUST,
    as_of: date = date(2026, 9, 1),
) -> tuple[SupplierInvoice, SupplierInvoice]:
    bill, closing = await utility_charges.build_utility_documents(
        session,
        account,
        period_start=period[0],
        period_end=period[1],
        expense_amount=Decimal(amount),
        payable_amount=Decimal(amount),
        as_of=as_of,
    )
    assert closing is not None
    return bill, closing


async def _pay_bill(session: AsyncSession, bill: SupplierInvoice, *, on: date) -> None:
    """Оплата счёта штатной дверью: денежная аллокация + чокпоинт ``reconcile_bill_prepayment``."""
    wallet = await make_wallet(session, name="Т-Банк")
    tx = CashflowTransaction(
        wallet_id=wallet.id,
        direction="out",
        amount=bill.amount,
        operation_date=on,
        counterparty_id=bill.counterparty_id,
        source_kind="bank_feed",
    )
    session.add(tx)
    await session.flush()
    session.add(
        InvoicePaymentAllocation(
            invoice_id=bill.id,
            source_kind="bank",
            cashflow_transaction_id=tx.id,
            amount=bill.amount,
        )
    )
    await session.flush()
    await supplier_prepayments.reconcile_bill_prepayment(session, bill)
    await session.flush()


async def _own(session: AsyncSession, bill: SupplierInvoice) -> SupplierPrepayment:
    own = await session.scalar(
        select(SupplierPrepayment).where(
            SupplierPrepayment.bill_invoice_id == bill.id,
            SupplierPrepayment.kind == supplier_prepayments.BILL_PREPAYMENT_KIND,
        )
    )
    assert own is not None, "оплата счёта не завела дебиторку"
    return own


async def _trail(
    session: AsyncSession, invoice_id: uuid.UUID
) -> list[tuple[uuid.UUID | None, Decimal, str | None]]:
    rows = (
        await session.scalars(
            select(InvoicePaymentAllocation)
            .where(InvoicePaymentAllocation.invoice_id == invoice_id)
            .order_by(InvoicePaymentAllocation.created_at, InvoicePaymentAllocation.id)
        )
    ).all()
    return [(a.prepayment_id, a.amount, a.match_basis) for a in rows]


async def _legacy_guess(
    session: AsyncSession, closing: SupplierInvoice, money: SupplierPrepayment
) -> None:
    """Прод-состояние до выкатки: акт уже погашен чужим авансом «наугад»."""
    await supplier_prepayments._allocate_invoice_from_prepayment(
        session,
        invoice=closing,
        prepayment=money,
        amount=Decimal(closing.amount),
        actor_user_id=None,
        match_basis=supplier_prepayments.MATCH_CHRONOLOGY,
    )
    await supplier_prepayments._recompute_status(session, closing)
    await session.flush()


# --- 1(а): акт коммуналки не гасится арендой и чужим потоком, в любой двери ----------------


async def test_rent_forward_of_lease_without_article_is_not_water_money(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Договор аренды без статьи (API это допускает) даёт «Аренду вперёд» со статьёй NULL.

    Белый список бота пропускал аванс без статьи как «назначение неизвестно» — и акт воды снова
    закрывался арендой. Назначение как раз известно: деньги выданы по договору аренды, это
    видно по ``lease_id`` проводки. Чужими они остаются и без статьи."""
    async with async_session_factory() as session:
        landlord, location = await _landlord(session)
        water = await _stream(session, landlord, location, kind="water", article="Вода")
        lease = await _lease(session, landlord, location, article_id=None, accrual_enabled=False)
        rent = await _rent_paid_forward(session, lease, on=date(2026, 8, 25))
        assert rent.article_id is None

        _, act = await _bot_pair(session, water, amount="9429.75")

        assert act.payment_status == "unpaid", "водяной акт закрылся арендой вперёд"
        assert await _trail(session, act.id) == []
        assert rent.status == "open" and rent.amount_settled == Decimal("0.00")
        await session.rollback()


async def test_water_act_activated_on_the_first_does_not_take_rent_money(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Дверь активации: акт, принятый ботом ДО конца периода, лежит ``pending`` до 1-го числа.

    Белый список жил только в двери бота при немедленной активации. Джоба 1-го числа зовёт
    ``apply_closing_document`` без списка — и акт гасился первым открытым авансом контрагента,
    то есть арендой вперёд. Правило обязано жить в ядре зачёта, а не в одной из дверей."""
    async with async_session_factory() as session:
        landlord, location = await _landlord(session)
        water = await _stream(session, landlord, location, kind="water", article="Вода")
        rent_article = await _article(session, name="Аренда торговых точек", lease_bound=True)
        lease = await _lease(
            session, landlord, location, article_id=rent_article.id, accrual_enabled=False
        )
        _, act = await _bot_pair(session, water, amount="9429.75", as_of=date(2026, 8, 25))
        assert act.activation_status == "pending"
        rent = await _rent_paid_forward(session, lease, on=date(2026, 8, 28))

        await supplier_prepayments.activate_due_closing_invoices(
            session, as_of=date(2026, 9, 1), commit=False
        )

        assert act.activation_status == "active"
        assert act.payment_status == "unpaid", "активация 1-го числа закрыла воду арендой"
        assert rent.status == "open" and rent.amount_settled == Decimal("0.00")
        await session.rollback()


async def test_two_utility_streams_do_not_cross_settle(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Вода и свет одного арендодателя: оплаченный счёт света не гасит акт воды (скептик S1).

    Дверь обратного порядка: оплата счёта света заводит его ДЗ, и ``_settle_counterparty_closing
    _from_prepayments`` перебирает ВСЕ неоплаченные акты контрагента по дате. Акт воды шёл
    первым и забирал деньги света рангом «хронология»; когда потом оплачивали воду, её ДЗ
    доставалась акту света — перекрёст навсегда, нетто сходится."""
    async with async_session_factory() as session:
        landlord, location = await _landlord(session)
        water = await _stream(session, landlord, location, kind="water", article="Вода")
        power = await _stream(session, landlord, location, kind="electricity", article="Свет")
        w_bill, w_act = await _bot_pair(session, water, amount="9429.75")
        e_bill, e_act = await _bot_pair(session, power, amount="5000.00")

        await _pay_bill(session, e_bill, on=date(2026, 9, 2))
        e_own = await _own(session, e_bill)
        assert w_act.payment_status == "unpaid", "акт воды закрылся деньгами света"
        assert await _trail(session, e_act.id) == [
            (e_own.id, Decimal("5000.00"), supplier_prepayments.MATCH_BASIS_INVOICE)
        ]

        await _pay_bill(session, w_bill, on=date(2026, 9, 3))
        w_own = await _own(session, w_bill)
        assert await _trail(session, w_act.id) == [
            (w_own.id, Decimal("9429.75"), supplier_prepayments.MATCH_BASIS_INVOICE)
        ]
        assert w_own.status == "settled" and e_own.status == "settled"
        await session.rollback()


# --- 1(б): арендный акт не гасится деньгами коммуналки --------------------------------------


async def test_rent_activation_does_not_take_utility_money(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """01.10 активируется «Аренда 09.2026», а своих денег у неё нет — только деньги коммуналки.

    Оплаченный авансовый счёт за свет (ДЗ ``prepaid_bill``, ждёт фактический акт) и переплата
    за воду наличными (аванс со статьёй воды) — деньги своих потоков. По хронологии аренда
    доедала их недостающим, и перекрёст становился двойным: вода на аренде, аренда на воде.
    Арендный акт без своих денег — честная кредиторка."""
    async with async_session_factory() as session:
        landlord, location = await _landlord(session)
        water = await _stream(session, landlord, location, kind="water", article="Вода")
        power = await _stream(session, landlord, location, kind="electricity", article="Свет")
        rent_article = await _article(session, name="Аренда торговых точек", lease_bound=True)
        lease = await _lease(session, landlord, location, article_id=rent_article.id)
        rent_act = await ensure_lease_invoice(
            session, lease, date(2026, 9, 1), as_of=date(2026, 9, 5)
        )
        assert rent_act is not None and rent_act.activation_status == "pending"

        advance, no_closing = await utility_charges.build_utility_documents(
            session,
            power,
            period_start=SEPTEMBER[0],
            period_end=SEPTEMBER[1],
            expense_amount=None,
            payable_amount=Decimal("3000.00"),
            as_of=date(2026, 9, 10),
        )
        assert no_closing is None
        await _pay_bill(session, advance, on=date(2026, 9, 12))
        power_money = await _own(session, advance)

        wallet = await make_wallet(session, name="Сейф")
        tx = CashflowTransaction(
            wallet_id=wallet.id,
            direction="out",
            amount=Decimal("1000.00"),
            operation_date=date(2026, 9, 14),
            counterparty_id=landlord.id,
            article_id=water.dds_article_id,
            source_kind="safe_payout",
            quality_status="final",
        )
        session.add(tx)
        await session.flush()
        assert await utility_charges.settle_utility_invoices_from_cash(
            session,
            counterparty_id=landlord.id,
            article_id=water.dds_article_id,
            location_id=location.id,
            transaction_id=tx.id,
            amount=Decimal("1000.00"),
            wallet_id=wallet.id,
        )
        water_money = await session.scalar(
            select(SupplierPrepayment).where(SupplierPrepayment.cashflow_transaction_id == tx.id)
        )
        assert water_money is not None

        await supplier_prepayments.activate_due_closing_invoices(
            session, as_of=date(2026, 10, 1), commit=False
        )

        assert rent_act.activation_status == "active"
        assert await _trail(session, rent_act.id) == [], "аренда закрылась деньгами коммуналки"
        assert rent_act.payment_status == "unpaid"
        assert power_money.status == "open" and power_money.amount_settled == Decimal("0.00")
        assert water_money.status == "open" and water_money.amount_settled == Decimal("0.00")
        await session.rollback()


# --- 2: своя ДЗ сначала своему акту ---------------------------------------------------------


async def test_own_bill_money_goes_to_its_own_act_first(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Оплата счёта воды: его ДЗ сначала акту, который этот счёт называет, и только потом — всем.

    Обратный порядок перебирает неоплаченные акты контрагента по ДАТЕ. Бумажный акт
    арендодателя без статьи (прислан почтой, датирован серединой месяца) стоял раньше водяного
    и забирал водяные деньги рангом «хронология» — а акт, названный этим счётом, оставался
    недоплаченным. Перенос угаданного его не спасал: он двигает только уже погашенные акты."""
    async with async_session_factory() as session:
        landlord, location = await _landlord(session)
        water = await _stream(session, landlord, location, kind="water", article="Вода")
        paper = SupplierInvoice(
            counterparty_id=landlord.id,
            source="email",
            direction="payable",
            doc_kind="closing",
            operational_scope="finance",
            number="Акт 15/08",
            invoice_date=date(2026, 8, 15),
            amount=Decimal("5000.00"),
            payment_status="unpaid",
            service_period_status="not_required",
        )
        session.add(paper)
        await session.flush()
        bill, act = await _bot_pair(session, water, amount="9429.75")

        await _pay_bill(session, bill, on=date(2026, 9, 2))

        own = await _own(session, bill)
        assert await _trail(session, act.id) == [
            (own.id, Decimal("9429.75"), supplier_prepayments.MATCH_BASIS_INVOICE)
        ], "деньги счёта воды ушли чужому акту"
        assert act.payment_status == "paid"
        assert own.status == "settled"
        assert await _trail(session, paper.id) == []
        await session.rollback()


# --- 3: пара электричества связывается по external_id ---------------------------------------


async def test_electricity_fact_act_names_both_bills_of_its_month(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """У света авансовый и фактический счёт месяца несут ОДИН номер — связь по номеру молчала.

    Связь по номеру видела два счёта «Возмещение: электричество, 08.2026» и отказывалась
    отвечать. Итог: оплаченный аванс месяца гасил факт-акт лишь рангом «хронология», а перенос
    угаданного на деньги фактического счёта не срабатывал вовсе — его ДЗ висела открытой, а
    аренда оставалась занятой (скептик S5). Пару порождает бот, ключ у неё точный:
    ``utility:<поток>:<месяц>:<роль>``."""
    async with async_session_factory() as session:
        landlord, location = await _landlord(session)
        power = await _stream(session, landlord, location, kind="electricity", article="Свет")
        rent = await _bare_rent_money(session, landlord, on=date(2026, 8, 20))
        advance, _ = await utility_charges.build_utility_documents(
            session,
            power,
            period_start=AUGUST[0],
            period_end=AUGUST[1],
            expense_amount=None,
            payable_amount=Decimal("3000.00"),
            as_of=date(2026, 8, 19),
        )
        await _pay_bill(session, advance, on=date(2026, 8, 21))
        advance_money = await _own(session, advance)

        due, act = await utility_charges.build_utility_documents(
            session,
            power,
            period_start=AUGUST[0],
            period_end=AUGUST[1],
            expense_amount=Decimal("10000.00"),
            payable_amount=Decimal("7000.00"),
            as_of=date(2026, 9, 17),
        )
        assert act is not None
        assert advance.number == due.number
        assert await supplier_prepayments._basis_bill_ids(session, act) == {advance.id, due.id}
        # Аванс месяца зачтён фактом как своё основание, а не «наугад».
        assert await _trail(session, act.id) == [
            (advance_money.id, Decimal("3000.00"), supplier_prepayments.MATCH_BASIS_INVOICE)
        ]

        # Прод-состояние: остаток факта уже закрыт арендой по хронологии (старая дверь).
        await supplier_prepayments._allocate_invoice_from_prepayment(
            session,
            invoice=act,
            prepayment=rent,
            amount=Decimal("7000.00"),
            actor_user_id=None,
            match_basis=supplier_prepayments.MATCH_CHRONOLOGY,
        )
        await supplier_prepayments._recompute_status(session, act)
        await session.flush()

        await _pay_bill(session, due, on=date(2026, 9, 18))

        due_money = await _own(session, due)
        assert due_money.status == "settled", "ДЗ оплаченного счёта факта повисла открытой"
        assert sorted(await _trail(session, act.id), key=lambda row: row[1]) == [
            (advance_money.id, Decimal("3000.00"), supplier_prepayments.MATCH_BASIS_INVOICE),
            (due_money.id, Decimal("7000.00"), supplier_prepayments.MATCH_BASIS_INVOICE),
        ]
        await session.refresh(rent)
        assert rent.amount_settled == Decimal("0.00")
        await session.rollback()


async def test_two_water_streams_of_one_landlord_share_a_title_but_not_a_basis(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Две точки одного арендодателя — два водяных потока с одинаковым заголовком месяца.

    Номер «Возмещение: вода, 08.2026» у них общий, и связь по номеру молчала для обоих. По
    ``external_id`` каждый акт знает ровно свой счёт."""
    async with async_session_factory() as session:
        landlord, location = await _landlord(session)
        other_location = await _location(session)
        first = await _stream(session, landlord, location, kind="water", article="Вода")
        second = await _stream(session, landlord, other_location, kind="water", article="Вода-2")
        first_bill, first_act = await _bot_pair(session, first, amount="9429.75")
        second_bill, second_act = await _bot_pair(session, second, amount="4100.00")
        assert first_bill.number == second_bill.number

        assert await supplier_prepayments._basis_bill_ids(session, first_act) == {first_bill.id}
        assert await supplier_prepayments._basis_bill_ids(session, second_act) == {second_bill.id}
        await session.rollback()


# --- 4: замок месяца в переносе угаданного --------------------------------------------------


async def test_repoint_does_not_rewrite_a_closed_month(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Август закрыт замком. Акт воды за август висит на аренде; счёт оплатили в сентябре.

    Перенос угаданного зачёта переписал бы остатки ДЗ/КЗ на 31.08 — месяц, уже сверенный и
    закрытый (скептик S6: ДЗ 40 570,25 → 50 000, КЗ 0 → 9 429,75). Скрипт адресного
    перегашения в закрытом месяце отказывает; перенос обязан пропускать такой акт так же.
    ДЗ оплаченного счёта остаётся открытой — видимо и честно, до ручного перегашения скриптом
    ``readdress_closing_settlements`` после открытия периода."""
    async with async_session_factory() as session:
        landlord, location = await _landlord(session)
        water = await _stream(session, landlord, location, kind="water", article="Вода")
        rent = await _bare_rent_money(session, landlord, on=date(2026, 8, 20))
        bill, act = await _bot_pair(session, water, amount="9429.75")
        await _legacy_guess(session, act, rent)
        session.add(AccountingPeriodClose(period_month=date(2026, 8, 1)))
        await session.commit()

        def landlord_row(sheet) -> list[tuple[Decimal, Decimal]]:
            return [
                (row.receivable, row.payable)
                for row in sheet.rows
                if row.counterparty_id == landlord.id
            ]

        before = landlord_row(await build_balance_as_of(session, as_of=date(2026, 8, 31)))
        trail_before = await _trail(session, act.id)

        await _pay_bill(session, bill, on=date(2026, 9, 2))
        await session.commit()

        assert await _trail(session, act.id) == trail_before
        assert landlord_row(await build_balance_as_of(session, as_of=date(2026, 8, 31))) == before
        own = await _own(session, bill)
        assert own.status == "open" and own.amount_settled == Decimal("0.00")
        await session.rollback()


# --- R4-1: фильтр потоков — чёрный список, а не белый ---------------------------------------


async def test_water_money_paid_under_a_general_article_still_settles_the_water_act(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Воду оплатили выпиской ДО прихода документов, статья проводки — общая, не статья потока.

    Белый список отдавал акту только авансы со статьёй потока или без статьи — и такие деньги
    стали «чужими»: на main акт гасился ими, на ветке оставался неоплаченным при живых своих
    деньгах (скептик A3). Отвергать надо только ЯВНО чужое — аренду и другой поток, — а деньги
    неизвестного назначения акту годятся, как любому поставщику."""
    async with async_session_factory() as session:
        landlord, location = await _landlord(session)
        water = await _stream(session, landlord, location, kind="water", article="Вода")
        general = await _article(session, name="Коммунальные платежи (общая)")
        wallet = await make_wallet(session, name="Т-Банк")
        tx = CashflowTransaction(
            wallet_id=wallet.id,
            direction="out",
            amount=Decimal("9429.75"),
            operation_date=date(2026, 8, 30),
            counterparty_id=landlord.id,
            article_id=general.id,
            source_kind="bank_operation",
            quality_status="final",
        )
        session.add(tx)
        await session.flush()
        money = await supplier_prepayments.ensure_prepayment_from_bank_transaction(session, tx)
        assert money is not None

        _, act = await _bot_pair(session, water, amount="9429.75")

        assert act.payment_status == "paid", "акт воды не взял деньги, оплаченные по общей статье"
        assert [row[0] for row in await _trail(session, act.id)] == [money.id]
        await session.rollback()


async def test_stream_overpayment_under_the_old_article_survives_an_article_change(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Переплату за воду выдали по старой статье потока, потом поток перевели на новую статью.

    Белый список сравнивал статью аванса с ТЕКУЩЕЙ статьёй потока — и собственная переплата
    потока стала «чужой» (скептик A11). Деньги по старой статье не аренда и не другой поток:
    акт воды их берёт."""
    async with async_session_factory() as session:
        landlord, location = await _landlord(session)
        water = await _stream(session, landlord, location, kind="water", article="Вода (старая)")
        wallet = await make_wallet(session, name="Сейф")
        tx = CashflowTransaction(
            wallet_id=wallet.id,
            direction="out",
            amount=Decimal("1000.00"),
            operation_date=date(2026, 8, 25),
            counterparty_id=landlord.id,
            article_id=water.dds_article_id,
            source_kind="safe_payout",
            quality_status="final",
        )
        session.add(tx)
        await session.flush()
        assert await utility_charges.settle_utility_invoices_from_cash(
            session,
            counterparty_id=landlord.id,
            article_id=water.dds_article_id,
            location_id=location.id,
            transaction_id=tx.id,
            amount=Decimal("1000.00"),
            wallet_id=wallet.id,
        )
        overpaid = await session.scalar(
            select(SupplierPrepayment).where(SupplierPrepayment.cashflow_transaction_id == tx.id)
        )
        assert overpaid is not None
        water.dds_article_id = (await _article(session, name="Вода (новая)")).id
        await session.flush()

        _, act = await _bot_pair(session, water, amount="9429.75")

        assert [row[0] for row in await _trail(session, act.id)] == [overpaid.id], (
            "переплата за воду не зачлась акту воды после смены статьи потока"
        )
        await session.rollback()


async def test_rent_money_recognised_by_the_lease_article_alone(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Аренда, выданная без ``lease_id`` проводки по статье договора, — всё равно аренда.

    Статья договора не обязана быть помечена ``lease_bound`` (каталог правит владелец), а
    проводка выдачи не обязана нести договор. Статья, записанная в договоре аренды этого
    арендодателя, — достаточный признак арендных денег."""
    async with async_session_factory() as session:
        landlord, location = await _landlord(session)
        water = await _stream(session, landlord, location, kind="water", article="Вода")
        rent_article = await _article(session, name="Аренда (без флага)", lease_bound=False)
        await _lease(session, landlord, location, article_id=rent_article.id, accrual_enabled=False)
        rent = SupplierPrepayment(
            counterparty_id=landlord.id,
            kind=supplier_prepayments.RULE1_PREPAYMENT_KIND,
            amount=Decimal("50000.00"),
            amount_settled=Decimal("0.00"),
            status="open",
            article_id=rent_article.id,
        )
        session.add(rent)
        await session.flush()

        _, act = await _bot_pair(session, water, amount="9429.75")

        assert act.payment_status == "unpaid", "акт воды закрылся деньгами по статье договора"
        assert rent.amount_settled == Decimal("0.00")
        await session.rollback()


async def test_other_stream_article_is_foreign_only_when_it_differs_from_own(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Переплата за газ наличными — деньги газа, акт воды их не берёт, ЕСЛИ статьи разные.

    На проде у воды и газа Станислава Юрьевича статья одна («Коммунальные платежи») — по статье
    такие деньги не развести, и акт воды их берёт, как на main. Если же у потоков статьи
    разные, статья газа — явный признак чужого потока."""
    for shared_article in (False, True):
        async with async_session_factory() as session:
            landlord, location = await _landlord(session)
            water = await _stream(session, landlord, location, kind="water", article="Вода")
            gas = await _stream(session, landlord, location, kind="gas", article="Газ")
            if shared_article:
                gas.dds_article_id = water.dds_article_id
                await session.flush()
            gas_money = SupplierPrepayment(
                counterparty_id=landlord.id,
                kind=supplier_prepayments.RULE1_PREPAYMENT_KIND,
                amount=Decimal("9429.75"),
                amount_settled=Decimal("0.00"),
                status="open",
                article_id=gas.dds_article_id,
            )
            session.add(gas_money)
            await session.flush()

            _, act = await _bot_pair(session, water, amount="9429.75")

            if shared_article:
                assert [row[0] for row in await _trail(session, act.id)] == [gas_money.id]
            else:
                assert act.payment_status == "unpaid", "акт воды закрылся деньгами газа"
                assert gas_money.amount_settled == Decimal("0.00")
            await session.rollback()


# --- R4-2: перенос угаданного — лимит по всем счетам-основаниям -----------------------------


async def _power_month_with_paid_advance(
    session: AsyncSession, landlord: Counterparty, location: Location
) -> tuple[UtilityAccount, SupplierPrepayment, SupplierInvoice, SupplierInvoice]:
    power = await _stream(session, landlord, location, kind="electricity", article="Свет")
    advance, no_closing = await utility_charges.build_utility_documents(
        session,
        power,
        period_start=AUGUST[0],
        period_end=AUGUST[1],
        expense_amount=None,
        payable_amount=Decimal("3000.00"),
        as_of=date(2026, 8, 19),
    )
    assert no_closing is None
    await _pay_bill(session, advance, on=date(2026, 8, 20))
    advance_money = await _own(session, advance)
    due, act = await utility_charges.build_utility_documents(
        session,
        power,
        period_start=AUGUST[0],
        period_end=AUGUST[1],
        expense_amount=Decimal("7000.00"),
        payable_amount=Decimal("4000.00"),
        as_of=date(2026, 9, 17),
    )
    assert act is not None
    # Легаси: снять то, что сделал новый код; прод-состояние кладёт каждый тест сам.
    await supplier_prepayments.release_invoice_prepayment_allocations(session, act)
    await session.flush()
    return power, advance_money, due, act


async def test_repoint_releases_rent_up_to_free_money_of_all_basis_bills(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Факт-акт света целиком на аренде (так его строил main), аванс месяца оплачен и открыт.

    Перенос освобождал не больше свободного остатка ТРИГГЕРНОГО счёта (4 000), а перегашение
    брало все деньги оснований акта по дате (аванс 3 000 первым): аренда оставалась на акте на
    3 000, а ДЗ оплаченного счёта доплаты — открытой (скептик A1b). Лимит — свободные деньги
    ВСЕХ счетов-оснований акта."""
    async with async_session_factory() as session:
        landlord, location = await _landlord(session)
        rent = await _bare_rent_money(session, landlord, on=date(2026, 8, 25))
        _, advance_money, due, act = await _power_month_with_paid_advance(
            session, landlord, location
        )
        await supplier_prepayments._allocate_invoice_from_prepayment(
            session,
            invoice=act,
            prepayment=rent,
            amount=Decimal("7000.00"),
            actor_user_id=None,
            match_basis=supplier_prepayments.MATCH_CHRONOLOGY,
        )
        await supplier_prepayments._recompute_status(session, act)
        await session.flush()
        assert advance_money.status == "open" and act.payment_status == "paid"

        await _pay_bill(session, due, on=date(2026, 9, 18))

        due_money = await _own(session, due)
        await session.refresh(rent)
        assert rent.amount_settled == Decimal("0.00"), "аренда осталась на акте света"
        assert advance_money.status == "settled" and due_money.status == "settled"
        assert sorted(await _trail(session, act.id), key=lambda row: row[1]) == [
            (advance_money.id, Decimal("3000.00"), supplier_prepayments.MATCH_BASIS_INVOICE),
            (due_money.id, Decimal("4000.00"), supplier_prepayments.MATCH_BASIS_INVOICE),
        ]
        await session.rollback()


async def test_repoint_never_releases_money_of_the_acts_own_basis_bills(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Аванс месяца лёг на факт-акт «хронологией» (легаси), недостающее добрала аренда.

    Обе аллокации записаны одной транзакцией — ``created_at`` совпадает, и LIFO решался
    порядком UUID: при «неудачном» id перенос снимал законный аванс своего месяца, а аренда
    оставалась (скептик A1). Деньги счёта, который акт сам называет, — свои, не угадка: их не
    снимаем никогда, от UUID это не зависит."""
    for advance_first in (True, False):
        async with async_session_factory() as session:
            landlord, location = await _landlord(session)
            rent = await _bare_rent_money(session, landlord, on=date(2026, 8, 25))
            _, advance_money, due, act = await _power_month_with_paid_advance(
                session, landlord, location
            )
            # Крайние UUID (уникальные на каждый прогон): у Postgres порядок UUID — порядок байт.
            salt = uuid.uuid4().int % 2**64
            low, high = uuid.UUID(int=salt), uuid.UUID(int=2**128 - 1 - salt)
            advance_id, rent_id = (low, high) if advance_first else (high, low)
            session.add_all(
                [
                    InvoicePaymentAllocation(
                        id=advance_id,
                        invoice_id=act.id,
                        source_kind="prepayment",
                        prepayment_id=advance_money.id,
                        amount=Decimal("3000.00"),
                        match_basis=supplier_prepayments.MATCH_CHRONOLOGY,
                    ),
                    InvoicePaymentAllocation(
                        id=rent_id,
                        invoice_id=act.id,
                        source_kind="prepayment",
                        prepayment_id=rent.id,
                        amount=Decimal("4000.00"),
                        match_basis=supplier_prepayments.MATCH_CHRONOLOGY,
                    ),
                ]
            )
            advance_money.amount_settled = Decimal("3000.00")
            advance_money.status = "settled"
            rent.amount_settled = Decimal("4000.00")
            rent.status = "partially_settled"
            await session.flush()
            await supplier_prepayments._recompute_status(session, act)
            await session.commit()
            assert act.payment_status == "paid"

            await _pay_bill(session, due, on=date(2026, 9, 18))

            due_money = await _own(session, due)
            await session.refresh(rent)
            await session.refresh(advance_money)
            assert rent.amount_settled == Decimal("0.00"), f"аренда осталась ({advance_first=})"
            assert advance_money.status == "settled" and due_money.status == "settled"
            trail = await _trail(session, act.id)
            assert {row[0] for row in trail} == {advance_money.id, due_money.id}


# --- R4-3: замок унаследованного периода -----------------------------------------------------


async def test_repoint_does_not_inherit_a_closed_period_into_an_unperioded_act(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Бумажный акт без периода, датированный открытым сентябрём, называет августовский счёт.

    Сам акт в замок не попадает: месяц документа открыт, периода нет. Но перенос на деньги
    счёта даёт акту АДРЕСНЫЙ зачёт, акт наследует период счёта (август) и заводит начисление —
    в закрытом месяце. Замок обязан смотреть и на период, который акт унаследует."""
    async with async_session_factory() as session:
        landlord, _location_ = await _landlord(session)
        stranger = await _bare_rent_money(session, landlord, on=date(2026, 8, 10))
        bill = SupplierInvoice(
            counterparty_id=landlord.id,
            source="email",
            direction="payable",
            doc_kind="bill",
            operational_scope="finance",
            number="С-5",
            invoice_date=date(2026, 8, 20),
            amount=Decimal("5000.00"),
            payment_status="unpaid",
            service_period_start=AUGUST[0],
            service_period_end=AUGUST[1],
            service_period_status="ready",
        )
        act = SupplierInvoice(
            counterparty_id=landlord.id,
            source="email",
            direction="payable",
            doc_kind="closing",
            operational_scope="finance",
            number="А-7",
            invoice_date=date(2026, 9, 5),
            amount=Decimal("5000.00"),
            payment_status="unpaid",
            service_period_status="missing",
            raw_payload={"recognition": {"basis_number": "С-5"}},
        )
        session.add_all([bill, act])
        await session.flush()
        await _legacy_guess(session, act, stranger)
        session.add(AccountingPeriodClose(period_month=date(2026, 8, 1)))
        await session.commit()
        trail_before = await _trail(session, act.id)

        await _pay_bill(session, bill, on=date(2026, 9, 6))
        await session.commit()

        await session.refresh(act)
        assert await _trail(session, act.id) == trail_before
        assert act.service_period_start is None, "акт унаследовал закрытый август"
        own = await _own(session, bill)
        assert own.status == "open"
        await session.rollback()


# --- R5-1: перенос не снимает переплату своего потока и деньги закрытого периода -----------


async def _power_advance(
    session: AsyncSession, power: UtilityAccount, period: tuple[date, date], *, paid_on: date
) -> tuple[SupplierInvoice, SupplierPrepayment]:
    advance, no_closing = await utility_charges.build_utility_documents(
        session,
        power,
        period_start=period[0],
        period_end=period[1],
        expense_amount=None,
        payable_amount=Decimal("3000.00"),
        as_of=paid_on,
    )
    assert no_closing is None
    await _pay_bill(session, advance, on=paid_on)
    return advance, await _own(session, advance)


async def test_repoint_keeps_the_streams_own_carryover_from_a_previous_month(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Переплата света за август законно перешла в сентябрь — оплата доплаты её не отнимает.

    Август: аванс 3 000, факт 2 500 — 500 остаются у арендодателя. Сентябрь: аванс 3 000, факт
    7 000; факт-акт берёт свой аванс (основание) и августовские 500 (свой поток, «хронология»).
    Оплата доплаты 4 000 снимала эти 500 как «угаданные» и отдавала место деньгам доплаты:
    у августовского аванса снова открывалась ДЗ с периодом 08.2026, и ОПиУ августа — даже
    закрытого замком — начинал «ждать документ» на 500, хотя акт августа давно пришёл
    (скептик T2b). Переплата своего потока — не догадка, это те же деньги, что и основания: по
    хронологии остаток — это самые поздние деньги, ДЗ доплаты."""
    for august_closed in (False, True):
        async with async_session_factory() as session:
            landlord, location = await _landlord(session)
            power = await _stream(session, landlord, location, kind="electricity", article="Свет")
            aug_advance, aug_money = await _power_advance(
                session, power, AUGUST, paid_on=date(2026, 8, 20)
            )
            aug_act = SupplierInvoice(
                counterparty_id=landlord.id,
                source=utility_charges.UTILITY_INVOICE_SOURCE,
                external_id=utility_charges.intake_external_id(power.id, AUGUST[0], "closing"),
                direction="payable",
                doc_kind="closing",
                operational_scope="finance",
                number=aug_advance.number,
                invoice_date=AUGUST[1],
                amount=Decimal("2500.00"),
                dds_article_id=power.dds_article_id,
                service_period_start=AUGUST[0],
                service_period_end=AUGUST[1],
                service_period_source=utility_charges.UTILITY_INVOICE_SOURCE,
                service_period_status="ready",
            )
            session.add(aug_act)
            await session.flush()
            await supplier_prepayments.apply_closing_document(
                session, aug_act, as_of=date(2026, 9, 17)
            )
            assert aug_money.amount_settled == Decimal("2500.00")

            _, sep_money = await _power_advance(
                session, power, SEPTEMBER, paid_on=date(2026, 9, 19)
            )
            sep_due, sep_act = await utility_charges.build_utility_documents(
                session,
                power,
                period_start=SEPTEMBER[0],
                period_end=SEPTEMBER[1],
                expense_amount=Decimal("7000.00"),
                payable_amount=Decimal("4000.00"),
                as_of=date(2026, 10, 5),
            )
            assert sep_act is not None
            carried = (aug_money.id, Decimal("500.00"), supplier_prepayments.MATCH_CHRONOLOGY)
            assert carried in await _trail(session, sep_act.id)
            if august_closed:
                session.add(AccountingPeriodClose(period_month=AUGUST[0]))
            await session.commit()
            waiting_before = await build_waiting_layer(session, *AUGUST)

            await _pay_bill(session, sep_due, on=date(2026, 10, 10))
            await session.commit()

            due_money = await _own(session, sep_due)
            await session.refresh(aug_money)
            assert aug_money.amount_settled == Decimal("3000.00"), (
                f"переплата августа снята с сентябрьского акта ({august_closed=})"
            )
            assert sorted(await _trail(session, sep_act.id), key=lambda row: row[1]) == [
                carried,
                (sep_money.id, Decimal("3000.00"), supplier_prepayments.MATCH_BASIS_INVOICE),
                (due_money.id, Decimal("3500.00"), supplier_prepayments.MATCH_BASIS_INVOICE),
            ]
            assert due_money.amount_settled == Decimal("3500.00")
            assert due_money.status == "partially_settled"
            waiting_after = await build_waiting_layer(session, *AUGUST)
            assert [i.prepayment_id for i in waiting_after.items] == [
                i.prepayment_id for i in waiting_before.items
            ], "ОПиУ августа начал ждать документ по уже закрытому авансу"
            await session.rollback()


async def test_repoint_does_not_reopen_money_of_a_closed_period(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """УПД сентября погашен «наугад» авансом за август; август закрыт замком.

    Оплата сентябрьского счёта переносит УПД на свои деньги и возвращает августовский аванс в
    открытую ДЗ. Это верно, пока август открыт: аванс за август документа за август так и не
    дождался, и ОПиУ августа должен его ждать. Но если август закрыт, перенос дописал бы
    ожидание в сверенный месяц без единого нового события в нём (скептик T2b): замок
    обязан сторожить и период снимаемых денег, а не только период самого акта."""
    for august_closed in (False, True):
        async with async_session_factory() as session:
            supplier = await make_counterparty(
                session,
                name=f"Синапсис {uuid.uuid4().hex[:4]}",
                inn=f"7707{uuid.uuid4().int % 10**6:06d}",
            )
            aug_money = SupplierPrepayment(
                counterparty_id=supplier.id,
                kind=supplier_prepayments.RULE1_PREPAYMENT_KIND,
                amount=Decimal("13000.00"),
                amount_settled=Decimal("0.00"),
                status="open",
                service_period_start=AUGUST[0],
                service_period_end=AUGUST[1],
                service_period_status="ready",
            )
            bill = SupplierInvoice(
                counterparty_id=supplier.id,
                source="email",
                direction="payable",
                doc_kind="bill",
                operational_scope="finance",
                number="С-9",
                invoice_date=date(2026, 9, 1),
                amount=Decimal("13000.00"),
                payment_status="unpaid",
                service_period_start=SEPTEMBER[0],
                service_period_end=SEPTEMBER[1],
                service_period_status="ready",
            )
            upd = SupplierInvoice(
                counterparty_id=supplier.id,
                source="email",
                direction="payable",
                doc_kind="closing",
                operational_scope="finance",
                number="У-30",
                invoice_date=SEPTEMBER[1],
                amount=Decimal("13000.00"),
                payment_status="unpaid",
                service_period_start=SEPTEMBER[0],
                service_period_end=SEPTEMBER[1],
                service_period_status="ready",
                raw_payload={"recognition": {"basis_number": "С-9"}},
            )
            session.add_all([aug_money, bill, upd])
            await session.flush()
            await _legacy_guess(session, upd, aug_money)
            if august_closed:
                session.add(AccountingPeriodClose(period_month=AUGUST[0]))
            await session.commit()
            trail_before = await _trail(session, upd.id)

            await _pay_bill(session, bill, on=date(2026, 10, 3))
            await session.commit()

            own = await _own(session, bill)
            await session.refresh(aug_money)
            if august_closed:
                assert await _trail(session, upd.id) == trail_before
                assert aug_money.amount_settled == Decimal("13000.00"), (
                    "аванс закрытого августа снова открылся"
                )
                assert own.status == "open"
            else:
                assert await _trail(session, upd.id) == [
                    (own.id, Decimal("13000.00"), supplier_prepayments.MATCH_BASIS_INVOICE)
                ]
                assert aug_money.amount_settled == Decimal("0.00")
                assert own.status == "settled"
            await session.rollback()


# --- R5-2: замок наследуемого периода — в самом наследовании, для всех дверей --------------


async def test_closed_period_is_not_inherited_through_any_door(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Акт без периода, датированный открытым сентябрём, называет счёт за закрытый август.

    Зачёт деньгами счёта законен — дата акта открыта, — но наследование перенесло бы в акт
    август, а ``sync_invoice_accrual`` завёл бы начисление в закрытом месяце (скептик L1).
    Замок стоял только в переносе угаданного; обратный порядок при оплате счёта (шаг «свой
    акт») и приём акта после оплаты шли мимо — так же, как на main. Замок живёт в самом
    наследовании: акт гасится, но остаётся без периода — в «оплачено, расход не признан»
    открытого сентября, где его решает человек."""
    for august_closed in (False, True):
        for door in ("bill_paid_after_act", "act_after_bill_paid"):
            async with async_session_factory() as session:
                supplier = await make_counterparty(
                    session,
                    name=f"Поставщик {uuid.uuid4().hex[:4]}",
                    inn=f"7708{uuid.uuid4().int % 10**6:06d}",
                )
                bill = SupplierInvoice(
                    counterparty_id=supplier.id,
                    source="email",
                    direction="payable",
                    doc_kind="bill",
                    operational_scope="finance",
                    number="С-5",
                    invoice_date=date(2026, 8, 20),
                    amount=Decimal("5000.00"),
                    payment_status="unpaid",
                    service_period_start=AUGUST[0],
                    service_period_end=AUGUST[1],
                    service_period_status="ready",
                )
                act = SupplierInvoice(
                    counterparty_id=supplier.id,
                    source="email",
                    direction="payable",
                    doc_kind="closing",
                    operational_scope="finance",
                    number="А-7",
                    invoice_date=date(2026, 9, 5),
                    amount=Decimal("5000.00"),
                    payment_status="unpaid",
                    service_period_status="missing",
                    raw_payload={"recognition": {"basis_number": "С-5"}},
                )
                session.add(bill)
                if august_closed:
                    session.add(AccountingPeriodClose(period_month=AUGUST[0]))
                await session.flush()
                if door == "bill_paid_after_act":
                    session.add(act)
                    await session.flush()
                    await supplier_prepayments.apply_closing_document(
                        session, act, as_of=date(2026, 9, 5)
                    )
                    assert act.payment_status == "unpaid"
                    await _pay_bill(session, bill, on=date(2026, 9, 6))
                else:
                    await _pay_bill(session, bill, on=date(2026, 9, 6))
                    session.add(act)
                    await session.flush()
                    await supplier_prepayments.apply_closing_document(
                        session, act, as_of=date(2026, 9, 7)
                    )
                await session.flush()

                own = await _own(session, bill)
                assert await _trail(session, act.id) == [
                    (own.id, Decimal("5000.00"), supplier_prepayments.MATCH_BASIS_INVOICE)
                ], f"акт не погашен деньгами своего счёта ({door=}, {august_closed=})"
                accrual = await session.scalar(
                    select(SupplierExpenseAccrual).where(
                        SupplierExpenseAccrual.invoice_id == act.id
                    )
                )
                if august_closed:
                    assert act.service_period_start is None, (
                        f"акт унаследовал закрытый август ({door=})"
                    )
                    assert act.service_period_status == "missing"
                    assert accrual is None, f"начисление в закрытом месяце ({door=})"
                else:
                    assert (act.service_period_start, act.service_period_end) == AUGUST
                    assert accrual is not None
                await session.rollback()


# --- R5-3: акт со своими датами наследовать не может — периоды оснований не проверяем -----


async def test_ambiguous_act_with_own_dates_is_repointed_despite_a_closed_basis_period(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Акт ``ambiguous`` с датами сентября называет счёт за закрытый август.

    Замок переноса проверял периоды счетов-оснований у ЛЮБОГО акта без готового периода — и у
    ``ambiguous`` с проставленными датами тоже, хотя такой акт наследовать не может
    (``_inherit_period_from_prepayment_allocations`` требует пустые даты). Перенос
    пропускался зря: акт оставался на чужих деньгах, ДЗ счёта — открытой (скептик L2).
    Месяцы акта — его собственные даты; открыты они — перенос законен."""
    async with async_session_factory() as session:
        landlord, _location_ = await _landlord(session)
        stranger = await _bare_rent_money(session, landlord, on=date(2026, 8, 10))
        bill = SupplierInvoice(
            counterparty_id=landlord.id,
            source="email",
            direction="payable",
            doc_kind="bill",
            operational_scope="finance",
            number="С-5",
            invoice_date=date(2026, 8, 20),
            amount=Decimal("5000.00"),
            payment_status="unpaid",
            service_period_start=AUGUST[0],
            service_period_end=AUGUST[1],
            service_period_status="ready",
        )
        act = SupplierInvoice(
            counterparty_id=landlord.id,
            source="email",
            direction="payable",
            doc_kind="closing",
            operational_scope="finance",
            number="А-7",
            invoice_date=date(2026, 9, 5),
            amount=Decimal("5000.00"),
            payment_status="unpaid",
            service_period_start=SEPTEMBER[0],
            service_period_end=SEPTEMBER[1],
            service_period_status="ambiguous",
            raw_payload={"recognition": {"basis_number": "С-5"}},
        )
        session.add_all([bill, act])
        await session.flush()
        await _legacy_guess(session, act, stranger)
        session.add(AccountingPeriodClose(period_month=AUGUST[0]))
        await session.commit()

        await _pay_bill(session, bill, on=date(2026, 9, 6))
        await session.commit()

        own = await _own(session, bill)
        await session.refresh(act)
        await session.refresh(stranger)
        assert await _trail(session, act.id) == [
            (own.id, Decimal("5000.00"), supplier_prepayments.MATCH_BASIS_INVOICE)
        ], "перенос пропущен из-за периода счёта, который акт не наследует"
        assert own.status == "settled"
        assert stranger.amount_settled == Decimal("0.00")
        assert (act.service_period_start, act.service_period_end) == SEPTEMBER
        assert act.service_period_status == "ambiguous"
        await session.rollback()
