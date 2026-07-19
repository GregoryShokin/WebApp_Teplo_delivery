"""Хвосты предкоммит-аудита взаиморасчётов (19.07.2026) — регресс-набор.

(1) Реестр «Документы» (`/accounting/suppliers/documents`) считал остаток бартерного займа как
«сумма − аллокации» и не знал о ТОВАРНЫХ возвратах: они живут в ``BarterReturnLine`` (леджер),
а не в аллокациях. Плитка «Остатки» (`/balances`) их вычитает — два числа на ОДНОЙ странице
расходились ровно на зачётную стоимость возврата.

(2) Взаимные поставки на вкладке «Бартер» считались брутто и ТОЛЬКО в статусе `unpaid`:
первый же частичный платёж переводил накладную в `partially_paid` и она пропадала из баланса
целиком (долг 3000, заплатили 1000 → на вкладке 0 вместо 2000).

(3) Бартерное партнёрство определялось АВТОМАТИЧЕСКИ по любой расходной накладной из
iiko-синка. Возврат некондиции обычному поставщику делал его «бартерным партнёром» и утаскивал
всю его денежную кредиторку на вкладку бартера как товарный долг. Решение владельца
(19.07.2026): авто-определения нет вовсе — партнёр назначается только вручную (карточка
контрагента) либо оформлением явного займа.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from cp_helpers import (
    admin_headers,
    cloud_invoice_docs,
    cloud_outgoing_docs,
    make_counterparty,
    make_expense_article,
    make_invoice,
    make_wallet,
    suppliers_xml,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    BarterSettlement,
    CounterpartyPayableProfile,
    IikoProduct,
    InvoiceLineItem,
    InvoicePaymentAllocation,
    SupplierInvoice,
)
from app.services.counterparty_barter_match import barter_partner_balances
from app.services.counterparty_invoice_sync import ingest_iiko_payables
from app.services.counterparty_payments import pay_invoice_from_wallet
from app.services.warehouse_invoices import (
    LineInput,
    ReturnLineInput,
    create_barter_return,
    create_warehouse_invoice,
)

BASE = "/api/v1/accounting/suppliers"
ISSUED = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)
RETURNED = datetime(2026, 7, 20, 10, 0, tzinfo=UTC)


async def _product(session: AsyncSession, name: str = "Моцарелла") -> IikoProduct:
    product = IikoProduct(
        iiko_id=str(uuid.uuid4()), name=name, type="GOODS", unit="кг",
        synced_at=datetime.now(UTC),
    )
    session.add(product)
    await session.flush()
    return product


async def _make_loan(
    session: AsyncSession, *, inn: str, we_lend: bool, qty: str = "10", price: str = "300"
) -> tuple[SupplierInvoice, InvoiceLineItem]:
    """Заём 10 кг × 300 = 3000: мы выдаём (we_lend) или нам выдают."""
    cp = await make_counterparty(session, name=f"Хвост-{inn}", inn=inn)
    await make_expense_article(session, code="payment_to_supplier", name="Оплата поставщикам")
    product = await _product(session)
    await session.commit()
    loan = await create_warehouse_invoice(
        session,
        counterparty_id=cp.id,
        issued_at=ISSUED,
        mode="loan",
        we_lend=we_lend,
        lines=[
            LineInput(
                name="Моцарелла", quantity=Decimal(qty), price=Decimal(price),
                iiko_product_id=product.id,
            )
        ],
    )
    line = (
        await session.scalars(
            select(InvoiceLineItem).where(InvoiceLineItem.invoice_id == loan.id)
        )
    ).one()
    return loan, line


async def test_documents_registry_subtracts_barter_goods_returns(
    client,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Их заём 3000, вернули 4 кг товаром (зачёт 4 × 300 = 1200) → долг 1800.

    Реестр «Документы» обязан показывать тот же остаток, что и плитка «Остатки»: товарный
    возврат гасит долг, хотя денег и аллокаций по нему нет.
    """
    async with async_session_factory() as session:
        loan, line = await _make_loan(session, inn="6155999001", we_lend=False)
        loan_id, cp_id = str(loan.id), str(loan.counterparty_id)
        await create_barter_return(
            session,
            loan_id=loan.id,
            issued_at=RETURNED,
            returns=[
                ReturnLineInput(
                    amount=Decimal("1200"), loan_line_item_id=line.id, quantity=Decimal("4")
                )
            ],
        )
        await session.commit()

    headers = await admin_headers(async_session_factory)

    docs = client.get(f"{BASE}/documents", params={"counterparty_id": cp_id}, headers=headers)
    assert docs.status_code == 200, docs.text
    body = docs.json()
    row = next(item for item in body["items"] if item["invoice_id"] == loan_id)
    assert row["remainder"] == 1800.0, "товарный возврат обязан уменьшать остаток документа"
    assert body["unpaid_total"] == 1800.0

    balances = client.get(f"{BASE}/balances", headers=headers)
    assert balances.status_code == 200, balances.text
    balance = next(
        item for item in balances.json()["items"] if item["counterparty_id"] == cp_id
    )
    # Главное требование: два числа на одной странице сходятся.
    assert balance["payable"] == body["unpaid_total"] == 1800.0


async def test_documents_registry_closed_loan_leaves_no_debt(
    client,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Заём, возвращённый товаром ПОЛНОСТЬЮ, не оставляет остатка в реестре.

    Край округлений: строка, погашенная по кг целиком, зачитывается ровно своей суммой
    (канон «сумма строки — эталон»), иначе копеечный дрейф оставит вечный хвост долга.
    """
    async with async_session_factory() as session:
        loan, line = await _make_loan(
            session, inn="6155999002", we_lend=False, qty="2.6", price="470.48"
        )
        loan_id, cp_id = str(loan.id), str(loan.counterparty_id)
        await create_barter_return(
            session,
            loan_id=loan.id,
            issued_at=RETURNED,
            returns=[
                ReturnLineInput(
                    amount=Decimal("1223.24"), loan_line_item_id=line.id, quantity=Decimal("2.6")
                )
            ],
        )
        await session.commit()

    headers = await admin_headers(async_session_factory)
    docs = client.get(f"{BASE}/documents", params={"counterparty_id": cp_id}, headers=headers)
    assert docs.status_code == 200, docs.text
    body = docs.json()
    row = next(item for item in body["items"] if item["invoice_id"] == loan_id)
    assert row["remainder"] == 0.0
    assert body["unpaid_total"] == 0.0


# --- хвост 2: арифметика взаимных поставок --------------------------------------------------


async def _partner_row(
    session: AsyncSession, counterparty_id: uuid.UUID
) -> dict[str, float] | None:
    rows = await barter_partner_balances(session)
    return next(
        (row for row in rows if row["counterparty_id"] == str(counterparty_id)), None
    )


async def test_mutual_supply_partially_paid_stays_visible_by_remainder(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Взаимная поставка 3000, оплатили 1000 деньгами → «мы должны» 2000.

    Отбор шёл по статусу `unpaid`, а первый платёж переводит накладную в `partially_paid` —
    долг исчезал со вкладки целиком. Плюс сумма бралась брутто, без вычета оплаченного.
    """
    async with async_session_factory() as session:
        cp = await make_counterparty(
            session, name="Хвост-взаимный", inn="6155999003", relationship="barter"
        )
        article = await make_expense_article(
            session, code="payment_to_supplier", name="Оплата поставщикам"
        )
        wallet = await make_wallet(session, name="Касса-хвосты", wallet_type="cash_safe")
        invoice = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="3000.00",
            doc_kind="closing",
            number="ВЗ-1",
            invoice_date=date(2026, 7, 14),
        )
        await session.commit()

        await pay_invoice_from_wallet(
            session,
            invoice_id=invoice.id,
            wallet_id=wallet.id,
            amount=Decimal("1000.00"),
            operation_date=date(2026, 7, 15),
            article_id=article.id,
        )
        await session.commit()

        refreshed = await session.get(SupplierInvoice, invoice.id)
        assert refreshed.payment_status == "partially_paid", "предусловие теста"

        row = await _partner_row(session, cp.id)
        assert row is not None, "партнёр не должен исчезать из списка"
        assert row["we_owe"] == 2000.0
        assert row["has_mutual"] is True


async def test_mutual_supply_unpaid_still_counts_in_full(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Регресс-гард к хвосту 2: неоплаченная взаимная поставка по-прежнему считается полностью."""
    async with async_session_factory() as session:
        cp = await make_counterparty(
            session, name="Хвост-взаимный-2", inn="6155999004", relationship="barter"
        )
        await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="1500.00",
            doc_kind="closing",
            number="ВЗ-2",
            invoice_date=date(2026, 7, 14),
        )
        await session.commit()

        row = await _partner_row(session, cp.id)
        assert row is not None
        assert row["we_owe"] == 1500.0


# --- хвост 3: бартерное партнёрство только вручную ------------------------------------------


BARTER_GUID = "guid-outgoing-supplier"


def _outgoing_supplier() -> str:
    return suppliers_xml(
        [{"id": BARTER_GUID, "name": "ИП Поставщик Обычный", "inn": "6155999005"}]
    )


def _outgoing_doc() -> dict:
    return {
        "id": "ar-doc-tails-1",
        "counteragent": BARTER_GUID,
        "status": "PROCESSED",
        "document_number": "0016",
        "incoming_date": "2026-06-01",
        "items": [{"sum": "1000.00", "vat_percent": "10", "vat_sum": "90.91"}],
    }


async def test_outgoing_invoice_does_not_auto_mark_barter_partner(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Расходная накладная из iiko-синка НЕ вербует контрагента в бартерные партнёры.

    Возврат некондиции обычному поставщику — тоже расходная накладная. Раньше она молча
    переводила профиль в relationship='barter', и вся денежная кредиторка этого поставщика
    (у Буряка на стенде — 218 415,60 ₽) показывалась на вкладке «Бартер» как товарный долг.
    Решение владельца: партнёрство назначается ТОЛЬКО вручную.
    """
    async with async_session_factory() as session:
        result = await ingest_iiko_payables(
            session,
            suppliers_xml=_outgoing_supplier(),
            incoming_docs=cloud_invoice_docs([]),
            outgoing_docs=cloud_outgoing_docs([_outgoing_doc()]),
        )
        await session.commit()

        # Сама накладная приходит как и раньше — меняется только вывод о типе отношений.
        assert result.receivables_created == 1
        invoice = await session.scalar(
            select(SupplierInvoice).where(SupplierInvoice.direction == "receivable")
        )
        assert invoice is not None and invoice.amount == Decimal("1000.00")

        profile = await session.scalar(
            select(CounterpartyPayableProfile).where(
                CounterpartyPayableProfile.counterparty_id == invoice.counterparty_id
            )
        )
        assert profile is None or profile.relationship != "barter", (
            "расходная накладная больше не делает контрагента бартерным партнёром"
        )

        # И на вкладке «Бартер» его нет — денежная кредиторка туда не утекает.
        assert await _partner_row(session, invoice.counterparty_id) is None


# --- находки предкоммит-аудита 19.07 --------------------------------------------------------


async def test_registry_shows_no_remainder_for_barter_netted_invoice(
    client,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Накладная, закрытая ВЗАИМОЗАЧЁТОМ, не должна висеть «Оплачено · остаток N».

    Зачёт (`_create_settlement`) ставит payment_status='paid' и barter_settlement_id, но НЕ создаёт
    аллокаций — денег не было. Реестр считал остаток по аллокациям и рисовал полную сумму долга
    рядом с бейджем «Оплачено». На стенде это 4 640,02 ₽ фантома (накладные 78787707 и 78787710).
    """
    from app.services.counterparty_barter_match import confirm_barter_settlement

    async with async_session_factory() as session:
        cp = await make_counterparty(
            session, name="Хвост-зачёт", inn="6155999006", relationship="barter"
        )
        payable = await make_invoice(
            session, counterparty_id=cp.id, amount="1000.00", doc_kind="closing",
            number="ЗАЧ-ПРИХ", invoice_date=date(2026, 7, 12),
        )
        receivable = await make_invoice(
            session, counterparty_id=cp.id, amount="1000.00", doc_kind="closing",
            direction="receivable", number="ЗАЧ-РАСХ", invoice_date=date(2026, 7, 13),
        )
        await session.commit()

        await confirm_barter_settlement(
            session,
            cp.id,
            payable_ids=[payable.id],
            receivable_ids=[receivable.id],
        )
        await session.commit()
        payable_id, cp_id = str(payable.id), str(cp.id)

    headers = await admin_headers(async_session_factory)
    docs = client.get(f"{BASE}/documents", params={"counterparty_id": cp_id}, headers=headers)
    assert docs.status_code == 200, docs.text
    body = docs.json()
    row = next(item for item in body["items"] if item["invoice_id"] == payable_id)
    assert row["payment_status"] == "paid", "предусловие: зачёт закрыл накладную"
    assert row["remainder"] == 0.0, "закрытая зачётом накладная не несёт остатка долга"
    assert body["unpaid_total"] == 0.0


async def test_partially_paid_mutual_supply_is_offsettable_by_remainder(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Симметрия шапки и списка: что показано в «мы должны», то и можно зачесть.

    Баланс партнёра стал считать частично оплаченную поставку остатком, а кандидаты зачёта
    (`_load_open`) остались на 'unpaid' брутто — в шапке «2 000 ₽», а зачесть нечем. Зачёт обязан
    брать ОСТАТОК (2 000), а не сумму накладной (3 000), иначе оплаченная деньгами 1 000
    зачтётся вторично.
    """
    from app.services.counterparty_barter_match import (
        confirm_barter_settlement,
        suggest_barter_matches,
    )

    async with async_session_factory() as session:
        cp = await make_counterparty(
            session, name="Хвост-симметрия", inn="6155999007", relationship="barter"
        )
        article = await make_expense_article(
            session, code="payment_to_supplier", name="Оплата поставщикам"
        )
        wallet = await make_wallet(session, name="Касса-симметрия", wallet_type="cash_safe")
        payable = await make_invoice(
            session, counterparty_id=cp.id, amount="3000.00", doc_kind="closing",
            number="СИМ-ПРИХ", invoice_date=date(2026, 7, 12),
        )
        receivable = await make_invoice(
            session, counterparty_id=cp.id, amount="2000.00", doc_kind="closing",
            direction="receivable", number="СИМ-РАСХ", invoice_date=date(2026, 7, 13),
        )
        await session.commit()

        await pay_invoice_from_wallet(
            session, invoice_id=payable.id, wallet_id=wallet.id,
            amount=Decimal("1000.00"), operation_date=date(2026, 7, 14), article_id=article.id,
        )
        await session.commit()

        # Шапка партнёра: мы должны остаток 2000.
        row = await _partner_row(session, cp.id)
        assert row is not None and row["we_owe"] == 2000.0, "предусловие теста"

        # Кандидаты зачёта обязаны видеть ту же накладную остатком 2000 — встречная ровно 2000.
        matches = await suggest_barter_matches(session, cp.id)
        assert matches, "частично оплаченная поставка должна попадать в кандидаты зачёта"

        await confirm_barter_settlement(
            session, cp.id, payable_ids=[payable.id], receivable_ids=[receivable.id]
        )
        await session.commit()

        settled = await session.get(SupplierInvoice, payable.id)
        assert settled.payment_status == "paid"
        # Зачтён ОСТАТОК, а не брутто: 1000 деньгами + 2000 зачётом = 3000.
        settlement_amount = await session.scalar(
            select(BarterSettlement.amount).where(
                BarterSettlement.id == settled.barter_settlement_id
            )
        )
        assert Decimal(str(settlement_amount)) == Decimal("2000.00")

        # Долг закрыт полностью — партнёр рассчитан.
        after = await _partner_row(session, cp.id)
        assert after is not None and after["we_owe"] == 0.0


LOAN_SUPPLIER_GUID = "guid-loan-supplier"


async def test_iiko_resync_does_not_rewrite_barter_loan_amount(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Реверс-синк iiko не переписывает сумму бартерного займа.

    Состав займа заморожен (`_materialize_iiko_lines(replace=... and barter_role is None)`) —
    строки суть основание долга, на них ссылается леджер BarterReturnLine. А `amount` тем же
    гардом прикрыт НЕ был: сумма уезжала, строки оставались, и `loan_settled_value` (считает по
    строкам) переставал сходиться с `loan.amount`. Итог: заём либо не закрывается никогда
    (все кг вернули, остаток висит), либо молча уходит в `returned` и возврат товаром теряется.
    """
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Хвост-заём-iiko", inn="6155999008")
        loan = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="3000.00",
            doc_kind="closing",
            source="iiko",
            external_id="loan-doc-1",
            number="ЗМ-1",
            invoice_date=date(2026, 7, 10),
        )
        loan.barter_role = "loan"
        loan.barter_return_status = "open"
        await session.commit()
        loan_id = loan.id

        # Тот же документ приезжает из iiko с ДРУГОЙ суммой (правка в iiko).
        await ingest_iiko_payables(
            session,
            suppliers_xml=suppliers_xml(
                [{"id": LOAN_SUPPLIER_GUID, "name": "Хвост-заём-iiko", "inn": "6155999008"}]
            ),
            incoming_docs=cloud_invoice_docs(
                [
                    {
                        "id": "loan-doc-1",
                        "supplier": LOAN_SUPPLIER_GUID,
                        "status": "PROCESSED",
                        "document_number": "ЗМ-1",
                        "incoming_date": "2026-07-10",
                        "items": [{"sum": "5000.00", "vat_percent": "10", "vat_sum": "454.55"}],
                    }
                ]
            ),
        )
        await session.commit()

        refreshed = await session.get(SupplierInvoice, loan_id)
        assert refreshed.barter_role == "loan", "предусловие: накладная осталась займом"
        assert Decimal(str(refreshed.amount)) == Decimal("3000.00"), (
            "сумма займа заморожена вместе с его строками"
        )


async def test_kassa_settlement_dates_expense_by_moscow_day(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    """Расход наличных при оплате накладной из Кассы датируется МОСКОВСКИМ днём.

    Контейнер API живёт в UTC (`date.today()` = UTC-дата), а касса считает день по MOSCOW_TZ.
    С 00:00 до 03:00 МСК расход падал во вчерашний, уже сверенный с iiko день — и та же
    накладная, оплаченная через диалог склада, получала МСК-дату с фронта: одна операция,
    две разные даты в одном ДДС-леджере.
    """
    import app.api.v1.routes.warehouse as warehouse_routes
    from app.models import CashflowTransaction

    # Заведомо НЕ сегодняшний день: иначе тест совпал бы с date.today() и не различал источник.
    moscow_day = date(2026, 1, 15)

    async def _noop_push(*args, **kwargs):
        return None

    monkeypatch.setattr(warehouse_routes, "kassa_today", lambda: moscow_day)
    monkeypatch.setattr(warehouse_routes, "post_kassa_payment_to_iiko", _noop_push)

    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Хвост-касса", inn="6155999009")
        await make_expense_article(
            session, code="payment_to_supplier", name="Оплата поставщикам"
        )
        # Кошелёк ТК Черникова приходит из сида — оплата наличными идёт строго с него.
        invoice = await make_invoice(
            session, counterparty_id=cp.id, amount="700.00", doc_kind="closing",
            number="КАС-1", invoice_date=date(2026, 7, 18),
        )
        await session.commit()

        await warehouse_routes._settle_paid_from_kassa(
            session, invoice, None, None, do_push=False
        )

        operation_date = await session.scalar(
            select(CashflowTransaction.operation_date)
            .join(
                InvoicePaymentAllocation,
                InvoicePaymentAllocation.cashflow_transaction_id == CashflowTransaction.id,
            )
            .where(InvoicePaymentAllocation.invoice_id == invoice.id)
        )
        assert operation_date == moscow_day, "расход датируется календарным днём МСК"


async def test_barter_netted_invoice_survives_money_unwind(
    client,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Снятие денежной оплаты с накладной, чей ОСТАТОК был зачтён товаром.

    Сценарий ре-аудита: УПД 10 000, деньгами 3 000, остаток 7 000 закрыт бартером. Потом
    банк-операцию исключили из учёта → аллокация 3 000 снята. Истинный долг = 3 000 (товар
    остаётся зачтённым). `_create_settlement` метит 'paid' прямым присвоением и не оставляет
    аллокации, а `_recompute_status` про barter_settlement_id не знает — статус падает в
    'unpaid', и реестр (обнуляет по settlement_id) расходится с плиткой (считает по аллокациям).
    """
    from app.services.counterparty_barter_match import confirm_barter_settlement
    from app.services.counterparty_matching import _recompute_status

    async with async_session_factory() as session:
        cp = await make_counterparty(
            session, name="Хвост-откат", inn="6155999010", relationship="barter"
        )
        article = await make_expense_article(
            session, code="payment_to_supplier", name="Оплата поставщикам"
        )
        wallet = await make_wallet(session, name="Касса-откат", wallet_type="cash_safe")
        payable = await make_invoice(
            session, counterparty_id=cp.id, amount="10000.00", doc_kind="closing",
            number="ОТК-ПРИХ", invoice_date=date(2026, 7, 12),
        )
        receivable = await make_invoice(
            session, counterparty_id=cp.id, amount="7000.00", doc_kind="closing",
            direction="receivable", number="ОТК-РАСХ", invoice_date=date(2026, 7, 13),
        )
        await session.commit()

        await pay_invoice_from_wallet(
            session, invoice_id=payable.id, wallet_id=wallet.id,
            amount=Decimal("3000.00"), operation_date=date(2026, 7, 14), article_id=article.id,
        )
        await session.commit()

        await confirm_barter_settlement(
            session, cp.id, payable_ids=[payable.id], receivable_ids=[receivable.id]
        )
        await session.commit()

        # Деньги отозвали: снимаются ДЕНЕЖНЫЕ аллокации операции (исключение операции /
        # пере-разбор). Бартерная аллокация зачёта остаётся — товар-то поставлен.
        money_allocations = (
            await session.scalars(
                select(InvoicePaymentAllocation).where(
                    InvoicePaymentAllocation.invoice_id == payable.id,
                    InvoicePaymentAllocation.source_kind != "barter",
                )
            )
        ).all()
        assert money_allocations, "предусловие: денежная оплата была"
        for allocation in money_allocations:
            await session.delete(allocation)
        await session.flush()
        refreshed = await session.get(SupplierInvoice, payable.id)
        await _recompute_status(session, refreshed)
        await session.commit()
        payable_id, cp_id = str(payable.id), str(cp.id)

    headers = await admin_headers(async_session_factory)
    docs = client.get(f"{BASE}/documents", params={"counterparty_id": cp_id}, headers=headers)
    assert docs.status_code == 200, docs.text
    registry_row = next(
        item for item in docs.json()["items"] if item["invoice_id"] == payable_id
    )
    balances = client.get(f"{BASE}/balances", headers=headers)
    balance = next(
        item for item in balances.json()["items"] if item["counterparty_id"] == cp_id
    )

    # Товар зачтён навсегда — деньги ушли: долг ровно 3 000, и он ОДИНАКОВ в реестре и плитке.
    assert registry_row["remainder"] == 3000.0
    assert balance["payable"] == registry_row["remainder"]
