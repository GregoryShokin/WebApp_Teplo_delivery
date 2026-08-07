"""Закрывающий документ выбирает СВОЙ аванс, а не первый по хронологии.

Кейс АО «АЙКО» (прод, 02.08.2026). У поставщика две регулярные линии под одним ИНН — Курьерика
(4 260 ₽) и лицензия iikoCloud (16 430 ₽), — и оба акта пришли одним днём. Гашение шло по
хронологии денег с приоритетом совпавшего периода, но периода у ручных актов не было вовсе, и
акты разошлись крест-накрест: акт на лицензию закрылся авансом за Курьерику плюс остатком
безадресного пула, а акт на Курьерику — куском того же пула.

Нетто по контрагенту при этом сходилось — и потому ошибка не всплывала. Всплыла бы она в первый
же месяц с изменением цены: чужой аванс закрыл бы акт частично, и в отчётах появились бы
фантомы — акт «частично не оплачен» в кредиторке и чужой открытый аванс в дебиторке
одновременно, при верном нетто.

Лестница приоритетов: счёт-основание → период услуги → продукт → сумма → хронология. Последняя
ступень означает «система угадала» и подписывается в ``match_basis``.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from cp_helpers import make_counterparty, make_wallet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    CashflowTransaction,
    InvoicePaymentAllocation,
    SupplierExpenseAccrual,
    SupplierInvoice,
    SupplierPrepayment,
)
from app.services import supplier_prepayments as prepayments

COURIER = "courierica"
LICENSE = "iiko_license"


async def _bill(
    session: AsyncSession,
    *,
    counterparty_id: uuid.UUID,
    number: str,
    amount: str,
    invoice_date: date,
    product: str | None = None,
    period: tuple[date, date] | None = None,
) -> SupplierInvoice:
    """Счёт поставщика — тот, что оплачивают вперёд и на который потом ссылается акт."""
    payload: dict[str, object] = {}
    if product:
        payload["recognition"] = {"product_hint": product}
    bill = SupplierInvoice(
        counterparty_id=counterparty_id,
        source="email",
        direction="payable",
        doc_kind="bill",
        operational_scope="finance",
        number=number,
        invoice_date=invoice_date,
        amount=Decimal(amount),
        payment_status="paid",
        service_period_start=period[0] if period else None,
        service_period_end=period[1] if period else None,
        service_period_status="ready" if period else "not_required",
        raw_payload=payload,
    )
    session.add(bill)
    await session.flush()
    return bill


async def _closing(
    session: AsyncSession,
    *,
    counterparty_id: uuid.UUID,
    number: str,
    amount: str,
    invoice_date: date,
    basis_number: str | None = None,
    product: str | None = None,
    period: tuple[date, date] | None = None,
) -> SupplierInvoice:
    """Закрывающий документ (УПД/акт) — он и выбирает, какой аванс погасить."""
    recognition: dict[str, object] = {}
    if basis_number:
        recognition["basis_number"] = basis_number
    if product:
        recognition["product_hint"] = product
    closing = SupplierInvoice(
        counterparty_id=counterparty_id,
        source="email",
        direction="payable",
        doc_kind="closing",
        operational_scope="finance",
        number=number,
        invoice_date=invoice_date,
        amount=Decimal(amount),
        payment_status="unpaid",
        service_period_start=period[0] if period else None,
        service_period_end=period[1] if period else None,
        service_period_status="ready" if period else "not_required",
        raw_payload={"recognition": recognition} if recognition else {},
    )
    session.add(closing)
    await session.flush()
    return closing


async def _prepaid(
    session: AsyncSession,
    *,
    counterparty_id: uuid.UUID,
    amount: str,
    paid_on: date,
    wallet_code: str,
    bill: SupplierInvoice | None = None,
    kind: str = "prepaid_bill",
    period: tuple[date, date] | None = None,
) -> SupplierPrepayment:
    """Аванс поставщику: деньги ушли, поставщик должен услугой."""
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
        kind=kind,
        wallet_id=wallet.id,
        amount=Decimal(amount),
        amount_settled=Decimal("0.00"),
        status="open",
        cashflow_transaction_id=tx.id,
        bill_invoice_id=bill.id if bill is not None else None,
        service_period_start=period[0] if period else None,
        service_period_end=period[1] if period else None,
    )
    session.add(prepayment)
    await session.flush()
    return prepayment


async def _allocations(
    session: AsyncSession, invoice_id: uuid.UUID
) -> list[InvoicePaymentAllocation]:
    return list(
        (
            await session.scalars(
                select(InvoicePaymentAllocation).where(
                    InvoicePaymentAllocation.invoice_id == invoice_id
                )
            )
        ).all()
    )


async def test_closing_takes_prepayment_of_its_basis_invoice(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Акт гасит аванс за СВОЙ счёт, даже когда чужой аванс ушёл раньше.

    Ровно то, чего не хватало у iiko: периода в тексте акта нет, а номер счёта есть — акт
    прямо называет его строкой «Основание Счет № … от …»."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="АЙКО-основание", inn="1655160001")
        early = await _bill(
            session,
            counterparty_id=cp.id,
            number="СЧЁТ-РАННИЙ",
            amount="4260.00",
            invoice_date=date(2026, 6, 7),
        )
        target = await _bill(
            session,
            counterparty_id=cp.id,
            number="040726-40618-лсп",
            amount="4260.00",
            invoice_date=date(2026, 7, 4),
        )
        # Ранний аванс по хронологии денег идёт первым — и без адресности съел бы документ.
        await _prepaid(
            session,
            counterparty_id=cp.id,
            amount="4260.00",
            paid_on=date(2026, 6, 10),
            wallet_code="basis-early",
            bill=early,
        )
        own = await _prepaid(
            session,
            counterparty_id=cp.id,
            amount="4260.00",
            paid_on=date(2026, 7, 8),
            wallet_code="basis-own",
            bill=target,
        )
        act = await _closing(
            session,
            counterparty_id=cp.id,
            number="10826-9432-лсп",
            amount="4260.00",
            invoice_date=date(2026, 8, 1),
            basis_number="040726-40618-лсп",
        )
        await session.commit()

        settled = await prepayments.auto_settle_invoice_from_open_prepayments(session, act)
        await session.commit()

        assert settled == Decimal("4260.00")
        allocations = await _allocations(session, act.id)
        assert [a.prepayment_id for a in allocations] == [own.id]
        assert [a.match_basis for a in allocations] == [prepayments.MATCH_BASIS_INVOICE]


async def test_two_product_lines_do_not_cross_settle(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Две линии одного поставщика не гасятся крест-накрест — воспроизведение кейса АЙКО.

    Ни у актов, ни у безадресного пула периода нет: развести их может только продукт и сумма.
    Прежний порядок отдавал акту на лицензию (16 430 ₽) сначала аванс за Курьерику (4 260 ₽),
    и дальше каждая сторона считала себя закрытой чужими деньгами."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="АЙКО-две линии", inn="1655160002")
        courier_bill = await _bill(
            session,
            counterparty_id=cp.id,
            number="070626-33538-лсп",
            amount="4260.00",
            invoice_date=date(2026, 6, 7),
            product=COURIER,
        )
        courier_prepaid = await _prepaid(
            session,
            counterparty_id=cp.id,
            amount="4260.00",
            paid_on=date(2026, 6, 29),
            wallet_code="cross-courier",
            bill=courier_bill,
        )
        # Безадресный пул: счёт лицензии в систему не попал, аванс висит сам по себе.
        pool = await _prepaid(
            session,
            counterparty_id=cp.id,
            amount="16430.00",
            paid_on=date(2026, 6, 20),
            wallet_code="cross-pool",
            kind="subscription",
        )
        license_act = await _closing(
            session,
            counterparty_id=cp.id,
            number="060626-4260-лк",
            amount="16430.00",
            invoice_date=date(2026, 7, 1),
        )
        courier_act = await _closing(
            session,
            counterparty_id=cp.id,
            number="070626-33538-лсп-акт",
            amount="4260.00",
            invoice_date=date(2026, 7, 1),
            product=COURIER,
        )
        await session.commit()

        # Порядок проведения как на проде: сначала акт на лицензию.
        assert await prepayments.auto_settle_invoice_from_open_prepayments(
            session, license_act
        ) == Decimal("16430.00")
        assert await prepayments.auto_settle_invoice_from_open_prepayments(
            session, courier_act
        ) == Decimal("4260.00")
        await session.commit()

        license_alloc = await _allocations(session, license_act.id)
        courier_alloc = await _allocations(session, courier_act.id)
        # Лицензия закрыта пулом целиком — одной строкой, а не двумя кусками чужих денег.
        assert [(a.prepayment_id, a.amount) for a in license_alloc] == [
            (pool.id, Decimal("16430.00"))
        ]
        assert [a.prepayment_id for a in courier_alloc] == [courier_prepaid.id]
        assert [a.match_basis for a in courier_alloc] == [prepayments.MATCH_PRODUCT]


async def test_matching_period_still_beats_chronology(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Регресс: совпадение периода по-прежнему сильнее хронологии денег.

    Это поведение было до лестницы (акт за июль не должен гасить платёж за 2 квартал) — новые
    ступени не имеют права его отменить."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Период-приоритет", inn="1655160003")
        await _prepaid(
            session,
            counterparty_id=cp.id,
            amount="5000.00",
            paid_on=date(2026, 4, 5),
            wallet_code="period-old",
            kind="subscription",
            period=(date(2026, 4, 1), date(2026, 4, 30)),
        )
        july = await _prepaid(
            session,
            counterparty_id=cp.id,
            amount="5000.00",
            paid_on=date(2026, 7, 3),
            wallet_code="period-july",
            kind="subscription",
            period=(date(2026, 7, 1), date(2026, 7, 31)),
        )
        act = await _closing(
            session,
            counterparty_id=cp.id,
            number="УПД-ИЮЛЬ",
            amount="5000.00",
            invoice_date=date(2026, 7, 31),
            period=(date(2026, 7, 1), date(2026, 7, 31)),
        )
        await session.commit()

        await prepayments.auto_settle_invoice_from_open_prepayments(session, act)
        await session.commit()

        allocations = await _allocations(session, act.id)
        assert [a.prepayment_id for a in allocations] == [july.id]
        assert [a.match_basis for a in allocations] == [prepayments.MATCH_SERVICE_PERIOD]


async def test_guess_is_marked_as_guess(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Без адресных признаков зачёт по-прежнему происходит — но подписан как догадка.

    Отказ гасить сломал бы почти весь прод: у большинства платежей нет ни периода, ни
    основания. Поэтому не запрещаем, а называем вещи своими именами."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Без признаков", inn="1655160004")
        pool = await _prepaid(
            session,
            counterparty_id=cp.id,
            amount="9000.00",
            paid_on=date(2026, 7, 2),
            wallet_code="guess-pool",
            kind="subscription",
        )
        act = await _closing(
            session,
            counterparty_id=cp.id,
            number="УПД-БЕЗ-ПРИЗНАКОВ",
            amount="3000.00",
            invoice_date=date(2026, 7, 31),
        )
        await session.commit()

        await prepayments.auto_settle_invoice_from_open_prepayments(session, act)
        await session.commit()

        allocations = await _allocations(session, act.id)
        assert [(a.prepayment_id, a.match_basis) for a in allocations] == [
            (pool.id, prepayments.MATCH_CHRONOLOGY)
        ]


async def test_manual_settlement_leaves_basis_empty(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Ручной зачёт основания не получает: аванс там назвал человек, обосновывать нечего."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Ручной зачёт", inn="1655160005")
        pool = await _prepaid(
            session,
            counterparty_id=cp.id,
            amount="1000.00",
            paid_on=date(2026, 7, 2),
            wallet_code="manual-pool",
            kind="subscription",
        )
        act = await _closing(
            session,
            counterparty_id=cp.id,
            number="УПД-РУЧНОЙ",
            amount="1000.00",
            invoice_date=date(2026, 7, 31),
        )
        await session.commit()

        await prepayments.settle_invoice_from_prepayment(
            session, invoice_id=act.id, prepayment_id=pool.id
        )
        await session.commit()

        allocations = await _allocations(session, act.id)
        assert [a.match_basis for a in allocations] == [None]


async def test_closing_waits_for_the_end_of_its_service_period(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Акт, выданный ВПЕРЁД на месяц, не гасит аванс в день прихода.

    «Акт на передачу прав» iiko датирован первым числом оплаченного месяца — собственная дата
    к приходу письма уже прошла, и правило 4 его пропускало. Аванс закрывался 1-го числа, а
    расход по нему признавался только после конца месяца: весь август 20 690 ₽ не значились
    ни активом, ни расходом."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="АЙКО-вперёд", inn="1655160006")
        bill = await _bill(
            session,
            counterparty_id=cp.id,
            number="040726-2486-лк",
            amount="16430.00",
            invoice_date=date(2026, 7, 4),
            period=(date(2026, 8, 1), date(2026, 8, 31)),
        )
        prepaid = await _prepaid(
            session,
            counterparty_id=cp.id,
            amount="16430.00",
            paid_on=date(2026, 7, 8),
            wallet_code="ahead-bill",
            bill=bill,
            period=(date(2026, 8, 1), date(2026, 8, 31)),
        )
        act = await _closing(
            session,
            counterparty_id=cp.id,
            number="10826-8581-лк",
            amount="16430.00",
            invoice_date=date(2026, 8, 1),
            basis_number="040726-2486-лк",
            period=(date(2026, 8, 1), date(2026, 8, 31)),
        )
        await session.commit()

        # 4 августа: собственная дата акта прошла, но услуга ещё оказывается.
        settled = await prepayments.apply_closing_document(session, act, as_of=date(2026, 8, 4))
        await session.commit()
        assert settled == Decimal("0.00")
        assert act.activation_status == "pending"
        await session.refresh(prepaid)
        assert prepaid.status == "open"
        assert await _allocations(session, act.id) == []

        # Вечером 31 августа услуга ещё оказывается — документ по-прежнему ждёт.
        assert (await prepayments.activate_due_closing_invoices(session, as_of=date(2026, 8, 31)))[
            "activated"
        ] == 0
        await session.refresh(prepaid)
        assert prepaid.status == "open"

        # 1 сентября — тем же прогоном, что признаёт расход: документ проводится и гасит
        # СВОЙ аванс. Промежуточного состояния «аванса уже нет, расхода ещё нет» не бывает.
        result = await prepayments.activate_due_closing_invoices(session, as_of=date(2026, 9, 1))
        await session.commit()
        assert result["activated"] == 1
        await session.refresh(act)
        await session.refresh(prepaid)
        assert act.activation_status == "active"
        assert prepaid.status == "settled"
        allocations = await _allocations(session, act.id)
        assert [a.match_basis for a in allocations] == [prepayments.MATCH_BASIS_INVOICE]


async def test_closing_for_finished_period_activates_immediately(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Регресс: акт за ЗАВЕРШЁННЫЙ месяц проводится сразу, как и раньше.

    Так шлют почти все (ЭкоЦентр, Авито, Стартер, МИКРОЭЛ) — документ датирован концом периода
    обслуживания. Ожидание конца периода для них ничего не меняет."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Завершённый период", inn="1655160007")
        pool = await _prepaid(
            session,
            counterparty_id=cp.id,
            amount="3348.22",
            paid_on=date(2026, 7, 5),
            wallet_code="finished-pool",
            kind="subscription",
        )
        act = await _closing(
            session,
            counterparty_id=cp.id,
            number="ВД-46890",
            amount="3348.22",
            invoice_date=date(2026, 7, 31),
            period=(date(2026, 7, 1), date(2026, 7, 31)),
        )
        await session.commit()

        settled = await prepayments.apply_closing_document(session, act, as_of=date(2026, 8, 1))
        await session.commit()

        assert settled == Decimal("3348.22")  # период июля закончился — документ в силе
        assert act.activation_status == "active"
        await session.refresh(pool)
        assert pool.status == "settled"


async def test_balance_as_of_keeps_advance_while_service_runs(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Баланс на дату согласован с правилом 4: аванс висит активом, пока услуга идёт.

    Плитка «Остатки» и баланс на дату отвечают на разные вопросы, но об одном контрагенте не
    вправе говорить разное. Пока событие гашения датировалось голой датой документа, акт iiko
    от 01.08 списывал аванс первым августа в БАЛАНСЕ, тогда как в плитке он ещё ждал в
    ``pending``: 20 690 ₽ разницы между двумя источниками правды."""
    from app.services.counterparty_balance_as_of import build_balance_as_of

    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="АЙКО-баланс", inn="1655160008")
        bill = await _bill(
            session,
            counterparty_id=cp.id,
            number="040726-2486-лк-бал",
            amount="16430.00",
            invoice_date=date(2026, 7, 4),
            period=(date(2026, 8, 1), date(2026, 8, 31)),
        )
        await _prepaid(
            session,
            counterparty_id=cp.id,
            amount="16430.00",
            paid_on=date(2026, 7, 8),
            wallet_code="balance-ahead",
            bill=bill,
            period=(date(2026, 8, 1), date(2026, 8, 31)),
        )
        act = await _closing(
            session,
            counterparty_id=cp.id,
            number="10826-8581-лк-бал",
            amount="16430.00",
            invoice_date=date(2026, 8, 1),
            basis_number="040726-2486-лк-бал",
            period=(date(2026, 8, 1), date(2026, 8, 31)),
        )
        await session.commit()

        await prepayments.apply_closing_document(session, act, as_of=date(2026, 9, 1))
        await session.commit()

        def mine(rows) -> Decimal:
            return sum((r.receivable for r in rows if r.counterparty_id == cp.id), Decimal("0.00"))

        # Услуга ещё идёт — деньги остаются нашим активом.
        mid = await build_balance_as_of(session, as_of=date(2026, 8, 15))
        assert mine(mid.rows) == Decimal("16430.00")
        # Месяц закрыт: аванс списан, дальше расход признаётся начислением августа.
        end = await build_balance_as_of(session, as_of=date(2026, 8, 31))
        assert mine(end.rows) == Decimal("0.00")
        # На конец июля дебиторка была и остаётся — регресс прежнего поведения.
        july = await build_balance_as_of(session, as_of=date(2026, 7, 31))
        assert mine(july.rows) == Decimal("16430.00")


async def test_closing_without_own_date_is_not_deferred(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Документ без своей даты не откладывается — иначе он не проснулся бы никогда.

    Дата у накладной nullable, и почта её регулярно не распознаёт. Джоба активации ищет
    документы С датой, поэтому отложенный без даты завис бы в pending навсегда: обязательство и
    расход по нему пропали бы молча. Пусть лучше действует сразу, как было до правила."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Акт без даты", inn="1655160009")
        pool = await _prepaid(
            session,
            counterparty_id=cp.id,
            amount="5000.00",
            paid_on=date(2026, 7, 2),
            wallet_code="nodate-pool",
            kind="subscription",
        )
        act = await _closing(
            session,
            counterparty_id=cp.id,
            number="БЕЗ-ДАТЫ",
            amount="5000.00",
            invoice_date=date(2026, 8, 1),
            period=(date(2026, 9, 1), date(2026, 9, 30)),
        )
        act.invoice_date = None
        await session.commit()

        settled = await prepayments.apply_closing_document(session, act, as_of=date(2026, 8, 4))
        await session.commit()

        assert act.activation_status == "active"
        assert settled == Decimal("5000.00")
        await session.refresh(pool)
        assert pool.status == "settled"


async def test_ambiguous_period_does_not_defer_the_document(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Недоверенный период (ambiguous) деньгами не двигает.

    Когда в тексте нашлось несколько периодов, ingest записывает один и ставит 'ambiguous' —
    именно чтобы им не пользовались до решения оператора: начисление по такому периоду тоже не
    создаётся. Дать ему откладывать обязательство значило бы доверить деньги догадке парсера."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Период под вопросом", inn="1655160010")
        pool = await _prepaid(
            session,
            counterparty_id=cp.id,
            amount="7000.00",
            paid_on=date(2026, 7, 2),
            wallet_code="ambig-pool",
            kind="subscription",
        )
        act = await _closing(
            session,
            counterparty_id=cp.id,
            number="УПД-АМБИГ",
            amount="7000.00",
            invoice_date=date(2026, 8, 1),
            period=(date(2026, 9, 1), date(2026, 9, 30)),
        )
        act.service_period_status = "ambiguous"
        await session.commit()

        await prepayments.apply_closing_document(session, act, as_of=date(2026, 8, 4))
        await session.commit()

        assert act.activation_status == "active"
        await session.refresh(pool)
        assert pool.status == "settled"


async def test_amount_rank_does_not_pull_money_paid_after_the_document(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Равенство суммы не перетягивает платёж, ушедший ПОЗЖЕ документа (инцидент Манго).

    У подписочного поставщика с ровной абонентской платой сумма совпадает каждый месяц, и
    слабейшая ступень лестницы утащила бы в июньский УПД июльские деньги — а июньские остались
    бы висеть непогашенными. Правило хронологии сильнее совпадения суммы."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Манго-хронология", inn="1655160011")
        june = await _prepaid(
            session,
            counterparty_id=cp.id,
            amount="8000.00",
            paid_on=date(2026, 6, 15),
            wallet_code="mango-june",
            kind="subscription",
        )
        july = await _prepaid(
            session,
            counterparty_id=cp.id,
            amount="5000.00",
            paid_on=date(2026, 7, 7),
            wallet_code="mango-july",
            kind="subscription",
        )
        act = await _closing(
            session,
            counterparty_id=cp.id,
            number="УПД-ИЮНЬ-МАНГО",
            amount="5000.00",
            invoice_date=date(2026, 6, 30),
        )
        await session.commit()

        await prepayments.auto_settle_invoice_from_open_prepayments(session, act)
        await session.commit()

        allocations = await _allocations(session, act.id)
        # Июньские деньги, а не совпавшие по сумме июльские.
        assert [a.prepayment_id for a in allocations] == [june.id]
        assert [a.match_basis for a in allocations] == [prepayments.MATCH_CHRONOLOGY]
        await session.refresh(july)
        assert july.status == "open"


async def test_product_beats_bare_period_for_two_lines_of_one_month(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Когда обе линии поставщика за ОДИН месяц, разводит продукт, а не период.

    Период у обоих авансов совпадает с периодом акта одинаково — как признак он не различает
    ничего, и приоритет периода вернул бы выбор в хронологию, то есть к тому же перекрёстному
    зачёту, ради которого лестница написана."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="АЙКО-сентябрь", inn="1655160012")
        period = (date(2026, 9, 1), date(2026, 9, 30))
        courier_bill = await _bill(
            session,
            counterparty_id=cp.id,
            number="010826-9723-лсп",
            amount="4260.00",
            invoice_date=date(2026, 8, 1),
            product=COURIER,
            period=period,
        )
        license_bill = await _bill(
            session,
            counterparty_id=cp.id,
            number="010826-3064-лк",
            amount="16430.00",
            invoice_date=date(2026, 8, 1),
            product=LICENSE,
            period=period,
        )
        courier_prepaid = await _prepaid(
            session,
            counterparty_id=cp.id,
            amount="4260.00",
            paid_on=date(2026, 8, 5),
            wallet_code="sep-courier",
            bill=courier_bill,
            period=period,
        )
        license_prepaid = await _prepaid(
            session,
            counterparty_id=cp.id,
            amount="16430.00",
            paid_on=date(2026, 8, 6),
            wallet_code="sep-license",
            bill=license_bill,
            period=period,
        )
        # Акт на лицензию без строки «Основание» — остаётся только продукт.
        license_act = await _closing(
            session,
            counterparty_id=cp.id,
            number="10926-0001-лк",
            amount="16430.00",
            invoice_date=date(2026, 9, 1),
            product=LICENSE,
            period=period,
        )
        await session.commit()

        await prepayments.apply_closing_document(session, license_act, as_of=date(2026, 10, 1))
        await session.commit()

        allocations = await _allocations(session, license_act.id)
        assert [a.prepayment_id for a in allocations] == [license_prepaid.id]
        assert [a.match_basis for a in allocations] == [prepayments.MATCH_PERIOD_PRODUCT]
        await session.refresh(courier_prepaid)
        assert courier_prepaid.status == "open"


async def test_period_edit_reopens_the_settlement(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Правка периода задним числом пересматривает и вступление документа в силу.

    Акт пришёл без периода и сразу погасил аванс. Оператор проставляет период — и оказывается,
    что услуга ещё идёт. Пока пересмотра не было, документ оставался активным по всем витринам,
    а баланс на дату уже считал его недействующим: два источника правды расходились."""
    from app.services import supplier_service_periods as periods

    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Правка периода", inn="1655160013")
        prepaid = await _prepaid(
            session,
            counterparty_id=cp.id,
            amount="16430.00",
            paid_on=date(2026, 7, 8),
            wallet_code="edit-period",
            kind="subscription",
        )
        act = await _closing(
            session,
            counterparty_id=cp.id,
            number="10826-8581-лк-правка",
            amount="16430.00",
            invoice_date=date(2026, 8, 1),
        )
        await session.commit()

        await prepayments.apply_closing_document(session, act, as_of=date(2026, 8, 4))
        await session.commit()
        assert act.activation_status == "active"
        await session.refresh(prepaid)
        assert prepaid.status == "settled"

        # Оператор уточняет: услуга за август, а он ещё идёт.
        await periods.set_invoice_service_period(
            session,
            invoice=act,
            start=date(2026, 8, 1),
            end=date(2026, 8, 31),
            actor_user_id=None,
        )
        await session.refresh(act)
        await session.refresh(prepaid)

        assert act.activation_status == "pending"
        assert prepaid.status == "open"
        assert await _allocations(session, act.id) == []


async def test_settlement_and_recognition_happen_in_one_run(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Гашение аванса и признание расхода происходят одним прогоном — дыры в ночь нет.

    Пока документ вступал в силу 31-го, а расход признавался 1-го, между ними стояла ночь: в
    остатках аванс уже списан, а расхода ещё нет — на сумму документа не сходилось ничего, если
    смотреть вечером последнего дня месяца. Теперь у обоих событий одно и то же условие
    «период закончился», и промежуточного состояния не существует.
    """
    from app.services import supplier_service_periods as periods

    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Один прогон", inn="1655160014")
        bill = await _bill(
            session,
            counterparty_id=cp.id,
            number="СЧЁТ-ОДИН-ПРОГОН",
            amount="16430.00",
            invoice_date=date(2026, 7, 4),
            period=(date(2026, 8, 1), date(2026, 8, 31)),
        )
        prepaid = await _prepaid(
            session,
            counterparty_id=cp.id,
            amount="16430.00",
            paid_on=date(2026, 7, 8),
            wallet_code="one-run",
            bill=bill,
            period=(date(2026, 8, 1), date(2026, 8, 31)),
        )
        act = await _closing(
            session,
            counterparty_id=cp.id,
            number="АКТ-ОДИН-ПРОГОН",
            amount="16430.00",
            invoice_date=date(2026, 8, 1),
            basis_number="СЧЁТ-ОДИН-ПРОГОН",
            period=(date(2026, 8, 1), date(2026, 8, 31)),
        )
        await session.commit()
        # Тот же порядок, что у почтового приёма: провести документ, затем завести начисление.
        await prepayments.apply_closing_document(session, act, as_of=date(2026, 8, 4))
        await periods.sync_invoice_accrual(session, act)
        await session.commit()

        accrual = await session.scalar(
            select(SupplierExpenseAccrual).where(SupplierExpenseAccrual.invoice_id == act.id)
        )
        assert accrual is not None

        # Вечер 31 августа: аванс на месте, расход не признан. Обе стороны говорят одно.
        assert (await prepayments.activate_due_closing_invoices(session, as_of=date(2026, 8, 31)))[
            "activated"
        ] == 0
        assert (
            await periods.recognize_due_expenses(
                session, as_of=date(2026, 8, 31), invoice_ids=[act.id]
            )
            == 0
        )
        await session.commit()
        await session.refresh(prepaid)
        await session.refresh(accrual)
        assert prepaid.status == "open"
        assert accrual.status == "scheduled"

        # 1 сентября: тот же день гасит аванс и признаёт расход августа.
        assert (await prepayments.activate_due_closing_invoices(session, as_of=date(2026, 9, 1)))[
            "activated"
        ] == 1
        assert (
            await periods.recognize_due_expenses(
                session, as_of=date(2026, 9, 1), invoice_ids=[act.id]
            )
            == 1
        )
        await session.commit()
        await session.refresh(prepaid)
        await session.refresh(accrual)
        assert prepaid.status == "settled"
        assert accrual.status == "recognized"
        assert accrual.recognition_month == date(2026, 8, 1)


async def test_landlord_rent_does_not_eat_the_water_receivable(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Кейс арендодателя (прод, 03.08.2026): аренда и вода одного месяца не гасятся крест-накрест.

    У Станислава Юрьевича каждый месяц приходит ПАРА: акт аренды и пара «счёт + акт» на
    возмещение воды, у обоих период одного месяца. Пару по воде порождает сама система, строки
    «Основание Счет №» в ней нет — поэтому ранг счёта-основания молчал. Аренда обрабатывалась
    первой (по дате документа она равна, дальше по номеру «А» < «В») и забирала водяную
    дебиторку рангом «период»; воде доставались арендные деньги. Нетто сходилось, адресность
    врала, и в августе 9 654,25 ₽ повисали фантомной кредиторкой перед арендодателем.
    """
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Арендодатель", inn="7712345678")
        july = (date(2026, 7, 1), date(2026, 7, 31))

        water_bill = await _bill(
            session,
            counterparty_id=cp.id,
            number="Возмещение: вода, 07.2026",
            amount="9654.25",
            invoice_date=date(2026, 7, 31),
            period=july,
        )
        # Арендный аванс: свободные деньги, ни к какому счёту не привязаны.
        rent_money = await _prepaid(
            session,
            counterparty_id=cp.id,
            amount="50000.00",
            paid_on=date(2026, 7, 1),
            wallet_code="landlord-rent",
            kind="subscription",
        )
        # Водяные деньги адресны: заплачено ИМЕННО по счёту за воду.
        water_money = await _prepaid(
            session,
            counterparty_id=cp.id,
            amount="9654.25",
            paid_on=date(2026, 8, 4),
            wallet_code="landlord-water",
            bill=water_bill,
            period=july,
        )

        rent_act = await _closing(
            session,
            counterparty_id=cp.id,
            number="Аренда 07.2026",
            amount="50000.00",
            invoice_date=date(2026, 7, 31),
            period=july,
        )
        water_act = await _closing(
            session,
            counterparty_id=cp.id,
            number="Возмещение: вода, 07.2026",
            amount="9654.25",
            invoice_date=date(2026, 7, 31),
            period=july,
        )
        await session.commit()

        # Порядок как у пересборки зачётов: аренда идёт первой и раньше съедала чужое.
        assert await prepayments.auto_settle_invoice_from_open_prepayments(
            session, rent_act
        ) == Decimal("50000.00")
        assert await prepayments.auto_settle_invoice_from_open_prepayments(
            session, water_act
        ) == Decimal("9654.25")
        await session.commit()

        rent_alloc = await _allocations(session, rent_act.id)
        assert [a.prepayment_id for a in rent_alloc] == [rent_money.id], (
            "аренда снова закрылась водяными деньгами"
        )
        water_alloc = await _allocations(session, water_act.id)
        assert [a.prepayment_id for a in water_alloc] == [water_money.id]
        assert [a.match_basis for a in water_alloc] == [prepayments.MATCH_BASIS_INVOICE]

        await session.refresh(rent_money)
        assert rent_money.amount - rent_money.amount_settled == Decimal("0.00")


async def test_system_generated_pair_matches_by_identical_number(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Пара «счёт + акт», порождённая системой, связывается номером: текста «Основание» нет."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="ЭкоЦентр-пара", inn="7712345679")
        august = (date(2026, 8, 1), date(2026, 8, 31))
        bill = await _bill(
            session,
            counterparty_id=cp.id,
            number="ВД-54475",
            amount="4185.28",
            invoice_date=date(2026, 8, 31),
        )
        own = await _prepaid(
            session,
            counterparty_id=cp.id,
            amount="4185.28",
            paid_on=date(2026, 8, 5),
            wallet_code="eco-own",
            bill=bill,
        )
        # Посторонний аванс с совпавшим периодом: раньше он выигрывал у безымянного акта.
        await _prepaid(
            session,
            counterparty_id=cp.id,
            amount="4185.28",
            paid_on=date(2026, 8, 1),
            wallet_code="eco-other",
            kind="subscription",
            period=august,
        )
        act = await _closing(
            session,
            counterparty_id=cp.id,
            number="ВД-54475",
            amount="4185.28",
            invoice_date=date(2026, 8, 31),
            period=august,
        )
        await session.commit()

        await prepayments.auto_settle_invoice_from_open_prepayments(session, act)
        await session.commit()

        alloc = await _allocations(session, act.id)
        assert [a.prepayment_id for a in alloc] == [own.id]
        assert [a.match_basis for a in alloc] == [prepayments.MATCH_BASIS_INVOICE]


async def test_foreign_bill_money_is_last_resort_but_still_usable(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Адресные деньги чужого счёта — это ПОРЯДОК, а не запрет.

    Если свободных денег нет вовсе, документ по-прежнему гасится адресным авансом чужого
    счёта — иначе он остался бы неоплаченным при живых деньгах. Но признак честно скажет
    «подобрано»: связь документом не подтверждена."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Только чужие деньги", inn="7712345680")
        july = (date(2026, 7, 1), date(2026, 7, 31))
        other_bill = await _bill(
            session,
            counterparty_id=cp.id,
            number="СЧЁТ-ЧУЖОЙ",
            amount="5000.00",
            invoice_date=date(2026, 7, 10),
            period=july,
        )
        foreign = await _prepaid(
            session,
            counterparty_id=cp.id,
            amount="5000.00",
            paid_on=date(2026, 7, 10),
            wallet_code="foreign-only",
            bill=other_bill,
            period=july,
        )
        act = await _closing(
            session,
            counterparty_id=cp.id,
            number="АКТ-БЕЗ-СВОИХ-ДЕНЕГ",
            amount="5000.00",
            invoice_date=date(2026, 7, 31),
            period=july,
        )
        await session.commit()

        settled = await prepayments.auto_settle_invoice_from_open_prepayments(session, act)
        await session.commit()

        assert settled == Decimal("5000.00"), "документ остался неоплаченным при живых деньгах"
        alloc = await _allocations(session, act.id)
        assert [a.prepayment_id for a in alloc] == [foreign.id]
        assert [a.match_basis for a in alloc] == [prepayments.MATCH_CHRONOLOGY], (
            "чужие адресные деньги выданы за подтверждённую связь"
        )


async def test_parsed_act_number_does_not_invent_a_basis(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Фолбэк по номеру не выдумывает основание: у разобранного акта номер свой, не счёта."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Обычный поставщик", inn="7712345681")
        bill = await _bill(
            session,
            counterparty_id=cp.id,
            number="СЧЁТ-100",
            amount="3000.00",
            invoice_date=date(2026, 7, 5),
        )
        act = await _closing(
            session,
            counterparty_id=cp.id,
            number="АКТ-777",
            amount="3000.00",
            invoice_date=date(2026, 7, 31),
        )
        await session.commit()

        assert await prepayments._basis_bill_id(session, act) is None
        # А документ с номером счёта — находит его.
        twin = await _closing(
            session,
            counterparty_id=cp.id,
            number="СЧЁТ-100",
            amount="3000.00",
            invoice_date=date(2026, 7, 31),
        )
        await session.commit()
        assert await prepayments._basis_bill_id(session, twin) == bill.id


async def test_own_bill_money_keeps_amount_rank_when_document_has_no_period(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """У документа БЕЗ периода равенство суммы остаётся единственным различителем.

    Граница гарда «чужие адресные деньги» проходит здесь, и её легко поставить неверно.
    Безусловный гард (без ``same_period``) отменял ранг ``amount`` у аванса СВОЕГО счёта:
    у акта без периода ``same_period`` ложен всегда, все кандидаты сваливались в
    ``chronology``, и победителя выбирала дата денег — тот же перекрёст АЙКО, только в ветке
    «периода нет». Тест держит границу сверху; ``test_two_product_lines_do_not_cross_settle``
    держит её снизу.
    """
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Акт без периода", inn="7712345682")
        own_bill = await _bill(
            session,
            counterparty_id=cp.id,
            number="СЧЁТ-СВОЙ",
            amount="4260.00",
            invoice_date=date(2026, 7, 4),
        )
        other_bill = await _bill(
            session,
            counterparty_id=cp.id,
            number="СЧЁТ-ЧУЖОЙ-БОЛЬШОЙ",
            amount="16430.00",
            invoice_date=date(2026, 6, 1),
        )
        # Чужие деньги ушли РАНЬШЕ: по хронологии они первые и без ранга суммы победили бы.
        await _prepaid(
            session,
            counterparty_id=cp.id,
            amount="16430.00",
            paid_on=date(2026, 6, 2),
            wallet_code="noperiod-foreign",
            bill=other_bill,
        )
        own = await _prepaid(
            session,
            counterparty_id=cp.id,
            amount="4260.00",
            paid_on=date(2026, 7, 5),
            wallet_code="noperiod-own",
            bill=own_bill,
        )
        act = await _closing(
            session,
            counterparty_id=cp.id,
            number="АКТ-БЕЗ-ПЕРИОДА",
            amount="4260.00",
            invoice_date=date(2026, 7, 31),
        )
        await session.commit()

        await prepayments.auto_settle_invoice_from_open_prepayments(session, act)
        await session.commit()

        alloc = await _allocations(session, act.id)
        assert [a.prepayment_id for a in alloc] == [own.id], "акт закрылся чужими деньгами"
        assert [a.match_basis for a in alloc] == [prepayments.MATCH_AMOUNT]
