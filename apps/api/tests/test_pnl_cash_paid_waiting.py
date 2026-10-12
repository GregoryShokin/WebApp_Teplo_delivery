"""ДЗ правила 1 не требует документа по деньгам, уже вошедшим в расход ОПиУ.

Местный закуп 629 + 262 ₽ одновременно входил в сентябрьский расход и краснел как
просроченный документ. Технический ``subscription`` означает автодебиторку, а ожидание
зависит от того же денежного вердикта, которым пользуется сумма отчёта.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

sys.path.append(str(Path(__file__).parent / "counterparties"))

from cp_helpers import make_counterparty, make_wallet

from app.models import (
    CashflowTransaction,
    CounterpartyPayableProfile,
    DdsArticle,
    PnlArticleRule,
    SupplierInvoice,
    SupplierPrepayment,
)
from app.services import clock
from app.services.pnl import drill, ledgers, projector
from app.services.pnl.sources import cashflow as cash_source
from app.services.pnl.sources import waiting as waiting_source
from app.services.pnl.types import Verdict

SEPTEMBER = (date(2026, 9, 1), date(2026, 9, 30))
TODAY = date(2026, 10, 12)


async def _article(
    session, line_code: str = "shop_maintenance", *, in_pnl: bool = True
) -> DdsArticle:
    article = DdsArticle(
        code=f"cash_wait_{uuid.uuid4().hex[:8]}",
        name=f"Статья {line_code}",
        movement_type="outflow",
        activity_type="operating",
    )
    session.add(article)
    await session.flush()
    session.add(
        PnlArticleRule(
            article_id=article.id,
            line_code=line_code if in_pnl else None,
            in_pnl=in_pnl,
            owner_stream="cash",
            sign=1,
            applies_to="both",
            is_active=True,
        )
    )
    await session.flush()
    return article


async def _cash_prepayment(
    session,
    counterparty_id,
    article_id,
    amount: str,
    paid_on: date,
    *,
    kind: str = "subscription",
    period: tuple[date, date] | None = None,
    classified: bool = True,
):
    wallet = await make_wallet(session, code=f"cash-wait-{uuid.uuid4().hex[:8]}")
    transaction = CashflowTransaction(
        wallet_id=wallet.id,
        direction="out",
        amount=Decimal(amount),
        operation_date=paid_on,
        article_id=article_id if classified else None,
        counterparty_id=counterparty_id,
        source_kind="bank_operation",
        payment_purpose="Оплата местной покупки",
        quality_status="manual_override",
    )
    session.add(transaction)
    await session.flush()
    prepayment = SupplierPrepayment(
        counterparty_id=counterparty_id,
        kind=kind,
        amount=Decimal(amount),
        amount_settled=Decimal("0.00"),
        status="open",
        article_id=article_id,
        cashflow_transaction_id=transaction.id,
        service_period_start=period[0] if period else None,
        service_period_end=period[1] if period else None,
        service_period_status="ready" if period else "missing",
        auto_recognize_monthly=False,
    )
    session.add(prepayment)
    await session.flush()
    return transaction, prepayment


async def _profile(session, counterparty_id) -> CounterpartyPayableProfile:
    return await session.scalar(
        select(CounterpartyPayableProfile).where(
            CounterpartyPayableProfile.counterparty_id == counterparty_id
        )
    )


@pytest.mark.parametrize("name", ["Местный закуп", "Покупки без закрывающих документов"])
def test_cash_expense_has_no_document_waiting_in_all_pnl_views(
    async_session_factory, monkeypatch, name: str
) -> None:
    """629, 262 и 380 ₽ остаются расходом; ни одна витрина не требует по ним документ."""
    monkeypatch.setattr(clock, "moscow_today", lambda: TODAY)

    async def scenario() -> None:
        async with async_session_factory() as session:
            counterparty = await make_counterparty(session, name=name)
            profile = await _profile(session, counterparty.id)
            assert profile.service_billing_mode is None
            assert profile.service_period_required is False
            assert profile.bank_payments_create_prepayment is False
            shop = await _article(session)
            meals = await _article(session, "staff_meals")
            for amount, day, article in (
                ("629.00", 30, shop),
                ("262.00", 17, shop),
                ("380.00", 21, meals),
            ):
                await _cash_prepayment(
                    session, counterparty.id, article.id, amount, date(2026, 9, day)
                )
            await session.commit()

            cash = await cash_source.build_cash_layer(session, *SEPTEMBER)
            assert cash.buckets["shop_maintenance"].amount == Decimal("891.00")
            assert cash.buckets["staff_meals"].amount == Decimal("380.00")
            waiting = await waiting_source.build_waiting_layer(session, *SEPTEMBER, today=TODAY)
            assert waiting.items == []
            assert waiting.total == Decimal("0.00")

            report = await projector.build_report(session, SEPTEMBER[0], today=TODAY)
            expected = {"shop_maintenance": Decimal("891.00"), "staff_meals": Decimal("380.00")}
            for line in report.lines:
                if line.code not in expected:
                    continue
                [cash_component] = [c for c in line.components if c.stream == "cashflow"]
                assert cash_component.amount == expected[line.code]
                assert not [c for c in line.components if c.component == "waiting"]
                assert sum(c.unrecognized_paid for c in line.components) == Decimal("0.00")
                detail = await drill.build_drill(session, line.code, SEPTEMBER[0])
                assert detail is not None
                assert detail.total == expected[line.code]
                assert not [group for group in detail.groups if group.stream == "waiting"]
            assert not [
                warning
                for warning in report.warnings
                if warning.code in {"overdue_document", "stalled_document"}
                and warning.line_code in expected
            ]
            ledger = await ledgers.build_recognition_ledger(session, SEPTEMBER[0])
            assert not [row for row in ledger.rows if row.status == "waiting_document"]
            assert ledger.totals.waiting_document == Decimal("0.00")
            assert ledger.totals.waiting_overdue == Decimal("0.00")

    asyncio.run(scenario())


@pytest.mark.parametrize("service_signal", ["billing_mode", "required_period"])
def test_actual_document_service_subscription_still_waits(
    async_session_factory, service_signal: str
) -> None:
    """Незакрытый сервисный аванс остаётся просрочкой, поскольку касса не признала расход."""

    async def scenario() -> None:
        async with async_session_factory() as session:
            counterparty = await make_counterparty(session, name="Услуга с документами")
            profile = await _profile(session, counterparty.id)
            if service_signal == "billing_mode":
                profile.service_billing_mode = "per_invoice"
            else:
                profile.service_period_required = True
            article = await _article(session)
            transaction, prepayment = await _cash_prepayment(
                session, counterparty.id, article.id, "262.00", date(2026, 9, 17)
            )
            await session.commit()
            context = await cash_source.build_context(session, *SEPTEMBER)
            verdict, _line = cash_source.classify_transaction(
                transaction, context, month_start=SEPTEMBER[0], month_end=SEPTEMBER[1]
            )
            assert verdict is Verdict.EXCLUDED_ACCRUAL_COUNTERPARTY
            [item] = (
                await waiting_source.build_waiting_layer(session, *SEPTEMBER, today=TODAY)
            ).items
            assert item.prepayment_id == prepayment.id
            assert item.amount == Decimal("262.00")
            assert item.state == waiting_source.STATE_OVERDUE
            assert item.overdue_days == 2

    asyncio.run(scenario())


def test_service_period_allocation_and_pending_document_are_preserved(
    async_session_factory,
) -> None:
    """Оплаченный в июле квартал даёт сентябрю свою долю; документ остаётся pending/stalled."""

    async def scenario() -> None:
        async with async_session_factory() as session:
            counterparty = await make_counterparty(session, name="Услуга за квартал")
            profile = await _profile(session, counterparty.id)
            profile.service_billing_mode = "per_invoice"
            article = await _article(session)
            _, prepayment = await _cash_prepayment(
                session,
                counterparty.id,
                article.id,
                "900.00",
                date(2026, 7, 20),
                period=(date(2026, 8, 1), date(2026, 10, 31)),
            )
            session.add(
                SupplierInvoice(
                    counterparty_id=counterparty.id,
                    source="email",
                    doc_kind="closing",
                    activation_status="pending",
                    number="Услуга за сентябрь",
                    amount=Decimal("300.00"),
                    invoice_date=SEPTEMBER[1],
                    dds_article_id=article.id,
                    service_period_start=SEPTEMBER[0],
                    service_period_end=SEPTEMBER[1],
                    service_period_status="ready",
                )
            )
            await session.commit()
            for today, expected_state in (
                (date(2026, 9, 25), waiting_source.STATE_DOCUMENT_PENDING),
                (TODAY, waiting_source.STATE_STALLED),
            ):
                [item] = (
                    await waiting_source.build_waiting_layer(session, *SEPTEMBER, today=today)
                ).items
                assert item.prepayment_id == prepayment.id
                assert item.amount == Decimal("300.00")
                assert item.period_known is True
                assert item.paid_on == date(2026, 7, 20)
                assert item.state == expected_state
                assert item.state_date == date(2026, 10, 1)
                assert item.basis == waiting_source.BASIS_DOCUMENT

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["prepaid_bill", "ad"])
def test_explicit_advance_is_kept_even_without_service_profile(
    async_session_factory, kind: str
) -> None:
    """Счёт/рекламный аванс — явное обязательство, а не техническая ДЗ обычной покупки."""

    async def scenario() -> None:
        async with async_session_factory() as session:
            counterparty = await make_counterparty(session, name="Оплаченный счёт без режима")
            article = await _article(session)
            transaction, prepayment = await _cash_prepayment(
                session,
                counterparty.id,
                article.id,
                "629.00",
                date(2026, 9, 30),
                kind=kind,
                period=SEPTEMBER,
            )
            await session.commit()
            context = await cash_source.build_context(session, *SEPTEMBER)
            verdict, _line = cash_source.classify_transaction(
                transaction, context, month_start=SEPTEMBER[0], month_end=SEPTEMBER[1]
            )
            assert verdict is Verdict.INCLUDED
            [item] = (
                await waiting_source.build_waiting_layer(session, *SEPTEMBER, today=TODAY)
            ).items
            assert item.prepayment_id == prepayment.id
            assert item.amount == Decimal("629.00")
            assert item.period_known is True

    asyncio.run(scenario())


def test_unclassified_payment_is_not_silenced_as_a_cash_expense(async_session_factory) -> None:
    """Отсутствие кассового расхода не позволяет считать технический аванс уже признанным."""

    async def scenario() -> None:
        async with async_session_factory() as session:
            counterparty = await make_counterparty(session, name="Неразобранная покупка")
            article = await _article(session)
            transaction, prepayment = await _cash_prepayment(
                session,
                counterparty.id,
                article.id,
                "262.00",
                date(2026, 9, 17),
                classified=False,
            )
            await session.commit()
            context = await cash_source.build_context(session, *SEPTEMBER)
            verdict, _line = cash_source.classify_transaction(
                transaction, context, month_start=SEPTEMBER[0], month_end=SEPTEMBER[1]
            )
            assert verdict is Verdict.UNMAPPED
            [item] = (
                await waiting_source.build_waiting_layer(session, *SEPTEMBER, today=TODAY)
            ).items
            assert item.prepayment_id == prepayment.id

    asyncio.run(scenario())


def test_standalone_period_advance_is_kept_without_a_cash_transaction(
    async_session_factory,
) -> None:
    """Периодная предоплата без денежной ссылки не теряется при фильтрации кассовых расходов."""

    async def scenario() -> None:
        async with async_session_factory() as session:
            counterparty = await make_counterparty(session, name="Самостоятельный аванс")
            article = await _article(session)
            prepayment = SupplierPrepayment(
                counterparty_id=counterparty.id,
                kind="subscription",
                amount=Decimal("600.00"),
                amount_settled=Decimal("0.00"),
                status="open",
                article_id=article.id,
                service_period_start=date(2026, 9, 1),
                service_period_end=date(2026, 10, 31),
                service_period_status="ready",
            )
            session.add(prepayment)
            await session.commit()
            [item] = (
                await waiting_source.build_waiting_layer(session, *SEPTEMBER, today=TODAY)
            ).items
            assert item.prepayment_id == prepayment.id
            assert item.amount == Decimal("300.00")
            assert item.paid_on is None
            assert item.period_known is True

    asyncio.run(scenario())


@pytest.mark.parametrize("auto_monthly", [False, True])
def test_linked_future_period_is_preserved_before_the_first_service_accrual(
    async_session_factory, auto_monthly: bool
) -> None:
    """Сентябрьская оплата октября–ноября не становится октябрьской кассой из-за NULL mode."""

    async def scenario() -> None:
        async with async_session_factory() as session:
            counterparty = await make_counterparty(session, name="Период без режима карточки")
            profile = await _profile(session, counterparty.id)
            assert profile.service_billing_mode is None
            assert profile.service_period_required is False
            article = await _article(session)
            transaction, prepayment = await _cash_prepayment(
                session,
                counterparty.id,
                article.id,
                "1200.00",
                date(2026, 9, 20),
                period=(date(2026, 10, 1), date(2026, 11, 30)),
            )
            prepayment.auto_recognize_monthly = auto_monthly
            prepayment.service_period_months = 2 if auto_monthly else None
            await session.commit()

            # Классификатор сам не ограничивает дату денег: до первого начисления он
            # отдаёт INCLUDED и октябрю. Но в октябрьском денежном слое этой оплаты нет.
            for month in (
                (date(2026, 10, 1), date(2026, 10, 31)),
                (date(2026, 11, 1), date(2026, 11, 30)),
            ):
                context = await cash_source.build_context(session, *month)
                verdict, _line = cash_source.classify_transaction(
                    transaction, context, month_start=month[0], month_end=month[1]
                )
                assert verdict is Verdict.INCLUDED
                cash = await cash_source.build_cash_layer(session, *month)
                assert "shop_maintenance" not in cash.buckets
                [item] = (
                    await waiting_source.build_waiting_layer(session, *month, today=TODAY)
                ).items
                assert item.prepayment_id == prepayment.id
                assert item.amount == Decimal("600.00")
                assert item.period_known is True
                assert item.paid_on == date(2026, 9, 20)
                assert item.state == waiting_source.STATE_PERIOD_RUNNING

    asyncio.run(scenario())


def test_received_document_is_kept_for_unperioded_subscription_without_service_mode(
    async_session_factory,
) -> None:
    """Полученный pending-документ подтверждает документный поток даже при NULL mode."""

    async def scenario() -> None:
        async with async_session_factory() as session:
            counterparty = await make_counterparty(session, name="Документ без режима карточки")
            article = await _article(session)
            _, prepayment = await _cash_prepayment(
                session, counterparty.id, article.id, "629.00", date(2026, 9, 30)
            )
            session.add(
                SupplierInvoice(
                    counterparty_id=counterparty.id,
                    source="email",
                    doc_kind="closing",
                    activation_status="pending",
                    number="Полученный документ",
                    amount=Decimal("629.00"),
                    invoice_date=SEPTEMBER[1],
                    dds_article_id=article.id,
                    service_period_start=SEPTEMBER[0],
                    service_period_end=SEPTEMBER[1],
                    service_period_status="ready",
                )
            )
            await session.commit()
            [item] = (
                await waiting_source.build_waiting_layer(
                    session, *SEPTEMBER, today=date(2026, 9, 30)
                )
            ).items
            assert item.prepayment_id == prepayment.id
            assert item.amount == Decimal("629.00")
            assert item.state == waiting_source.STATE_DOCUMENT_PENDING
            assert item.state_date == date(2026, 10, 1)
            assert item.basis == waiting_source.BASIS_DOCUMENT

    asyncio.run(scenario())


def test_technical_advance_on_an_article_outside_pnl_does_not_wait_for_an_expense_document(
    async_session_factory,
) -> None:
    """380 ₽ вне ОПиУ не становятся обещанием документного расхода через техническую ДЗ."""

    async def scenario() -> None:
        async with async_session_factory() as session:
            counterparty = await make_counterparty(session, name="Покупка вне ОПиУ")
            article = await _article(session, in_pnl=False)
            transaction, _prepayment = await _cash_prepayment(
                session, counterparty.id, article.id, "380.00", date(2026, 9, 17)
            )
            await session.commit()
            context = await cash_source.build_context(session, *SEPTEMBER)
            verdict, line = cash_source.classify_transaction(
                transaction, context, month_start=SEPTEMBER[0], month_end=SEPTEMBER[1]
            )
            assert verdict is Verdict.EXCLUDED_OUT_OF_PNL
            assert line is None
            waiting = await waiting_source.build_waiting_layer(session, *SEPTEMBER, today=TODAY)
            assert waiting.items == []
            assert waiting.total == Decimal("0.00")

    asyncio.run(scenario())


@pytest.mark.parametrize("mismatch", ["article", "period"])
def test_unrelated_pending_document_does_not_keep_a_technical_cash_purchase_waiting(
    async_session_factory, mismatch: str
) -> None:
    """Документ того же контрагента защищает только свой поток и свой период."""

    async def scenario() -> None:
        async with async_session_factory() as session:
            counterparty = await make_counterparty(session, name="Покупка рядом с чужим документом")
            article = await _article(session)
            other_article = await _article(session, "automation")
            await _cash_prepayment(
                session, counterparty.id, article.id, "629.00", date(2026, 9, 30)
            )
            period = (date(2026, 10, 1), date(2026, 10, 31)) if mismatch == "period" else SEPTEMBER
            session.add(
                SupplierInvoice(
                    counterparty_id=counterparty.id,
                    source="email",
                    doc_kind="closing",
                    activation_status="pending",
                    number="Документ другого потока",
                    amount=Decimal("629.00"),
                    invoice_date=period[1],
                    dds_article_id=other_article.id if mismatch == "article" else article.id,
                    service_period_start=period[0],
                    service_period_end=period[1],
                    service_period_status="ready",
                )
            )
            await session.commit()
            waiting = await waiting_source.build_waiting_layer(session, *SEPTEMBER, today=TODAY)
            assert waiting.items == []
            assert waiting.total == Decimal("0.00")

    asyncio.run(scenario())
