"""Два потока у одного контрагента: арендодатель отдельно за помещение, отдельно за коммуналку.

ЗАЧЕМ ЭТОТ ФАЙЛ. У арендодателя редко бывает одна услуга: он сдаёт помещение и он же
перевыставляет воду, газ и свет. Пока «договор услуги» матчился на контрагента почти без
оглядки на статью, второй поток съедал первый — причём молча, без единой ошибки в логе.

Ловилось это так. ``apply_closing_document`` помечал справочным ЛЮБОЙ документ, у которого
``source != 'self_billed'``, — под условие попадало и собственное арендное начисление
(``source='lease'``). Достаточно было завести арендодателю informal-договор услуги, чтобы
ночная джоба завела аренду за месяц, а следующая же строка кода объявила её информационной:
``informational=True`` выводит документ и из кредиторки, и из признания расхода. В отчёте это
выглядит не ошибкой, а отсутствием: аренды за месяц просто нет.

Вторая половина той же ловушки — договор с пустой статьёй. Начислять он не может (без статьи
расход некуда отнести, ``ensure_agreement_invoice`` возвращает ``None``), а покрывающим себя
объявлял для любой статьи контрагента. То есть договор, не создающий ни рубля, обесценивал
чужие документы.

Потеря расхода хуже задвоения: задвоение видно в отчёте, пропажа — нет.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from cp_helpers import make_counterparty
from sqlalchemy import func as sa_func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    CounterpartyServiceAgreement,
    DdsArticle,
    Location,
    LocationLease,
    Organization,
    SupplierExpenseAccrual,
    SupplierInvoice,
)
from app.services import supplier_prepayments
from app.services.lease_accruals import ensure_lease_invoice
from app.services.service_agreement_accruals import covered_by_agreement


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
        id=uuid.uuid4(), organization_id=organization_id, name=f"Точка {uuid.uuid4().hex[:6]}"
    )
    session.add(location)
    await session.flush()
    return location


async def _agreement(
    session: AsyncSession,
    *,
    counterparty_id: uuid.UUID,
    article_id: uuid.UUID | None,
    title: str = "Обслуживание",
) -> CounterpartyServiceAgreement:
    agreement = CounterpartyServiceAgreement(
        counterparty_id=counterparty_id,
        title=title,
        monthly_amount=Decimal("3000.00"),
        dds_article_id=article_id,
        documents_mode="informal",
        accrual_enabled=True,
        started_on=date(2026, 1, 1),
    )
    session.add(agreement)
    await session.flush()
    return agreement


async def _lease_with_agreement(
    session: AsyncSession, *, agreement_article: str
) -> tuple[LocationLease, uuid.UUID]:
    """Арендодатель, у которого есть и договор аренды, и informal-договор услуги.

    ``agreement_article``: ``none`` — договор без статьи (тот, что раньше матчился на любую),
    ``same`` — договор ровно по арендной статье.
    """
    landlord = await make_counterparty(
        session, name=f"Журенков {uuid.uuid4().hex[:4]}", inn=f"6143{uuid.uuid4().int % 10**8:08d}"
    )
    rent_article = await _article(session, name="Аренда торговых точек", lease_bound=True)
    location = await _location(session)
    lease = LocationLease(
        id=uuid.uuid4(),
        location_id=location.id,
        counterparty_id=landlord.id,
        monthly_amount=Decimal("100000.00"),
        started_on=date(2026, 1, 1),
        dds_article_id=rent_article.id,
        payment_mode="postpaid",
        documents_mode="informal",
        accrual_enabled=True,
    )
    session.add(lease)
    await _agreement(
        session,
        counterparty_id=landlord.id,
        article_id=None if agreement_article == "none" else rent_article.id,
    )
    await session.flush()
    return lease, rent_article.id


async def test_lease_survives_agreement_without_article(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Договор-пустышка у арендодателя не должен стирать аренду месяца.

    Репро дефекта: договор без статьи покрывал ЛЮБУЮ статью контрагента, поэтому арендное
    начисление объявлялось справочным — и пропадало из кредиторки и из расхода.
    """
    async with async_session_factory() as session:
        lease, _ = await _lease_with_agreement(session, agreement_article="none")

        invoice = await ensure_lease_invoice(
            session, lease, date(2026, 6, 1), as_of=date(2026, 7, 1)
        )
        assert invoice is not None
        assert invoice.informational is False
        assert invoice.payment_status == "unpaid"

        accrual = await session.scalar(
            select(SupplierExpenseAccrual).where(SupplierExpenseAccrual.invoice_id == invoice.id)
        )
        assert accrual is not None
        assert accrual.amount == Decimal("100000.00")
        await session.rollback()


async def test_own_accrual_is_never_informational(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Даже при полном совпадении статьи собственное начисление остаётся долгом.

    Вторая половина дефекта, независимая от сверки статей: справочным помечалось всё, что не
    ``self_billed``, — в том числе наше собственное начисление. Бумагой оно не является, и
    обесценивать его нечем; иначе месяц остаётся вообще без расхода — ни по документу, ни по
    договору.
    """
    async with async_session_factory() as session:
        lease, _ = await _lease_with_agreement(session, agreement_article="same")

        invoice = await ensure_lease_invoice(
            session, lease, date(2026, 6, 1), as_of=date(2026, 7, 1)
        )
        assert invoice is not None
        assert invoice.informational is False

        accrual = await session.scalar(
            select(SupplierExpenseAccrual).where(SupplierExpenseAccrual.invoice_id == invoice.id)
        )
        assert accrual is not None
        await session.rollback()


async def test_agreement_without_article_covers_nothing(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Договор без статьи не начисляет — и потому не вправе закрывать чужие периоды."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="ООО «Двухуслужное»", inn="614300000002")
        article = await _article(session, name="Коммунальные платежи")
        await _agreement(session, counterparty_id=cp.id, article_id=None)
        await session.flush()

        assert (
            await covered_by_agreement(session, cp.id, on=date(2026, 6, 30), article_id=article.id)
            is False
        )
        await session.rollback()


async def test_agreement_still_covers_its_own_article(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Обратная сторона: по своей статье договор остаётся источником истины.

    Гард сужали ради соседних потоков, а не ради его отмены: акт по услуге, которую мы и так
    начисляем сами, обязан оставаться справочным — иначе расход месяца удвоится.
    """
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="ИП Наумченко", inn="614300000003")
        article = await _article(session, name="Бухгалтерское обслуживание")
        other = await _article(session, name="Юридические услуги")
        await _agreement(session, counterparty_id=cp.id, article_id=article.id)
        await session.flush()

        assert (
            await covered_by_agreement(session, cp.id, on=date(2026, 6, 30), article_id=article.id)
            is True
        )
        # А по соседней услуге того же контрагента документ по-прежнему полноценный.
        assert (
            await covered_by_agreement(session, cp.id, on=date(2026, 6, 30), article_id=other.id)
            is False
        )
        await session.rollback()


async def test_external_document_under_agreement_stays_informational(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Сужение не должно пропустить настоящий акт контрагента мимо гарда.

    Проверяем не предикат, а дверь: ``apply_closing_document`` на внешнем документе по статье
    действующего договора обязан вернуть 0 и пометить документ справочным.
    """
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="ИП Наумченко", inn="614300000004")
        article = await _article(session, name="Бухгалтерское обслуживание")
        await _agreement(session, counterparty_id=cp.id, article_id=article.id)
        invoice = SupplierInvoice(
            id=uuid.uuid4(),
            counterparty_id=cp.id,
            source="email",
            direction="payable",
            doc_kind="closing",
            operational_scope="finance",
            number="АКТ-77",
            invoice_date=date(2026, 6, 30),
            amount=Decimal("3000.00"),
            dds_article_id=article.id,
            service_period_start=date(2026, 6, 1),
            service_period_end=date(2026, 6, 30),
            service_period_status="ready",
        )
        session.add(invoice)
        await session.flush()

        settled = await supplier_prepayments.apply_closing_document(
            session, invoice, as_of=date(2026, 7, 1)
        )
        assert settled == Decimal("0.00")
        assert invoice.informational is True
        await session.rollback()


async def test_lease_without_article_never_accrues(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Договор аренды без статьи не создаёт ни кредиторки, ни расхода — и не жалуется.

    Это ЗАФИКСИРОВАННОЕ поведение, а не проверенное намерение. Завести такой договор API
    позволяет (``dds_article_id`` в схеме необязателен), а ``ensure_lease_invoice`` на нём
    возвращает ``None``: относить расход некуда. Снаружи это выглядит как исправно заведённая
    аренда, которой месяцами нет ни в долгах, ни в отчёте.

    Тест стоит здесь, чтобы дыру нельзя было потерять: если однажды статью сделают
    обязательной, он сломается и потребует осознанного решения, а не молчаливого дрейфа.
    """
    async with async_session_factory() as session:
        landlord = await make_counterparty(
            session, name="ИП Безстатейный", inn="614300000005"
        )
        location = await _location(session)
        lease = LocationLease(
            id=uuid.uuid4(),
            location_id=location.id,
            counterparty_id=landlord.id,
            monthly_amount=Decimal("100000.00"),
            started_on=date(2026, 1, 1),
            dds_article_id=None,
            payment_mode="postpaid",
            documents_mode="informal",
            accrual_enabled=True,
        )
        session.add(lease)
        await session.flush()

        invoice = await ensure_lease_invoice(
            session, lease, date(2026, 6, 1), as_of=date(2026, 7, 1)
        )
        assert invoice is None

        accruals = await session.scalar(
            select(sa_func.count())
            .select_from(SupplierExpenseAccrual)
            .where(SupplierExpenseAccrual.counterparty_id == landlord.id)
        )
        assert accruals == 0
        await session.rollback()
