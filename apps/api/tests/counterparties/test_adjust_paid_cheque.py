"""Правка ОПЛАЧЕННОГО чека Кассы («Исправить оплаченную»): статья ДДС строки и пометка
возврата обязаны пережить пересборку позиций.

Чек Кассы (``source='kassa_cheque'``) держит признак «расход vs склад» в СТАТЬЕ строки, а не
в ``is_staff`` (он у чека всегда false), и помечает возвращённые в магазин позиции
``is_return``: они остаются в чеке для сверки с бумажным чеком копейка в копейку, но не
проводятся. Пересборка строк общая с накладной (``_rebuild_invoice_lines``), поэтому оба
признака должны сохраняться — иначе расходная строка становится товарной (уйдёт в iiko
приходом на склад), возврат — проведённым, а сумма чека вырастает с net до gross.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from cp_helpers import (
    make_account,
    make_bank_operation,
    make_counterparty,
    make_expense_article,
    make_iiko_product,
    make_wallet,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import AppSetting, InvoiceLineItem
from app.services.kassa.cheque import ChequeBankPart, ChequeLineInput, create_cheque
from app.services.warehouse_invoice_push import prepare_push
from app.services.warehouse_invoices import (
    LineInput,
    WarehouseInvoiceError,
    adjust_paid_invoice,
)

pytestmark = pytest.mark.usefixtures("migrated_db")

ISSUED = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
OP_DATE = date(2026, 7, 20)


async def _card_op(session: AsyncSession, *, amount: str):
    """Счёт + карт-кошелёк на нём + card-операция — чем оплачивается чек Кассы."""
    account = await make_account(session)
    wallet = await make_wallet(
        session, name="Тинькофф карта", wallet_type="bank", account_id=account.id
    )
    op = await make_bank_operation(
        session,
        amount=amount,
        operation_date=OP_DATE,
        posted_at=ISSUED,
        category="cardOperation",
        account_id=account.id,
    )
    return wallet, op


async def _mixed_cheque(session: AsyncSession):
    """Чек на 600 ₽ «как с кассы»: склад 300 + расход 200 + возвращённая позиция 100.

    Проводится net (500): возвращённая в магазин позиция в оплату не идёт.
    """
    supplier_article = await make_expense_article(
        session, code="payment_to_supplier", name="Оплата поставщикам"
    )
    staff_article = await make_expense_article(
        session, code="staff_meals", name="Расходы на питание персонала"
    )
    cp = await make_counterparty(session, name="Местный закуп")
    product = await make_iiko_product(session, name="Лук")
    returned_product = await make_iiko_product(session, name="Творог")
    _wallet, op = await _card_op(session, amount="600.00")
    await session.commit()

    cheque = await create_cheque(
        session,
        counterparty_id=cp.id,
        issued_at=ISSUED,
        bank_parts=[ChequeBankPart(bank_operation_id=op.id)],
        track_nomenclature=True,
        lines=[
            ChequeLineInput(
                name="Лук",
                quantity=Decimal("3"),
                unit="кг",
                price=Decimal("100.00"),
                amount=Decimal("300.00"),
                dds_article_id=supplier_article.id,
                iiko_product_id=product.id,
            ),
            ChequeLineInput(
                name="Обед смены",
                quantity=Decimal("1"),
                price=Decimal("200.00"),
                amount=Decimal("200.00"),
                dds_article_id=staff_article.id,
            ),
            ChequeLineInput(
                name="Творог",
                quantity=Decimal("1"),
                unit="кг",
                price=Decimal("100.00"),
                amount=Decimal("100.00"),
                dds_article_id=supplier_article.id,
                iiko_product_id=returned_product.id,
                is_return=True,
            ),
        ],
    )
    return cheque, supplier_article, staff_article, product, returned_product


async def _lines(session: AsyncSession, invoice_id) -> list[InvoiceLineItem]:
    return list(
        (
            await session.scalars(
                select(InvoiceLineItem)
                .where(InvoiceLineItem.invoice_id == invoice_id)
                .order_by(InvoiceLineItem.sort_order)
            )
        ).all()
    )


async def test_adjust_paid_cheque_keeps_line_articles_and_returns(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Правка оплаченного чека сохраняет статью ДДС каждой строки и пометку возврата.

    Иначе расходная строка «Расходы на питание персонала» теряет статью, по критерию
    ``line_is_goods`` становится товарной и при следующей выгрузке уходит в iiko приходом
    на склад, а разнос чека по статьям ДДС рассыпается.
    """
    async with async_session_factory() as session:
        cheque, supplier_article, staff_article, product, returned_product = await _mixed_cheque(
            session
        )
        assert cheque.amount == Decimal("500.00")  # net: 300 + 200, возврат 100 не проводится

        # Правка «как из окна»: те же позиции, у расходной чуть другая сумма.
        await adjust_paid_invoice(
            session,
            cheque,
            lines=[
                LineInput(
                    name="Лук",
                    quantity=Decimal("3"),
                    price=Decimal("100.00"),
                    sum=Decimal("300.00"),
                    iiko_product_id=product.id,
                    dds_article_id=supplier_article.id,
                ),
                LineInput(
                    name="Обед смены",
                    quantity=Decimal("1"),
                    price=Decimal("200.00"),
                    sum=Decimal("200.00"),
                    dds_article_id=staff_article.id,
                ),
                LineInput(
                    name="Творог",
                    quantity=Decimal("1"),
                    price=Decimal("100.00"),
                    sum=Decimal("100.00"),
                    iiko_product_id=returned_product.id,
                    dds_article_id=supplier_article.id,
                    is_return=True,
                ),
            ],
        )

        lines = await _lines(session, cheque.id)
        assert [line.dds_article_id for line in lines] == [
            supplier_article.id,
            staff_article.id,
            supplier_article.id,
        ]
        assert [line.is_return for line in lines] == [False, False, True]


async def test_adjust_paid_cheque_amount_stays_net(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Сумма исправленного чека считается net: возвращённая позиция в неё не входит.

    Иначе amount уходит в gross (600 против оплаченных 500), чек становится
    «частично оплаченным» и у местного закупа появляется несуществующий долг.
    """
    async with async_session_factory() as session:
        cheque, supplier_article, staff_article, product, returned_product = await _mixed_cheque(
            session
        )

        await adjust_paid_invoice(
            session,
            cheque,
            lines=[
                LineInput(
                    name="Лук",
                    quantity=Decimal("3"),
                    price=Decimal("100.00"),
                    sum=Decimal("300.00"),
                    iiko_product_id=product.id,
                    dds_article_id=supplier_article.id,
                ),
                LineInput(
                    name="Обед смены",
                    quantity=Decimal("1"),
                    price=Decimal("200.00"),
                    sum=Decimal("200.00"),
                    dds_article_id=staff_article.id,
                ),
                LineInput(
                    name="Творог",
                    quantity=Decimal("1"),
                    price=Decimal("100.00"),
                    sum=Decimal("100.00"),
                    iiko_product_id=returned_product.id,
                    dds_article_id=supplier_article.id,
                    is_return=True,
                ),
            ],
        )
        await session.refresh(cheque)

        assert cheque.amount == Decimal("500.00")
        assert cheque.payment_status == "paid"


async def test_adjusted_cheque_pushes_only_goods_to_iiko(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """После правки в iiko уходит только складская позиция — расход и возврат остаются у нас."""
    async with async_session_factory() as session:
        cheque, supplier_article, staff_article, product, returned_product = await _mixed_cheque(
            session
        )
        # Пуш требует iiko-GUID контрагента и настроенного склада.
        from app.models import CounterpartyAlias

        session.add(
            CounterpartyAlias(
                counterparty_id=cheque.counterparty_id, source="iiko", alias="cp-guid"
            )
        )
        session.add(
            AppSetting(
                key="iiko.default_store_guid",
                value={"guid": "ST-1"},
                value_type="json",
                category="iiko",
                display_name="Склад",
                widget_type="json",
            )
        )
        await session.commit()

        await adjust_paid_invoice(
            session,
            cheque,
            lines=[
                LineInput(
                    name="Лук",
                    quantity=Decimal("3"),
                    price=Decimal("100.00"),
                    sum=Decimal("300.00"),
                    iiko_product_id=product.id,
                    dds_article_id=supplier_article.id,
                ),
                LineInput(
                    name="Обед смены",
                    quantity=Decimal("1"),
                    price=Decimal("200.00"),
                    sum=Decimal("200.00"),
                    dds_article_id=staff_article.id,
                ),
                LineInput(
                    name="Творог",
                    quantity=Decimal("1"),
                    price=Decimal("100.00"),
                    sum=Decimal("100.00"),
                    iiko_product_id=returned_product.id,
                    dds_article_id=supplier_article.id,
                    is_return=True,
                ),
            ],
        )

        prepared = await prepare_push(session, cheque)
        assert prepared.doc is not None, prepared.skip_reason
        assert [line.product for line in prepared.doc.lines] == [product.iiko_id]


async def test_adjust_paid_cheque_store_line_still_needs_product(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Складская строка (статья «Оплата поставщикам») без номенклатуры не сохраняется.

    Статья «Оплата поставщикам» — товарная: такая строка уходит в iiko, а без
    ``product_guid`` там молча отбрасывается. Гард обязан ловить её так же, как строку
    вообще без статьи.
    """
    async with async_session_factory() as session:
        cheque, supplier_article, staff_article, product, _returned = await _mixed_cheque(session)

        with pytest.raises(WarehouseInvoiceError, match="номенклатуры iiko"):
            await adjust_paid_invoice(
                session,
                cheque,
                lines=[
                    LineInput(
                        name="Лук без товара",
                        quantity=Decimal("3"),
                        price=Decimal("100.00"),
                        sum=Decimal("300.00"),
                        dds_article_id=supplier_article.id,
                    ),
                ],
            )


async def _expense_only_cheque(session: AsyncSession):
    """Чек на 210,50 ₽ с ОДНОЙ расходной строкой без номенклатуры («Содержание торговых точек»).

    Так на кассе заводят покупку «мимо склада»; в iiko приходом она не идёт, поэтому пуш
    помечает накладную ``skipped``. Ровно это состояние было у чеков Ч-54/Ч-55 (27.07.2026).
    """
    supplier_article = await make_expense_article(
        session, code="payment_to_supplier", name="Оплата поставщикам"
    )
    expense_article = await make_expense_article(
        session, code="venue_upkeep", name="Содержание торговых точек"
    )
    cp = await make_counterparty(session, name="Местный закуп")
    product = await make_iiko_product(session, name="Мешок для мусора 120л", unit="шт")
    _wallet, op = await _card_op(session, amount="210.50")
    await session.commit()

    cheque = await create_cheque(
        session,
        counterparty_id=cp.id,
        issued_at=ISSUED,
        bank_parts=[ChequeBankPart(bank_operation_id=op.id)],
        track_nomenclature=True,
        lines=[
            ChequeLineInput(
                name="Мешок для мусора 120л",
                quantity=Decimal("1"),
                price=Decimal("210.50"),
                amount=Decimal("210.50"),
                dds_article_id=expense_article.id,
            )
        ],
    )
    # Состояние после пуша, который пропустил документ: вердикт «нет товарных строк» закэширован.
    cheque.iiko_push_status = "skipped"
    cheque.iiko_push_error = "Нет товарных строк с iiko-GUID (персонал/ручные)"
    await session.commit()
    return cheque, supplier_article, expense_article, product


async def _allow_push(session: AsyncSession, counterparty_id) -> None:
    """iiko-GUID поставщика + склад по умолчанию — без них ``prepare_push`` откажет раньше строк."""
    from app.models import CounterpartyAlias

    session.add(
        CounterpartyAlias(counterparty_id=counterparty_id, source="iiko", alias="cp-guid")
    )
    session.add(
        AppSetting(
            key="iiko.default_store_guid",
            value={"guid": "ST-1"},
            value_type="json",
            category="iiko",
            display_name="Склад",
            widget_type="json",
        )
    )
    await session.commit()


async def test_adjust_paid_cheque_revives_push_after_nomenclature_added(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Правка оплаченного чека, добавившая номенклатуру, снимает залипший вердикт ``skipped``.

    ``iiko_push_status`` — это КЭШ ответа ``prepare_push``, а не свойство документа. Пока его не
    сбрасывали (правка оплаченной трогала iiko-контур только при наличии ``external_id``), чек,
    у которого расходную строку исправили на товарную, навсегда оставался «Не отправляется в
    iiko»: у этого статуса в карточке нет даже кнопки отправки. Кейс Ч-54/Ч-55.
    """
    async with async_session_factory() as session:
        cheque, supplier_article, _expense_article, product = await _expense_only_cheque(session)
        await _allow_push(session, cheque.counterparty_id)

        await adjust_paid_invoice(
            session,
            cheque,
            lines=[
                LineInput(
                    name="Мешок для мусора 120л",
                    quantity=Decimal("1"),
                    price=Decimal("210.50"),
                    sum=Decimal("210.50"),
                    iiko_product_id=product.id,
                    dds_article_id=supplier_article.id,
                )
            ],
        )
        await session.refresh(cheque)

        assert cheque.external_id is None  # в iiko документа не было — разворачивать нечего
        assert cheque.iiko_return_status == "none"  # контур коррекции не запускается
        assert cheque.iiko_push_status == "not_pushed"
        assert cheque.iiko_push_error is None
        # Классификация снова идёт ПО ПОЗИЦИЯМ: товар с GUID есть → накладную можно отправить.
        prepared = await prepare_push(session, cheque)
        assert prepared.doc is not None, prepared.skip_reason
        assert [line.product for line in prepared.doc.lines] == [product.iiko_id]


async def test_adjust_paid_cheque_without_goods_is_skipped_again(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Сброс статуса не выдаёт расходный чек за отправляемый: вердикт заново выносит
    ``prepare_push``, и без товарных строк он снова «нет товарных строк с iiko-GUID»."""
    async with async_session_factory() as session:
        cheque, _supplier_article, expense_article, _product = await _expense_only_cheque(session)
        await _allow_push(session, cheque.counterparty_id)

        await adjust_paid_invoice(
            session,
            cheque,
            lines=[
                LineInput(
                    name="Мешок для мусора 120л",
                    quantity=Decimal("1"),
                    price=Decimal("210.50"),
                    sum=Decimal("210.50"),
                    dds_article_id=expense_article.id,
                )
            ],
        )
        await session.refresh(cheque)

        assert cheque.iiko_push_status == "not_pushed"  # кэш сброшен
        prepared = await prepare_push(session, cheque)
        assert prepared.doc is None
        assert prepared.skip_reason == "Нет товарных строк с iiko-GUID (персонал/ручные)"
