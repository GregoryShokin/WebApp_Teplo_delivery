"""Дебиторка наличной выплаты обязана сниматься теми же дверями, что и заводится.

ПОЧЕМУ ГЕЙТ ОДИН НА ВСЕ ДВЕРИ. Первая версия страховки (06.08.2026) стояла в контуре выплаты
из Сейфа: она заводила дебиторку и на этом заканчивалась. Все остальные двери — исключение
проводки, смена контрагента, разбор по статьям — идут через ``manual_payment_money_is_free``,
а он для наличных механизмов отвечал «деньги заняты» и предоплату не трогал. Долг оставался
жить сам по себе: проверка перед выкаткой намерила 175 224,75 ₽ такого фантома.

Плюс страховка смотрела на контрагента в РЕЗЕРВЕ Сейфа, а его там почти никогда нет — на
данных прода контрагент заполнен у 4 резервов из 28. То есть на собственном сценарии она не
срабатывала вовсе: контрагента наличной выплате проставляют потом, руками, в журнале ДДС.

Теперь исключение живёт в самом гейте, и все двери работают одинаково.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    CashflowTransaction,
    CounterpartyPayableProfile,
    CounterpartyPaymentDraft,
    DdsArticle,
    SafeAllocation,
    SupplierPrepayment,
)
from app.services.supplier_prepayments import (
    manual_payment_money_is_free,
    sync_manual_payment_receivable,
)
from cp_helpers import make_counterparty, make_invoice, make_wallet


async def _service_counterparty(session: AsyncSession, *, name: str, inn: str):
    counterparty = await make_counterparty(session, name=name, inn=inn)
    profile = await session.scalar(
        select(CounterpartyPayableProfile).where(
            CounterpartyPayableProfile.counterparty_id == counterparty.id
        )
    )
    profile.service_billing_mode = "agreement"
    await session.flush()
    return counterparty


async def _article(session: AsyncSession, *, code: str, name: str) -> DdsArticle:
    article = DdsArticle(
        code=code, name=name, movement_type="outflow", activity_type="operating"
    )
    session.add(article)
    await session.flush()
    return article


async def _safe_payout(
    session: AsyncSession, *, wallet_id, article_id, counterparty_id, amount, source_id=None
):
    txn = CashflowTransaction(
        wallet_id=wallet_id,
        direction="out",
        amount=Decimal(amount),
        operation_date=date(2026, 7, 8),
        article_id=article_id,
        counterparty_id=counterparty_id,
        source_kind="safe_payout",
        source_id=source_id,
        payment_purpose="Наличная оплата услуг",
        quality_status="manual_override",
    )
    session.add(txn)
    await session.flush()
    return txn


async def _draft(session: AsyncSession, *, counterparty_id, amount, document_id: str):
    draft = CounterpartyPaymentDraft(
        counterparty_id=counterparty_id,
        document_id=document_id,
        amount=Decimal(amount),
        status="paid",
        pays_via_safe=True,
    )
    session.add(draft)
    await session.flush()
    return draft


async def _reserve(
    session: AsyncSession, *, wallet_id, amount, draft_id=None, lease_id=None, counterparty_id=None
):
    reserve = SafeAllocation(
        wallet_id=wallet_id,
        amount=Decimal(amount),
        amount_paid=Decimal(amount),
        counterparty_id=counterparty_id,
        lease_id=lease_id,
        source_draft_id=draft_id,
        status="paid",
    )
    session.add(reserve)
    await session.flush()
    return reserve


@pytest.mark.asyncio
async def test_receivable_appears_when_counterparty_is_set_later(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Контрагента наличной выплате ставят потом — дебиторка обязана появиться тогда же.

    Это и есть настоящий сценарий: в резерве Сейфа контрагента нет (на проде — у 4 из 28),
    его проставляют руками в журнале ДДС уже после выдачи денег.
    """
    async with async_session_factory() as session:
        counterparty = await _service_counterparty(session, name="Услуги-позже", inn="6155036001")
        article = await _article(session, code="test_doors_services", name="Услуги")
        wallet = await make_wallet(session, code="doors-w1", name="Сейф")
        txn = await _safe_payout(
            session,
            wallet_id=wallet.id,
            article_id=article.id,
            counterparty_id=None,
            amount="9879.00",
        )
        await session.commit()

        # Без контрагента дебиторке взяться неоткуда — и это правильно.
        assert await sync_manual_payment_receivable(session, txn) is None

        txn.counterparty_id = counterparty.id
        await session.flush()
        prepayment = await sync_manual_payment_receivable(session, txn)
        await session.commit()

        assert prepayment is not None, "привязка контрагента не завела дебиторку"
        assert prepayment.amount == Decimal("9879.00")


@pytest.mark.asyncio
async def test_receivable_moves_with_the_counterparty(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Сменили контрагента — долг переезжает, а не остаётся фантомом на прежнем."""
    async with async_session_factory() as session:
        first = await _service_counterparty(session, name="Услуги-первый", inn="6155036002")
        second = await _service_counterparty(session, name="Услуги-второй", inn="6155036003")
        article = await _article(session, code="test_doors_move", name="Услуги-переезд")
        wallet = await make_wallet(session, code="doors-w2", name="Сейф")
        txn = await _safe_payout(
            session,
            wallet_id=wallet.id,
            article_id=article.id,
            counterparty_id=first.id,
            amount="5000.00",
        )
        await sync_manual_payment_receivable(session, txn)
        await session.commit()

        txn.counterparty_id = second.id
        await session.flush()
        await sync_manual_payment_receivable(session, txn)
        await session.commit()

        rows = (
            await session.scalars(
                select(SupplierPrepayment).where(
                    SupplierPrepayment.cashflow_transaction_id == txn.id
                )
            )
        ).all()
        assert len(rows) == 1, "на одной проводке оказалось два долга"
        assert rows[0].counterparty_id == second.id, "долг остался на прежнем контрагенте"


@pytest.mark.asyncio
async def test_advance_to_supplier_article_keeps_its_own_prepayment(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Статья «Авансы поставщикам» заводит свою целевую предоплату — второй быть не должно."""
    async with async_session_factory() as session:
        counterparty = await _service_counterparty(session, name="Аванс-услуги", inn="6155036004")
        # Статья сидируется миграцией — берём существующую, а не заводим дубль.
        article_id = await session.scalar(
            select(DdsArticle.id).where(DdsArticle.code == "advance_to_supplier")
        )
        assert article_id is not None, "статья «Авансы поставщикам» обязана быть в справочнике"
        article = await session.get(DdsArticle, article_id)
        wallet = await make_wallet(session, code="doors-w3", name="Сейф")
        txn = await _safe_payout(
            session,
            wallet_id=wallet.id,
            article_id=article.id,
            counterparty_id=counterparty.id,
            amount="10000.00",
        )
        await session.commit()

        assert await manual_payment_money_is_free(session, txn) is False
        assert await sync_manual_payment_receivable(session, txn) is None


@pytest.mark.asyncio
async def test_payment_window_reserve_still_leaves_a_receivable(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Выплата из окна «Новый платёж» — обычный расход, накладных нет, дебиторка нужна.

    Прежний гейт закрывал дверь любому резерву, у которого есть черновик, а черновик у
    «Нового платежа» есть всегда. Проверка перед выкаткой намерила 12 таких выплат на
    312 135 ₽: дебиторка не терялась только потому, что все они были либо арендой, либо
    контрагентами без режима услуг. Первый же услуговый неарендный платёж уходил в никуда.
    """
    async with async_session_factory() as session:
        counterparty = await _service_counterparty(session, name="Услуги-окно", inn="6155036005")
        article = await _article(session, code="test_doors_window", name="Услуги-окно")
        wallet = await make_wallet(session, code="doors-w4", name="Сейф")
        draft = await _draft(
            session, counterparty_id=counterparty.id, amount="9879.00", document_id="TPL-DOORS-1"
        )
        reserve = await _reserve(
            session, wallet_id=wallet.id, amount="9879.00", draft_id=draft.id
        )
        txn = await _safe_payout(
            session,
            wallet_id=wallet.id,
            article_id=article.id,
            counterparty_id=counterparty.id,
            amount="9879.00",
            source_id=reserve.id,
        )
        await session.commit()

        prepayment = await sync_manual_payment_receivable(session, txn)
        await session.commit()

        assert prepayment is not None, "выплата из «Нового платежа» осталась без дебиторки"
        assert prepayment.amount == Decimal("9879.00")


@pytest.mark.asyncio
async def test_informal_purchase_reserve_keeps_its_addressed_money(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Закуп у неофициала: черновик выписан под накладные — деньги адресны, дебиторки нет."""
    async with async_session_factory() as session:
        counterparty = await _service_counterparty(session, name="Услуги-закуп", inn="6155036006")
        article = await _article(session, code="test_doors_purchase", name="Услуги-закуп")
        wallet = await make_wallet(session, code="doors-w5", name="Сейф")
        draft = await _draft(
            session, counterparty_id=counterparty.id, amount="7000.00", document_id="TPL-DOORS-2"
        )
        invoice = await make_invoice(
            session,
            counterparty_id=counterparty.id,
            amount="7000.00",
            number="УПД-ЗАКУП-1",
            doc_kind="closing",
            invoice_date=date(2026, 7, 8),
            operational_scope="finance",
        )
        invoice.draft_id = draft.id
        reserve = await _reserve(
            session, wallet_id=wallet.id, amount="7000.00", draft_id=draft.id
        )
        txn = await _safe_payout(
            session,
            wallet_id=wallet.id,
            article_id=article.id,
            counterparty_id=counterparty.id,
            amount="7000.00",
            source_id=reserve.id,
        )
        await session.commit()

        assert await manual_payment_money_is_free(session, txn) is False
        assert await sync_manual_payment_receivable(session, txn) is None


@pytest.mark.asyncio
async def test_lease_reserve_is_left_to_the_lease_circuit(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Арендный резерв ведёт свой контур: правило 1 не вправе пересобирать его зачёты.

    Вид записи у дебиторки аренды тот же, что у правила 1 (``lease_accruals`` заводит её
    как ``RULE1_PREPAYMENT_KIND``), поэтому дальше по гейту она выглядит «своей» — и
    открытая здесь дверь дала бы FIFO право перевесить арендные деньги на посторонний
    документ. На стенде под этим ходят 100 000 ₽ двух арендодателей.
    """
    from app.models import Location, LocationLease, Organization

    async with async_session_factory() as session:
        counterparty = await _service_counterparty(session, name="Арендодатель", inn="6155036007")
        article = await _article(session, code="test_doors_lease", name="Аренда")
        wallet = await make_wallet(session, code="doors-w6", name="Сейф")
        organization_id = await session.scalar(select(Organization.id).limit(1))
        if organization_id is None:
            organization = Organization(name="Тест-организация-двери")
            session.add(organization)
            await session.flush()
            organization_id = organization.id
        location = Location(organization_id=organization_id, name="Точка-двери")
        session.add(location)
        await session.flush()
        lease = LocationLease(
            location_id=location.id,
            counterparty_id=counterparty.id,
            monthly_amount=Decimal("50000.00"),
            payment_mode="prepaid",
            payment_day=28,
            started_on=date(2026, 7, 1),
            documents_mode="informal",
            dds_article_id=article.id,
        )
        session.add(lease)
        await session.flush()
        reserve = await _reserve(
            session, wallet_id=wallet.id, amount="50000.00", lease_id=lease.id
        )
        txn = await _safe_payout(
            session,
            wallet_id=wallet.id,
            article_id=article.id,
            counterparty_id=counterparty.id,
            amount="50000.00",
            source_id=reserve.id,
        )
        await session.commit()

        assert await manual_payment_money_is_free(session, txn) is False
        assert await sync_manual_payment_receivable(session, txn) is None
