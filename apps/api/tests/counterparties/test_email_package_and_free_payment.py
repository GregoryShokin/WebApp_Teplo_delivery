"""Пакет «счёт + УПД» одним PDF и свободный платёж контрагенту — два разрыва канона ДЗ/КЗ.

Кейс СДЭК (27.07.2026, прод). Поставщик прислал ОДИН файл с тремя страницами: счёт на оплату,
УПД и приложение к УПД. Система завела из него один документ — УПД — и повесила кредиторку, а
счёт исчез из очереди оплат. Оплату владелец сделал свободным платежом («Новый платёж» → расход
по реквизитам), и она не погасила кредиторку и не создала дебиторку: путь
``apply_payment_status`` для черновика без накладных не звал правило 1 вовсе.

Проверяем канон владельца (17.07): счёт — не долг, он живёт в очереди оплат; УПД создаёт
кредиторку; платёж поставщику FIFO гасит открытую кредиторку, излишек становится дебиторкой.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

from cp_helpers import make_counterparty
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_admin_payout_split import _payer_wallet, _safe_wallet

from app.models import (
    CashflowTransaction,
    DdsArticle,
    EmailInvoiceIntake,
    SupplierInvoice,
    SupplierPrepayment,
)
from app.services.bank_payment_status import apply_payment_status
from app.services.counterparty_payments import ExpenseLineInput, create_expense_payment_draft
from app.services.email_invoice_ingest import (
    delete_intake_forever,
    exclude_intake,
    materialize_from_intake,
    restore_intake,
)
from app.services.new_payment import new_payment_article_flow

PAST = date.today() - timedelta(days=8)


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


def _package_intake(counterparty_id: uuid.UUID | None) -> EmailInvoiceIntake:
    """Вложение-пакет СДЭК: счёт СКБ-0437096 + закрывающий УПД СКБ-0008640 на ту же сумму."""
    return EmailInvoiceIntake(
        mailbox="personal",
        from_addr="noreply-oplata@cdek.ru",
        subject="Пакет документов СКБ-0437096 от 19.07.2026",
        attachment_sha256=uuid.uuid4().hex + uuid.uuid4().hex,
        status="needs_review",
        counterparty_id=counterparty_id,
        recognition={
            "amount": "7984.90",
            "document_kind": "invoice",
            "invoice_number": "СКБ-0437096",
            "invoice_date": PAST.isoformat(),
            "companion": {
                "amount": "7984.90",
                "document_kind": "upd",
                "invoice_number": "СКБ-0008640",
                "invoice_date": PAST.isoformat(),
            },
        },
    )


async def _free_expense_article(session: AsyncSession) -> DdsArticle:
    rows = await session.scalars(select(DdsArticle).where(DdsArticle.is_active.is_(True)))
    return next(a for a in rows.all() if new_payment_article_flow(a) == "expense")


async def test_package_creates_bill_in_queue_and_closing_in_kz(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Из пакета рождаются ДВА документа: счёт к оплате и закрывающий в кредиторке.

    Прежде вложение давало ровно один документ, и им оказывался УПД (маркер «универсальный
    передаточный документ» со второй страницы бьёт маркеры счёта с первой) — счёт терялся."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="СДЭК-Славянск", inn="2370006152")
        intake = _package_intake(cp.id)
        session.add(intake)
        await session.flush()

        status = await materialize_from_intake(session, intake)
        await session.commit()

        # Счёт — в очереди оплат («В банк» доступна именно для linked).
        assert status == "linked"
        bill = await session.get(SupplierInvoice, intake.invoice_id)
        assert bill is not None
        assert bill.doc_kind == "bill"
        assert bill.amount == Decimal("7984.90")

        # Закрывающий — отдельный документ, он и несёт кредиторку.
        assert intake.companion_invoice_id is not None
        closing = await session.get(SupplierInvoice, intake.companion_invoice_id)
        assert closing is not None
        assert closing.doc_kind == "closing"
        assert closing.number == "СКБ-0008640"
        assert closing.operational_scope == "finance"
        assert await _payable(session, cp.id) == Decimal("7984.90")


async def test_package_exclude_restore_and_delete_move_both_documents(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Исключение/возврат/удаление ведут ОБА документа пакета: УПД не остаётся сиротой в КЗ."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="СДЭК-корзина", inn="2370006153")
        intake = _package_intake(cp.id)
        session.add(intake)
        await session.flush()
        await materialize_from_intake(session, intake)
        await session.commit()
        bill_id, closing_id = intake.invoice_id, intake.companion_invoice_id

        await exclude_intake(session, intake)
        await session.commit()
        assert intake.status == "excluded"
        for doc_id in (bill_id, closing_id):
            doc = await session.get(SupplierInvoice, doc_id)
            assert doc is not None and doc.payment_status == "void"
        assert await _payable(session, cp.id) == Decimal("0.00")

        await restore_intake(session, intake)
        await session.commit()
        assert intake.status == "linked"
        for doc_id in (bill_id, closing_id):
            doc = await session.get(SupplierInvoice, doc_id)
            assert doc is not None and doc.payment_status == "unpaid"
        assert await _payable(session, cp.id) == Decimal("7984.90")

        await exclude_intake(session, intake)
        await session.commit()
        await delete_intake_forever(session, intake)
        await session.commit()
        assert await session.get(SupplierInvoice, bill_id) is None
        assert await session.get(SupplierInvoice, closing_id) is None


async def test_free_counterparty_payment_settles_open_kz_and_keeps_change_as_receivable(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Свободный платёж («Новый платёж» → расход по реквизитам) идёт по правилу 1.

    Кейс СДЭК: открытая кредиторка по УПД 7 984,90, платёж 8 000 → КЗ гасится, разница 15,10
    остаётся дебиторкой. Прежде такой платёж не делал ни того, ни другого — он заводил только
    проводку ДДС, и приходящая операция выписки заклеймляла её prebooked-claim'ом мимо канона."""
    async with async_session_factory() as session:
        _account, payer_wallet = await _payer_wallet(session)
        await _safe_wallet(session)
        article = await _free_expense_article(session)
        cp = await make_counterparty(
            session,
            name="СДЭК-платёж",
            inn="2370006154",
            requisites={
                "bankAcnt": "40702810430000013829",
                "bankBik": "040349602",
                "recipientCorrAccountNumber": "30101810100000000602",
            },
            requisites_verified=True,
        )
        intake = _package_intake(cp.id)
        session.add(intake)
        await session.flush()
        await materialize_from_intake(session, intake)
        await session.commit()
        assert await _payable(session, cp.id) == Decimal("7984.90")

        draft = await create_expense_payment_draft(
            session,
            lines=[
                ExpenseLineInput(
                    article_id=article.id,
                    amount=Decimal("8000.00"),
                    purpose="Услуги доставки",
                    counterparty_id=cp.id,
                )
            ],
        )
        assert await apply_payment_status(session, draft=draft, raw_status="executed") == "paid"
        await session.commit()

        # Расход проведён по выбранной статье — это поведение не менялось.
        txn = await session.scalar(
            select(CashflowTransaction).where(
                CashflowTransaction.source_kind == "counterparty_payment",
                CashflowTransaction.source_id == draft.id,
            )
        )
        assert txn is not None
        assert txn.wallet_id == payer_wallet.id
        assert txn.article_id == article.id

        # Канон: кредиторка погашена платежом, излишек — дебиторка.
        assert await _payable(session, cp.id) == Decimal("0.00")
        assert await _receivable(session, cp.id) == Decimal("15.10")


async def test_free_counterparty_payment_without_kz_becomes_receivable(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Платёж поставщику, у которого долга нет, целиком становится дебиторкой (аванс).

    Второй прод-случай той же дыры: платёж ИП Вишневецкому 6 000 не оставил в учёте следа."""
    async with async_session_factory() as session:
        await _payer_wallet(session)
        await _safe_wallet(session)
        article = await _free_expense_article(session)
        cp = await make_counterparty(
            session,
            name="ИП Вишневецкий (свободный платёж)",
            inn="616100000009",
            requisites={
                "bankAcnt": "40802810000000000001",
                "bankBik": "044525225",
                "recipientCorrAccountNumber": "30101810400000000225",
            },
            requisites_verified=True,
        )

        draft = await create_expense_payment_draft(
            session,
            lines=[
                ExpenseLineInput(
                    article_id=article.id,
                    amount=Decimal("6000.00"),
                    purpose="Комиссия агрегатору",
                    counterparty_id=cp.id,
                )
            ],
        )
        await apply_payment_status(session, draft=draft, raw_status="executed")
        await session.commit()

        assert await _receivable(session, cp.id) == Decimal("6000.00")


# --- Регрессы предделойного аудита (wf_8244f364) --------------------------------------------


async def test_existing_closing_is_not_adopted_as_companion(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Если такой закрывающий уже есть (СБИС/вручную/отдельным письмом) — он ЧУЖОЙ.

    Присвоение его в companion_invoice_id делало исключение пакета разрушительным: exclude
    аннулировал кредиторку, к которой это письмо отношения не имеет."""
    from cp_helpers import make_invoice

    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="СДЭК-чужой-УПД", inn="2370006155")
        foreign = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount=Decimal("7984.90"),
            number="СКБ-0008640",
            source="sbis",
            doc_kind="closing",
            operational_scope="finance",
            invoice_date=PAST,
        )
        await session.commit()

        intake = _package_intake(cp.id)
        session.add(intake)
        await session.flush()
        await materialize_from_intake(session, intake)
        await session.commit()

        # Спутник не создан и НЕ присвоен: чужой документ живёт своей жизнью.
        assert intake.companion_invoice_id is None
        assert await _payable(session, cp.id) == Decimal("7984.90")  # ровно один долг, не два

        await exclude_intake(session, intake)
        await session.commit()
        await session.refresh(foreign)
        assert foreign.payment_status == "unpaid"  # чужой документ не тронут
        assert await _payable(session, cp.id) == Decimal("7984.90")


async def test_duplicate_bill_still_creates_companion(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Повтор СЧЁТА не должен терять закрывающий: поставщик прислал тот же счёт, добавив УПД."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="СДЭК-повтор-счёта", inn="2370006156")

        first = _package_intake(cp.id)
        first.recognition = {**first.recognition, "companion": None}
        session.add(first)
        await session.flush()
        await materialize_from_intake(session, first)
        await session.commit()
        assert first.companion_invoice_id is None
        assert await _payable(session, cp.id) == Decimal("0.00")  # счёт долгом не является

        second = _package_intake(cp.id)  # тот же счёт, теперь с УПД в пакете
        session.add(second)
        await session.flush()
        status = await materialize_from_intake(session, second)
        await session.commit()

        assert status == "duplicate"  # счёт — повтор
        assert second.companion_invoice_id is not None  # а вот УПД новый
        assert await _payable(session, cp.id) == Decimal("7984.90")


async def test_package_books_single_expense_accrual(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Одна услуга — одно признание расхода, и ведёт его закрывающий, а не счёт.

    Счёт расходом не является по канону: он основание для платежа. С 01.08.2026 начисление на
    нём не заводится вовсе — иначе период, указанный при оплате счёта, признавал бы расход, а
    пришедший акт признавал его второй раз."""
    from app.models import SupplierExpenseAccrual

    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="СДЭК-начисление", inn="2370006157")
        intake = _package_intake(cp.id)
        rec = dict(intake.recognition)
        period = {
            "service_period_start": PAST.replace(day=1).isoformat(),
            "service_period_end": PAST.isoformat(),
        }
        # Период стоит на ОБОИХ документах пакета — так и приходит от поставщика услуг
        # (счёт и УПД за один и тот же отчётный период).
        rec.update(period)
        rec["companion"] = {**rec["companion"], **period}
        intake.recognition = rec
        session.add(intake)
        await session.flush()
        await materialize_from_intake(session, intake)
        await session.commit()

        accruals = (
            await session.scalars(
                select(SupplierExpenseAccrual).where(
                    SupplierExpenseAccrual.counterparty_id == cp.id
                )
            )
        ).all()
        assert len(accruals) == 1
        assert accruals[0].invoice_id == intake.companion_invoice_id


async def test_reparse_moves_companion_to_new_counterparty(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Смена контрагента в окне разбора ведёт ОБА документа пакета, не только счёт."""
    from app.services.email_invoice_ingest import confirm_intake_with_review

    async with async_session_factory() as session:
        wrong = await make_counterparty(session, name="Заглушка ИНН 237000615", inn="2370006158")
        right = await make_counterparty(
            session, name="ООО СДЭК-Славянск (верный)", inn="2370006159"
        )
        intake = _package_intake(wrong.id)
        session.add(intake)
        await session.flush()
        await materialize_from_intake(session, intake)
        await session.commit()
        assert await _payable(session, wrong.id) == Decimal("7984.90")

        await confirm_intake_with_review(
            session, intake, actor_user_id=None, counterparty_id=right.id
        )
        await session.commit()

        bill = await session.get(SupplierInvoice, intake.invoice_id)
        companion = await session.get(SupplierInvoice, intake.companion_invoice_id)
        assert bill is not None and bill.counterparty_id == right.id
        assert companion is not None and companion.counterparty_id == right.id
        assert await _payable(session, wrong.id) == Decimal("0.00")
        assert await _payable(session, right.id) == Decimal("7984.90")


async def test_free_payment_does_not_double_book_when_statement_row_came_first(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Обратный порядок: операция выписки разобрана РАНЬШЕ paid-перехода черновика.

    Тогда правило 1 уже отработало на строке операции (классификатор зовёт его сам), и повторный
    прогон по prebooked-строке зачёл бы те же деньги второй раз: кредиторка «погашена» дважды,
    а разница осела бы дебиторкой из воздуха. Найдено предделойным аудитом (blocker)."""
    import uuid as _uuid

    from app.models import BankOperation, InvoicePaymentAllocation
    from app.services.banking.tbank import _document_number

    async with async_session_factory() as session:
        _account, payer_wallet = await _payer_wallet(session)
        await _safe_wallet(session)
        article = await _free_expense_article(session)
        cp = await make_counterparty(
            session,
            name="СДЭК-обратный-порядок",
            inn="2370006160",
            requisites={
                "bankAcnt": "40702810430000013829",
                "bankBik": "040349602",
                "recipientCorrAccountNumber": "30101810100000000602",
            },
            requisites_verified=True,
        )
        intake = _package_intake(cp.id)
        session.add(intake)
        await session.flush()
        await materialize_from_intake(session, intake)
        await session.commit()
        closing_id = intake.companion_invoice_id
        assert await _payable(session, cp.id) == Decimal("7984.90")

        draft = await create_expense_payment_draft(
            session,
            lines=[
                ExpenseLineInput(
                    article_id=article.id,
                    amount=Decimal("7984.00"),
                    purpose="Услуги доставки",
                    counterparty_id=cp.id,
                )
            ],
        )
        draft.document_id = f"doc-{_uuid.uuid4()}"
        await session.flush()

        # Выписка пришла первой: своя проводка + зачёт кредиторки правилом 1 (как это делает
        # классификатор при разборе операции).
        statement_txn = CashflowTransaction(
            wallet_id=payer_wallet.id,
            direction="out",
            amount=Decimal("7984.00"),
            operation_date=PAST,
            article_id=article.id,
            counterparty_id=cp.id,
            source_kind="bank_operation",
            payment_purpose="Услуги доставки",
            quality_status="auto",
        )
        session.add(statement_txn)
        await session.flush()
        session.add(
            InvoicePaymentAllocation(
                invoice_id=closing_id,
                source_kind="cash",
                cashflow_transaction_id=statement_txn.id,
                amount=Decimal("7984.00"),
            )
        )
        session.add(
            BankOperation(
                provider="tbank",
                provider_operation_id=f"op-{_uuid.uuid4()}",
                account_id=payer_wallet.account_id,
                operation_date=PAST,
                direction="out",
                amount=Decimal("7984.00"),
                document_number=_document_number(draft.document_id),
                payment_purpose="Услуги доставки",
                raw_payload={},
                classification_status="classified",
                cashflow_transaction_id=statement_txn.id,
            )
        )
        await session.commit()
        assert await _payable(session, cp.id) == Decimal("0.90")

        # Теперь приходит статус «исполнен» по тому же платежу.
        await apply_payment_status(session, draft=draft, raw_status="executed")
        await session.commit()

        # Деньги учтены ОДИН раз: остаток кредиторки не изменился, дебиторка не появилась.
        assert await _payable(session, cp.id) == Decimal("0.90")
        assert await _receivable(session, cp.id) == Decimal("0.00")


async def test_statement_payment_closes_both_documents_of_the_package(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Пакет «счёт + УПД» и свободный платёж ВЫПИСКОЙ: закрываются оба документа.

    Зонд 02.08.2026: платёж, пришедший не через очередь оплат, уходил в правило 1, а то
    подбирало только закрывающие. УПД гасился, а счёт за тот же месяц оставался ``unpaid`` и
    висел на «Странице на оплату» как «Готов к оплате» — человек видел неоплаченный счёт за уже
    оплаченный месяц и мог заплатить второй раз. Свойство пакета, а не одного поставщика: так
    ведут себя счета СДЭК и любая пара из ``_materialize_package_companion``."""
    from app.services.banking.classifier import _drop_untouched_bank_prepayments

    async with async_session_factory() as session:
        _account, payer_wallet = await _payer_wallet(session)
        await _safe_wallet(session)
        article = await _free_expense_article(session)
        cp = await make_counterparty(session, name="СДЭК-выписка", inn="2370006162")
        intake = _package_intake(cp.id)
        session.add(intake)
        await session.flush()
        await materialize_from_intake(session, intake)
        await session.commit()
        bill_id, closing_id = intake.invoice_id, intake.companion_invoice_id
        assert await _payable(session, cp.id) == Decimal("7984.90")

        # Платёж пришёл банковской выпиской: своя проводка, ссылки на документы нет.
        txn = CashflowTransaction(
            wallet_id=payer_wallet.id,
            direction="out",
            amount=Decimal("7984.90"),
            operation_date=PAST,
            article_id=article.id,
            counterparty_id=cp.id,
            source_kind="bank_operation",
            payment_purpose="Оплата по счёту СКБ-0437096",
            quality_status="auto",
        )
        session.add(txn)
        await session.flush()
        from app.services.supplier_prepayments import ensure_prepayment_from_bank_transaction

        await ensure_prepayment_from_bank_transaction(session, txn)
        await session.commit()

        # Месяц закрыт целиком: счёт ушёл из очереди оплат, кредиторки и дебиторки не осталось.
        assert (await session.get(SupplierInvoice, bill_id)).payment_status == "paid"
        assert (await session.get(SupplierInvoice, closing_id)).payment_status == "paid"
        assert await _payable(session, cp.id) == Decimal("0.00")
        assert await _receivable(session, cp.id) == Decimal("0.00")

        # Исключение операции возвращает пару в исходное состояние — без фантомов.
        await _drop_untouched_bank_prepayments(session, {txn.id})
        await session.commit()
        assert (await session.get(SupplierInvoice, bill_id)).payment_status == "unpaid"
        assert (await session.get(SupplierInvoice, closing_id)).payment_status == "unpaid"
        assert await _payable(session, cp.id) == Decimal("7984.90")
        assert (
            await session.scalar(
                select(func.count()).select_from(SupplierPrepayment).where(
                    SupplierPrepayment.counterparty_id == cp.id
                )
            )
        ) == 0


async def test_free_payment_does_not_settle_document_awaiting_bank(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Документ, ушедший в банк-черновик, чужим платежом не гасится.

    Иначе поставщику платят дважды: сначала этим свободным платежом (зачёт кредиторки), потом
    исполнением черновика. Гард симметричен тому, что уже стоит в авто-зачёте из предоплат."""
    from cp_helpers import make_invoice

    from app.services.counterparty_payments import create_payment_draft_for_invoices

    async with async_session_factory() as session:
        await _payer_wallet(session)
        await _safe_wallet(session)
        article = await _free_expense_article(session)
        cp = await make_counterparty(
            session,
            name="Поставщик с платежом в пути",
            inn="2370006161",
            requisites={
                "bankAcnt": "40702810430000013829",
                "bankBik": "040349602",
                "recipientCorrAccountNumber": "30101810100000000602",
            },
            requisites_verified=True,
        )
        in_bank = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount=Decimal("5000.00"),
            number="УПД-В-БАНКЕ",
            source="email",
            doc_kind="closing",
            operational_scope="finance",
            invoice_date=PAST,
        )
        await session.commit()
        # Документ реально уходит в банк штатным путём — так же, как из «Страницы на оплату».
        await create_payment_draft_for_invoices(
            session, invoice_ids=[in_bank.id], actor_user_id=None
        )
        await session.refresh(in_bank)
        assert in_bank.draft_id is not None

        draft = await create_expense_payment_draft(
            session,
            lines=[
                ExpenseLineInput(
                    article_id=article.id,
                    amount=Decimal("3000.00"),
                    purpose="Другая услуга",
                    counterparty_id=cp.id,
                )
            ],
        )
        await apply_payment_status(session, draft=draft, raw_status="executed")
        await session.commit()

        await session.refresh(in_bank)
        assert in_bank.payment_status == "unpaid"  # платёж в пути не тронут
        assert await _receivable(session, cp.id) == Decimal("3000.00")  # деньги стали авансом
