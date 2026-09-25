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
            operational_scope="finance",
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
            operational_scope="finance",
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
            session,
            counterparty_id=cp.id,
            amount="1000.00",
            doc_kind="bill",
            number="СЧ-994",
            invoice_date=date(2026, 7, 1),
        )
        await session.commit()
        operation, tx = await _classified_payment(
            session, counterparty_id=cp.id, amount="1000.00", inn="6155990006"
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
            number="УПД-994",
            invoice_date=date(2026, 7, 20),
            operational_scope="finance",
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


async def _bill_receivable(session: AsyncSession, bill_id: uuid.UUID) -> Decimal | None:
    prepayment = await session.scalar(
        select(SupplierPrepayment).where(SupplierPrepayment.bill_invoice_id == bill_id)
    )
    return None if prepayment is None else Decimal(str(prepayment.amount))


async def _pay_bill_directly(
    session: AsyncSession, bill: SupplierInvoice, *, amount: str
) -> CashflowTransaction:
    """Оплата счёта дверью, которая правило 1 не зовёт (ручная оплата с кошелька)."""
    from app.services.counterparty_matching import _recompute_status

    wallet = await make_wallet(session, name=f"Касса-{amount}", wallet_type="bank")
    tx = CashflowTransaction(
        wallet_id=wallet.id,
        direction="out",
        amount=Decimal(amount),
        operation_date=date(2026, 8, 5),
        counterparty_id=bill.counterparty_id,
        source_kind="manual",
        quality_status="auto",
    )
    session.add(tx)
    await session.flush()
    session.add(
        InvoicePaymentAllocation(
            invoice_id=bill.id,
            source_kind="cash",
            cashflow_transaction_id=tx.id,
            amount=Decimal(amount),
        )
    )
    await session.flush()
    await _recompute_status(session, bill)
    await session.commit()
    return tx


async def test_touching_a_bill_does_not_take_money_carried_by_rule1(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Повторное касание счёта не забирает в ДЗ по счёту деньги аванса правила 1.

    Счёт 10 000: первые 4 000 — классифицированный платёж (его деньги несёт аванс правила 1),
    потом оператор привязал его к счёту — своей ДЗ счёт не завёл. Доплату 6 000 провели
    дверью без правила 1 — ДЗ по счёту 6 000. Прежний чокпоинт уступал правилу 1 только при
    СОЗДАНИИ записи, и следующее касание счёта растило её до всей оплаты: 14 000 на 10 000.
    """
    from app.services.counterparty_bank_match import confirm_invoice_match
    from app.services.counterparty_matching import _recompute_status

    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Касание-счёта", inn="6155990007")
        bill = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="10000.00",
            doc_kind="bill",
            number="СЧ-993",
            invoice_date=date(2026, 7, 1),
        )
        await session.commit()
        operation, tx = await _classified_payment(
            session, counterparty_id=cp.id, amount="4000.00", inn="6155990007"
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
        assert await _bill_receivable(session, bill.id) is None
        await _pay_bill_directly(session, bill, amount="6000.00")
        assert await _bill_receivable(session, bill.id) == Decimal("6000.00")

        await _recompute_status(session, await session.get(SupplierInvoice, bill.id))
        await session.commit()

        assert await _bill_receivable(session, bill.id) == Decimal("6000.00")
        assert await _receivable_total(session, cp.id) == Decimal("10000.00")


async def test_classified_payment_matched_to_a_bill_with_own_receivable(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Естественный порядок без повторного касания и пересборка аванса после него.

    Счёт 10 000 уже несёт свою ДЗ 6 000 (доплата дверью без правила 1). Второй платёж 4 000
    классифицирован (аванс правила 1 на всю сумму) и привязан к тому же счёту: прежний
    чокпоинт брал в ДЗ по счёту всю оплату — 14 000. А когда правило 1 затем пересобирает
    аванс (правка проводки), счёт со своей ДЗ оно себе не берёт — аванс исчезает, и его
    деньги обязана подхватить ДЗ по счёту, иначе дебиторка проседает до 6 000.
    """
    from app.services.counterparty_bank_match import confirm_invoice_match

    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Счёт-со-своей-ДЗ", inn="6155990008")
        bill = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="10000.00",
            doc_kind="bill",
            number="СЧ-992",
            invoice_date=date(2026, 7, 1),
        )
        await session.commit()
        await _pay_bill_directly(session, bill, amount="6000.00")
        assert await _bill_receivable(session, bill.id) == Decimal("6000.00")

        operation, tx = await _classified_payment(
            session, counterparty_id=cp.id, amount="4000.00", inn="6155990008"
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
        assert await _receivable_total(session, cp.id) == Decimal("10000.00")

        await ensure_prepayment_from_bank_transaction(session, tx)
        await session.commit()
        rule1 = await session.scalar(
            select(SupplierPrepayment).where(SupplierPrepayment.cashflow_transaction_id == tx.id)
        )
        assert rule1 is None, "счёт со своей ДЗ правило 1 себе не берёт"
        assert await _bill_receivable(session, bill.id) == Decimal("10000.00")
        assert await _receivable_total(session, cp.id) == Decimal("10000.00")


async def test_rule1_remainder_of_a_bigger_payment_survives_touching_the_bill(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Аванс-остаток платежа больше счёта денег счёта не несёт — и касание это помнит.

    Платёж 15 000 сначала привязали к счёту на 10 000 (ДЗ по счёту 10 000), потом правило 1
    сделало остаток 5 000 своим авансом. Вычти аванс из денег счёта при касании — и ДЗ по счёту
    усохла бы до 5 000: 10 000 дебиторки на 15 000 денег.
    """
    from app.services.counterparty_bank_match import confirm_invoice_match
    from app.services.counterparty_matching import _recompute_status

    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Остаток-платежа", inn="6155990009")
        bill = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="10000.00",
            doc_kind="bill",
            number="СЧ-991",
            invoice_date=date(2026, 7, 1),
        )
        await session.commit()
        operation, tx = await _classified_payment(
            session, counterparty_id=cp.id, amount="15000.00", inn="6155990009"
        )
        await confirm_invoice_match(
            session,
            invoice_id=bill.id,
            bank_operation_id=operation.id,
            enrich=False,
            actor_user_id=None,
        )
        rule1 = await ensure_prepayment_from_bank_transaction(session, tx)
        await session.commit()
        assert rule1 is not None and rule1.amount == Decimal("5000.00")
        assert await _bill_receivable(session, bill.id) == Decimal("10000.00")

        await _recompute_status(session, await session.get(SupplierInvoice, bill.id))
        await session.commit()

        assert await _bill_receivable(session, bill.id) == Decimal("10000.00")
        assert await _receivable_total(session, cp.id) == Decimal("15000.00")


async def _assert_balance_matches_ledger(session: AsyncSession, counterparty_id: uuid.UUID) -> None:
    """Баланс на дату (зеркало чокпоинта в SQL) видит ту же дебиторку, что леджер предоплат."""
    from app.services.counterparty_balance_as_of import build_balance_as_of

    sheet = await build_balance_as_of(session, as_of=date(2026, 12, 31))
    row = next((r for r in sheet.rows if r.counterparty_id == counterparty_id), None)
    receivable = row.receivable if row is not None else Decimal("0.00")
    assert receivable == await _receivable_total(session, counterparty_id), (
        "баланс на дату разошёлся с леджером"
    )


async def test_reassigned_payment_does_not_carry_another_counterpartys_bill_money(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Скептик 25.09 (п. 4): перевес платежа на другого контрагента не уносит деньги чужого счёта.

    Платёж 10 000 классифицирован на X, 4 000 из него оператор привязал к счёту X — деньги счёта
    несёт аванс правила 1 (своей ДЗ у счёта нет). Потом платёж перевесили на Y. Ручную оплату
    счёта правило 1 не снимает, но и нести её деньги аванс Y не вправе: у X оплаченный счёт без
    дебиторки, у Y — аванс на 4 000 больше денег платежа, оставшихся ему."""
    from app.services.counterparty_matching import allocate_cash_to_invoice

    async with async_session_factory() as session:
        x = await make_counterparty(session, name="Перевес-X", inn="6155990013")
        y = await make_counterparty(session, name="Перевес-Y", inn="6155990014")
        bill = await make_invoice(
            session,
            counterparty_id=x.id,
            amount="4000.00",
            doc_kind="bill",
            number="СЧ-986",
            invoice_date=date(2026, 7, 1),
        )
        await session.commit()
        _, tx = await _classified_payment(
            session, counterparty_id=x.id, amount="10000.00", inn="6155990013"
        )
        await ensure_prepayment_from_bank_transaction(session, tx)
        await session.commit()
        await allocate_cash_to_invoice(
            session, invoice_id=bill.id, amount=Decimal("4000.00"), cashflow_transaction_id=tx.id
        )
        assert await _bill_receivable(session, bill.id) is None
        assert await _receivable_total(session, x.id) == Decimal("10000.00")

        tx = await session.get(CashflowTransaction, tx.id)
        tx.counterparty_id = y.id
        await session.flush()
        await ensure_prepayment_from_bank_transaction(session, tx)
        await session.commit()

        assert await _bill_receivable(session, bill.id) == Decimal("4000.00"), (
            "деньги оплаченного счёта X выпали из его дебиторки"
        )
        assert await _receivable_total(session, x.id) == Decimal("4000.00")
        assert await _receivable_total(session, y.id) == Decimal("6000.00"), (
            "аванс Y унёс деньги счёта X"
        )
        await _assert_balance_matches_ledger(session, x.id)
        await _assert_balance_matches_ledger(session, y.id)


async def _bill_paid_by_rule1_and_a_bank_top_up(
    session: AsyncSession, *, inn: str, name: str, act_amount: str
) -> tuple[SupplierInvoice, SupplierInvoice, SupplierPrepayment, uuid.UUID]:
    """Счёт 10 000: первые 5 000 несёт аванс правила 1, доплату 5 000 — ДЗ по счёту (сверка
    второй операции). Потом приходит акт и гасится обеими."""
    from app.services.counterparty_bank_match import confirm_invoice_match

    cp = await make_counterparty(session, name=name, inn=inn)
    bill = await make_invoice(
        session,
        counterparty_id=cp.id,
        amount="10000.00",
        doc_kind="bill",
        number=f"СЧ-{inn[-3:]}",
        invoice_date=date(2026, 7, 1),
    )
    await session.commit()
    first, tx = await _classified_payment(session, counterparty_id=cp.id, amount="5000.00", inn=inn)
    rule1 = await ensure_prepayment_from_bank_transaction(session, tx)
    await session.commit()
    assert rule1 is not None
    await confirm_invoice_match(
        session, invoice_id=bill.id, bank_operation_id=first.id, enrich=False, actor_user_id=None
    )
    assert await _bill_receivable(session, bill.id) is None
    top_up = await make_bank_operation(
        session,
        amount="5000.00",
        direction="out",
        inn=inn,
        operation_date=date(2026, 7, 16),
        classification_status="classified",
    )
    await session.commit()
    await confirm_invoice_match(
        session, invoice_id=bill.id, bank_operation_id=top_up.id, enrich=False, actor_user_id=None
    )
    assert await _bill_receivable(session, bill.id) == Decimal("5000.00")
    act = await make_invoice(
        session,
        counterparty_id=cp.id,
        amount=act_amount,
        doc_kind="closing",
        number=f"АКТ-{inn[-3:]}",
        invoice_date=date(2026, 7, 20),
        operational_scope="finance",
    )
    await apply_closing_document(session, act, as_of=date(2026, 7, 21))
    await session.commit()
    return bill, act, rule1, top_up.id


async def _act_paid(session: AsyncSession, act_id: uuid.UUID) -> Decimal:
    return Decimal(
        str(
            await session.scalar(
                select(func.coalesce(func.sum(InvoicePaymentAllocation.amount), 0)).where(
                    InvoicePaymentAllocation.invoice_id == act_id
                )
            )
        )
    )


async def test_rolled_back_bill_top_up_does_not_leave_the_act_paid_twice(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Скептик 25.09 (п. 5): откат доплаты счёта не оставляет акт закрытым одними деньгами дважды.

    Акт 10 000 погашен авансом правила 1 (первая оплата счёта) и ДЗ по счёту (доплата). Доплату
    исключили: у счёта оплачено 5 000, и все их несёт аванс — своей ДЗ счёт больше не держит.
    Откат зачётов целился в ОПЛАЧЕННОЕ (5 000 — «зачтено не больше оплаты»), а не в то, что
    ДЗ по счёту несёт (0): ДЗ усаживалась до зачтённых 5 000 и оставалась на акте, акт —
    оплаченным 10 000 при 5 000 денег. Теперь зачёт доплаты снимается, акт — кредиторка 5 000."""
    from app.services.supplier_prepayments import unwind_operation_bank_allocations

    async with async_session_factory() as session:
        bill, act, rule1, top_up_id = await _bill_paid_by_rule1_and_a_bank_top_up(
            session, inn="6155990015", name="Откат-доплаты", act_amount="10000.00"
        )
        await session.refresh(rule1)
        assert act.payment_status == "paid"
        assert rule1.amount_settled == Decimal("5000.00")

        assert await unwind_operation_bank_allocations(session, top_up_id)
        await session.commit()

        assert await _bill_receivable(session, bill.id) is None
        assert await _act_paid(session, act.id) == Decimal("5000.00"), (
            "акт остался закрытым деньгами, которых нет"
        )
        assert (await session.get(SupplierInvoice, act.id)).payment_status == "partially_paid"
        assert await _receivable_total(session, bill.counterparty_id) == Decimal("0.00")
        await _assert_balance_matches_ledger(session, bill.counterparty_id)


async def test_rolled_back_bill_top_up_leaves_the_act_a_payable_not_a_phantom(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Снятый зачёт не оставляет фантомной дебиторки, но и не перегашается сам.

    Акт 5 000 погашен ДЗ по счёту (доплата), аванс правила 1 (первая оплата) открыт. Доплату
    исключили — деньги счёта теперь только в авансе. Прежде ДЗ по счёту оставалась на акте, а
    аванс висел открытым: дебиторка 5 000 при оплаченном акте — нетто врало на 5 000. Теперь
    зачёт снят: акт — кредиторка 5 000, аванс — дебиторка 5 000, нетто ноль. Перегасить акт
    авансом — решение человека: автоматическое перегашение из отката брало аванс проводки,
    которую в этот момент выводят из учёта (скептик Fable, F1)."""
    from app.services.supplier_prepayments import unwind_operation_bank_allocations

    async with async_session_factory() as session:
        bill, act, rule1, top_up_id = await _bill_paid_by_rule1_and_a_bank_top_up(
            session, inn="6155990016", name="Откат-кредиторка", act_amount="5000.00"
        )
        assert act.payment_status == "paid"

        assert await unwind_operation_bank_allocations(session, top_up_id)
        await session.commit()

        assert await _bill_receivable(session, bill.id) is None
        assert await _act_paid(session, act.id) == Decimal("0.00")
        assert (await session.get(SupplierInvoice, act.id)).payment_status == "unpaid"
        await session.refresh(rule1)
        assert rule1.status == "open" and rule1.amount_settled == Decimal("0.00")
        assert await _receivable_total(session, bill.counterparty_id) == Decimal("5000.00")
        await _assert_balance_matches_ledger(session, bill.counterparty_id)


async def test_excluding_a_matched_operation_does_not_resettle_the_act_with_its_own_advance(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Скептик Fable F1: исключённая операция не оставляет акт оплаченным своими же деньгами.

    Операцию 15 000 сверили со счётом 10 000 до правила 1 (ДЗ по счёту 10 000), акт 10 000
    погашен этой ДЗ, остаток 5 000 правило 1 сделало авансом. «Исключить операцию» снимает
    сверку, откат зачёта возвращает акт в кредиторку — и перегашение из открытых авансов брало
    аванс исключаемой операции: тронутый, он переживал снос нетронутых, и акт оставался
    оплаченным на 5 000 деньгами, которых в учёте нет."""
    from app.services.banking.classifier import apply_operation_action
    from app.services.counterparty_bank_match import confirm_invoice_match

    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Исключение-сверки", inn="6155990017")
        bill = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="10000.00",
            doc_kind="bill",
            number="СЧ-983",
            invoice_date=date(2026, 7, 1),
        )
        await session.commit()
        operation, tx = await _classified_payment(
            session, counterparty_id=cp.id, amount="15000.00", inn="6155990017"
        )
        await confirm_invoice_match(
            session,
            invoice_id=bill.id,
            bank_operation_id=operation.id,
            enrich=False,
            actor_user_id=None,
        )
        act = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="10000.00",
            doc_kind="closing",
            number="АКТ-983",
            invoice_date=date(2026, 7, 20),
            operational_scope="finance",
        )
        await apply_closing_document(session, act, as_of=date(2026, 7, 21))
        rule1 = await ensure_prepayment_from_bank_transaction(session, tx)
        await session.commit()
        assert rule1 is not None and rule1.amount == Decimal("5000.00")
        assert act.payment_status == "paid"
        act_id, cp_id = act.id, cp.id

        await apply_operation_action(session, operation, action="exclude")
        await session.commit()

        assert await _act_paid(session, act_id) == Decimal("0.00"), (
            "акт оплачен деньгами исключённой операции"
        )
        assert await _receivable_total(session, cp_id) == Decimal("0.00")


async def test_bill_can_be_matched_to_a_payment_whose_rule1_advance_went_to_its_upd(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Скептик Fable F2: счёт привязывается к платежу, чей аванс уже погасил УПД.

    Платёж 10 000 классифицирован — аванс правила 1; УПД 10 000 погасил аванс; счёт пришёл
    позже. Деньги счёта аванс несёт и так — счёт лишь называет, за что заплачено, и путь «счёт →
    УПД» законен. Потолок по остатку аванса не давал привязать счёт, и тот висел «Готов к
    оплате» при уже ушедших деньгах."""
    from app.services.counterparty_bank_match import confirm_invoice_match

    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Счёт-после-УПД", inn="6155990018")
        operation, tx = await _classified_payment(
            session, counterparty_id=cp.id, amount="10000.00", inn="6155990018"
        )
        rule1 = await ensure_prepayment_from_bank_transaction(session, tx)
        await session.commit()
        upd = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="10000.00",
            doc_kind="closing",
            number="УПД-982",
            invoice_date=date(2026, 7, 20),
            operational_scope="finance",
        )
        await apply_closing_document(session, upd, as_of=date(2026, 7, 21))
        await session.commit()
        await session.refresh(rule1)
        assert rule1.status == "settled"
        bill = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="10000.00",
            doc_kind="bill",
            number="СЧ-982",
            invoice_date=date(2026, 7, 1),
        )
        await session.commit()

        await confirm_invoice_match(
            session,
            invoice_id=bill.id,
            bank_operation_id=operation.id,
            enrich=False,
            actor_user_id=None,
        )

        assert (await session.get(SupplierInvoice, bill.id)).payment_status == "paid"
        assert await _bill_receivable(session, bill.id) is None
        assert await _receivable_total(session, cp.id) == Decimal("0.00")
        await _assert_balance_matches_ledger(session, cp.id)


async def test_reassigned_payment_with_a_touched_advance_does_not_double_the_receivable(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Скептик Fable F3: тронутый аванс прежнего контрагента несёт деньги счёта нового.

    Платёж 10 000 классифицирован на X, УПД X 6 000 погасил аванс; платёж перевесили на Y —
    тронутый аванс перевес не пересобирает, он остаётся у X. Потом той же проводкой оплатили счёт
    Y 4 000. Дебиторка — 4 000 (остаток денег платежа), а не 4 000 у X плюс 4 000 ДЗ по счёту Y:
    чокпоинт не вправе отбросить аванс платежа только потому, что тот записан на другого."""
    from app.services.counterparty_matching import allocate_cash_to_invoice

    async with async_session_factory() as session:
        x = await make_counterparty(session, name="Тронутый-X", inn="6155990019")
        y = await make_counterparty(session, name="Тронутый-Y", inn="6155990020")
        _, tx = await _classified_payment(
            session, counterparty_id=x.id, amount="10000.00", inn="6155990019"
        )
        rule1 = await ensure_prepayment_from_bank_transaction(session, tx)
        await session.commit()
        upd = await make_invoice(
            session,
            counterparty_id=x.id,
            amount="6000.00",
            doc_kind="closing",
            number="УПД-981",
            invoice_date=date(2026, 7, 20),
            operational_scope="finance",
        )
        await apply_closing_document(session, upd, as_of=date(2026, 7, 21))
        await session.commit()
        await session.refresh(rule1)
        assert rule1.amount_settled == Decimal("6000.00")

        tx = await session.get(CashflowTransaction, tx.id)
        tx.counterparty_id = y.id
        await session.flush()
        await ensure_prepayment_from_bank_transaction(session, tx)
        await session.commit()
        bill = await make_invoice(
            session,
            counterparty_id=y.id,
            amount="4000.00",
            doc_kind="bill",
            number="СЧ-981",
            invoice_date=date(2026, 7, 22),
        )
        await session.commit()
        await allocate_cash_to_invoice(
            session, invoice_id=bill.id, amount=Decimal("4000.00"), cashflow_transaction_id=tx.id
        )

        total = await _receivable_total(session, x.id) + await _receivable_total(session, y.id)
        assert total == Decimal("4000.00"), f"дебиторка {total} при 4 000 денег платежа"


async def test_statement_payment_does_not_borrow_the_period_of_a_bill_with_own_receivable(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Скептик Fable F5 — прод-путь S3: сентябрь выпиской не получает период августа.

    Абонентка ровная, 3 700 ₽. Счёт за август оплачен из очереди (своя ДЗ с периодом августа).
    Сентябрь заплачен выпиской раньше, чем пришёл его счёт, — правило 1 искало период по
    оплаченному счёту той же суммы и находило единственный: августовский. Сентябрьские деньги
    получали период августа, и УПД за август гасил их рангом «период», а ДЗ августа висела."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Абонентка-F5", inn="6155990021")
        august_bill = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="3700.00",
            doc_kind="bill",
            number="АБ-08",
            invoice_date=date(2026, 7, 8),
        )
        august_bill.service_period_start = date(2026, 8, 1)
        august_bill.service_period_end = date(2026, 8, 31)
        august_bill.service_period_status = "ready"
        await session.commit()
        await _pay_bill_directly(session, august_bill, amount="3700.00")
        august_money = await session.scalar(
            select(SupplierPrepayment).where(SupplierPrepayment.bill_invoice_id == august_bill.id)
        )
        assert august_money is not None

        _, tx = await _classified_payment(
            session,
            counterparty_id=cp.id,
            amount="3700.00",
            inn="6155990021",
            operation_date=date(2026, 8, 10),
        )
        september_money = await ensure_prepayment_from_bank_transaction(session, tx)
        await session.commit()
        assert september_money is not None
        assert september_money.service_period_start is None, (
            "сентябрьские деньги получили период августа"
        )

        upd = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="3700.00",
            doc_kind="closing",
            number="УПД-08",
            invoice_date=date(2026, 8, 31),
            operational_scope="finance",
        )
        upd.service_period_start = date(2026, 8, 1)
        upd.service_period_end = date(2026, 8, 31)
        upd.service_period_status = "ready"
        await session.flush()
        await apply_closing_document(session, upd, as_of=date(2026, 9, 1))
        await session.commit()

        rows = (
            await session.scalars(
                select(InvoicePaymentAllocation).where(
                    InvoicePaymentAllocation.invoice_id == upd.id
                )
            )
        ).all()
        assert [r.prepayment_id for r in rows] == [august_money.id], (
            "УПД за август погасил сентябрьские деньги"
        )


async def test_foreign_bill_paid_by_the_same_payment_does_not_shrink_own_bill_receivable(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Скептик Fable P1: чужой счёт той же проводки — деньги аванса, а не своего счёта.

    Платёж Y 10 000: счёт Y 6 000 сверен до правила 1 (своя ДЗ 6 000, акт Y погашен ею),
    остаток 4 000 — аванс правила 1. Той же проводкой оплатили счёт X 4 000 — его деньги несёт
    аванс. Когда чокпоинт считал «другие счета» только по контрагенту платежа, касание счёта Y
    относило эти 4 000 к нему: ДЗ по счёту Y падала до 2 000, откат снимал зачёт с оплаченного
    акта, и деньги не числились нигде."""
    from app.services.counterparty_matching import _recompute_status, allocate_cash_to_invoice

    async with async_session_factory() as session:
        x = await make_counterparty(session, name="Чужой-счёт-X", inn="6155990022")
        y = await make_counterparty(session, name="Свой-счёт-Y", inn="6155990023")
        own_bill = await make_invoice(
            session,
            counterparty_id=y.id,
            amount="6000.00",
            doc_kind="bill",
            number="СЧ-980",
            invoice_date=date(2026, 7, 1),
        )
        foreign_bill = await make_invoice(
            session,
            counterparty_id=x.id,
            amount="4000.00",
            doc_kind="bill",
            number="СЧ-979",
            invoice_date=date(2026, 7, 1),
        )
        await session.commit()
        _, tx = await _classified_payment(
            session, counterparty_id=y.id, amount="10000.00", inn="6155990023"
        )
        await session.commit()
        await allocate_cash_to_invoice(
            session,
            invoice_id=own_bill.id,
            amount=Decimal("6000.00"),
            cashflow_transaction_id=tx.id,
        )
        act = await make_invoice(
            session,
            counterparty_id=y.id,
            amount="6000.00",
            doc_kind="closing",
            number="АКТ-980",
            invoice_date=date(2026, 7, 20),
            operational_scope="finance",
        )
        await apply_closing_document(session, act, as_of=date(2026, 7, 21))
        rule1 = await ensure_prepayment_from_bank_transaction(session, tx)
        await session.commit()
        assert rule1 is not None and rule1.amount == Decimal("4000.00")
        assert act.payment_status == "paid"
        await allocate_cash_to_invoice(
            session,
            invoice_id=foreign_bill.id,
            amount=Decimal("4000.00"),
            cashflow_transaction_id=tx.id,
        )

        await _recompute_status(session, await session.get(SupplierInvoice, own_bill.id))
        await session.commit()

        assert await _bill_receivable(session, own_bill.id) == Decimal("6000.00")
        assert await _act_paid(session, act.id) == Decimal("6000.00"), (
            "касание своего счёта сняло зачёт с оплаченного акта"
        )
