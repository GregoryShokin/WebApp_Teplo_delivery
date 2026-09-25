"""Замок закрытого месяца на пересборке правила 1 и на соседних дверях расчётов.

Находка 25.09.2026 на копии прода с закрытым августом: прямой прогон
``ensure_prepayment_from_bank_transaction`` по всем расходным проводкам с 01.07 завёл семь новых
авансов — дебиторка на 31.08 выросла на 101 866 ₽, и замок этого не заметил. Пять из семи —
артефакт прогона (выплаты из Сейфа и дивиденды, которые двери пропускают через гейт «свободных
денег»), но два — настоящие: банковские платежи 13 и 15 июля, разобранные ДО канона 18.07,
когда правило 1 ещё гейтилось флагом. Любая пересборка такой проводки — дебиторка задним числом
в закрытом месяце. Двери замок держали, а сама пересборка — нет; и одна дверь
(``PATCH /dds/transactions/{id}``) не держала его вовсе.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from cp_helpers import make_counterparty, make_invoice, make_wallet
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    AccountingPeriodClose,
    CashflowTransaction,
    DdsArticle,
    InvoicePaymentAllocation,
    PnlArticleRule,
    SupplierInvoice,
    SupplierPrepayment,
)
from app.services import accounting_periods
from app.services.counterparty_balance_as_of import build_balance_as_of
from app.services.pnl.projector import build_report
from app.services.supplier_prepayments import (
    _closing_period_open,
    _prepayment_waiting_months,
    activate_due_closing_invoices,
    assert_closing_months_open,
    assert_prepayment_months_open,
    ensure_prepayment_from_bank_transaction,
)

AUGUST = date(2026, 8, 1)
HEADERS = {"X-User-Role": "finance_manager"}


def _payment(
    wallet_id: uuid.UUID,
    counterparty_id: uuid.UUID,
    *,
    amount: str,
    day: date,
    article_id: uuid.UUID | None = None,
) -> CashflowTransaction:
    return CashflowTransaction(
        wallet_id=wallet_id,
        direction="out",
        amount=Decimal(amount),
        operation_date=day,
        counterparty_id=counterparty_id,
        article_id=article_id,
        source_kind="bank_operation",
        payment_purpose="Оплата поставщику",
        quality_status="owner_review",
    )


async def _close_august(session: AsyncSession) -> None:
    session.add(AccountingPeriodClose(period_month=AUGUST))
    await session.commit()


async def _prepayments_of(session: AsyncSession, transaction_id: uuid.UUID):
    return list(
        (
            await session.scalars(
                select(SupplierPrepayment).where(
                    SupplierPrepayment.cashflow_transaction_id == transaction_id
                )
            )
        ).all()
    )


async def test_rebuilding_a_counted_payment_cannot_open_receivable_in_a_closed_month(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Платёж, учтённый без правила 1 (как июльские 550 и 1 316 ₽), в закрытом месяце не
    пересобирается: ни новой дебиторки, ни гашения КЗ. Своё правило 1 откатывает само, точкой
    сохранения, — сессия остаётся рабочей."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Правило1-замок-А", inn="6155090101")
        wallet = await make_wallet(session, name="Р/с замок А", wallet_type="bank")
        act = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="300.00",
            invoice_date=date(2026, 8, 10),
            operational_scope="finance",
        )
        txn = _payment(wallet.id, cp.id, amount="50000.00", day=date(2026, 8, 19))
        session.add(txn)
        # Учтён до закрытия: сверенный август этих денег как дебиторку не видел.
        await session.commit()
        txn_id, act_id = txn.id, act.id
        await _close_august(session)

        with pytest.raises(accounting_periods.PeriodClosed, match="08.2026 закрыт"):
            await ensure_prepayment_from_bank_transaction(session, txn)

        # Точка сохранения откатилась сама: вызывающий мог и не откатывать сессию.
        assert await _prepayments_of(session, txn_id) == []
        assert (
            await session.scalar(
                select(InvoicePaymentAllocation.id).where(
                    InvoicePaymentAllocation.cashflow_transaction_id == txn_id
                )
            )
        ) is None
        await session.commit()

    async with async_session_factory() as session:
        assert await _prepayments_of(session, txn_id) == []
        refreshed = await session.get(SupplierInvoice, act_id)
        assert refreshed.payment_status == "unpaid", "КЗ закрытого месяца погашена задним числом"


async def test_rerun_that_changes_nothing_passes_the_lock(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Замок на изменение, а не на вызов: повторный прогон по уже разложенному платежу безвреден."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Правило1-замок-Б", inn="6155090102")
        wallet = await make_wallet(session, name="Р/с замок Б", wallet_type="bank")
        txn = _payment(wallet.id, cp.id, amount="2830.00", day=date(2026, 8, 3))
        session.add(txn)
        await session.flush()
        first = await ensure_prepayment_from_bank_transaction(session, txn)
        assert first is not None
        await session.commit()
        await _close_august(session)

        again = await ensure_prepayment_from_bank_transaction(session, txn)

        assert again is not None and again.id == first.id
        assert again.amount == Decimal("2830.00")


async def test_new_statement_money_in_a_closed_month_still_becomes_receivable(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Выписка вправе приезжать задним числом: проводка, заведённая в этой же транзакции, —
    новые деньги, и правило 1 проводит их как обычно."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Правило1-замок-В", inn="6155090103")
        wallet = await make_wallet(session, name="Р/с замок В", wallet_type="bank")
        await session.commit()
        await _close_august(session)

        txn = _payment(wallet.id, cp.id, amount="717.00", day=date(2026, 8, 25))
        session.add(txn)
        await session.flush()
        prepayment = await ensure_prepayment_from_bank_transaction(session, txn)

        assert prepayment is not None
        assert prepayment.amount == Decimal("717.00")


async def test_legacy_allocation_rebuilt_with_the_same_money_passes_the_lock(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Старая аллокация без метки ``origin`` пересобирается той же суммой с меткой «rule1».

    Цифры месяца те же — отказывать не в чем. Скептик 25.09.2026 поймал ложный отказ ровно
    здесь: на копии прода так лежат две проводки на 88 439 ₽, и ремонтный скрипт упал бы на них
    с неправдой «пересобрать дебиторку в закрытом месяце нельзя»."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Правило1-метка", inn="6155090109")
        wallet = await make_wallet(session, name="Р/с метка", wallet_type="bank")
        act = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="300.00",
            invoice_date=date(2026, 8, 10),
            operational_scope="finance",
            payment_status="paid",
        )
        txn = _payment(wallet.id, cp.id, amount="300.00", day=date(2026, 8, 12))
        session.add(txn)
        await session.flush()
        session.add(
            InvoicePaymentAllocation(
                invoice_id=act.id,
                source_kind="cash",
                cashflow_transaction_id=txn.id,
                amount=Decimal("300.00"),
                origin=None,
            )
        )
        await session.commit()
        await _close_august(session)

        assert await ensure_prepayment_from_bank_transaction(session, txn) is None
        await session.commit()

        allocations = list(
            (
                await session.scalars(
                    select(InvoicePaymentAllocation).where(
                        InvoicePaymentAllocation.cashflow_transaction_id == txn.id
                    )
                )
            ).all()
        )
        assert [(a.invoice_id, a.amount) for a in allocations] == [(act.id, Decimal("300.00"))]


async def test_money_month_decides_not_the_advance_period(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Сентябрьские деньги за августовскую услугу пересобираются при закрытом августе.

    Опечатаны суммы и баланс месяца денег. Период аванса двигает только пометку «ждём
    документ» — её гасит и пришедший позже документ, и новая выписка заводит такую же без
    отказа; запрещать её одной пересборке значило бы держать учтённые деньги строже новых."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Правило1-период", inn="6155090110")
        wallet = await make_wallet(session, name="Р/с период", wallet_type="bank")
        session.add(
            SupplierInvoice(
                counterparty_id=cp.id,
                source="email",
                direction="payable",
                doc_kind="bill",
                operational_scope="finance",
                number="СЧ-08",
                invoice_date=date(2026, 8, 20),
                amount=Decimal("4200.00"),
                payment_status="unpaid",
                service_period_start=date(2026, 8, 1),
                service_period_end=date(2026, 8, 31),
                service_period_status="ready",
            )
        )
        txn = _payment(wallet.id, cp.id, amount="4200.00", day=date(2026, 9, 5))
        session.add(txn)
        await session.commit()
        await _close_august(session)

        # Платёж оплачивает счёт, и ДЗ по нему заводит чокпоинт — с августовским периодом.
        await ensure_prepayment_from_bank_transaction(session, txn)

        receivable = (
            await session.scalars(
                select(SupplierPrepayment).where(SupplierPrepayment.counterparty_id == cp.id)
            )
        ).all()
        assert [(p.service_period_start, p.amount) for p in receivable] == [
            (date(2026, 8, 1), Decimal("4200.00"))
        ]


async def test_open_month_payment_is_rebuilt_while_an_earlier_month_is_closed(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Закрытый август не мешает пересборке сентябрьского платежа."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Правило1-замок-Г", inn="6155090104")
        wallet = await make_wallet(session, name="Р/с замок Г", wallet_type="bank")
        txn = _payment(wallet.id, cp.id, amount="1316.00", day=date(2026, 9, 15))
        session.add(txn)
        await session.commit()
        await _close_august(session)

        prepayment = await ensure_prepayment_from_bank_transaction(session, txn)

        assert prepayment is not None
        assert prepayment.amount == Decimal("1316.00")


def test_reclassifying_a_counted_payment_of_a_closed_month_via_patch_is_refused(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """``PATCH /dds/transactions/{id}`` — дверь без замка: смена контрагента пересобирала
    дебиторку, смена статьи переносила сумму между строками закрытого ОПиУ."""

    async def seed() -> dict[str, uuid.UUID]:
        async with async_session_factory() as session:
            first = await make_counterparty(session, name="Правило1-PATCH-1", inn="6155090105")
            second = await make_counterparty(session, name="Правило1-PATCH-2", inn="6155090106")
            wallet = await make_wallet(session, name="Р/с PATCH", wallet_type="bank")
            article = DdsArticle(
                code="rule1_patch_lock",
                name="Прочие услуги",
                movement_type="outflow",
                activity_type="operating",
            )
            session.add(article)
            await session.flush()
            txn = _payment(
                wallet.id, first.id, amount="550.00", day=date(2026, 8, 13), article_id=article.id
            )
            session.add(txn)
            await session.flush()
            await ensure_prepayment_from_bank_transaction(session, txn)
            await session.commit()
            await _close_august(session)
            return {"txn": txn.id, "first": first.id, "second": second.id, "article": article.id}

    ids = asyncio.run(seed())

    moved = client.patch(
        f"/api/v1/dds/transactions/{ids['txn']}",
        json={"article_id": str(ids["article"]), "counterparty_id": str(ids["second"])},
        headers=HEADERS,
    )
    assert moved.status_code == 409, moved.text
    assert "08.2026 закрыт" in moved.json()["detail"]

    dropped = client.patch(
        f"/api/v1/dds/transactions/{ids['txn']}",
        json={"article_id": None, "counterparty_id": str(ids["first"])},
        headers=HEADERS,
    )
    assert dropped.status_code == 409, dropped.text

    async def check() -> None:
        async with async_session_factory() as session:
            txn = await session.get(CashflowTransaction, ids["txn"])
            assert txn.counterparty_id == ids["first"]
            assert txn.article_id == ids["article"]
            prepayments = await _prepayments_of(session, ids["txn"])
            assert [(p.counterparty_id, p.amount) for p in prepayments] == [
                (ids["first"], Decimal("550.00"))
            ]

    asyncio.run(check())


async def test_waiting_months_follow_the_pnl_expense_kinds(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Замок снятия аванса меряет тем же, что и слой ожидания ОПиУ, — и вид тоже.

    Аванс «прочее» из закрытого августа ОПиУ ни к какому месяцу не относит: документ по нему
    не ждут. Замок, который такой аванс всё же держал, был строже отчёта."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Правило1-вид", inn="6155090107")
        wallet = await make_wallet(session, name="Р/с вид", wallet_type="bank")
        txn = _payment(wallet.id, cp.id, amount="4351.00", day=date(2026, 8, 5))
        session.add(txn)
        await session.flush()
        other = SupplierPrepayment(
            counterparty_id=cp.id,
            kind="other",
            amount=Decimal("4351.00"),
            amount_settled=Decimal("0.00"),
            status="open",
            cashflow_transaction_id=txn.id,
        )
        service = SupplierPrepayment(
            counterparty_id=cp.id,
            kind="subscription",
            amount=Decimal("2830.00"),
            amount_settled=Decimal("0.00"),
            status="open",
            cashflow_transaction_id=txn.id,
        )
        session.add_all([other, service])
        await session.commit()
        await _close_august(session)

        months = await _prepayment_waiting_months(session, [other, service])
        assert months[other.id] == []
        assert months[service.id] == [AUGUST]

        await assert_prepayment_months_open(session, other, action="снять аванс")
        with pytest.raises(accounting_periods.PeriodClosed):
            await assert_prepayment_months_open(session, service, action="снять аванс")


async def _closed_august_figures(session: AsyncSession, counterparty_id: uuid.UUID):
    """Что сверено в августе: суммы строк ОПиУ, дебиторка контрагента на 31.08 и пометки
    «ждём документ» строки, куда ложится аванс."""
    report = await build_report(session, AUGUST, today=date(2026, 10, 1))
    balance = await build_balance_as_of(session, as_of=date(2026, 8, 31))
    receivable = next(
        (row.receivable for row in balance.rows if row.counterparty_id == counterparty_id),
        Decimal("0.00"),
    )
    waiting = [
        component.unrecognized_paid
        for line in report.lines
        if line.code == "office_maintenance"
        for component in line.components
        if component.component == "waiting"
    ]
    return {line.code: line.amount for line in report.lines}, receivable, waiting


async def test_new_document_consumes_a_closed_month_advance_without_touching_closed_numbers(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Аванс 03.08 без периода гасит акт «Аренда серверов 09.2026» при активации 01.10.

    Замок этого не держит — решение 25.09.2026. Зачёт — событие открытого месяца: баланс
    датирует его вступлением акта (сентябрь), расход акт признаёт в своём периоде, и в
    закрытом августе не меняется ни сумма строки ОПиУ, ни дебиторка на 31.08. Гаснет только
    пометка «документ просрочен» на августовской строке: документ пришёл, ждать больше нечего,
    а суммы у пометки нет (``projector._apply_waiting``). Запрет же оставил бы акт кредиторкой
    рядом с открытой дебиторкой тех же денег — двойной долг."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Правило1-IHC", inn="6155090108")
        wallet = await make_wallet(session, name="Р/с IHC", wallet_type="bank")
        article = DdsArticle(
            code="rule1_ihc_servers",
            name="Аренда серверов",
            movement_type="outflow",
            activity_type="operating",
        )
        session.add(article)
        await session.flush()
        session.add(
            PnlArticleRule(
                article_id=article.id,
                line_code="office_maintenance",
                in_pnl=True,
                owner_stream="cash",
                sign=1,
                applies_to="both",
                is_active=True,
            )
        )
        txn = _payment(
            wallet.id, cp.id, amount="2830.00", day=date(2026, 8, 3), article_id=article.id
        )
        session.add(txn)
        await session.flush()
        advance = await ensure_prepayment_from_bank_transaction(session, txn)
        assert advance is not None and advance.service_period_start is None
        act = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="2830.00",
            number="IHC-0925",
            invoice_date=date(2026, 9, 30),
            operational_scope="finance",
            activation_status="pending",
        )
        act.service_period_start = date(2026, 9, 1)
        act.service_period_end = date(2026, 9, 30)
        act.service_period_status = "ready"
        await session.commit()
        cp_id, advance_id, act_id = cp.id, advance.id, act.id
        await _close_august(session)

        lines_before, receivable_before, waiting_before = await _closed_august_figures(
            session, cp_id
        )
        assert receivable_before == Decimal("2830.00")
        assert waiting_before == [Decimal("2830.00")]

        result = await activate_due_closing_invoices(session, as_of=date(2026, 10, 1))
        assert result["settled_from_prepayments"] == 1

    async with async_session_factory() as session:
        consumed = await session.get(SupplierPrepayment, advance_id)
        assert consumed.amount_settled == Decimal("2830.00")
        assert (await session.get(SupplierInvoice, act_id)).payment_status == "paid"

        lines_after, receivable_after, waiting_after = await _closed_august_figures(session, cp_id)
        assert receivable_after == receivable_before
        assert lines_after == lines_before
        assert waiting_after == []


async def test_closing_without_a_date_is_locked_by_the_day_it_was_recorded(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Документ без даты баланс считает действующим со дня записи — тем же днём его и запираем.

    Раньше замок перегашения такой документ не видел вовсе (``assert_month_open(None)``), и
    снятие зачёта меняло ДЗ и КЗ на конец закрытого месяца."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Правило1-без-даты", inn="6155090111")
        act = SupplierInvoice(
            counterparty_id=cp.id,
            source="email",
            direction="payable",
            doc_kind="closing",
            operational_scope="finance",
            number="без даты",
            invoice_date=None,
            amount=Decimal("1521.00"),
            payment_status="unpaid",
            created_at=datetime(2026, 8, 20, 12, 0, tzinfo=UTC),
        )
        session.add(act)
        await session.commit()
        await _close_august(session)

        with pytest.raises(accounting_periods.PeriodClosed, match="08.2026 закрыт"):
            await assert_closing_months_open(session, act, action="перегасить документ")
        assert await _closing_period_open(session, act) is False
