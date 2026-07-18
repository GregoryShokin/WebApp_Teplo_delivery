"""АУДИТ-4, хвост email-ingest: эмпирическая проверка находок #23-26 adversarial-workflow.

Три подозрения в ``email_invoice_ingest.py`` (пере-разбор / restore / исключение-удаление
intake) проверяются красными тестами по канону ДЗ/КЗ владельца (17.07):

1. Повторный разбор письма (``confirm_intake_with_review``) переписывает amount/дату УЖЕ
   ПРОВЕДЁННОГО документа без ``_recompute_status`` и без пересчёта ``activation_status`` →
   непокрытый остаток закрывающего не попадает в КЗ (дашборд фильтрует
   payment_status IN ('unpaid','partially_paid')), будущий УПД остаётся «активным».
2. ``restore_intake`` возвращает void→unpaid БЕЗ пересчёта по аллокациям → частично
   оплаченный счёт показывается неоплаченным.
3. ``exclude_intake``/``delete_intake_forever`` аннулируют/удаляют закрывающий документ,
   НЕ возвращая списанную им предоплату (аллокации умирают каскадом, amount_settled
   финансировавшей предоплаты остаётся списанным) → ДЗ занижена; при удалении — безвозвратно.

Красный тест = подозрение подтверждено.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

from cp_helpers import make_counterparty, make_expense_article, make_wallet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import EmailInvoiceIntake, SupplierInvoice, SupplierPrepayment
from app.services.email_invoice_ingest import (
    confirm_intake_with_review,
    delete_intake_forever,
    exclude_intake,
    materialize_from_intake,
    restore_intake,
)
from app.services.supplier_prepayments import create_opening_prepayment

PAST = date.today() - timedelta(days=15)
FUTURE = date.today() + timedelta(days=170)


async def _receivable(session: AsyncSession, counterparty_id: uuid.UUID) -> Decimal:
    """ДЗ так, как её считает плитка: остатки открытых предоплат."""
    rows = (
        await session.scalars(
            select(SupplierPrepayment).where(
                SupplierPrepayment.counterparty_id == counterparty_id,
                SupplierPrepayment.status.in_(("open", "partially_settled")),
            )
        )
    ).all()
    return sum(
        (Decimal(str(r.amount)) - Decimal(str(r.amount_settled)) for r in rows), Decimal("0.00")
    )


async def _payable(session: AsyncSession, counterparty_id: uuid.UUID) -> Decimal:
    """КЗ так, как её считает плитка: неоплаченный остаток активных закрывающих."""
    from app.services.counterparty_matching import _invoice_remaining

    rows = (
        await session.scalars(
            select(SupplierInvoice).where(
                SupplierInvoice.counterparty_id == counterparty_id,
                SupplierInvoice.direction == "payable",
                SupplierInvoice.doc_kind == "closing",
                SupplierInvoice.activation_status == "active",
                SupplierInvoice.payment_status.in_(("unpaid", "partially_paid")),
            )
        )
    ).all()
    total = Decimal("0.00")
    for inv in rows:
        total += max(await _invoice_remaining(session, inv), Decimal("0.00"))
    return total


def _make_intake(
    *,
    counterparty_id: uuid.UUID | None,
    amount: str,
    document_kind: str,
    invoice_number: str,
    invoice_date: date,
) -> EmailInvoiceIntake:
    return EmailInvoiceIntake(
        mailbox="corporate",
        from_addr=None,
        subject=f"Документ {invoice_number}",
        attachment_sha256=uuid.uuid4().hex + uuid.uuid4().hex,
        status="needs_review",
        counterparty_id=counterparty_id,
        recognition={
            "amount": amount,
            "document_kind": document_kind,
            "invoice_number": invoice_number,
            "invoice_date": invoice_date.isoformat(),
        },
    )


async def test_reparse_amount_of_paid_closing_restores_kz_remainder(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Подозрение 1 (amount): пере-разбор поднял сумму УЖЕ погашенного закрывающего.

    УПД на 400 полностью зачтён из аванса 400 → paid, КЗ 0, ДЗ 0. Оператор пере-разбором
    исправляет сумму на 1000 (распознавание взяло «без НДС» и т.п.). Канон: зачтено 400 из
    1000 → partially_paid, непокрытые 600 — кредиторка. Баг: статус остаётся 'paid', и 600
    никогда не попадают в КЗ (дашборд фильтрует unpaid/partially_paid)."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Email-переразбор-сумма", inn="6155990101")
        await session.commit()
        await create_opening_prepayment(session, counterparty_id=cp.id, amount=Decimal("400.00"))

        intake = _make_intake(
            counterparty_id=cp.id,
            amount="400.00",
            document_kind="upd",
            invoice_number="УПД-РЕПАРС-1",
            invoice_date=PAST,
        )
        session.add(intake)
        await session.flush()
        status = await materialize_from_intake(session, intake)
        await session.commit()
        assert status == "closing"

        inv = await session.get(SupplierInvoice, intake.invoice_id)
        assert inv is not None and inv.payment_status == "paid"
        assert await _receivable(session, cp.id) == Decimal("0.00")
        assert await _payable(session, cp.id) == Decimal("0.00")

        await confirm_intake_with_review(
            session, intake, actor_user_id=None, counterparty_id=cp.id, amount="1000.00"
        )
        await session.commit()

        await session.refresh(inv)
        assert inv.amount == Decimal("1000.00")
        assert inv.payment_status == "partially_paid", (
            f"статус {inv.payment_status!r}: после увеличения суммы проведённого УПД зачтено "
            "400 из 1000 — документ обязан стать partially_paid, иначе остаток невидим для КЗ"
        )
        payable = await _payable(session, cp.id)
        assert payable == Decimal("600.00"), (
            f"КЗ {payable}: непокрытые 600 по УПД должны быть кредиторкой"
        )


async def test_reparse_date_reapplies_rule4_activation(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Подозрение 1 (дата): пере-разбор уводит дату закрывающего в будущее — правило 4.

    УПД прошлой датой активен (КЗ 1000). Оператор исправляет дату на будущую (ЭкоЦентр-кейс:
    УПД датирован 31-м числом будущего месяца). Канон (правило 4): до своей даты УПД — не
    обязательство, activation_status='pending', в КЗ не входит. Баг: активация не
    пересчитывается, будущий УПД остаётся в КЗ. Обратная правка (будущее→прошлое) обязана
    вернуть документ в КЗ сразу, не дожидаясь ночной джобы."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Email-переразбор-дата", inn="6155990102")
        intake = _make_intake(
            counterparty_id=cp.id,
            amount="1000.00",
            document_kind="upd",
            invoice_number="УПД-РЕПАРС-2",
            invoice_date=PAST,
        )
        session.add(intake)
        await session.flush()
        await materialize_from_intake(session, intake)
        await session.commit()

        inv = await session.get(SupplierInvoice, intake.invoice_id)
        assert inv is not None and inv.activation_status == "active"
        assert await _payable(session, cp.id) == Decimal("1000.00")

        await confirm_intake_with_review(
            session,
            intake,
            actor_user_id=None,
            counterparty_id=cp.id,
            invoice_date=FUTURE.isoformat(),
        )
        await session.commit()
        await session.refresh(inv)
        assert inv.invoice_date == FUTURE
        assert inv.activation_status == "pending", (
            f"activation {inv.activation_status!r}: УПД будущей датой обязан уйти в pending "
            "(правило 4) — до своей даты он не обязательство"
        )
        assert await _payable(session, cp.id) == Decimal("0.00"), (
            "КЗ: будущий УПД не должен сидеть в кредиторке"
        )

        await confirm_intake_with_review(
            session,
            intake,
            actor_user_id=None,
            counterparty_id=cp.id,
            invoice_date=PAST.isoformat(),
        )
        await session.commit()
        await session.refresh(inv)
        assert inv.activation_status == "active", (
            f"activation {inv.activation_status!r}: дата вернулась в прошлое — документ обязан "
            "активироваться сразу, а не ждать ночную джобу"
        )
        assert await _payable(session, cp.id) == Decimal("1000.00")


async def test_restore_intake_recomputes_status_from_allocations(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Подозрение 2: restore возвращает void→unpaid, игнорируя фактические оплаты.

    Счёт 1000 частично оплачен из кошелька (400) → partially_paid. Исключили (void),
    вернули из «Исключённых». Канон: статус пересчитывается по аллокациям → partially_paid.
    Баг: голое присвоение 'unpaid' — счёт выглядит полностью неоплаченным (риск повторной
    оплаты полной суммы), ДЗ по оплаченной части при этом уже существует."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Email-restore-статус", inn="6155990103")
        wallet = await make_wallet(session, name="Сейф-restore", wallet_type="cash_safe")
        article = await make_expense_article(session)
        intake = _make_intake(
            counterparty_id=cp.id,
            amount="1000.00",
            document_kind="invoice",
            invoice_number="СЧ-RESTORE-1",
            invoice_date=PAST,
        )
        session.add(intake)
        await session.flush()
        status = await materialize_from_intake(session, intake)
        assert status == "linked"
        await session.commit()

        from app.services.counterparty_payments import pay_invoice_from_wallet

        await pay_invoice_from_wallet(
            session,
            invoice_id=intake.invoice_id,
            wallet_id=wallet.id,
            amount=Decimal("400.00"),
            operation_date=PAST,
            article_id=article.id,
        )
        inv = await session.get(SupplierInvoice, intake.invoice_id)
        assert inv is not None and inv.payment_status == "partially_paid"
        assert await _receivable(session, cp.id) == Decimal("400.00")

        await exclude_intake(session, intake)
        await session.commit()
        await session.refresh(inv)
        assert inv.payment_status == "void"

        await restore_intake(session, intake)
        await session.commit()
        await session.refresh(inv)
        assert inv.payment_status == "partially_paid", (
            f"статус {inv.payment_status!r}: счёт с оплаченными 400 из 1000 после возврата из "
            "«Исключённых» обязан быть partially_paid, а не считаться полностью неоплаченным"
        )
        assert await _receivable(session, cp.id) == Decimal("400.00"), (
            "ДЗ по оплаченной части счёта должна пережить исключение/возврат"
        )


async def test_exclude_closing_releases_prepayment_and_restore_resettles(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Подозрение 3 (исключение): void-закрывающий не должен продолжать «съедать» аванс.

    Аванс 400 (ДЗ 400) → УПД на 1000 зачёл его целиком (ДЗ 0, КЗ 600). Оператор исключает
    intake → УПД void, из КЗ уходит. Канон («ДЗ следует за деньгами»): документа-основания
    больше нет, деньги аванса по-прежнему у поставщика → зачёт возвращается, ДЗ снова 400.
    Баг: аллокация и amount_settled остаются — ДЗ занижена, пока документ в корзине.
    Возврат из «Исключённых» обязан симметрично пере-зачесть аванс (ДЗ 0, КЗ 600)."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Email-exclude-аванс", inn="6155990104")
        await session.commit()
        await create_opening_prepayment(session, counterparty_id=cp.id, amount=Decimal("400.00"))

        intake = _make_intake(
            counterparty_id=cp.id,
            amount="1000.00",
            document_kind="upd",
            invoice_number="УПД-EXCL-1",
            invoice_date=PAST,
        )
        session.add(intake)
        await session.flush()
        await materialize_from_intake(session, intake)
        await session.commit()

        inv = await session.get(SupplierInvoice, intake.invoice_id)
        assert inv is not None and inv.payment_status == "partially_paid"
        assert await _receivable(session, cp.id) == Decimal("0.00")
        assert await _payable(session, cp.id) == Decimal("600.00")

        await exclude_intake(session, intake)
        await session.commit()
        await session.refresh(inv)
        assert inv.payment_status == "void"
        receivable = await _receivable(session, cp.id)
        assert receivable == Decimal("400.00"), (
            f"ДЗ {receivable}: аннулированный УПД не может продолжать гасить аванс — деньги "
            "у поставщика, закрывающего документа нет"
        )

        await restore_intake(session, intake)
        await session.commit()
        await session.refresh(inv)
        assert inv.payment_status == "partially_paid"
        assert await _receivable(session, cp.id) == Decimal("0.00"), (
            "возврат из «Исключённых» обязан пере-зачесть аванс (правило 2)"
        )
        assert await _payable(session, cp.id) == Decimal("600.00")


async def test_delete_intake_returns_consumed_prepayment(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Подозрение 3 (удаление): каскад аллокаций без возврата списанной предоплаты.

    Аванс 400 → УПД на 1000 зачёл его (ДЗ 0). Intake исключили и удалили НАВСЕГДА: накладная
    удаляется, аллокация умирает каскадом (FK CASCADE), а amount_settled предоплаты никто не
    возвращает. Канон: деньги аванса ушли реально, документа-основания больше нет → ДЗ 400.
    Баг: остаток предоплаты занижен безвозвратно (ДЗ 0 навсегда)."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Email-delete-аванс", inn="6155990105")
        await session.commit()
        prepayment = await create_opening_prepayment(
            session, counterparty_id=cp.id, amount=Decimal("400.00")
        )

        intake = _make_intake(
            counterparty_id=cp.id,
            amount="1000.00",
            document_kind="upd",
            invoice_number="УПД-DEL-1",
            invoice_date=PAST,
        )
        session.add(intake)
        await session.flush()
        await materialize_from_intake(session, intake)
        await session.commit()
        invoice_id = intake.invoice_id
        assert await _receivable(session, cp.id) == Decimal("0.00")

        await exclude_intake(session, intake)
        await session.commit()
        await delete_intake_forever(session, intake)
        await session.commit()

        assert await session.get(SupplierInvoice, invoice_id) is None, "накладная удалена"
        row = await session.get(SupplierPrepayment, prepayment.id)
        assert row is not None
        await session.refresh(row)
        assert Decimal(str(row.amount_settled)) == Decimal("0.00"), (
            f"amount_settled {row.amount_settled}: удалённый УПД обязан вернуть списанный "
            "аванс — иначе остаток предоплаты потерян безвозвратно"
        )
        assert row.status == "open"
        receivable = await _receivable(session, cp.id)
        assert receivable == Decimal("400.00"), (
            f"ДЗ {receivable}: деньги аванса реально ушли, закрывающего документа больше нет"
        )
