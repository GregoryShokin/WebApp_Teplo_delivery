"""Абонентские платежи: помесячное признание расхода без закрывающего документа.

Наумченко не присылает УПД вовсе, Микроэль присылает не всегда, а платят им вперёд за
несколько месяцев. Раньше такие деньги висели дебиторкой до конца всего периода, а расход
не признавался ни в одном месяце — на 01.08.2026 так стояло 311 969 ₽ у десяти контрагентов.

Механика повторяет аренду (``lease_accruals``): раз в месяц заводится внутренний закрывающий
документ ``source='self_billed'`` на долю периода, гасит дебиторку и признаёт расход. Главное,
что здесь закреплено, — этот документ НЕ складывается с настоящим УПД: если контрагент всё же
прислал документ за тот же месяц, самоакт аннулируется и возвращает дебиторку, иначе и расход
в P&L, и гашение удвоятся.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from cp_helpers import (
    make_counterparty,
    make_draft,
    make_expense_article,
    make_invoice,
    make_wallet,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    CounterpartyPayableProfile,
    DdsArticle,
    InvoicePaymentAllocation,
    SupplierExpenseAccrual,
    SupplierInvoice,
    SupplierPrepayment,
)
from app.services import supplier_prepayments
from app.services.subscription_accruals import (
    SELF_BILLED_SOURCE,
    accrue_due_months,
    covered_months,
    monthly_shares,
    supersede_self_billed,
)


async def _subscription_prepayment(
    session: AsyncSession,
    *,
    counterparty_id: uuid.UUID,
    wallet_id: uuid.UUID,
    amount: str,
    start: date,
    months: int,
    auto: bool = True,
) -> SupplierPrepayment:
    """Дебиторка от абонентского платежа: сумма вперёд за N месяцев."""
    last_month_start = date(
        start.year + (start.month - 1 + months - 1) // 12,
        (start.month - 1 + months - 1) % 12 + 1,
        1,
    )
    end = date(
        last_month_start.year + (1 if last_month_start.month == 12 else 0),
        1 if last_month_start.month == 12 else last_month_start.month + 1,
        1,
    )
    prepayment = SupplierPrepayment(
        counterparty_id=counterparty_id,
        kind="subscription",
        wallet_id=wallet_id,
        amount=Decimal(amount),
        amount_settled=Decimal("0"),
        status="open",
        service_period_start=start,
        service_period_end=date.fromordinal(end.toordinal() - 1),
        service_period_status="ready",
        service_period_months=months,
        auto_recognize_monthly=auto,
    )
    session.add(prepayment)
    await session.flush()
    return prepayment


def test_shares_split_evenly_and_last_month_takes_the_remainder() -> None:
    """Копейка от деления достаётся последнему месяцу, а не теряется.

    10 000 / 3 = 3 333,33 — три такие доли дают 9 999,99, и дебиторка не закрылась бы до нуля.
    """
    assert monthly_shares(Decimal("9000.00"), 3) == [
        Decimal("3000.00"),
        Decimal("3000.00"),
        Decimal("3000.00"),
    ]
    assert monthly_shares(Decimal("10000.00"), 3) == [
        Decimal("3333.33"),
        Decimal("3333.33"),
        Decimal("3333.34"),
    ]
    assert sum(monthly_shares(Decimal("10000.00"), 3)) == Decimal("10000.00")


async def test_quarterly_payment_recognized_month_by_month(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """9 000 ₽ за апрель-июнь: по 3 000 в каждом месяце, дебиторка тает до нуля.

    Это кейс Наумченко: закрывающих документов не будет, но расход должен лечь в свои месяцы,
    а не висеть авансом до конца квартала.
    """
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="ИП Наумченко", inn="614307902094")
        wallet = await make_wallet(session, code="tbank-sub-1", name="Т-Банк")
        prepayment = await _subscription_prepayment(
            session,
            counterparty_id=cp.id,
            wallet_id=wallet.id,
            amount="9000.00",
            start=date(2026, 4, 1),
            months=3,
        )
        await session.commit()

        # В мае закрыт только апрель: май ещё идёт, июнь не начался.
        created = await accrue_due_months(session, as_of=date(2026, 5, 15))
        assert [inv.amount for inv in created] == [Decimal("3000.00")]
        assert created[0].service_period_start == date(2026, 4, 1)
        assert created[0].source == "self_billed"

        # Повторный прогон ничего не задваивает — ключ идемпотентности тот же.
        assert await accrue_due_months(session, as_of=date(2026, 5, 15)) == []

        # После конца квартала признаны все три месяца.
        created = await accrue_due_months(session, as_of=date(2026, 7, 1))
        assert [inv.amount for inv in created] == [Decimal("3000.00"), Decimal("3000.00")]

        await session.refresh(prepayment)
        assert prepayment.amount_settled == Decimal("9000.00")
        assert prepayment.status == "settled"

        # Расход признан помесячно: три строки P&L, по одной на месяц.
        accruals = list(
            (
                await session.scalars(
                    select(SupplierExpenseAccrual).where(
                        SupplierExpenseAccrual.counterparty_id == cp.id
                    )
                )
            ).all()
        )
        assert sorted(a.service_period_start for a in accruals) == [
            date(2026, 4, 1),
            date(2026, 5, 1),
            date(2026, 6, 1),
        ]
        assert sum(a.amount for a in accruals) == Decimal("9000.00")


async def test_month_is_not_recognized_before_it_ends(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Внутри месяца признавать нечего: услуга ещё оказывается.

    Та же строгая граница, что у recognize_due_expenses, — иначе расход попал бы в P&L
    на день раньше своего месяца.
    """
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Абонент", inn="614307902095")
        wallet = await make_wallet(session, code="tbank-sub-2", name="Т-Банк")
        await _subscription_prepayment(
            session,
            counterparty_id=cp.id,
            wallet_id=wallet.id,
            amount="3000.00",
            start=date(2026, 4, 1),
            months=1,
        )
        await session.commit()

        assert await accrue_due_months(session, as_of=date(2026, 4, 30)) == []
        assert len(await accrue_due_months(session, as_of=date(2026, 5, 1))) == 1


async def test_real_document_supersedes_self_billed(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Пришедший УПД замещает самоакт, а не складывается с ним.

    Кейс Микроэля: месяц уже признан внутренним документом, и тут приходит настоящий УПД.
    Без замещения период закрылся бы дважды — расход месяца вырос бы вдвое, а дебиторка
    ушла бы в минус.
    """
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Микроэл", inn="6143049372")
        wallet = await make_wallet(session, code="tbank-sub-3", name="Т-Банк")
        prepayment = await _subscription_prepayment(
            session,
            counterparty_id=cp.id,
            wallet_id=wallet.id,
            amount="3230.00",
            start=date(2026, 7, 1),
            months=1,
        )
        await session.commit()

        await accrue_due_months(session, as_of=date(2026, 8, 1))
        await session.refresh(prepayment)
        assert prepayment.amount_settled == Decimal("3230.00")

        # Настоящий УПД за июль приходит с опозданием.
        real = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="3230.00",
            number="УПД-707",
            doc_kind="closing",
            invoice_date=date(2026, 7, 31),
        )
        real.service_period_start = date(2026, 7, 1)
        real.service_period_end = date(2026, 7, 31)
        real.service_period_status = "ready"
        await session.flush()

        superseded = await supersede_self_billed(session, real)
        await session.commit()

        assert len(superseded) == 1
        assert superseded[0].payment_status == "void"
        # Дебиторка вернулась и доступна настоящему документу.
        await session.refresh(prepayment)
        assert prepayment.amount_settled == Decimal("0.00")
        assert prepayment.status == "open"
        # Признание самоакта снято — расход останется только по настоящему документу.
        cancelled = await session.scalar(
            select(SupplierExpenseAccrual).where(
                SupplierExpenseAccrual.invoice_id == superseded[0].id
            )
        )
        assert cancelled is not None and cancelled.status == "cancelled"


async def test_apply_closing_document_supersedes_automatically(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Замещение срабатывает на штатном пути проведения документа, а не только вручную.

    УПД приходит из почты/СБИС и проводится через apply_closing_document — если бы
    замещение висело отдельной ручной операцией, про него бы просто забыли.
    """
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Абонент-2", inn="614307902096")
        wallet = await make_wallet(session, code="tbank-sub-4", name="Т-Банк")
        prepayment = await _subscription_prepayment(
            session,
            counterparty_id=cp.id,
            wallet_id=wallet.id,
            amount="5000.00",
            start=date(2026, 6, 1),
            months=1,
        )
        await session.commit()
        await accrue_due_months(session, as_of=date(2026, 7, 5))

        real = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="5000.00",
            number="АКТ-1",
            doc_kind="closing",
            invoice_date=date(2026, 6, 30),
            # finance — контур сервисных УПД: только он участвует в авто-зачёте предоплат
            # (AUTO_SETTLEMENT_OPERATIONAL_SCOPE). Складская накладная зачёта не требует.
            operational_scope="finance",
        )
        real.service_period_start = date(2026, 6, 1)
        real.service_period_end = date(2026, 6, 30)
        real.service_period_status = "ready"
        await session.flush()

        await supplier_prepayments.apply_closing_document(session, real, as_of=date(2026, 7, 5))
        await session.commit()

        # Самоакт снят, а настоящий документ погашен той же вернувшейся дебиторкой.
        await session.refresh(prepayment)
        assert prepayment.amount_settled == Decimal("5000.00")
        allocations = list(
            (
                await session.scalars(
                    select(InvoicePaymentAllocation).where(
                        InvoicePaymentAllocation.invoice_id == real.id
                    )
                )
            ).all()
        )
        assert sum(a.amount for a in allocations) == Decimal("5000.00")


async def test_existing_real_document_blocks_self_billing(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Месяц, за который документ уже есть, самоактом не закрывается.

    Микроэль присылает УПД вовремя — для таких месяцев внутренний документ не нужен вовсе,
    иначе он появлялся бы и тут же аннулировался, засоряя историю.
    """
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Абонент-3", inn="614307902097")
        wallet = await make_wallet(session, code="tbank-sub-5", name="Т-Банк")
        await _subscription_prepayment(
            session,
            counterparty_id=cp.id,
            wallet_id=wallet.id,
            amount="4000.00",
            start=date(2026, 5, 1),
            months=1,
        )
        real = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="4000.00",
            number="УПД-5",
            doc_kind="closing",
            invoice_date=date(2026, 5, 31),
        )
        real.service_period_start = date(2026, 5, 1)
        real.service_period_end = date(2026, 5, 31)
        real.service_period_status = "ready"
        await session.commit()

        assert await accrue_due_months(session, as_of=date(2026, 6, 10)) == []


async def test_auto_recognition_is_opt_in(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Без флага на платеже ничего не признаётся: молча закрывать чужую дебиторку нельзя."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Обычный", inn="614307902098")
        wallet = await make_wallet(session, code="tbank-sub-6", name="Т-Банк")
        await _subscription_prepayment(
            session,
            counterparty_id=cp.id,
            wallet_id=wallet.id,
            amount="7000.00",
            start=date(2026, 4, 1),
            months=2,
            auto=False,
        )
        await session.commit()

        assert await accrue_due_months(session, as_of=date(2026, 7, 1)) == []


def test_covered_months_spans_the_whole_period() -> None:
    """Список месяцев считается от начала периода и переходит через год."""
    prepayment = SupplierPrepayment(
        counterparty_id=uuid.uuid4(),
        kind="subscription",
        amount=Decimal("100"),
        amount_settled=Decimal("0"),
        status="open",
        service_period_start=date(2026, 11, 1),
        service_period_end=date(2027, 1, 31),
        service_period_months=3,
        auto_recognize_monthly=True,
    )
    assert covered_months(prepayment) == [date(2026, 11, 1), date(2026, 12, 1), date(2027, 1, 1)]


async def test_real_document_without_period_still_blocks_self_billing(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Настоящий документ БЕЗ периода услуги всё равно закрывает свой месяц.

    Гард сравнивал только периоды услуги, а на проде 01.08.2026 период стоял у 4 закрывающих
    документов из 221. Значит для остальных 217 самоакт встал бы поверх настоящего УПД, и
    расход попал бы в P&L дважды. Период документа без периода берём по его дате.
    """
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Абонент-без-периода", inn="614307902090")
        wallet = await make_wallet(session, code="tbank-sub-7", name="Т-Банк")
        prepayment = await _subscription_prepayment(
            session,
            counterparty_id=cp.id,
            wallet_id=wallet.id,
            amount="3000.00",
            start=date(2026, 6, 1),
            months=1,
        )
        real = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="3000.00",
            number="УПД-606",
            doc_kind="closing",
            invoice_date=date(2026, 6, 30),
        )
        assert real.service_period_start is None
        await session.commit()

        assert await accrue_due_months(session, as_of=date(2026, 7, 1)) == []
        await session.refresh(prepayment)
        assert prepayment.amount_settled == Decimal("0.00")


async def test_document_without_period_supersedes_self_billed(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Опоздавший УПД без периода снимает наше признание за месяц своей даты.

    Обратная сторона того же: пока замещение требовало период у пришедшего документа, оно не
    срабатывало почти никогда — самоакт оставался жить рядом с настоящим УПД.
    """
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Абонент-поздний", inn="614307902091")
        wallet = await make_wallet(session, code="tbank-sub-8", name="Т-Банк")
        prepayment = await _subscription_prepayment(
            session,
            counterparty_id=cp.id,
            wallet_id=wallet.id,
            amount="4200.00",
            start=date(2026, 6, 1),
            months=1,
        )
        await session.commit()

        await accrue_due_months(session, as_of=date(2026, 7, 1))
        await session.refresh(prepayment)
        assert prepayment.amount_settled == Decimal("4200.00")

        late = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="4200.00",
            number="УПД-610",
            doc_kind="closing",
            invoice_date=date(2026, 6, 30),
        )
        await session.flush()

        superseded = await supersede_self_billed(session, late)
        await session.commit()

        assert len(superseded) == 1
        await session.refresh(prepayment)
        assert prepayment.amount_settled == Decimal("0.00")


async def test_superseded_month_can_be_recognised_again(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Аннулированный самоакт освобождает ключ месяца — иначе месяц пропал бы навсегда.

    Ключ ``source + external_id`` уникален. Пока его держала void-строка, повторное признание
    было невозможно: убрали ошибочный УПД — и месяц молча остался без расхода вовсе.
    """
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Абонент-повтор", inn="614307902092")
        wallet = await make_wallet(session, code="tbank-sub-9", name="Т-Банк")
        prepayment = await _subscription_prepayment(
            session,
            counterparty_id=cp.id,
            wallet_id=wallet.id,
            amount="2500.00",
            start=date(2026, 6, 1),
            months=1,
        )
        await session.commit()

        await accrue_due_months(session, as_of=date(2026, 7, 1))
        real = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="2500.00",
            number="УПД-620",
            doc_kind="closing",
            invoice_date=date(2026, 6, 30),
        )
        real.service_period_start = date(2026, 6, 1)
        real.service_period_end = date(2026, 6, 30)
        await session.flush()
        superseded = await supersede_self_billed(session, real)
        await session.commit()
        assert superseded[0].external_id.endswith(str(real.id))

        # УПД оказался ошибочным и снят с учёта — месяц должен признаться заново.
        real.payment_status = "void"
        real.service_period_start = None
        real.service_period_end = None
        real.invoice_date = date(2030, 1, 1)
        await session.commit()

        again = await accrue_due_months(session, as_of=date(2026, 7, 1))
        assert len(again) == 1
        await session.refresh(prepayment)
        assert prepayment.amount_settled == Decimal("2500.00")


async def test_settlement_prefers_prepayment_of_the_same_period(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Документ гасит дебиторку СВОЕГО периода, а не самую старую.

    FIFO брал предоплаты по дате создания, не глядя на период: акт за июль закрывал платёж за
    2 квартал, и признание за апрель потом не находило, что гасить, — расход заводился ещё раз.
    """
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Абонент-два-периода", inn="614307902093")
        wallet = await make_wallet(session, code="tbank-sub-10", name="Т-Банк")
        second_quarter = await _subscription_prepayment(
            session,
            counterparty_id=cp.id,
            wallet_id=wallet.id,
            amount="9000.00",
            start=date(2026, 4, 1),
            months=3,
            auto=False,
        )
        july = await _subscription_prepayment(
            session,
            counterparty_id=cp.id,
            wallet_id=wallet.id,
            amount="3000.00",
            start=date(2026, 7, 1),
            months=1,
            auto=False,
        )
        await session.commit()

        act = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="3000.00",
            number="АКТ-707",
            doc_kind="closing",
            invoice_date=date(2026, 7, 31),
            operational_scope="finance",
        )
        act.service_period_start = date(2026, 7, 1)
        act.service_period_end = date(2026, 7, 31)
        await session.flush()

        settled = await supplier_prepayments.auto_settle_invoice_from_open_prepayments(session, act)
        await session.commit()

        assert settled == Decimal("3000.00")
        await session.refresh(july)
        await session.refresh(second_quarter)
        assert july.amount_settled == Decimal("3000.00")
        assert second_quarter.amount_settled == Decimal("0.00")


async def test_unperioded_closing_prefers_exact_bill_and_inherits_its_period(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Акт без периода связывается с равным счётом, а не со старым большим платежом."""
    from app.services import supplier_service_periods

    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="АЙКО-точная-сумма", inn="1655166016")
        # Дата денег обязательна обоим: равенство суммы работает только для аванса, ушедшего
        # НЕ ПОЗЖЕ документа (правило хронологии, инцидент Манго). У счёта её даёт сам счёт,
        # у абонентского платежа без проводки — дата записи.
        legacy = SupplierPrepayment(
            counterparty_id=cp.id,
            kind="subscription",
            amount=Decimal("16430.00"),
            amount_settled=Decimal("0.00"),
            status="open",
            service_period_status="missing",
            created_at=datetime(2026, 6, 5, tzinfo=UTC),
        )
        bill = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="4260.00",
            number="СЧЁТ-4260",
            doc_kind="bill",
            invoice_date=date(2026, 7, 1),
            operational_scope="finance",
        )
        july_bill = SupplierPrepayment(
            counterparty_id=cp.id,
            kind="prepaid_bill",
            bill_invoice_id=bill.id,
            amount=Decimal("4260.00"),
            amount_settled=Decimal("0.00"),
            status="open",
            service_period_status="ready",
            service_period_start=date(2026, 7, 1),
            service_period_end=date(2026, 7, 31),
        )
        session.add_all([legacy, july_bill])
        await session.flush()
        act = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="4260.00",
            number="070626-33538-лсп-акт",
            doc_kind="closing",
            invoice_date=date(2026, 7, 31),
            operational_scope="finance",
        )

        settled = await supplier_prepayments.auto_settle_invoice_from_open_prepayments(session, act)
        await supplier_service_periods.sync_invoice_accrual(session, act)

        assert settled == Decimal("4260.00")
        assert july_bill.amount_settled == Decimal("4260.00")
        assert legacy.amount_settled == Decimal("0.00")
        assert act.service_period_status == "ready"
        assert act.service_period_start == date(2026, 7, 1)
        assert act.service_period_end == date(2026, 7, 31)
        assert act.service_period_source == "settled_prepayment"
        accrual = await session.scalar(
            select(SupplierExpenseAccrual).where(SupplierExpenseAccrual.invoice_id == act.id)
        )
        assert accrual is not None
        assert accrual.service_period_start == date(2026, 7, 1)
        assert accrual.service_period_end == date(2026, 7, 31)


async def test_repair_unperioded_closings_rebuilds_old_fifo_cross_match(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Ремонт освобождает старые перекрёстные зачёты и собирает их по точной сумме заново."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="АЙКО-ремонт-FIFO", inn="1655166017")
        legacy = SupplierPrepayment(
            counterparty_id=cp.id,
            kind="subscription",
            amount=Decimal("16430.00"),
            amount_settled=Decimal("16430.00"),
            status="settled",
            service_period_status="missing",
            created_at=datetime(2026, 6, 5, tzinfo=UTC),
        )
        bill = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="4260.00",
            number="СЧЁТ-РЕМОНТ-4260",
            doc_kind="bill",
            invoice_date=date(2026, 7, 1),
            operational_scope="finance",
        )
        july_bill = SupplierPrepayment(
            counterparty_id=cp.id,
            kind="prepaid_bill",
            bill_invoice_id=bill.id,
            amount=Decimal("4260.00"),
            amount_settled=Decimal("4260.00"),
            status="settled",
            service_period_status="ready",
            service_period_start=date(2026, 7, 1),
            service_period_end=date(2026, 7, 31),
        )
        session.add_all([legacy, july_bill])
        await session.flush()
        small = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="4260.00",
            number="АКТ-4260",
            doc_kind="closing",
            invoice_date=date(2026, 7, 31),
            operational_scope="finance",
        )
        large = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="16430.00",
            number="АКТ-16430",
            doc_kind="closing",
            invoice_date=date(2026, 7, 31),
            operational_scope="finance",
        )
        small.payment_status = "paid"
        large.payment_status = "paid"
        session.add_all(
            [
                InvoicePaymentAllocation(
                    invoice_id=small.id,
                    source_kind="prepayment",
                    prepayment_id=legacy.id,
                    amount=Decimal("4260.00"),
                ),
                InvoicePaymentAllocation(
                    invoice_id=large.id,
                    source_kind="prepayment",
                    prepayment_id=legacy.id,
                    amount=Decimal("12170.00"),
                ),
                InvoicePaymentAllocation(
                    invoice_id=large.id,
                    source_kind="prepayment",
                    prepayment_id=july_bill.id,
                    amount=Decimal("4260.00"),
                ),
            ]
        )
        await session.commit()

        result = await supplier_prepayments.repair_unperioded_closing_settlements(
            session,
            invoice_ids=[small.id, large.id],
        )

        assert result == {"documents": 2, "periods_restored": 1}
        await session.refresh(small)
        await session.refresh(large)
        await session.refresh(legacy)
        await session.refresh(july_bill)
        assert small.service_period_start == date(2026, 7, 1)
        assert small.service_period_end == date(2026, 7, 31)
        assert large.service_period_start is None
        assert july_bill.amount_settled == Decimal("4260.00")
        assert legacy.amount_settled == Decimal("16430.00")
        small_allocations = (
            await session.scalars(
                select(InvoicePaymentAllocation).where(
                    InvoicePaymentAllocation.invoice_id == small.id
                )
            )
        ).all()
        large_allocations = (
            await session.scalars(
                select(InvoicePaymentAllocation).where(
                    InvoicePaymentAllocation.invoice_id == large.id
                )
            )
        ).all()
        assert [(row.prepayment_id, row.amount) for row in small_allocations] == [
            (july_bill.id, Decimal("4260.00"))
        ]
        assert [(row.prepayment_id, row.amount) for row in large_allocations] == [
            (legacy.id, Decimal("16430.00"))
        ]


async def test_equal_open_bills_of_different_periods_do_not_infer_period(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Одинаковая сумма двух месяцев не даёт права выбрать период по FIFO."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Два одинаковых счёта", inn="1655166018")
        july = SupplierPrepayment(
            counterparty_id=cp.id,
            kind="prepaid_bill",
            amount=Decimal("4260.00"),
            amount_settled=Decimal("0.00"),
            status="open",
            service_period_status="ready",
            service_period_start=date(2026, 7, 1),
            service_period_end=date(2026, 7, 31),
        )
        august = SupplierPrepayment(
            counterparty_id=cp.id,
            kind="prepaid_bill",
            amount=Decimal("4260.00"),
            amount_settled=Decimal("0.00"),
            status="open",
            service_period_status="ready",
            service_period_start=date(2026, 8, 1),
            service_period_end=date(2026, 8, 31),
        )
        session.add_all([july, august])
        await session.flush()
        act = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="4260.00",
            number="АКТ-БЕЗ-ПЕРИОДА",
            doc_kind="closing",
            invoice_date=date(2026, 8, 31),
            operational_scope="finance",
        )

        settled = await supplier_prepayments.auto_settle_invoice_from_open_prepayments(session, act)

        assert settled == Decimal("4260.00")
        assert act.service_period_start is None
        assert act.service_period_end is None
        assert act.service_period_status != "ready"


async def test_monthly_recognition_does_not_double_the_expense_line(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Строка платежа и помесячное признание не складываются в двойной расход.

    Платёж 9 000 ₽ за апрель-июнь, заведённый через окно «Новый платёж» с галкой
    «Закрывающих документов не будет», давал 9 000 признания СО СТРОКИ (одним куском, месяцем
    окончания периода) плюс три самоакта по 3 000 — в реестре признания 18 000 ₽ вместо 9 000.
    Деньги и дебиторка при этом сходились, врала только прибыль.
    """
    from app.models import ExpenseDraftLine
    from app.services import supplier_service_periods as periods_service

    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Двойной расход", inn="614307902099")
        article = await session.scalar(
            select(DdsArticle).where(DdsArticle.movement_type == "outflow").limit(1)
        )
        draft = await make_draft(session, counterparty_id=cp.id, amount="9000.00")
        line = ExpenseDraftLine(
            draft_id=draft.id,
            counterparty_id=cp.id,
            article_id=article.id,
            amount=Decimal("9000.00"),
            purpose="Услуги ФД и НК за 2 квартал",
            service_period_start=date(2026, 4, 1),
            service_period_end=date(2026, 6, 30),
            service_period_months=3,
            auto_recognize_monthly=True,
        )
        session.add(line)
        await session.flush()

        accrual = await periods_service.sync_expense_line_accrual(session, line)
        await session.commit()

        # Строка помесячного платежа своего признания не заводит — расход признают самоакты.
        assert accrual is None
        rows = await session.scalars(
            select(SupplierExpenseAccrual).where(SupplierExpenseAccrual.counterparty_id == cp.id)
        )
        assert rows.all() == []


async def test_other_service_document_does_not_block_recognition(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Документ по ДРУГОЙ услуге не глушит признание за месяц.

    У Манго за 30.06 два акта — 6 108,69 и 5 250,00: контрагент с несколькими услугами.
    Без сверки статьи первый же чужой документ отменял признание по всем остальным.
    """
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Две услуги", inn="614307902100")
        wallet = await make_wallet(session, code="tbank-sub-11", name="Т-Банк")
        articles = (
            await session.scalars(
                select(DdsArticle).where(DdsArticle.movement_type == "outflow").limit(2)
            )
        ).all()
        assert len(articles) == 2, "в сидах нужно минимум две расходные статьи"

        prepayment = await _subscription_prepayment(
            session,
            counterparty_id=cp.id,
            wallet_id=wallet.id,
            amount="5000.00",
            start=date(2026, 6, 1),
            months=1,
        )
        prepayment.article_id = articles[0].id
        # Настоящий документ за тот же месяц, но по ДРУГОЙ услуге.
        alien = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="6108.69",
            number="МРД#606000577",
            doc_kind="closing",
            invoice_date=date(2026, 6, 30),
        )
        alien.dds_article_id = articles[1].id
        await session.commit()

        created = await accrue_due_months(session, as_of=date(2026, 7, 1))
        await session.commit()
        assert len(created) == 1
        assert created[0].amount == Decimal("5000.00")


def test_covered_months_falls_back_to_the_period_itself() -> None:
    """Период на три месяца без явного «сколько месяцев» — это три месяца, а не один.

    ``service_period_months`` заполняет только окно «Новый платёж», а период приходит ещё из
    счёта, из ЭДО и из ручной разметки — там поле пустое всегда. Пока пустое означало «один
    месяц», квартальная лицензия признавала всю сумму первым месяцем: 36 000 ₽ в июле вместо
    12 000 в каждом из трёх.
    """
    prepayment = SupplierPrepayment(
        counterparty_id=uuid.uuid4(),
        kind="subscription",
        amount=Decimal("36000.00"),
        amount_settled=Decimal("0"),
        status="open",
        service_period_start=date(2026, 7, 1),
        service_period_end=date(2026, 9, 30),
        service_period_status="ready",
    )
    assert covered_months(prepayment) == [date(2026, 7, 1), date(2026, 8, 1), date(2026, 9, 1)]


async def test_fixed_tariff_recognizes_without_the_monthly_flag(async_session_factory) -> None:
    """Режим «счёт за период» признаёт расход сам — галочку «помесячно» никто не ставил.

    Канон владельца, режим 2: лицензия с фиксированной платой и указанным в счёте периодом
    (Синапсис, АЙКО, Лемма, ДоксИнБокс). УПД по таким не ждут — месяц оплачен, 31-го
    обязательство исполнено. Без этой ветки платёж вечно висел в «ждём документ» и краснел
    просрочкой за документ, которого никто не выставит.
    """
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Лицензия-Период", inn="6155020101")
        wallet = await make_wallet(session, name="Лицензия-Кошелёк")
        article = await make_expense_article(session, code="LIC-PER", name="Лицензии период")
        profile = await session.scalar(
            select(CounterpartyPayableProfile).where(
                CounterpartyPayableProfile.counterparty_id == cp.id
            )
        )
        profile.service_billing_mode = "fixed_tariff"
        prepayment = SupplierPrepayment(
            counterparty_id=cp.id,
            kind="subscription",
            wallet_id=wallet.id,
            amount=Decimal("13000.00"),
            amount_settled=Decimal("0"),
            status="open",
            article_id=article.id,
            service_period_start=date(2026, 7, 1),
            service_period_end=date(2026, 7, 31),
            service_period_status="ready",
            auto_recognize_monthly=False,
        )
        session.add(prepayment)
        await session.commit()

        created = await accrue_due_months(session, as_of=date(2026, 8, 1), commit=True)
        assert [invoice.counterparty_id for invoice in created] == [cp.id]
        assert created[0].amount == Decimal("13000.00")

        accrual = await session.scalar(
            select(SupplierExpenseAccrual).where(SupplierExpenseAccrual.invoice_id == created[0].id)
        )
        assert accrual.service_period_end == date(2026, 7, 31)


async def test_document_supersedes_only_its_own_article(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """УПД по одной услуге не сносит признание ДРУГОЙ услуги того же контрагента.

    У АО «АЙКО» две лицензии с разными статьями ДДС — iikoOffice и iikoDelivery, — и они
    признаются самостоятельными самоактами. Фильтра по статье в ``supersede_self_billed``
    не было: пришедший УПД по одной лицензии аннулировал самоакты ОБЕИХ, расход второй
    исчезал из месяца, а её дебиторка возвращалась открытой. Деньги при этом сходились —
    видно было только в прибыли.
    """
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="АЙКО две лицензии", inn="6143049390")
        wallet = await make_wallet(session, code="tbank-sub-art", name="Т-Банк")
        office = await make_expense_article(session, code="LIC-OFF", name="Лицензия офис")
        delivery = await make_expense_article(session, code="LIC-DEL", name="Лицензия доставка")

        first = await _subscription_prepayment(
            session,
            counterparty_id=cp.id,
            wallet_id=wallet.id,
            amount="16430.00",
            start=date(2026, 7, 1),
            months=1,
        )
        first.article_id = office.id
        second = await _subscription_prepayment(
            session,
            counterparty_id=cp.id,
            wallet_id=wallet.id,
            amount="4260.00",
            start=date(2026, 7, 1),
            months=1,
        )
        second.article_id = delivery.id
        await session.commit()

        await accrue_due_months(session, as_of=date(2026, 8, 1))
        await session.refresh(first)
        await session.refresh(second)
        assert first.amount_settled == Decimal("16430.00")
        assert second.amount_settled == Decimal("4260.00")

        # Пришёл УПД только по офисной лицензии.
        real = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="16430.00",
            number="УПД-ОФИС",
            doc_kind="closing",
            invoice_date=date(2026, 7, 31),
        )
        real.dds_article_id = office.id
        real.service_period_start = date(2026, 7, 1)
        real.service_period_end = date(2026, 7, 31)
        real.service_period_status = "ready"
        await session.flush()

        superseded = await supersede_self_billed(session, real)
        await session.commit()

        assert len(superseded) == 1, "снесён самоакт чужой лицензии"
        # Дебиторка офисной вернулась под настоящий документ...
        await session.refresh(first)
        assert first.amount_settled == Decimal("0.00")
        # ...а доставка осталась признанной: её УПД никто не присылал.
        await session.refresh(second)
        assert second.amount_settled == Decimal("4260.00")
        assert second.status == "settled"


async def test_fixed_tariff_line_and_nightly_job_do_not_double_count(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Режим «счёт за период»: строка платежа и ночная джоба не признают одни деньги дважды.

    Гард против двойного счёта стоял ТОЛЬКО в ручном признании — ночная джоба про начисление
    по строке платежа не знала вовсе, потому что оно не документ. У Синапсиса это давало
    13 000 ₽ строкой плюс 13 000 ₽ самоактом за тот же июль: деньги сходились, врала прибыль.

    Проверяем обе стороны: строка при таком режиме своего начисления не заводит, а если оно
    всё же есть (заведено до разметки карточки) — джоба поверх него самоакт не создаёт.
    """
    from app.models import ExpenseDraftLine
    from app.services import supplier_service_periods as periods_service

    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Синапсис тариф", inn="614307902101")
        wallet = await make_wallet(session, code="tbank-fixed-1", name="Т-Банк")
        article = await make_expense_article(session, code="SYN-LIC", name="Лицензия Синапсис")
        # Профиль карточки заводит make_counterparty — здесь только выставляем режим.
        profile = await session.scalar(
            select(CounterpartyPayableProfile).where(
                CounterpartyPayableProfile.counterparty_id == cp.id
            )
        )
        assert profile is not None
        profile.service_billing_mode = "fixed_tariff"
        await session.flush()
        draft = await make_draft(session, counterparty_id=cp.id, amount="13000.00")
        line = ExpenseDraftLine(
            draft_id=draft.id,
            counterparty_id=cp.id,
            article_id=article.id,
            amount=Decimal("13000.00"),
            purpose="Лицензия за июль",
            service_period_start=date(2026, 7, 1),
            service_period_end=date(2026, 7, 31),
            auto_recognize_monthly=False,
        )
        session.add(line)
        await session.flush()
        # Дебиторка, из которой пойдут самоакты. Без неё признавать расход некому, и глушить
        # строку не за что — см. test_fixed_tariff_without_prepayment_keeps_the_payment_line.
        prepayment = await _subscription_prepayment(
            session,
            counterparty_id=cp.id,
            wallet_id=wallet.id,
            amount="13000.00",
            start=date(2026, 7, 1),
            months=1,
            auto=False,
        )
        prepayment.article_id = article.id
        await session.flush()

        # Сторона 1: расход месяца считает помесячный самоакт, а не строка.
        assert await periods_service.sync_expense_line_accrual(session, line) is None

        # Сторона 2: начисление по строке всё-таки существует (карточку разметили позже) —
        # ночная джоба обязана его увидеть и самоакт не заводить.
        session.add(
            SupplierExpenseAccrual(
                counterparty_id=cp.id,
                expense_draft_line_id=line.id,
                payment_draft_id=draft.id,
                article_id=article.id,
                amount=Decimal("13000.00"),
                service_period_start=date(2026, 7, 1),
                service_period_end=date(2026, 7, 31),
                status="scheduled",
            )
        )
        await session.commit()

        await accrue_due_months(session, as_of=date(2026, 8, 1))
        await session.commit()

        self_billed = await session.scalars(
            select(SupplierInvoice).where(
                SupplierInvoice.counterparty_id == cp.id,
                SupplierInvoice.source == SELF_BILLED_SOURCE,
            )
        )
        assert self_billed.all() == [], "джоба завела самоакт поверх начисления по строке"
        live = await session.scalars(
            select(SupplierExpenseAccrual).where(
                SupplierExpenseAccrual.counterparty_id == cp.id,
                SupplierExpenseAccrual.status != "cancelled",
            )
        )
        rows = live.all()
        assert len(rows) == 1
        assert rows[0].amount == Decimal("13000.00")


async def test_partial_agreement_does_not_mute_the_whole_payment_line(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Договор, покрывающий ЧАСТЬ периода строки, не глушит её целиком.

    Гард против двойного расхода проверял договор на КОНЦАХ периода: договор, действовавший
    только в апреле, глушил платёж за апрель-июнь целиком, а сам начислял один апрель — май и
    июнь не признавал никто. Потеря расхода хуже задвоения: задвоение видно в отчёте, пропажа
    нет. Глушим только при ПОЛНОМ покрытии периода.
    """
    from app.models import CounterpartyServiceAgreement, ExpenseDraftLine
    from app.services import supplier_service_periods as periods_service

    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Частичный договор", inn="614307902103")
        article = await make_expense_article(session, code="PART-AGR", name="Бухгалтерия")
        session.add(
            CounterpartyServiceAgreement(
                counterparty_id=cp.id,
                title="Договор на апрель",
                monthly_amount=Decimal("3000.00"),
                dds_article_id=article.id,
                documents_mode="informal",
                accrual_enabled=True,
                started_on=date(2026, 4, 1),
                ended_on=date(2026, 4, 30),
            )
        )
        draft = await make_draft(session, counterparty_id=cp.id, amount="9000.00")
        line = ExpenseDraftLine(
            draft_id=draft.id,
            counterparty_id=cp.id,
            article_id=article.id,
            amount=Decimal("9000.00"),
            purpose="Услуги за 2 квартал",
            service_period_start=date(2026, 4, 1),
            service_period_end=date(2026, 6, 30),
            auto_recognize_monthly=False,
        )
        session.add(line)
        await session.flush()

        accrual = await periods_service.sync_expense_line_accrual(session, line)
        await session.commit()

        # Договор закрывает только апрель — май и июнь остались бы без расхода вовсе.
        assert accrual is not None
        assert accrual.amount == Decimal("9000.00")


async def test_fixed_tariff_without_prepayment_keeps_the_payment_line_accrual(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Режим «счёт за период» без дебиторки строку не глушит: самоактов не будет.

    Самоакты режима fixed_tariff рождаются ИЗ ПРЕДОПЛАТЫ. Если платёж прошёл мимо неё,
    признавать расход некому, и отказ строки означал бы просто потерю расхода.
    """
    from app.models import ExpenseDraftLine
    from app.services import supplier_service_periods as periods_service

    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Тариф без аванса", inn="614307902104")
        article = await make_expense_article(session, code="FT-NOPRE", name="Лицензия")
        profile = await session.scalar(
            select(CounterpartyPayableProfile).where(
                CounterpartyPayableProfile.counterparty_id == cp.id
            )
        )
        assert profile is not None
        profile.service_billing_mode = "fixed_tariff"
        draft = await make_draft(session, counterparty_id=cp.id, amount="5000.00")
        line = ExpenseDraftLine(
            draft_id=draft.id,
            counterparty_id=cp.id,
            article_id=article.id,
            amount=Decimal("5000.00"),
            purpose="Лицензия за июль",
            service_period_start=date(2026, 7, 1),
            service_period_end=date(2026, 7, 31),
            auto_recognize_monthly=False,
        )
        session.add(line)
        await session.flush()

        accrual = await periods_service.sync_expense_line_accrual(session, line)
        await session.commit()

        assert accrual is not None, "расход потерян: самоактов нет и строка заглушена"
        assert accrual.amount == Decimal("5000.00")
