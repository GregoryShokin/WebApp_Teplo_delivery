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

import asyncio
import uuid
from decimal import Decimal

import pytest
from cp_helpers import make_counterparty
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_new_payment_window import _free_expense_article

from app.models import CounterpartyPaymentDraft
from app.services.counterparty_payments import (
    CounterpartyPaymentError,
    ExpenseLineInput,
    create_expense_payment_draft,
    create_standalone_payment_draft,
    strip_bank_only_tail,
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


async def _direct_expense(
    session: AsyncSession,
    *,
    amount: str,
    purpose: str = "Услуги доставки",
    vat_rate: str | None = None,
    inn: str = "7701234567",
):
    """Черновик расхода ПО РЕКВИЗИТАМ получателя — единственный маршрут окна с НДС.

    Без получателя тот же расход уходит траншем на собственную карту ИП, и налог там запрещён
    (решение владельца 19.08.2026): перевод себе не о чем облагать.
    """
    article = await _free_expense_article(session)
    supplier = await make_counterparty(
        session,
        name="ООО Прямой",
        inn=inn,
        requisites=SUPPLIER_REQUISITES,
        requisites_verified=True,
    )
    await session.commit()
    return await create_expense_payment_draft(
        session,
        lines=[
            ExpenseLineInput(
                article_id=article.id,
                amount=Decimal(amount),
                purpose=purpose,
                counterparty_id=supplier.id,
            )
        ],
        vat_rate=vat_rate,
    )


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


def test_half_kopeck_rounds_up_and_matches_the_form() -> None:
    """Ровная половина копейки идёт ВВЕРХ — и ровно так же считает предпросмотр в окне.

    3,33 ₽ по ставке 20 % — это ровно 0,555 ₽ налога. Здесь Decimal даёт 0,56, а наивная
    формула в double (`Math.round(total * p / (100 + p) * 100) / 100`) — 0,55: окно обещало бы
    одну цифру, а в банк уходила бы другая. Фронт (`VatRateField.vatAmountFor`) поэтому считает
    в копейках целыми; эти суммы держат обе стороны вместе — правя одну, поправь и вторую.
    """
    assert vat_amount_for_rate(Decimal("3.33"), "20") == Decimal("0.56")
    assert vat_amount_for_rate(Decimal("6.15"), "20") == Decimal("1.03")
    assert vat_amount_for_rate(Decimal("615.00"), "20") == Decimal("102.50")
    # Крупная сумма того же класса: расхождение не зависит от масштаба.
    assert vat_amount_for_rate(Decimal("3647226.45"), "20") == Decimal("607871.08")


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


async def test_direct_requisites_payment_carries_vat(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Платёж ПО РЕКВИЗИТАМ контрагента — тот маршрут, где налог обязателен по-настоящему.

    Деньги уходят внешнему юрлицу, а не на собственную карту ИП: именно это назначение банк
    и налоговая читают как утверждение о налоге. Черновик прямого платежа выписывается одной
    строкой на одного контрагента (``direct_recipient``), и НДС на нём проверяется отдельно
    от via-safe маршрута — ветки разные.
    """
    async with async_session_factory() as session:
        article = await _free_expense_article(session)
        supplier = await make_counterparty(
            session,
            name="ООО Прямой",
            inn="7701234567",
            requisites=SUPPLIER_REQUISITES,
            requisites_verified=True,
        )
        await session.commit()

        draft = await create_expense_payment_draft(
            session,
            lines=[
                ExpenseLineInput(
                    article_id=article.id,
                    amount=Decimal("7984.90"),
                    purpose="Услуги доставки",
                    counterparty_id=supplier.id,
                )
            ],
            vat_rate="22",
        )
        # Прямой платёж: получатель — контрагент, а не карта ИП.
        assert draft.pays_via_safe is False
        assert draft.counterparty_id == supplier.id
        assert draft.payload["paymentPurpose"] == (
            f"Услуги доставки. В т.ч. НДС: 22% - 1439,90 руб. [TPL-{draft.id.hex[:12].upper()}]"
        )
        assert draft.vat_amount == Decimal("1439.90")


def test_expense_draft_endpoint_passes_vat_rate(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Связка «HTTP → сервис»: ставка из формы доезжает до платёжки, а не теряется в схеме.

    Схемы окна объявлены с ``extra="forbid"``, поэтому забытое поле — это не «НДС не
    применился», а 422 на всю кнопку. Тест сервиса такого не поймает.
    """

    async def _supplier() -> uuid.UUID:
        async with async_session_factory() as session:
            cp = await make_counterparty(
                session,
                name="ООО Прямой",
                inn="7701234567",
                requisites=SUPPLIER_REQUISITES,
                requisites_verified=True,
            )
            await session.commit()
            return cp.id

    counterparty_id = asyncio.run(_supplier())
    articles = client.get(
        "/api/v1/dds/new-payment/context", headers={"X-User-Role": "owner"}
    ).json()["articles"]
    article = next(
        item
        for item in articles
        if item["flow"] == "expense"
        and not item["location_required"]
        and not item["asset_link_kind"]
    )

    response = client.post(
        "/api/v1/dds/new-payment/expense-draft",
        headers={"X-User-Role": "owner"},
        json={
            "lines": [
                {
                    "article_id": article["id"],
                    "amount": 7984.90,
                    "purpose": "Услуги",
                    "counterparty_id": str(counterparty_id),
                }
            ],
            "vat_rate": "22",
        },
    )
    assert response.status_code == 201, response.text
    draft_id = uuid.UUID(response.json()["id"])

    async def _stored() -> CounterpartyPaymentDraft | None:
        async with async_session_factory() as session:
            return await session.scalar(
                select(CounterpartyPaymentDraft).where(CounterpartyPaymentDraft.id == draft_id)
            )

    draft = asyncio.run(_stored())
    assert draft is not None
    assert draft.vat_rate == "22"
    assert draft.vat_amount == Decimal("1439.90")
    assert "В т.ч. НДС: 22% - 1439,90 руб." in draft.payload["paymentPurpose"]


def test_expense_draft_endpoint_rejects_impossible_rate(client: TestClient) -> None:
    """Ставка ≥ 100 % — ошибка ввода, а не платёж: столько налога в сумме не бывает."""
    response = client.post(
        "/api/v1/dds/new-payment/expense-draft",
        headers={"X-User-Role": "owner"},
        json={
            "lines": [{"article_id": str(uuid.uuid4()), "amount": 100, "purpose": "x"}],
            "vat_rate": "122",
        },
    )
    assert response.status_code == 422, response.text


async def test_expense_draft_carries_vat_rate_into_bank_purpose(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Платёж по реквизитам со ставкой: налог выделен из итога и стоит хвостом назначения."""
    async with async_session_factory() as session:
        draft = await _direct_expense(session, amount="7984.90", vat_rate="22")
        marker = f"[TPL-{draft.id.hex[:12].upper()}]"
        assert draft.payload["paymentPurpose"] == (
            f"Услуги доставки. В т.ч. НДС: 22% - 1439,90 руб. {marker}"
        )
        # Ставка и сумма помнятся на черновике: назначение уже ушло в банк, и по нему потом
        # спрашивают, откуда взялась цифра.
        assert draft.vat_rate == "22"
        assert draft.vat_amount == Decimal("1439.90")
        # Человеческое назначение (проводка, журнал ДДС) НДС-хвоста не несёт: налог —
        # реквизит платёжки, а не описание траты.
        assert draft.target_purpose == "Услуги доставки"


async def test_vat_is_refused_on_the_ip_card_route(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Транш на собственную карту ИП налога не заявляет — решение владельца 19.08.2026.

    Деньги идут переводом СЕБЕ, а не поставщику: «в т.ч. НДС» в таком назначении утверждало бы
    то, чего никто не утверждал. Форма ставку в этом маршруте не спрашивает и не шлёт, но
    правило держится бэком — ручка внутренняя, но не единственный путь в эту функцию.
    """
    async with async_session_factory() as session:
        article = await _free_expense_article(session)
        # Без получателя — свободный вывод на карту ИП.
        with pytest.raises(CounterpartyPaymentError, match="по реквизитам получателя"):
            await create_expense_payment_draft(
                session,
                article_id=article.id,
                amount=Decimal("1000.00"),
                purpose="Канцтовары",
                vat_rate="22",
            )
        # Транш из нескольких строк — тот же маршрут, тот же запрет.
        with pytest.raises(CounterpartyPaymentError, match="по реквизитам получателя"):
            await create_expense_payment_draft(
                session,
                lines=[
                    ExpenseLineInput(
                        article_id=article.id, amount=Decimal("600.00"), purpose="Раз"
                    ),
                    ExpenseLineInput(
                        article_id=article.id, amount=Decimal("500.00"), purpose="Два"
                    ),
                ],
                vat_rate="10",
            )
        # Без ставки тот же транш проходит и уходит в банк с «Без НДС.».
        draft = await create_expense_payment_draft(
            session,
            lines=[
                ExpenseLineInput(article_id=article.id, amount=Decimal("600.00"), purpose="Раз"),
                ExpenseLineInput(article_id=article.id, amount=Decimal("500.00"), purpose="Два"),
            ],
        )
        assert draft.pays_via_safe is True
        assert "Без НДС." in draft.payload["paymentPurpose"]
        assert draft.vat_rate is None


async def test_zero_rounding_tax_leaves_no_vat_claim_at_all(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Копеечный платёж: налог округляется в ноль — и в тексте, и в записи черновика.

    0,02 ₽ по ставке 22 % дают 0,0036 ₽ налога, то есть 0,00 после округления. Платёжка
    честно говорит «Без НДС.», и черновик обязан говорить то же: запись «ставка 22 %, суммы
    налога нет» противоречила бы тексту, который человек отправил в банк.
    """
    async with async_session_factory() as session:
        draft = await _direct_expense(session, amount="0.02", purpose="Копейки", vat_rate="22")
        assert "Без НДС." in draft.payload["paymentPurpose"]
        assert draft.vat_rate is None
        assert draft.vat_amount is None


async def test_expense_purpose_keeps_vat_when_description_is_long(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Лимит платёжки съедает ОПИСАНИЕ, а не налог: НДС юридически значим, описание — нет."""
    async with async_session_factory() as session:
        draft = await _direct_expense(
            session, amount="7984.90", purpose="Услуги доставки " * 20, vat_rate="22"
        )
        purpose = draft.payload["paymentPurpose"]
        assert len(purpose) <= 210
        # Метка связи черновика с операцией выписки переживает и длинное описание, и налог:
        # под неё бюджет резервируется заранее, ужимается описание.
        assert purpose.endswith(f"В т.ч. НДС: 22% - 1439,90 руб. [TPL-{draft.id.hex[:12].upper()}]")


async def test_vat_columns_are_persisted(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Колонки НДС переживают перечитывание черновика (миграция 0275, не только питон)."""
    async with async_session_factory() as session:
        draft = await _direct_expense(
            session, amount="2200.00", purpose="Проверка хранения", vat_rate="22"
        )
        draft_id = draft.id

    async with async_session_factory() as session:
        stored = await session.scalar(
            select(CounterpartyPaymentDraft).where(CounterpartyPaymentDraft.id == draft_id)
        )
        assert stored is not None
        assert stored.vat_rate == "22"
        assert stored.vat_amount == Decimal("396.72")


def test_bank_only_tail_is_stripped_for_the_dds_journal() -> None:
    """Банковский текст → человеческий: снимаются ОБА хвоста, техметка и налог.

    ``_book_via_safe`` подписывает проводки транзита р/с→Сейф банковским назначением, когда
    у черновика нет своего (``target_purpose`` пуст у транша). Налог — реквизит платёжного
    поручения, а не описание траты: в журнале ДДС ему делать нечего, как и метке ``[TPL-…]``.
    """
    assert (
        strip_bank_only_tail("Услуги доставки. В т.ч. НДС: 22% - 1439,90 руб. [TPL-ABCDEF012345]")
        == "Услуги доставки."
    )
    assert strip_bank_only_tail("Транш 2 платежей: Раз; Два. Без НДС.") == (
        "Транш 2 платежей: Раз; Два."
    )
    # Смешанные ставки платёжки по счёту — тоже один хвост.
    assert (
        strip_bank_only_tail("Оплата поставщику. В т.ч. НДС: 10% - 10,00 руб.; 22% - 5,00 руб.")
        == "Оплата поставщику."
    )
    # Назначение без хвостов не трогаем.
    assert strip_bank_only_tail("Закуп: ООО Ромашка, счета №1, №2") == (
        "Закуп: ООО Ромашка, счета №1, №2"
    )
