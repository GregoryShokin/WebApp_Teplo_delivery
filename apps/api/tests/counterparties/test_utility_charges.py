"""Коммуналка: от принесённой платёжки до долга, расхода и его гашения.

ЧТО ЗДЕСЬ ЗАКРЕПЛЕНО. Собственной механики долга у коммуналки нет — она пользуется каноном
ДЗ/КЗ наравне с арендой. Поэтому проверяем не «работает ли сервис», а стыки, на которых
контур мог бы разъехаться:

* расход попадает в месяц ПОТРЕБЛЕНИЯ и несёт помещение — иначе прибыль точки считается без
  коммуналки, а сама коммуналка оседает в графе «без помещения»;
* месяц занимается ровно один раз, а отзыв его освобождает: ошибка в сумме — не редкость, и
  без освобождения ключа месяц нельзя было бы провести заново никогда;
* аренда того же арендодателя переживает появление коммуналки, и наоборот. Два потока одного
  контрагента — главный источник тихих потерь: гарды «месяц уже закрыт» смотрят на контрагента,
  и только статья разводит их между собой.
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
    DdsArticle,
    Location,
    LocationLease,
    Organization,
    SupplierExpenseAccrual,
    SupplierInvoice,
    UtilityAccount,
    UtilityIntake,
)
from app.services import utility_charges
from app.services.lease_accruals import ensure_lease_invoice
from app.services.supplier_prepayments import ensure_prepayment_from_bank_transaction
from app.services.utility_charges import UtilityChargeError


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


async def _account(
    session: AsyncSession,
    *,
    kind: str = "water",
    expected_day: int | None = None,
) -> UtilityAccount:
    landlord = await make_counterparty(
        session,
        name=f"Журенков {uuid.uuid4().hex[:4]}",
        inn=f"6143{uuid.uuid4().int % 10**8:08d}",
        cp_type="individual",
        relationship="informal",
    )
    account = UtilityAccount(
        location_id=(await _location(session)).id,
        counterparty_id=landlord.id,
        kind=kind,
        dds_article_id=(await _article(session, name="Коммунальные платежи")).id,
        expected_day=expected_day,
        started_on=date(2026, 1, 1),
    )
    session.add(account)
    await session.flush()
    return account


async def _intake(
    session: AsyncSession,
    account: UtilityAccount,
    *,
    amount: str = "9878.79",
    start: date = date(2026, 7, 1),
    end: date = date(2026, 7, 31),
    with_file: bool = True,
) -> UtilityIntake:
    intake = UtilityIntake(
        account_id=account.id,
        status="ready",
        filename="kvitanciya.jpg" if with_file else None,
        mime="image/jpeg" if with_file else None,
        attachment_sha256=uuid.uuid4().hex * 2 if with_file else None,
        content=b"\xff\xd8\xff" if with_file else None,
        period_start=start,
        period_end=end,
        amount=Decimal(amount),
    )
    session.add(intake)
    await session.flush()
    return intake


async def test_promote_creates_payable_with_location_and_month(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Проведённая платёжка = кредиторка перед арендодателем + расход июля по этой точке."""
    async with async_session_factory() as session:
        account = await _account(session)
        intake = await _intake(session, account)

        invoice = await utility_charges.promote_intake(
            session, intake, as_of=date(2026, 8, 5)
        )

        assert invoice.counterparty_id == account.counterparty_id
        assert invoice.doc_kind == "closing"
        assert invoice.source == "utility"
        # finance — иначе ни правило 1, ни авто-зачёт предоплат документ не увидят.
        assert invoice.operational_scope == "finance"
        assert invoice.payment_status == "unpaid"
        # Долг датирован концом периода, а не днём, когда принесли бумажку: на 31.07 бизнес
        # уже был должен.
        assert invoice.invoice_date == date(2026, 7, 31)
        assert invoice.amount == Decimal("9878.79")
        assert intake.status == "promoted"
        assert intake.invoice_id == invoice.id

        accrual = await session.scalar(
            select(SupplierExpenseAccrual).where(SupplierExpenseAccrual.invoice_id == invoice.id)
        )
        assert accrual is not None
        assert accrual.amount == Decimal("9878.79")
        assert accrual.service_period_end == date(2026, 7, 31)
        # Ось «где» — ради неё и заводился поток: без неё расход не попадёт в прибыль точки.
        assert accrual.location_id == account.location_id
        await session.rollback()


async def test_month_is_taken_once(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Второй снимок той же квитанции не должен создавать второй долг."""
    async with async_session_factory() as session:
        account = await _account(session)
        first = await _intake(session, account)
        await utility_charges.promote_intake(session, first, as_of=date(2026, 8, 5))

        second = await _intake(session, account, amount="9878.79")
        try:
            await utility_charges.promote_intake(session, second, as_of=date(2026, 8, 5))
        except UtilityChargeError as exc:
            assert "уже проведён" in str(exc)
        else:
            raise AssertionError("второй документ за тот же месяц не должен проводиться")
        await session.rollback()


async def test_payment_settles_utility_payable(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Деньги арендодателю гасят коммунальный долг штатным правилом 1 — без своей механики."""
    async with async_session_factory() as session:
        account = await _account(session)
        intake = await _intake(session, account, amount="9878.79")
        invoice = await utility_charges.promote_intake(session, intake, as_of=date(2026, 8, 5))

        wallet = await make_wallet(session)
        tx = CashflowTransaction(
            wallet_id=wallet.id,
            direction="out",
            amount=Decimal("9878.79"),
            operation_date=date(2026, 8, 5),
            counterparty_id=account.counterparty_id,
            source_kind="bank_operation",
            payment_purpose="возмещение коммунальных",
            quality_status="auto",
        )
        session.add(tx)
        await session.flush()

        prepayment = await ensure_prepayment_from_bank_transaction(session, tx)

        assert (await session.get(SupplierInvoice, invoice.id)).payment_status == "paid"
        # Ровно в размер долга — дебиторке взяться неоткуда.
        assert prepayment is None
        await session.rollback()


async def test_prepaid_month_settles_on_promote(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Обратный порядок: сначала заплатили, потом принесли бумажку — долг гасится сразу."""
    async with async_session_factory() as session:
        account = await _account(session)
        wallet = await make_wallet(session)
        tx = CashflowTransaction(
            wallet_id=wallet.id,
            direction="out",
            amount=Decimal("10000.00"),
            operation_date=date(2026, 7, 20),
            counterparty_id=account.counterparty_id,
            source_kind="bank_operation",
            payment_purpose="аванс за коммуналку",
            quality_status="auto",
        )
        session.add(tx)
        await session.flush()
        prepayment = await ensure_prepayment_from_bank_transaction(session, tx)
        assert prepayment is not None

        intake = await _intake(session, account, amount="9878.79")
        invoice = await utility_charges.promote_intake(session, intake, as_of=date(2026, 8, 5))

        assert (await session.get(SupplierInvoice, invoice.id)).payment_status == "paid"
        # Остаток аванса живёт дальше — он и есть дебиторка перед следующим месяцем.
        assert prepayment.amount - prepayment.amount_settled == Decimal("121.21")
        await session.rollback()


async def test_revoke_frees_the_month(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Ошиблись в сумме — отозвали и провели заново. Иначе месяц заперт навсегда."""
    async with async_session_factory() as session:
        account = await _account(session)
        wrong = await _intake(session, account, amount="98787.90")
        invoice = await utility_charges.promote_intake(session, wrong, as_of=date(2026, 8, 5))

        await utility_charges.revoke_intake(session, wrong)
        assert wrong.status == "ready"
        assert wrong.invoice_id is None
        voided = await session.get(SupplierInvoice, invoice.id)
        assert voided is not None and voided.payment_status == "void"
        # Расход за месяц снят вместе с долгом — иначе в июле осталась бы неверная цифра.
        accrual = await session.scalar(
            select(SupplierExpenseAccrual).where(SupplierExpenseAccrual.invoice_id == invoice.id)
        )
        assert accrual is None or accrual.status == "cancelled"

        wrong.amount = Decimal("9878.79")
        fixed = await utility_charges.promote_intake(session, wrong, as_of=date(2026, 8, 5))
        assert fixed.amount == Decimal("9878.79")
        assert fixed.id != invoice.id
        await session.rollback()


async def test_revoke_refuses_when_money_already_went(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Оплаченный долг отзывать нельзя: платёж остался бы висеть на аннулированном документе."""
    async with async_session_factory() as session:
        account = await _account(session)
        intake = await _intake(session, account, amount="9878.79")
        await utility_charges.promote_intake(session, intake, as_of=date(2026, 8, 5))

        wallet = await make_wallet(session)
        tx = CashflowTransaction(
            wallet_id=wallet.id,
            direction="out",
            amount=Decimal("9878.79"),
            operation_date=date(2026, 8, 5),
            counterparty_id=account.counterparty_id,
            source_kind="bank_operation",
            payment_purpose="возмещение коммунальных",
            quality_status="auto",
        )
        session.add(tx)
        await session.flush()
        await ensure_prepayment_from_bank_transaction(session, tx)

        try:
            await utility_charges.revoke_intake(session, intake)
        except UtilityChargeError as exc:
            assert "оплата" in str(exc)
        else:
            raise AssertionError("оплаченный долг не должен отзываться")
        await session.rollback()


async def test_manual_row_without_document_works(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Газ: бумажки нет вовсе, сумму называет арендодатель — учёт всё равно замкнут."""
    async with async_session_factory() as session:
        account = await _account(session, kind="gas")
        intake = await _intake(session, account, amount="6400.00", with_file=False)

        invoice = await utility_charges.promote_intake(session, intake, as_of=date(2026, 8, 5))

        assert invoice.amount == Decimal("6400.00")
        assert intake.has_document is False
        await session.rollback()


async def test_rent_and_utilities_of_one_landlord_coexist(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Два потока одного арендодателя не гасят друг друга.

    Гарды «месяц уже закрыт» ищут чужой документ по контрагенту, и развести аренду с
    коммуналкой может только статья. Проверяем обе стороны: сначала коммуналка, потом аренда.
    """
    async with async_session_factory() as session:
        account = await _account(session)
        rent_article = await _article(session, name="Аренда торговых точек", lease_bound=True)
        lease = LocationLease(
            id=uuid.uuid4(),
            location_id=account.location_id,
            counterparty_id=account.counterparty_id,
            monthly_amount=Decimal("100000.00"),
            started_on=date(2026, 1, 1),
            dds_article_id=rent_article.id,
            payment_mode="postpaid",
            documents_mode="informal",
            accrual_enabled=True,
        )
        session.add(lease)
        await session.flush()

        intake = await _intake(session, account, amount="9878.79")
        utility_invoice = await utility_charges.promote_intake(
            session, intake, as_of=date(2026, 8, 5)
        )
        rent_invoice = await ensure_lease_invoice(
            session, lease, date(2026, 7, 1), as_of=date(2026, 8, 5)
        )

        assert rent_invoice is not None, "коммунальный документ съел арендное начисление месяца"
        assert rent_invoice.informational is False
        assert utility_invoice.informational is False
        # Оба долга живут рядом и оба несут расход июля.
        accruals = (
            await session.scalars(
                select(SupplierExpenseAccrual).where(
                    SupplierExpenseAccrual.invoice_id.in_([utility_invoice.id, rent_invoice.id])
                )
            )
        ).all()
        assert len(accruals) == 2
        assert sum(a.amount for a in accruals) == Decimal("109878.79")
        await session.rollback()


async def test_calendar_shows_month_without_document(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Пропущенный месяц обязан быть видимым: это единственное место, где видна ПРОПАЖА."""
    async with async_session_factory() as session:
        account = await _account(session, expected_day=10)
        intake = await _intake(
            session, account, start=date(2026, 6, 1), end=date(2026, 6, 30), amount="8500.00"
        )
        await utility_charges.promote_intake(session, intake, as_of=date(2026, 8, 5))

        rows = await utility_charges.expected_periods(
            session, account=account, months_back=3, as_of=date(2026, 8, 20)
        )
        by_month = {row["month"]: row["state"] for row in rows}

        assert by_month[date(2026, 6, 1)] == "provided"
        # За июль документа нет, а срок (10 августа) прошёл — это просрочка, а не тишина.
        assert by_month[date(2026, 7, 1)] == "overdue"
        await session.rollback()
