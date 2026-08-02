"""Коммуналка: из одной принесённой бумажки — счёт к оплате, расход месяца и его гашение.

ЧТО ЗДЕСЬ ЗАКРЕПЛЕНО. Собственной механики долга у коммуналки нет — она пользуется каноном
ДЗ/КЗ наравне с арендой. Поэтому проверяем не «работает ли сервис», а стыки, на которых
контур мог бы разъехаться:

* бумажка одна, а документов два, и расход несёт ровно один из них. Пока начисление
  заводилось и на счёте, услуга на 13 000 ₽ давала 26 000 ₽ в P&L, причём деньги сходились и
  увидеть удвоение можно было только в отчёте о прибыли;
* у электричества суммы у пары РАЗНЫЕ: расход месяца больше суммы к доплате на зачтённый
  аванс. Признать расход по сумме платёжки значит потерять из прибыли ровно ту часть, что
  оплачена вперёд;
* расход попадает в месяц ПОТРЕБЛЕНИЯ и несёт помещение — иначе прибыль точки считается без
  коммуналки, а сама коммуналка оседает в графе «без помещения»;
* месяц занимается ровно один раз: sha ловит лишь повтор того же файла, а пересняли
  квитанцию — байты другие, и без ключа на месяц вырос бы второй долг;
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
from app.services.lease_accruals import ensure_lease_invoice
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
    started_on: date = date(2026, 1, 1),
    ended_on: date | None = None,
    is_active: bool = True,
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
        started_on=started_on,
        ended_on=ended_on,
        is_active=is_active,
    )
    session.add(account)
    await session.flush()
    return account


async def _accruals(
    session: AsyncSession, *invoices: SupplierInvoice
) -> list[SupplierExpenseAccrual]:
    """Строки признания расхода по перечисленным документам — включая отменённые.

    Отменённые тоже нужны: «расход снят» и «расхода не было» — разные состояния, и путать их
    нельзя ни в одну сторону.
    """
    return list(
        (
            await session.scalars(
                select(SupplierExpenseAccrual).where(
                    SupplierExpenseAccrual.invoice_id.in_([invoice.id for invoice in invoices])
                )
            )
        ).all()
    )


async def _pay_bill(
    session: AsyncSession, bill: SupplierInvoice, *, on: date
) -> CashflowTransaction:
    """Оплата счёта штатным путём: деньги ушли — по канону возникает ДЗ (``prepaid_bill``).

    Повторяет то, что делает любая дверь гашения: аллокация на реальную проводку и единый
    чокпоинт ``reconcile_bill_prepayment``. Своей механики у теста нет намеренно — иначе он
    проверял бы выдуманный путь, а не тот, которым деньги ходят на самом деле.
    """
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
    return tx


async def test_receipt_makes_bill_and_closing(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Квитанция за воду на 9 878,79 → счёт (его платят) и закрывающий (он несёт расход).

    Роли разделены жёстко: в очередь оплат попадает счёт, а расход — только на закрывающем.
    Начисление на обоих документах удвоило бы расход в P&L, и деньги при этом сходились бы.
    """
    async with async_session_factory() as session:
        account = await _account(session)

        bill, closing = await utility_charges.build_utility_documents(
            session,
            account,
            period_start=date(2026, 7, 1),
            period_end=date(2026, 7, 31),
            expense_amount=Decimal("9878.79"),
            payable_amount=Decimal("9878.79"),
            as_of=date(2026, 8, 5),
        )

        assert closing is not None
        assert bill.doc_kind == "bill"
        assert closing.doc_kind == "closing"
        for document in (bill, closing):
            assert document.counterparty_id == account.counterparty_id
            assert document.source == "utility"
            # finance — иначе ни правило 1 канона, ни авто-зачёт предоплат документ не увидят.
            assert document.operational_scope == "finance"
            assert document.amount == Decimal("9878.79")
            # Долг датирован концом периода, а не днём, когда принесли бумажку: на 31.07 бизнес
            # уже был должен.
            assert document.invoice_date == date(2026, 7, 31)
            assert document.service_period_start == date(2026, 7, 1)
            assert document.service_period_end == date(2026, 7, 31)
            # «Возмещение», а не «акт»: акта у нас нет и не будет — счёт ресурсника выставлен
            # на арендодателя. Человек в кредиторке должен понимать природу строки, не открывая
            # карточку.
            assert document.number == "Возмещение: вода, 07.2026"

        accruals = await _accruals(session, bill, closing)
        assert [accrual.invoice_id for accrual in accruals] == [closing.id], (
            "расход обязан висеть ровно на закрывающем: на счёте он удваивает P&L"
        )
        assert accruals[0].amount == Decimal("9878.79")
        assert accruals[0].service_period_start == date(2026, 7, 1)
        assert accruals[0].service_period_end == date(2026, 7, 31)
        # Ось «где» — ради неё и заводился поток: без неё расход не попадёт в прибыль точки.
        assert accruals[0].location_id == account.location_id
        await session.rollback()


async def test_electricity_fact_recognises_full_expense(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Фактический акт: расход месяца 90 000, к доплате 40 000 — и это РАЗНЫЕ числа.

    Из потребления вычтен внесённый ранее аванс, поэтому сумма платёжки меньше расхода. Признай
    мы расход по сумме к доплате — 50 000 ₽ электричества исчезли бы из прибыли августа, а
    оплаченный аванс так и остался бы дебиторкой, которую нечем закрыть.
    """
    async with async_session_factory() as session:
        account = await _account(session, kind="electricity")

        bill, closing = await utility_charges.build_utility_documents(
            session,
            account,
            period_start=date(2026, 8, 1),
            period_end=date(2026, 8, 31),
            expense_amount=Decimal("90000.00"),
            payable_amount=Decimal("40000.00"),
            as_of=date(2026, 9, 5),
        )

        assert closing is not None
        assert closing.amount == Decimal("90000.00")
        assert bill.amount == Decimal("40000.00")

        accruals = await _accruals(session, bill, closing)
        assert [accrual.invoice_id for accrual in accruals] == [closing.id]
        assert accruals[0].amount == Decimal("90000.00"), (
            "расход августа = потребление целиком, а не остаток к доплате"
        )
        await session.rollback()


async def test_electricity_advance_has_no_closing(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Авансовый акт — основание платежа и ничего больше: расхода он не несёт вовсе.

    Расход придёт фактическим актом. Признай мы его уже сейчас, тот же месяц был бы признан
    дважды: авансом и фактом, — а фактический акт как раз и приходит на полное потребление.
    """
    async with async_session_factory() as session:
        account = await _account(session, kind="electricity")

        bill, closing = await utility_charges.build_utility_documents(
            session,
            account,
            period_start=date(2026, 8, 1),
            period_end=date(2026, 8, 31),
            expense_amount=None,
            payable_amount=Decimal("50000.00"),
            as_of=date(2026, 8, 15),
        )

        assert closing is None
        assert bill.doc_kind == "bill"
        assert bill.amount == Decimal("50000.00")
        assert await _accruals(session, bill) == []
        # И по контрагенту в целом расхода нет: авансовый акт в P&L не попадает ничем.
        assert (
            await session.scalar(
                select(SupplierExpenseAccrual.id).where(
                    SupplierExpenseAccrual.counterparty_id == account.counterparty_id
                )
            )
        ) is None
        await session.rollback()


async def test_paid_advance_is_credited_by_fact_act(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Полный круг электричества: аванс оплачен → факт-акт его зачёл, расход признан один раз.

    Аванс июля 50 000 уплачен и по канону стал дебиторкой (счёт — не долг, его оплата —
    деньги у поставщика). Пришедший фактический акт августа на 90 000 обязан ЗАЧЕСТЬ эту
    дебиторку, а не встать рядом с ней: иначе у одного арендодателя одновременно висят 50 000
    дебиторки и 90 000 кредиторки — то есть 140 000 несуществующих взаиморасчётов.
    """
    async with async_session_factory() as session:
        account = await _account(session, kind="electricity")

        advance, no_closing = await utility_charges.build_utility_documents(
            session,
            account,
            period_start=date(2026, 7, 1),
            period_end=date(2026, 7, 31),
            expense_amount=None,
            payable_amount=Decimal("50000.00"),
            as_of=date(2026, 7, 15),
        )
        assert no_closing is None
        await _pay_bill(session, advance, on=date(2026, 7, 20))

        receivable = await session.scalar(
            select(SupplierPrepayment).where(
                SupplierPrepayment.bill_invoice_id == advance.id,
                SupplierPrepayment.kind == supplier_prepayments.BILL_PREPAYMENT_KIND,
            )
        )
        assert receivable is not None and receivable.amount == Decimal("50000.00")

        bill, closing = await utility_charges.build_utility_documents(
            session,
            account,
            period_start=date(2026, 8, 1),
            period_end=date(2026, 8, 31),
            expense_amount=Decimal("90000.00"),
            payable_amount=Decimal("40000.00"),
            as_of=date(2026, 9, 5),
        )

        assert closing is not None
        assert receivable.amount_settled == Decimal("50000.00"), "аванс не зачтён фактическим актом"
        assert receivable.status == "settled"
        allocated = await session.scalar(
            select(InvoicePaymentAllocation.amount).where(
                InvoicePaymentAllocation.invoice_id == closing.id,
                InvoicePaymentAllocation.source_kind == "prepayment",
            )
        )
        assert allocated == Decimal("50000.00")
        # Остаток обязательства = ровно то, что предъявлено к доплате счётом.
        assert closing.amount - allocated == bill.amount == Decimal("40000.00")
        assert closing.payment_status == "partially_paid"

        accruals = await _accruals(session, advance, bill, closing)
        assert [accrual.invoice_id for accrual in accruals] == [closing.id]
        assert accruals[0].amount == Decimal("90000.00"), (
            "расход августа признаётся один раз и целиком: зачёт аванса — движение денег, "
            "а не уменьшение потребления"
        )
        await session.rollback()


async def test_month_is_taken_once(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Второй снимок той же квитанции не должен заводить второй долг.

    Хэш файла ловит только повтор ТОГО ЖЕ файла: пересняли квитанцию — байты другие, документ
    тот же. Единственная защита от двойного долга — ключ на месяц по потоку.
    """
    async with async_session_factory() as session:
        account = await _account(session)
        period = {"period_start": date(2026, 7, 1), "period_end": date(2026, 7, 31)}
        _, first = await utility_charges.build_utility_documents(
            session,
            account,
            expense_amount=Decimal("9878.79"),
            payable_amount=Decimal("9878.79"),
            as_of=date(2026, 8, 5),
            **period,
        )
        assert first is not None

        try:
            await utility_charges.build_utility_documents(
                session,
                account,
                expense_amount=Decimal("9878.79"),
                payable_amount=Decimal("9878.79"),
                as_of=date(2026, 8, 5),
                **period,
            )
        except UtilityChargeError as exc:
            # Текст называет уже существующий документ: человеку нужно понять, что именно
            # занимает месяц, — счёт или проведённый долг. Проверяем суть отказа, а не
            # конкретную формулировку.
            assert "07.2026" in str(exc) and "уже" in str(exc)
        else:
            raise AssertionError("второй документ за тот же месяц не должен проводиться")

        closings = (
            await session.scalars(
                select(SupplierInvoice).where(
                    SupplierInvoice.counterparty_id == account.counterparty_id,
                    SupplierInvoice.doc_kind == "closing",
                )
            )
        ).all()
        assert [invoice.id for invoice in closings] == [first.id]
        await session.rollback()


async def test_period_before_account_start_refused(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """До начала учёта потока документов быть не может — иначе расход поедет в чужую эпоху."""
    async with async_session_factory() as session:
        account = await _account(session, started_on=date(2026, 6, 1))
        try:
            await utility_charges.build_utility_documents(
                session,
                account,
                period_start=date(2026, 5, 1),
                period_end=date(2026, 5, 31),
                expense_amount=Decimal("5000.00"),
                payable_amount=Decimal("5000.00"),
                as_of=date(2026, 8, 5),
            )
        except UtilityChargeError as exc:
            assert "раньше" in str(exc)
        else:
            raise AssertionError("период раньше начала учёта не должен проводиться")
        await session.rollback()


async def test_period_after_account_end_refused(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Закрытый поток долгов больше не рождает: съехали с точки — платить за неё нечего."""
    async with async_session_factory() as session:
        account = await _account(session, ended_on=date(2026, 6, 30))
        try:
            await utility_charges.build_utility_documents(
                session,
                account,
                period_start=date(2026, 7, 1),
                period_end=date(2026, 7, 31),
                expense_amount=Decimal("5000.00"),
                payable_amount=Decimal("5000.00"),
                as_of=date(2026, 8, 5),
            )
        except UtilityChargeError as exc:
            assert "позже" in str(exc)
        else:
            raise AssertionError("период после закрытия потока не должен проводиться")
        await session.rollback()


async def test_reversed_period_refused(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Перевёрнутый период — опечатка в разборе. Пропустив её, получим расход в пустоте.

    Отказ до создания документов важен сам по себе: их рождается два, и падение на середине
    оставило бы счёт без закрывающего — то есть платёж без расхода.
    """
    async with async_session_factory() as session:
        account = await _account(session)
        try:
            await utility_charges.build_utility_documents(
                session,
                account,
                period_start=date(2026, 7, 31),
                period_end=date(2026, 7, 1),
                expense_amount=Decimal("5000.00"),
                payable_amount=Decimal("5000.00"),
                as_of=date(2026, 8, 5),
            )
        except UtilityChargeError as exc:
            assert "Конец периода раньше начала" in str(exc)
        else:
            raise AssertionError("перевёрнутый период не должен проводиться")
        assert (
            await session.scalar(
                select(SupplierInvoice.id).where(
                    SupplierInvoice.counterparty_id == account.counterparty_id
                )
            )
        ) is None
        await session.rollback()


async def test_disabled_account_refused(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Отключённый поток — сознательное решение человека, и разбор его не отменяет."""
    async with async_session_factory() as session:
        account = await _account(session, is_active=False)
        try:
            await utility_charges.build_utility_documents(
                session,
                account,
                period_start=date(2026, 7, 1),
                period_end=date(2026, 7, 31),
                expense_amount=Decimal("9878.79"),
                payable_amount=Decimal("9878.79"),
                as_of=date(2026, 8, 5),
            )
        except UtilityChargeError as exc:
            assert "отключён" in str(exc)
        else:
            raise AssertionError("по отключённому потоку долг заводить нельзя")
        await session.rollback()


async def test_cash_payout_settles_utility_debt(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Наличные арендодателю гасят коммунальный долг — это ОСНОВНОЙ канал расчётов.

    Банковский платёж проходит правило 1 сам, а выдача из Сейфа в него не заходит вовсе
    (адресные деньги слепой FIFO разнёс бы не туда). До правки коммунальная кредиторка
    наличными не гасилась ничем: деньги уходили, долг оставался висеть.
    """
    async with async_session_factory() as session:
        account = await _account(session)
        bill, closing = await utility_charges.build_utility_documents(
            session,
            account,
            period_start=date(2026, 7, 1),
            period_end=date(2026, 7, 31),
            expense_amount=Decimal("9878.79"),
            payable_amount=Decimal("9878.79"),
            as_of=date(2026, 8, 5),
        )
        assert closing is not None

        wallet = await make_wallet(session)
        tx = CashflowTransaction(
            wallet_id=wallet.id,
            direction="out",
            amount=Decimal("9878.79"),
            operation_date=date(2026, 8, 6),
            counterparty_id=account.counterparty_id,
            source_kind="safe_payout",
            payment_purpose="возмещение коммуналки наличными",
            quality_status="auto",
        )
        session.add(tx)
        await session.flush()

        handled = await utility_charges.settle_utility_invoices_from_cash(
            session,
            counterparty_id=account.counterparty_id,
            article_id=account.dds_article_id,
            location_id=account.location_id,
            transaction_id=tx.id,
            amount=Decimal("9878.79"),
            wallet_id=wallet.id,
        )

        assert handled is True
        # Одни деньги закрывают обе бумаги, и дважды из Сейфа не уходят: наличные гасят счёт,
        # его оплата по канону становится дебиторкой, и та зачитывает закрывающий. Иначе месяц
        # числился бы неоплаченным при ушедших деньгах.
        assert (await session.get(SupplierInvoice, bill.id)).payment_status == "paid"
        assert (await session.get(SupplierInvoice, closing.id)).payment_status == "paid"
        sources = (
            await session.scalars(
                select(InvoicePaymentAllocation.source_kind).where(
                    InvoicePaymentAllocation.invoice_id == closing.id
                )
            )
        ).all()
        assert list(sources) == ["prepayment"], "закрывающий гасится зачётом ДЗ, а не вторым разом"
        await session.rollback()


async def test_cash_payout_overpay_becomes_receivable(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Заплатили больше предъявленного — остаток становится дебиторкой, а не исчезает."""
    async with async_session_factory() as session:
        account = await _account(session)
        await utility_charges.build_utility_documents(
            session,
            account,
            period_start=date(2026, 7, 1),
            period_end=date(2026, 7, 31),
            expense_amount=Decimal("9000.00"),
            payable_amount=Decimal("9000.00"),
            as_of=date(2026, 8, 5),
        )

        wallet = await make_wallet(session)
        tx = CashflowTransaction(
            wallet_id=wallet.id,
            direction="out",
            amount=Decimal("10000.00"),
            operation_date=date(2026, 8, 6),
            counterparty_id=account.counterparty_id,
            source_kind="safe_payout",
            payment_purpose="возмещение коммуналки",
            quality_status="auto",
        )
        session.add(tx)
        await session.flush()

        await utility_charges.settle_utility_invoices_from_cash(
            session,
            counterparty_id=account.counterparty_id,
            article_id=account.dds_article_id,
            location_id=account.location_id,
            transaction_id=tx.id,
            amount=Decimal("10000.00"),
            wallet_id=wallet.id,
        )

        prepayment = await session.scalar(
            select(SupplierPrepayment).where(
                SupplierPrepayment.cashflow_transaction_id == tx.id,
            )
        )
        assert prepayment is not None
        assert prepayment.amount == Decimal("1000.00")
        await session.rollback()


async def test_cash_payout_ignores_unrelated_counterparty(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Выдача не по коммунальному потоку не должна трогать коммунальные долги."""
    async with async_session_factory() as session:
        account = await _account(session)
        other = await make_counterparty(session, name="ООО «Прочее»", inn="6143000000")
        _, closing = await utility_charges.build_utility_documents(
            session,
            account,
            period_start=date(2026, 7, 1),
            period_end=date(2026, 7, 31),
            expense_amount=Decimal("9878.79"),
            payable_amount=Decimal("9878.79"),
            as_of=date(2026, 8, 5),
        )
        assert closing is not None

        handled = await utility_charges.settle_utility_invoices_from_cash(
            session,
            counterparty_id=other.id,
            article_id=account.dds_article_id,
            location_id=account.location_id,
            transaction_id=uuid.uuid4(),
            amount=Decimal("5000.00"),
        )
        assert handled is False
        assert (await session.get(SupplierInvoice, closing.id)).payment_status == "unpaid"
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

        _, utility_closing = await utility_charges.build_utility_documents(
            session,
            account,
            period_start=date(2026, 7, 1),
            period_end=date(2026, 7, 31),
            expense_amount=Decimal("9878.79"),
            payable_amount=Decimal("9878.79"),
            as_of=date(2026, 8, 5),
        )
        rent_invoice = await ensure_lease_invoice(
            session, lease, date(2026, 7, 1), as_of=date(2026, 8, 5)
        )

        assert utility_closing is not None
        assert rent_invoice is not None, "коммунальный документ съел арендное начисление месяца"
        assert rent_invoice.informational is False
        assert utility_closing.informational is False
        # Оба долга живут рядом и оба несут расход июля.
        accruals = await _accruals(session, utility_closing, rent_invoice)
        assert len(accruals) == 2
        assert sum(accrual.amount for accrual in accruals) == Decimal("109878.79")
        await session.rollback()


async def test_calendar_shows_month_without_document(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Пропущенный месяц обязан быть видимым: это единственное место, где видна ПРОПАЖА.

    Существующие витрины показывают то, что в системе есть. Месяц, за который бумажку не
    принесли, не оставляет следа нигде — обнаружить его можно было только вспомнив.
    """
    async with async_session_factory() as session:
        account = await _account(session, expected_day=10)
        await utility_charges.build_utility_documents(
            session,
            account,
            period_start=date(2026, 6, 1),
            period_end=date(2026, 6, 30),
            expense_amount=Decimal("8500.00"),
            payable_amount=Decimal("8500.00"),
            as_of=date(2026, 7, 5),
        )

        rows = await utility_charges.expected_periods(
            session, account=account, months_back=3, as_of=date(2026, 8, 20)
        )
        by_month = {row["month"]: row["state"] for row in rows}

        assert by_month[date(2026, 6, 1)] == "provided"
        # За июль документа нет, а срок (10 августа) прошёл — это просрочка, а не тишина.
        assert by_month[date(2026, 7, 1)] == "overdue"
        await session.rollback()


async def test_advance_and_actual_of_the_same_month_coexist(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Аванс за июнь и факт за июнь — один поток, один месяц, и оба обязаны завестись.

    Это не выдуманный угол, а обычный ход дел у энергетика (акты ИП Гордеева, присланные
    владельцем 02.08.2026): 19 июня выписывается авансовый счёт ЗА ИЮНЬ, а 17 июля приходит
    фактический акт ЗА ТОТ ЖЕ ИЮНЬ. Пока ключ идемпотентности состоял только из потока и месяца,
    второй документ бился о частичный уникум source+external_id и падал сырым IntegrityError —
    то есть на втором месяце работы контур встал бы намертво, и не с внятным отказом, а с
    пятисотой. Роль в ключе разводит их: advance, due и closing живут в месяце каждый сам по себе.
    """
    async with async_session_factory() as session:
        account = await _account(session, kind="electricity")

        # 19.06 — авансовый счёт на июнь: расхода не несёт, платится вперёд.
        advance_bill, advance_closing = await utility_charges.build_utility_documents(
            session,
            account,
            period_start=date(2026, 6, 1),
            period_end=date(2026, 6, 30),
            expense_amount=None,
            payable_amount=Decimal("65000.00"),
            as_of=date(2026, 6, 19),
        )
        assert advance_closing is None
        assert advance_bill.amount == Decimal("65000.00")

        # 17.07 — фактический акт за тот же июнь: расход 95 402, к доплате 30 402.
        due_bill, closing = await utility_charges.build_utility_documents(
            session,
            account,
            period_start=date(2026, 6, 1),
            period_end=date(2026, 6, 30),
            expense_amount=Decimal("95402.00"),
            payable_amount=Decimal("30402.00"),
            as_of=date(2026, 7, 17),
        )

        assert closing is not None
        assert closing.amount == Decimal("95402.00")
        assert due_bill.amount == Decimal("30402.00")
        # Три РАЗНЫХ ключа в одном месяце — иначе база отвергла бы второй документ.
        assert len({advance_bill.external_id, due_bill.external_id, closing.external_id}) == 3

        # Расход июня признан ровно один раз и на полную сумму: аванс расхода не несёт.
        accruals = await _accruals(session, advance_bill, due_bill, closing)
        assert [accrual.invoice_id for accrual in accruals] == [closing.id]
        assert accruals[0].amount == Decimal("95402.00")
        await session.rollback()
