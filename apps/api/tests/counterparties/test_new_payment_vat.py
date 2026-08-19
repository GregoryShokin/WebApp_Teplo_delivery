"""НДС платежа из окна «Новый платёж» — до назначения платёжного поручения.

Назначение обязано называть налог («в т.ч. НДС 22% — 1 439,90») или прямо говорить «Без НДС»:
это читают банк и налоговая. У платёжки по СЧЁТУ налог берётся с накладной (``vat_total``,
см. ``test_invoice_vat_to_purpose``), а свободный расход и предоплата поставщику счёта не
имеют — и уходили в банк вообще без упоминания налога, каким бы он ни был.

Решение владельца 19.08.2026: человек называет СТАВКУ, сумма выделяется из итога платежа
(«в том числе»), по умолчанию — без НДС. Выделяется, а не начисляется сверху: сумму платежа
согласовали с получателем, менять её из-за выбора ставки нельзя.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from cp_helpers import make_counterparty
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_new_payment_window import _free_expense_article

from app.services.counterparty_payments import (
    ExpenseLineInput,
    create_expense_payment_draft,
    create_standalone_payment_draft,
)
from app.services.vat import (
    VAT_RATES,
    normalize_vat_rate,
    validate_vat_rate,
    vat_amount_for_rate,
)

SUPPLIER_REQUISITES = {
    "bankAcnt": "40702810400000012349",
    "bankBik": "044525225",
    "recipientCorrAccountNumber": "30101810400000000225",
}


def test_vat_is_extracted_from_the_total_not_added_on_top() -> None:
    """Налог «в том числе»: 7 984,90 при 22 % → 1 439,90, а не 1 756,68.

    Начислить сверху значило бы отправить в банк не ту сумму, которую согласовали с
    получателем. Цифра сверена с живым счётом СДЭК, на котором построен НДС-контур счетов.
    """
    assert vat_amount_for_rate(Decimal("7984.90"), "22") == Decimal("1439.90")
    assert vat_amount_for_rate(Decimal("1100.00"), "10") == Decimal("100.00")
    assert vat_amount_for_rate(Decimal("105.00"), "5") == Decimal("5.00")
    # Ставки нет — налога нет; отрицательного налога не бывает.
    assert vat_amount_for_rate(Decimal("1000.00"), None) == Decimal("0.00")
    assert vat_amount_for_rate(Decimal("1000.00"), "0") == Decimal("0.00")


def test_rate_normalisation_and_validation() -> None:
    assert normalize_vat_rate("22 %") == "22"
    assert normalize_vat_rate("  ") == ""
    assert normalize_vat_rate("0") == ""
    assert validate_vat_rate(None) is None
    assert validate_vat_rate("") is None
    assert validate_vat_rate("22") == "22"
    # Сотня и выше — ошибка ввода: столько налога в сумме платежа не бывает.
    with pytest.raises(ValueError, match="Ставка НДС"):
        validate_vat_rate("122")
    # Список окна и то, что принимает бэк, живут в одном месте — иначе кнопка отдаст 422.
    assert all(validate_vat_rate(rate) == rate for rate in VAT_RATES)


async def test_expense_draft_carries_vat_rate_into_bank_purpose(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Свободный расход со ставкой: налог выделен из итога и стоит хвостом назначения."""
    async with async_session_factory() as session:
        article = await _free_expense_article(session)
        draft = await create_expense_payment_draft(
            session,
            article_id=article.id,
            amount=Decimal("7984.90"),
            purpose="Услуги доставки",
            vat_rate="22",
        )
        marker = f"[TPL-{draft.id.hex[:12].upper()}]"
        assert draft.payload["paymentPurpose"] == (
            f"Услуги доставки. В т.ч. НДС: 22% - 1439,90 руб. {marker}"
        )
        # Ставка и сумма помнятся на черновике: назначение уже ушло в банк, и по нему потом
        # спрашивают, откуда взялась цифра.
        assert draft.vat_rate == "22"
        assert draft.vat_amount == Decimal("1439.90")
        # Человеческое назначение (целёвка Сейфа, журнал ДДС) НДС-хвоста не несёт: налог —
        # реквизит платёжки, а не описание траты.
        assert draft.target_purpose == "Услуги доставки"


async def test_expense_draft_without_rate_says_no_vat(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Ставку не задали — в банк уходит «Без НДС.», а не молчание.

    До этой правки свободный расход не говорил о налоге вообще: банк получал назначение,
    из которого не следует ни наличие налога, ни его отсутствие.
    """
    async with async_session_factory() as session:
        article = await _free_expense_article(session)
        draft = await create_expense_payment_draft(
            session, article_id=article.id, amount=Decimal("1000.00"), purpose="Канцтовары"
        )
        assert "Без НДС." in draft.payload["paymentPurpose"]
        assert draft.vat_rate is None
        assert draft.vat_amount is None


async def test_expense_purpose_keeps_vat_when_description_is_long(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Лимит платёжки съедает ОПИСАНИЕ, а не налог: НДС юридически значим, описание — нет."""
    async with async_session_factory() as session:
        article = await _free_expense_article(session)
        draft = await create_expense_payment_draft(
            session,
            article_id=article.id,
            amount=Decimal("7984.90"),
            purpose="Услуги доставки " * 20,
            vat_rate="22",
        )
        purpose = draft.payload["paymentPurpose"]
        assert len(purpose) <= 210
        # Метка связи черновика с операцией выписки переживает и длинное описание, и налог:
        # под неё бюджет резервируется заранее, ужимается описание.
        assert purpose.endswith(f"В т.ч. НДС: 22% - 1439,90 руб. [TPL-{draft.id.hex[:12].upper()}]")


async def test_expense_tranche_shares_one_vat_line(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Транш из нескольких строк: НДС общий на черновик — банк списывает одну сумму."""
    async with async_session_factory() as session:
        article = await _free_expense_article(session)
        draft = await create_expense_payment_draft(
            session,
            lines=[
                ExpenseLineInput(article_id=article.id, amount=Decimal("600.00"), purpose="Раз"),
                ExpenseLineInput(article_id=article.id, amount=Decimal("500.00"), purpose="Два"),
            ],
            vat_rate="10",
        )
        assert draft.amount == Decimal("1100.00")
        assert draft.vat_amount == Decimal("100.00")
        assert draft.payload["paymentPurpose"].startswith("Транш 2 платежей: Раз; Два.")
        assert "В т.ч. НДС: 10% - 100,00 руб." in draft.payload["paymentPurpose"]


async def test_supplier_prepayment_draft_carries_vat(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Предоплата поставщику по реквизитам — платёж внешнему получателю, налог обязателен."""
    async with async_session_factory() as session:
        supplier = await make_counterparty(
            session,
            name="ООО Поставщик",
            inn="7707083893",
            requisites=SUPPLIER_REQUISITES,
            requisites_verified=True,
        )
        await session.commit()

        draft = await create_standalone_payment_draft(
            session, counterparty_id=supplier.id, amount=Decimal("7984.90"), vat_rate="22"
        )
        purpose = draft.payload["paymentPurpose"]
        assert purpose.startswith("Предоплата поставщику ООО Поставщик.")
        assert "В т.ч. НДС: 22% - 1439,90 руб." in purpose
        assert purpose.endswith(f"[TPL-{draft.id.hex[:12].upper()}]")
        assert len(purpose) <= 210
        assert draft.vat_rate == "22"
        assert draft.vat_amount == Decimal("1439.90")

        plain = await create_standalone_payment_draft(
            session, counterparty_id=supplier.id, amount=Decimal("500.00")
        )
        assert "Без НДС." in plain.payload["paymentPurpose"]
        assert plain.vat_rate is None


async def test_vat_columns_are_persisted(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Колонки НДС переживают перечитывание черновика (миграция 0275, не только питон)."""
    async with async_session_factory() as session:
        article = await _free_expense_article(session)
        draft = await create_expense_payment_draft(
            session,
            article_id=article.id,
            amount=Decimal("2200.00"),
            purpose="Проверка хранения",
            vat_rate="22",
        )
        draft_id = draft.id

    async with async_session_factory() as session:
        from app.models import CounterpartyPaymentDraft

        stored = await session.scalar(
            select(CounterpartyPaymentDraft).where(CounterpartyPaymentDraft.id == draft_id)
        )
        assert stored is not None
        assert stored.vat_rate == "22"
        assert stored.vat_amount == Decimal("396.72")
