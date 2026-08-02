"""Сверка расчётов: хронология платежей и УПД, бегущий остаток, разрывы по сроку.

Задача, которую закрывает этот реестр: заплатили — а закрывающий документ не пришёл, и
заметить это негде. На 31.07.2026 так висело 311 969,51 ₽ у десяти контрагентов, а УПД
Микроэля за май нашли случайно через два месяца.

Здесь закреплено главное свойство сверки: её итог обязан сходиться с плиткой «Остатки» на
той же странице. Плитка считает дебиторку как открытые предоплаты, кредиторку — как
неоплаченный остаток закрывающих; хронология считает «деньги минус документы». Разъедутся —
и владелец получит два разных ответа на один вопрос, а доверие к экрану кончится на первом
же расхождении.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from cp_helpers import make_counterparty, make_invoice, make_wallet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    CashflowTransaction,
    CounterpartyPayableProfile,
    InvoicePaymentAllocation,
    SupplierPrepayment,
)
from app.services.counterparty_settlement_ledger import (
    build_ledger,
    expected_by,
    list_gaps,
    resolve_contour,
)


async def _payment(
    session: AsyncSession,
    *,
    counterparty_id: uuid.UUID,
    wallet_id: uuid.UUID,
    amount: str,
    on: date,
    period: tuple[date, date] | None = None,
    settled: str = "0",
) -> tuple[CashflowTransaction, SupplierPrepayment]:
    """Платёж контрагенту, породивший дебиторку (предоплату) — обычный путь денег."""
    tx = CashflowTransaction(
        wallet_id=wallet_id,
        counterparty_id=counterparty_id,
        direction="out",
        amount=Decimal(amount),
        operation_date=on,
        source_kind="manual",
        quality_status="auto",
    )
    session.add(tx)
    await session.flush()
    prepayment = SupplierPrepayment(
        counterparty_id=counterparty_id,
        kind="subscription",
        wallet_id=wallet_id,
        amount=Decimal(amount),
        amount_settled=Decimal(settled),
        status="settled" if Decimal(settled) >= Decimal(amount) else "open",
        cashflow_transaction_id=tx.id,
        service_period_start=period[0] if period else None,
        service_period_end=period[1] if period else None,
        service_period_status="ready" if period else "missing",
    )
    session.add(prepayment)
    await session.flush()
    return tx, prepayment


async def test_payment_then_document_nets_to_zero(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Платёж и пришедший на него УПД гасят друг друга — остаток 0, разрыва нет."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Микроэл", inn="6143049372")
        wallet = await make_wallet(session, code="tbank-ledger-1", name="Т-Банк")
        _tx, prepayment = await _payment(
            session,
            counterparty_id=cp.id,
            wallet_id=wallet.id,
            amount="3230.00",
            on=date(2026, 7, 7),
            period=(date(2026, 7, 1), date(2026, 7, 31)),
            settled="3230.00",
        )
        invoice = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="3230.00",
            number="6539",
            doc_kind="closing",
            invoice_date=date(2026, 7, 31),
            payment_status="paid",
        )
        session.add(
            InvoicePaymentAllocation(
                invoice_id=invoice.id,
                prepayment_id=prepayment.id,
                amount=Decimal("3230.00"),
                source_kind="prepayment",
            )
        )
        await session.commit()

        ledger = await build_ledger(session, cp.id, today=date(2026, 8, 15))

        assert [row.kind for row in ledger.rows] == ["document", "payment"]
        assert ledger.closing_balance == Decimal("0.00")
        assert ledger.overdue_amount == Decimal("0")
        payment_row = next(row for row in ledger.rows if row.kind == "payment")
        assert payment_row.status == "ok"
        assert payment_row.uncovered == Decimal("0")


async def test_payment_without_document_becomes_overdue_after_deadline(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Платёж за май без УПД: 1 июня — ещё ждём, 2 июня — уже разрыв.

    Это ровно кейс Микроэля: деньги за май ушли 9 июня, документ не пришёл никогда.
    """
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Микроэл-май", inn="6143049373")
        wallet = await make_wallet(session, code="tbank-ledger-2", name="Т-Банк")
        await _payment(
            session,
            counterparty_id=cp.id,
            wallet_id=wallet.id,
            amount="3230.00",
            on=date(2026, 5, 20),
            period=(date(2026, 5, 1), date(2026, 5, 31)),
        )
        await session.commit()

        # Внутри периода услуги ждать документ ещё рано.
        inside = await build_ledger(session, cp.id, today=date(2026, 5, 25))
        assert inside.rows[0].status == "waiting"

        # Последний день периода — ещё ждём…
        on_deadline = await build_ledger(session, cp.id, today=date(2026, 5, 31))
        assert on_deadline.rows[0].status == "waiting"

        # …и первые числа следующего месяца тоже: УПД за май 31 мая не выставит никто, он
        # приходит в начале июня. Срок по умолчанию — 10-е число (решение владельца 02.08.2026
        # взамен прежнего «конец периода»: ложное красное обесценивает настоящую просрочку).
        assert (await build_ledger(session, cp.id, today=date(2026, 6, 5))).rows[0].status == (
            "waiting"
        )
        assert (await build_ledger(session, cp.id, today=date(2026, 6, 10))).rows[0].status == (
            "waiting"
        )
        after = await build_ledger(session, cp.id, today=date(2026, 6, 15))
        row = after.rows[0]
        assert row.status == "overdue"
        assert row.uncovered == Decimal("3230.00")
        assert row.days_overdue == 5
        assert after.overdue_amount == Decimal("3230.00")
        assert after.closing_balance == Decimal("3230.00")  # дебиторка


async def test_expected_day_from_profile_delays_the_alarm(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Контрагент присылает УПД 5-го — до 5-го числа он не должен краснеть.

    Без этого поля сводка кричала бы 1-го числа на всех, кто честно присылает документы
    чуть позже, и владелец перестал бы её открывать.
    """
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Поставщик 5-го", inn="6143049374")
        wallet = await make_wallet(session, code="tbank-ledger-3", name="Т-Банк")
        profile = await session.scalar(
            select(CounterpartyPayableProfile).where(
                CounterpartyPayableProfile.counterparty_id == cp.id
            )
        )
        profile.closing_doc_expected_day = 5
        await _payment(
            session,
            counterparty_id=cp.id,
            wallet_id=wallet.id,
            amount="1000.00",
            on=date(2026, 6, 10),
            period=(date(2026, 6, 1), date(2026, 6, 30)),
        )
        await session.commit()

        # 5-е — день, когда контрагент обычно присылает: ещё ждём.
        for day in (3, 5):
            ledger = await build_ledger(session, cp.id, today=date(2026, 7, day))
            assert ledger.rows[0].status == "waiting"
        assert (await build_ledger(session, cp.id, today=date(2026, 7, 6))).rows[0].status == (
            "overdue"
        )


async def test_running_balance_matches_open_prepayments_and_unpaid_documents(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Итог хронологии = дебиторка минус кредиторка по тем же правилам, что и плитка.

    Три события вперемешку: закрытый платёж, открытый платёж и неоплаченный УПД.
    """
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Смешанный", inn="6143049375")
        wallet = await make_wallet(session, code="tbank-ledger-4", name="Т-Банк")
        # 1. Закрытая пара платёж+документ — на баланс не влияет.
        _tx, closed = await _payment(
            session,
            counterparty_id=cp.id,
            wallet_id=wallet.id,
            amount="1000.00",
            on=date(2026, 6, 5),
            period=(date(2026, 6, 1), date(2026, 6, 30)),
            settled="1000.00",
        )
        closed_doc = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="1000.00",
            number="D-1",
            doc_kind="closing",
            invoice_date=date(2026, 6, 30),
            payment_status="paid",
        )
        session.add(
            InvoicePaymentAllocation(
                invoice_id=closed_doc.id,
                prepayment_id=closed.id,
                amount=Decimal("1000.00"),
                source_kind="prepayment",
            )
        )
        # 2. Открытый платёж — дебиторка 500.
        await _payment(
            session,
            counterparty_id=cp.id,
            wallet_id=wallet.id,
            amount="500.00",
            on=date(2026, 7, 5),
            period=(date(2026, 7, 1), date(2026, 7, 31)),
        )
        # 3. Неоплаченный УПД — кредиторка 200.
        await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="200.00",
            number="D-2",
            doc_kind="closing",
            invoice_date=date(2026, 7, 20),
            payment_status="unpaid",
        )
        await session.commit()

        ledger = await build_ledger(session, cp.id, today=date(2026, 8, 10))

        # Дебиторка 500 − кредиторка 200 = 300, ровно как посчитала бы плитка «Остатки».
        assert ledger.closing_balance == Decimal("300.00")
        assert ledger.total_paid == Decimal("1500.00")
        assert ledger.total_documented == Decimal("1200.00")


async def test_ledger_rows_carry_running_balance_in_chronological_order(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Остаток в строке — состояние ПОСЛЕ неё, как в банковской выписке.

    Строки отдаются свежими сверху, но накопление считается по возрастанию дат — иначе
    «остаток после» в верхней строке показывал бы не итог, а первое событие.
    """
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Хронология", inn="6143049376")
        wallet = await make_wallet(session, code="tbank-ledger-5", name="Т-Банк")
        await _payment(
            session,
            counterparty_id=cp.id,
            wallet_id=wallet.id,
            amount="1000.00",
            on=date(2026, 6, 10),
            period=(date(2026, 6, 1), date(2026, 6, 30)),
        )
        await _payment(
            session,
            counterparty_id=cp.id,
            wallet_id=wallet.id,
            amount="700.00",
            on=date(2026, 7, 10),
            period=(date(2026, 7, 1), date(2026, 7, 31)),
        )
        await session.commit()

        ledger = await build_ledger(session, cp.id, today=date(2026, 8, 10))

        assert [row.row_date for row in ledger.rows] == [date(2026, 7, 10), date(2026, 6, 10)]
        assert [row.balance_after for row in ledger.rows] == [
            Decimal("1700.00"),
            Decimal("1000.00"),
        ]
        assert ledger.months[0].month == "2026-07"
        assert ledger.months[0].gap == Decimal("700.00")


async def test_contour_falls_back_to_warehouse_fact_and_manual_wins(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Контур: по факту складских накладных, но выбор в карточке сильнее факта."""
    async with async_session_factory() as session:
        goods = await make_counterparty(session, name="Товарный", inn="6143049377")
        await make_invoice(
            session,
            counterparty_id=goods.id,
            amount="100.00",
            doc_kind="closing",
            operational_scope="warehouse",
        )
        service = await make_counterparty(session, name="Сервисный", inn="6143049378")
        await session.commit()

        assert await resolve_contour(session, goods.id) == ("goods", False)
        assert await resolve_contour(session, service.id) == ("service", False)

        profile = await session.scalar(
            select(CounterpartyPayableProfile).where(
                CounterpartyPayableProfile.counterparty_id == goods.id
            )
        )
        profile.settlement_contour = "service"
        await session.commit()
        assert await resolve_contour(session, goods.id) == ("service", True)


async def test_gaps_summary_groups_by_period_and_hides_goods(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Сводка: два платежа за один период — одна строка; товарные не показываются.

    Группировка по периоду и есть смысл сводки: без неё это тот же реестр платежей,
    из которого разрывы и приходилось вылавливать глазами.
    """
    async with async_session_factory() as session:
        service_cp = await make_counterparty(session, name="Сервис", inn="6143049379")
        goods_cp = await make_counterparty(session, name="Товар", inn="6143049380")
        wallet = await make_wallet(session, code="tbank-ledger-6", name="Т-Банк")
        for day in (3, 20):
            await _payment(
                session,
                counterparty_id=service_cp.id,
                wallet_id=wallet.id,
                amount="1500.00",
                on=date(2026, 6, day),
                period=(date(2026, 6, 1), date(2026, 6, 30)),
            )
        await make_invoice(
            session,
            counterparty_id=goods_cp.id,
            amount="100.00",
            doc_kind="closing",
            operational_scope="warehouse",
        )
        await _payment(
            session,
            counterparty_id=goods_cp.id,
            wallet_id=wallet.id,
            amount="9000.00",
            on=date(2026, 6, 15),
            period=(date(2026, 6, 1), date(2026, 6, 30)),
        )
        await session.commit()

        gaps = await list_gaps(session, today=date(2026, 7, 20))

        assert len(gaps) == 1
        gap = gaps[0]
        assert gap.counterparty_name == "Сервис"
        assert gap.amount == Decimal("3000.00")
        assert gap.payments == 2
        assert gap.period_start == date(2026, 6, 1)
        # Срок по умолчанию — 10-е число следующего месяца, значит на 20.07 просрочка 10 дней.
        assert gap.days_overdue == 10

        # Товарный контрагент виден только по явному запросу.
        with_goods = await list_gaps(session, today=date(2026, 7, 20), include_goods=True)
        assert {row.counterparty_name for row in with_goods} == {"Сервис", "Товар"}


async def test_payment_without_period_falls_back_to_its_own_month(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Платёж без размеченного периода всё равно попадает под контроль — по месяцу операции.

    Иначе неразмеченные платежи (а их большинство у ручных выплат) молча выпадали бы из
    сводки — то есть ровно те деньги, которые чаще всего и теряются.
    """
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Без периода", inn="6143049381")
        wallet = await make_wallet(session, code="tbank-ledger-7", name="Т-Банк")
        await _payment(
            session,
            counterparty_id=cp.id,
            wallet_id=wallet.id,
            amount="9000.00",
            on=date(2026, 6, 15),
        )
        await session.commit()

        ledger = await build_ledger(session, cp.id, today=date(2026, 7, 20))

        row = ledger.rows[0]
        assert (row.period_start, row.period_end) == (date(2026, 6, 1), date(2026, 6, 30))
        assert row.status == "overdue"


async def test_payment_in_the_second_half_of_the_month_is_an_advance_for_the_next(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Платёж 16-го и позже без периода — аванс за следующий месяц, а не за свой.

    Услуги оплачивают вперёд, в конце предыдущего месяца: Директ и Синапсис платят 29-го,
    ДоксИнБокс 23-го и 27-го — и каждый раз это следующий месяц (владелец, 02.08.2026).
    Прежний фолбэк «месяц платежа» ставил такие авансы на месяц раньше: расход июля числился
    июньским, а документ по нему ждали на месяц раньше срока и зря красили строку.
    """
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Аванс вперёд", inn="6143049382")
        wallet = await make_wallet(session, code="tbank-ledger-8", name="Т-Банк")
        await _payment(
            session,
            counterparty_id=cp.id,
            wallet_id=wallet.id,
            amount="13000.00",
            on=date(2026, 6, 29),
        )
        await session.commit()

        ledger = await build_ledger(session, cp.id, today=date(2026, 7, 20))

        row = ledger.rows[0]
        assert (row.period_start, row.period_end) == (date(2026, 7, 1), date(2026, 7, 31))
        # Период июля ещё идёт — документа ждём до 10 августа, красного быть не должно.
        assert row.status == "waiting"


async def test_barter_documents_stay_out_of_the_ledger(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Бартерный заём гасится товаром, а не деньгами — в «деньги минус документы» он не идёт.

    Иначе остаток по бартерному контрагенту уходил бы в минус на всю сумму займа, и сверка
    противоречила бы плитке, где у бартера свой нетто-контур.
    """
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Бартерный", inn="6143049382")
        await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="5000.00",
            doc_kind="closing",
            invoice_date=date(2026, 7, 1),
            barter_role="loan",
        )
        await session.commit()

        ledger = await build_ledger(session, cp.id, today=date(2026, 8, 1))

        assert ledger.rows == []
        assert ledger.closing_balance == Decimal("0")
        assert ledger.has_barter is True


async def test_document_paid_by_another_counterpartys_transaction_still_balances(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Документ закрыт платежом, помеченным ДРУГИМ контрагентом — остаток не должен «уплыть».

    Найдено на проде 01.08.2026: накладную ИП Скачковой на 3 561,60 ₽ закрыла проводка,
    у которой в ДДС стоит ООО «ТОРА». Пока такие деньги не попадали в хронологию, документ
    в ней был, а платежа под ним не было — сверка занижала остаток ровно на эту сумму и
    расходилась с плиткой. Строка теперь есть и честно подписана, чья это проводка: либо
    ошибка разметки в ДДС, либо оплата за третье лицо — решает человек.
    """
    async with async_session_factory() as session:
        supplier = await make_counterparty(session, name="Поставщик", inn="6143049385")
        payer = await make_counterparty(session, name='ООО "ТОРА"', inn="6143049386")
        wallet = await make_wallet(session, code="tbank-ledger-9", name="Т-Банк")
        invoice = await make_invoice(
            session,
            counterparty_id=supplier.id,
            amount="3561.60",
            number="DX001312A",
            doc_kind="closing",
            invoice_date=date(2026, 6, 19),
            payment_status="paid",
        )
        alien_tx = CashflowTransaction(
            wallet_id=wallet.id,
            counterparty_id=payer.id,
            direction="out",
            amount=Decimal("3561.60"),
            operation_date=date(2026, 6, 30),
            source_kind="counterparty_payment",
            quality_status="final",
        )
        session.add(alien_tx)
        await session.flush()
        session.add(
            InvoicePaymentAllocation(
                invoice_id=invoice.id,
                cashflow_transaction_id=alien_tx.id,
                amount=Decimal("3561.60"),
                source_kind="cash",
            )
        )
        await session.commit()

        ledger = await build_ledger(session, supplier.id, today=date(2026, 8, 1))

        assert ledger.closing_balance == Decimal("0.00")
        alien_row = next(row for row in ledger.rows if row.kind == "payment")
        assert "ТОРА" in alien_row.title
        assert alien_row.subtitle is not None and "другого контрагента" in alien_row.subtitle


def test_expected_by_clamps_to_short_month() -> None:
    """28-е в карточке и февраль: дата ожидания не должна выпадать за край месяца."""
    assert expected_by(date(2026, 1, 31), 28) == date(2026, 2, 28)
    assert expected_by(date(2026, 12, 31), 5) == date(2027, 1, 5)
    # NULL = 10-е число следующего месяца. Прежний дефолт «конец периода» назначал документ
    # последним днём самого периода: УПД за август 31.08 не выставят никогда, и строка краснела
    # 1 сентября заведомо зря (правка по замечанию владельца 02.08.2026).
    assert expected_by(date(2026, 6, 30), None) == date(2026, 7, 10)
    assert expected_by(date(2026, 12, 31), None) == date(2027, 1, 10)


async def test_opening_balance_for_filtered_period(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Фильтр по датам не должен ронять остаток: то, что было раньше, идёт входящим сальдо."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="С фильтром", inn="6143049383")
        wallet = await make_wallet(session, code="tbank-ledger-8", name="Т-Банк")
        await _payment(
            session,
            counterparty_id=cp.id,
            wallet_id=wallet.id,
            amount="1000.00",
            on=date(2026, 5, 5),
            period=(date(2026, 5, 1), date(2026, 5, 31)),
        )
        await _payment(
            session,
            counterparty_id=cp.id,
            wallet_id=wallet.id,
            amount="400.00",
            on=date(2026, 7, 5),
            period=(date(2026, 7, 1), date(2026, 7, 31)),
        )
        await session.commit()

        ledger = await build_ledger(
            session, cp.id, today=date(2026, 8, 1), date_from=date(2026, 7, 1)
        )

        assert ledger.opening_balance == Decimal("1000.00")
        assert [row.amount for row in ledger.rows] == [Decimal("400.00")]
        assert ledger.closing_balance == Decimal("1400.00")


async def test_opening_prepayment_shows_as_money_row(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Входящий остаток (деньги до внедрения системы) — такая же строка платежа."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="С опенингом", inn="6143049384")
        session.add(
            SupplierPrepayment(
                counterparty_id=cp.id,
                kind="subscription",
                amount=Decimal("2500.00"),
                amount_settled=Decimal("0"),
                status="open",
                opening=True,
                created_at=datetime(2026, 7, 20, tzinfo=UTC),
                note="Входящий остаток на старте",
            )
        )
        await session.commit()

        ledger = await build_ledger(session, cp.id, today=date(2026, 9, 1))

        assert len(ledger.rows) == 1
        assert ledger.rows[0].title == "Входящий остаток"
        assert ledger.closing_balance == Decimal("2500.00")


async def test_derived_prepayment_is_not_a_second_money_row(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Производная дебиторка вторым денежным фактом в сверку не идёт.

    Излишек оплаты накладной, оплата счёта, возврат при откате расхода — у всех этих
    предоплат нет ДДС-проводки, но деньги посчитаны ДРУГОЙ строкой. Сверка отсекала только
    ``prepaid_bill``, и излишек оплаты ООО «АЛЬЯНС ЮГ» на 23 730 ₽ (``kind='goods'``) уводил
    бегущий остаток от плитки «Остатки» ровно на эту сумму.
    """
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Излишек оплаты", inn="6143049385")
        session.add(
            SupplierPrepayment(
                counterparty_id=cp.id,
                kind="goods",
                amount=Decimal("23730.00"),
                amount_settled=Decimal("0"),
                status="open",
                # opening не выставлен: дебиторка выведена из оплаты накладной, а не внесена
                # как входящий остаток.
                created_at=datetime(2026, 7, 20, tzinfo=UTC),
                note="Излишек оплаты по накладной №DX001323A — перенесён в дебиторку",
            )
        )
        await session.commit()

        ledger = await build_ledger(session, cp.id, today=date(2026, 9, 1))

        assert ledger.rows == []
        assert ledger.closing_balance == Decimal("0")


async def test_future_and_informational_documents_do_not_move_the_balance(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Будущий и справочный документы видны в хронологии, но остаток не двигают.

    Бегущий остаток вычитал ЛЮБОЙ документ подряд, а плитка «Остатки» честно считала только
    активные и не-информационные. Расхождение на проде составляло 100 000 ₽: два арендных УПД
    от 31.08.2026 по 50 000 ждали своей даты (правило 4), а сверка уже записала их в долг.
    """
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Будущее и справка", inn="6143049380")
        wallet = await make_wallet(session, code="tbank-ledger-9", name="Т-Банк")
        # Платёж 1 000 — дебиторка, единственное, что должно остаться в остатке.
        await _payment(
            session,
            counterparty_id=cp.id,
            wallet_id=wallet.id,
            amount="1000.00",
            on=date(2026, 7, 5),
            period=(date(2026, 7, 1), date(2026, 7, 31)),
        )
        # Будущий закрывающий: дата ещё не наступила (правило 4 канона).
        future = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="50000.00",
            number="БУД-1",
            doc_kind="closing",
            invoice_date=date(2026, 8, 31),
            payment_status="unpaid",
        )
        future.activation_status = "pending"
        # Справочный: расход по нему уже начислен договором услуги.
        informational = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="3000.00",
            number="СПР-1",
            doc_kind="closing",
            invoice_date=date(2026, 7, 20),
            payment_status="unpaid",
        )
        informational.informational = True
        await session.commit()

        ledger = await build_ledger(session, cp.id, today=date(2026, 8, 10))

        # Остаток — только живой платёж; 53 000 ₽ чужих документов его не тронули.
        assert ledger.closing_balance == Decimal("1000.00")
        # Но сами строки в хронологии есть: расхождение с договором надо замечать, а не прятать.
        numbers = {row.title for row in ledger.rows if row.kind == "document"}
        assert any("БУД-1" in title for title in numbers)
        assert any("СПР-1" in title for title in numbers)
