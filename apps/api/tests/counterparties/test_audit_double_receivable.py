"""АУДИТ-4: подозрение на ЗАДВОЕНИЕ ДЕБИТОРКИ при штатном порядке банк-фида.

Порядок «classify-then-match» (как реально работает банк-фид):
1. Банк-операция приходит в фид → классификатор заводит ДДС-проводку с контрагентом и
   БЕЗУСЛОВНО зовёт ``ensure_prepayment_from_bank_transaction`` (правило 1). Открытых
   закрывающих нет → весь платёж становится дебиторкой (kind='subscription').
2. Оператор в сверке привязывает ТУ ЖЕ операцию к открытому СЧЁТУ (doc_kind='bill') →
   ``confirm_invoice_match`` → аллокация → ``_recompute_status`` → единый чокпоинт
   ``reconcile_bill_prepayment`` заводит ВТОРУЮ дебиторку (kind='prepaid_bill').

Обе записи живут по разным ключам (cashflow_transaction_id vs bill_invoice_id) и друг о
друге не знают → ДЗ = 2× платежа.

Тесты канон-агента этот порядок не покрывают: их банк-операции идут БЕЗ ДДС-проводки, поэтому
правило 1 в них не срабатывает и вторая дебиторка не появляется.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from cp_helpers import make_bank_operation, make_counterparty, make_invoice, make_wallet
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    CashflowTransaction,
    InvoicePaymentAllocation,
    SupplierInvoice,
    SupplierPrepayment,
)
from app.services.supplier_prepayments import (
    apply_closing_document,
    ensure_prepayment_from_bank_transaction,
)


async def _classified_payment(
    session: AsyncSession,
    *,
    counterparty_id: uuid.UUID,
    amount: str,
    inn: str,
    operation_date: date = date(2026, 7, 15),
):
    """Банк-операция + её ДДС-проводка — ровно то, что оставляет за собой классификатор
    (``classifier.py``: создаёт CashflowTransaction, ставит
    ``operation.cashflow_transaction_id``)."""
    wallet = await make_wallet(session, name=f"Банк-{inn}", wallet_type="bank")
    operation = await make_bank_operation(
        session,
        amount=amount,
        direction="out",
        inn=inn,
        operation_date=operation_date,
        classification_status="classified",
    )
    tx = CashflowTransaction(
        wallet_id=wallet.id,
        direction="out",
        amount=Decimal(amount),
        operation_date=operation_date,
        counterparty_id=counterparty_id,
        source_kind="bank_operation",
        source_id=operation.id,
        payment_purpose="Оплата по счёту",
        quality_status="auto",
    )
    session.add(tx)
    await session.flush()
    operation.cashflow_transaction_id = tx.id
    await session.flush()
    return operation, tx


async def _receivable_total(session: AsyncSession, counterparty_id: uuid.UUID) -> Decimal:
    """Дебиторка контрагента так, как её считает плитка дашборда: открытые предоплаты
    (amount − amount_settled) без фильтра по kind."""
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


async def test_classify_then_match_bill_does_not_double_receivable(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """ШТАТНЫЙ порядок: платёж классифицирован (правило 1 → ДЗ), затем оператор привязал ту же
    операцию к счёту (чокпоинт → ДЗ по счёту). Дебиторка обязана остаться 1000, а не 2000."""
    from app.services.counterparty_bank_match import confirm_invoice_match

    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Задвоение-ДЗ", inn="6155990001")
        bill = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="1000.00",
            doc_kind="bill",
            number="СЧ-999",
            invoice_date=date(2026, 7, 1),
        )
        await session.commit()

        operation, tx = await _classified_payment(
            session, counterparty_id=cp.id, amount="1000.00", inn="6155990001"
        )

        # Шаг 1 — классификатор: правило 1. Открытых закрывающих нет → вся сумма в дебиторку.
        rule1 = await ensure_prepayment_from_bank_transaction(session, tx)
        await session.commit()
        assert rule1 is not None
        assert rule1.amount == Decimal("1000.00")
        assert rule1.kind == "subscription"
        assert await _receivable_total(session, cp.id) == Decimal("1000.00")

        # Шаг 2 — оператор в сверке привязывает ЭТУ ЖЕ операцию к счёту.
        await confirm_invoice_match(
            session,
            invoice_id=bill.id,
            bank_operation_id=operation.id,
            enrich=False,
            actor_user_id=None,
        )

        prepayments = (
            await session.scalars(
                select(SupplierPrepayment).where(SupplierPrepayment.counterparty_id == cp.id)
            )
        ).all()
        detail = [
            f"{p.kind} amount={p.amount} settled={p.amount_settled} status={p.status} "
            f"tx={p.cashflow_transaction_id} bill={p.bill_invoice_id}"
            for p in prepayments
        ]
        total = await _receivable_total(session, cp.id)
        assert total == Decimal("1000.00"), (
            f"ЗАДВОЕНИЕ ДЕБИТОРКИ: один платёж 1000 дал ДЗ {total}. Записи: {detail}"
        )


async def test_classify_then_match_then_upd_leaves_no_phantom(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Продолжение цикла: после задвоения приходит закрывающий УПД на 1000. Он гасит ОДНУ из
    двух дебиторок — вторая остаётся фантомом навсегда. Итог обязан быть 0/0."""
    from app.services.counterparty_bank_match import confirm_invoice_match

    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Задвоение-цикл", inn="6155990002")
        bill = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="1000.00",
            doc_kind="bill",
            number="СЧ-998",
            invoice_date=date(2026, 7, 1),
        )
        await session.commit()

        operation, tx = await _classified_payment(
            session, counterparty_id=cp.id, amount="1000.00", inn="6155990002"
        )
        await ensure_prepayment_from_bank_transaction(session, tx)
        await session.commit()
        await confirm_invoice_match(
            session,
            invoice_id=bill.id,
            bank_operation_id=operation.id,
            enrich=False,
            actor_user_id=None,
        )

        upd = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="1000.00",
            doc_kind="closing",
            number="УПД-998",
            invoice_date=date(2026, 7, 20),
        )
        await apply_closing_document(session, upd, as_of=date(2026, 7, 21))
        await session.commit()

        total = await _receivable_total(session, cp.id)
        assert (await session.get(SupplierInvoice, upd.id)).payment_status == "paid"
        assert total == Decimal("0.00"), (
            f"ФАНТОМНАЯ ДЕБИТОРКА после полного цикла (счёт оплачен, УПД пришёл): ДЗ {total}"
        )


async def test_manual_cash_allocation_to_bill_survives_reclassification(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Смежное подозрение: ``_unwind_transaction_kz_settlements`` снимает ВСЕ cash-аллокации
    проводки без фильтра doc_kind, а ``allocate_cash_to_invoice`` заводит именно такие по счёту.
    Повторный прогон правила 1 не должен сносить ручную оплату счёта оператором."""
    from app.services.counterparty_matching import allocate_cash_to_invoice

    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Нал-счёт-реклас", inn="6155990003")
        bill = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="700.00",
            doc_kind="bill",
            number="СЧ-997",
            invoice_date=date(2026, 7, 1),
        )
        await session.commit()
        _, tx = await _classified_payment(
            session, counterparty_id=cp.id, amount="700.00", inn="6155990003"
        )
        await session.commit()

        # Оператор вручную разнёс наличную/банковскую проводку на счёт.
        await allocate_cash_to_invoice(
            session,
            invoice_id=bill.id,
            amount=Decimal("700.00"),
            cashflow_transaction_id=tx.id,
        )
        assert (await session.get(SupplierInvoice, bill.id)).payment_status == "paid"

        # Повторная классификация той же проводки (правки разметки, повторный прогон правил).
        await ensure_prepayment_from_bank_transaction(session, tx)
        await session.commit()

        invoice = await session.get(SupplierInvoice, bill.id)
        assert invoice.payment_status == "paid", (
            "Повторная классификация снесла ручную аллокацию оператора по счёту — "
            f"счёт снова {invoice.payment_status}"
        )
        total = await _receivable_total(session, cp.id)
        assert total == Decimal("700.00"), f"Дебиторка по оплаченному счёту {total}, ожидалось 700"


async def test_split_bill_and_rule1_share_one_payment(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Частичный случай: операция 1000 привязана к счёту на 400 → ДЗ должна остаться 1000
    суммарно (400 «по счёту» + 600 свободных), а не 1400."""
    from app.services.counterparty_bank_match import confirm_invoice_match

    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Задвоение-частич", inn="6155990004")
        bill = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="400.00",
            doc_kind="bill",
            number="СЧ-996",
            invoice_date=date(2026, 7, 1),
        )
        await session.commit()
        operation, tx = await _classified_payment(
            session, counterparty_id=cp.id, amount="1000.00", inn="6155990004"
        )
        await ensure_prepayment_from_bank_transaction(session, tx)
        await session.commit()

        await confirm_invoice_match(
            session,
            invoice_id=bill.id,
            bank_operation_id=operation.id,
            enrich=False,
            actor_user_id=None,
        )

        total = await _receivable_total(session, cp.id)
        count = await session.scalar(
            select(func.count())
            .select_from(SupplierPrepayment)
            .where(SupplierPrepayment.counterparty_id == cp.id)
        )
        assert total == Decimal("1000.00"), (
            f"ДЗ {total} при платеже 1000 (счёт 400 + свободные 600); записей предоплат: {count}"
        )


async def test_one_payment_cannot_fund_more_than_itself(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """ПЕРЕРАСХОД ПЛАТЕЖА: «занятость» банк-операции проверяется по ``bank_operation_id``
    (``_op_already_allocated``), а правило 1 метит свои зачёты ``cashflow_transaction_id`` —
    операция выглядит свободной, хотя её деньги уже погасили кредиторку.

    Платёж 1000 при открытой КЗ 300 и счёте 1000: правило 1 гасит закрывающий на 300, затем
    оператор привязывает ту же операцию к счёту на 1000 → документов погашено на 1300."""
    from app.services.counterparty_bank_match import confirm_invoice_match

    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Перерасход", inn="6155990005")
        closing = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="300.00",
            doc_kind="closing",
            number="УПД-995",
            invoice_date=date(2026, 6, 1),
        )
        bill = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="1000.00",
            doc_kind="bill",
            number="СЧ-995",
            invoice_date=date(2026, 7, 1),
        )
        await session.commit()

        operation, tx = await _classified_payment(
            session, counterparty_id=cp.id, amount="1000.00", inn="6155990005"
        )
        await ensure_prepayment_from_bank_transaction(session, tx)
        await session.commit()
        assert (await session.get(SupplierInvoice, closing.id)).payment_status == "paid"

        await confirm_invoice_match(
            session,
            invoice_id=bill.id,
            bank_operation_id=operation.id,
            enrich=False,
            actor_user_id=None,
        )

        funded = await session.scalar(
            select(func.coalesce(func.sum(InvoicePaymentAllocation.amount), 0)).where(
                InvoicePaymentAllocation.source_kind.in_(("cash", "bank")),
                (InvoicePaymentAllocation.cashflow_transaction_id == tx.id)
                | (InvoicePaymentAllocation.bank_operation_id == operation.id),
            )
        )
        assert Decimal(str(funded)) <= Decimal("1000.00"), (
            f"ПЕРЕРАСХОД: платёж 1000 профинансировал документов на {funded}"
        )


async def test_bill_touched_after_upd_does_not_resurrect_receivable(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Регресс на край САМОГО фикса: после того как УПД погасил rule-1-дебиторку, она уходит из
    «открытых». Повторное касание счёта (любая дверь зовёт _recompute_status) не должно счесть
    деньги непокрытыми и завести дебиторку заново."""
    from app.services.counterparty_bank_match import confirm_invoice_match
    from app.services.counterparty_matching import _recompute_status

    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Повторное-касание", inn="6155990006")
        bill = await make_invoice(
            session, counterparty_id=cp.id, amount="1000.00", doc_kind="bill",
            number="СЧ-994", invoice_date=date(2026, 7, 1),
        )
        await session.commit()
        operation, tx = await _classified_payment(
            session, counterparty_id=cp.id, amount="1000.00", inn="6155990006"
        )
        await ensure_prepayment_from_bank_transaction(session, tx)
        await session.commit()
        await confirm_invoice_match(
            session, invoice_id=bill.id, bank_operation_id=operation.id,
            enrich=False, actor_user_id=None,
        )
        upd = await make_invoice(
            session, counterparty_id=cp.id, amount="1000.00", doc_kind="closing",
            number="УПД-994", invoice_date=date(2026, 7, 20),
        )
        await apply_closing_document(session, upd, as_of=date(2026, 7, 21))
        await session.commit()
        assert await _receivable_total(session, cp.id) == Decimal("0.00")

        # Счёт трогают снова — например пере-разбор операции или правка накладной.
        await _recompute_status(session, await session.get(SupplierInvoice, bill.id))
        await session.commit()

        total = await _receivable_total(session, cp.id)
        assert total == Decimal("0.00"), (
            f"Повторное касание счёта воскресило дебиторку {total} на уже отработанные деньги"
        )
