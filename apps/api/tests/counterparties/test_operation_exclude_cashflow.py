"""«Исключить» на банковской операции обязано убрать её проводку из статьи ДДС.

Баг (найден 30.07.2026): ``apply_operation_action(action='exclude')`` снимало предоплаты,
ставило операции ``classification_status='excluded'`` и обнуляло якорь
``cashflow_transaction_id``, но саму строку ``CashflowTransaction`` не трогало. Журнал ДДС
листает проводки, не спрашивая статус операции, — осиротевшая строка оставалась размеченным
расходом по статье. Владелец жал «Исключить», а сумма из отчёта не уходила.

Достижимость широкая: проводку заводит ЛЮБАЯ разметка правилом (``set_article``), то есть
весь поток оплат поставщикам, а не только налоговые платежи.

Лечение — мягкое исключение (``quality_status='excluded'``), как у ручной проводки
(``cashflow_classify.apply_cashflow_exclude``): обратимо, все фильтры по нему уже написаны,
а id строки переживает исключение (на него ссылаются налоговые факты).
"""

from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal

import pytest
from cp_helpers import make_account, make_bank_operation, make_expense_article, make_wallet
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import BankOperation, CashflowTransaction
from app.services.banking.classifier import (
    OperationSplitLine,
    apply_operation_action,
    apply_operation_split,
)

HEADERS = {"X-User-Role": "finance_manager"}
WINDOW = {"from": "2026-06-01", "to": "2026-06-30"}
OP_DATE = date(2026, 6, 22)
OP_AMOUNT = "8000.00"


def _run(coro):
    return asyncio.run(coro)


async def _bank_fixture(session: AsyncSession, *, wallet_code: str | None = None):
    """Банковский кошелёк со счётом + исходящая операция выписки на нём.

    Статья намеренно нейтральная (не «Оплата поставщикам» / не «Авансы»): проверяем судьбу
    самой проводки, без правила 1 и его дебиторки — их границы закрыты другими тестами.
    """
    account = await make_account(session)
    wallet = await make_wallet(
        session, name="Р/с", wallet_type="bank", code=wallet_code, account_id=account.id
    )
    article = await make_expense_article(session, code="uslugi_svyazi", name="Услуги связи")
    operation = await make_bank_operation(
        session,
        amount=OP_AMOUNT,
        direction="out",
        account_id=account.id,
        operation_date=OP_DATE,
    )
    return wallet, article, operation


async def _operation_rows(session: AsyncSession, operation_id) -> list[CashflowTransaction]:
    return list(
        (
            await session.scalars(
                select(CashflowTransaction)
                .where(
                    CashflowTransaction.source_kind == "bank_operation",
                    CashflowTransaction.source_id == operation_id,
                )
                .order_by(CashflowTransaction.created_at, CashflowTransaction.id)
            )
        ).all()
    )


async def test_exclude_takes_the_cashflow_row_out_of_the_article(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Замок бага: после «Исключить» проводка операции помечена excluded, а не висит расходом."""
    async with async_session_factory() as session:
        _wallet, article, operation = await _bank_fixture(session)
        await session.commit()

        await apply_operation_action(
            session, operation, action="set_article", article_id=article.id
        )
        await session.commit()

        rows = await _operation_rows(session, operation.id)
        assert len(rows) == 1
        assert rows[0].article_id == article.id
        assert rows[0].quality_status != "excluded"

        await apply_operation_action(session, operation, action="exclude")
        await session.commit()

        rows = await _operation_rows(session, operation.id)
        assert len(rows) == 1, "проводку исключаем мягко, а не удаляем — исключение обратимо"
        assert rows[0].quality_status == "excluded"
        assert operation.classification_status == "excluded"
        assert operation.cashflow_transaction_id is None


async def test_reclassify_after_exclude_revives_the_same_row(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Повторная разметка возвращает сумму в статью — той же строкой, без исключённого дубля.

    Тот же ``id`` принципиален: на него ссылаются налоговые факты
    (``TaxPayment.cashflow_transaction_id``), и после возврата ссылка обязана указывать на
    действующую проводку, а не на исключённую копию.
    """
    async with async_session_factory() as session:
        _wallet, article, operation = await _bank_fixture(session)
        await session.commit()

        await apply_operation_action(
            session, operation, action="set_article", article_id=article.id
        )
        await session.commit()
        original_id = (await _operation_rows(session, operation.id))[0].id

        await apply_operation_action(session, operation, action="exclude")
        await session.commit()

        await apply_operation_action(
            session, operation, action="set_article", article_id=article.id
        )
        await session.commit()

        rows = await _operation_rows(session, operation.id)
        assert len(rows) == 1, "вторая проводка на тот же платёж — двойной расход в журнале"
        assert rows[0].id == original_id
        assert rows[0].quality_status != "excluded"
        assert rows[0].article_id == article.id
        assert operation.cashflow_transaction_id == original_id
        assert operation.classification_status == "classified"


async def test_exclude_does_not_move_the_bank_wallet_balance(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Баланс банка идёт от ВЫПИСКИ — проводка его не двигает ни в одну сторону.

    Исключение убирает из баланса саму операцию (``classification_status='excluded'``,
    ``wallet_movement_deltas``) — это by design. Мягкое исключение её проводки не смеет
    добавить к этому второй, уже двойной, эффект.
    """
    from app.services.wallet_balance_as_of import wallet_movement_deltas

    async with async_session_factory() as session:
        wallet, article, operation = await _bank_fixture(session)
        await session.commit()

        await apply_operation_action(
            session, operation, action="set_article", article_id=article.id
        )
        await session.commit()
        classified = (await wallet_movement_deltas(session))[wallet.id]
        assert classified == Decimal(f"-{OP_AMOUNT}")

        await apply_operation_action(session, operation, action="exclude")
        await session.commit()
        assert (await wallet_movement_deltas(session)).get(wallet.id, Decimal("0")) == Decimal(
            "0"
        ), "ушла ровно операция выписки; проводка вычлась бы вторым разом"

        await apply_operation_action(
            session, operation, action="set_article", article_id=article.id
        )
        await session.commit()
        assert (await wallet_movement_deltas(session))[wallet.id] == classified


async def test_exclude_marks_every_split_line(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """У мультисплита исключаются ВСЕ доли, а повторная разметка схлопывает их в одну строку.

    По той же причине, что и снятие дебиторки всех долей: по якорю ушла бы лишь первая, а
    остальные остались бы расходом по своим статьям без денег за ними.
    """
    async with async_session_factory() as session:
        _wallet, article, operation = await _bank_fixture(session)
        await session.commit()

        await apply_operation_split(
            session,
            operation,
            splits=[
                OperationSplitLine(article.id, Decimal("5000.00")),
                OperationSplitLine(article.id, Decimal("3000.00")),
            ],
        )
        await session.commit()
        assert len(await _operation_rows(session, operation.id)) == 2

        await apply_operation_action(session, operation, action="exclude")
        await session.commit()

        rows = await _operation_rows(session, operation.id)
        assert [row.quality_status for row in rows] == ["excluded", "excluded"]

        await apply_operation_action(
            session, operation, action="set_article", article_id=article.id
        )
        await session.commit()

        rows = await _operation_rows(session, operation.id)
        assert len(rows) == 1, "статья на всю сумму — лишние доли прошлого разбора уходят"
        assert rows[0].amount == Decimal(OP_AMOUNT)
        assert rows[0].quality_status != "excluded"


async def test_exclude_keeps_a_prebooked_row_of_another_circuit(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Чужую prebooked-проводку исключение не трогает — её факт завёл доменный контур.

    Операция лишь ссылалась на неё (оплата поставщику из черновика). Пометить её excluded
    значило бы стереть чужой денежный факт; отменяется он там, где создан.
    """
    async with async_session_factory() as session:
        wallet, article, operation = await _bank_fixture(session)
        prebooked = CashflowTransaction(
            wallet_id=wallet.id,
            direction="out",
            amount=Decimal(OP_AMOUNT),
            operation_date=OP_DATE,
            article_id=article.id,
            source_kind="counterparty_payment",
            payment_purpose="Оплата по счёту",
            quality_status="final",
        )
        session.add(prebooked)
        await session.flush()
        operation.cashflow_transaction_id = prebooked.id
        operation.classification_status = "classified"
        await session.commit()

        await apply_operation_action(session, operation, action="exclude")
        await session.commit()

        await session.refresh(prebooked)
        assert prebooked.quality_status == "final"
        assert operation.cashflow_transaction_id is None


async def test_exclude_refuses_while_an_asset_hangs_on_the_row(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Проводку с привязанным ОС не исключаем молча — отказываем, как ручной переразметке.

    Тот же гард, что в ``classify_transaction``: иначе объект остался бы со стоимостью из
    денег, которых в учёте больше нет, а капитализация могла уже уехать в закрытый месяц.
    Мягкое исключение — новый путь к этой дыре, поэтому гард нужен и здесь.
    """
    from app.models import AssetCashflowLink, FixedAsset
    from app.services.asset_analytics import AssetLinkError

    async with async_session_factory() as session:
        _wallet, article, operation = await _bank_fixture(session)
        article.asset_link_kind = "purchase"
        asset = FixedAsset(
            name="Печь для пиццы",
            initial_cost=Decimal(OP_AMOUNT),
            useful_life_months=84,
            commissioned_on=OP_DATE,
            status="in_use",
            valuation_basis="payment",
        )
        session.add(asset)
        await session.flush()
        await session.commit()

        await apply_operation_action(
            session, operation, action="set_article", article_id=article.id
        )
        await session.commit()
        booked = (await _operation_rows(session, operation.id))[0]
        session.add(
            AssetCashflowLink(
                asset_id=asset.id,
                cashflow_transaction_id=booked.id,
                kind="purchase",
                amount=Decimal(OP_AMOUNT),
            )
        )
        await session.commit()

        # id держим отдельно: после rollback объекты сессии истекают, и обращение к
        # ``operation.id`` полезло бы за ленивой загрузкой уже вне async-контекста.
        operation_id = operation.id

        with pytest.raises(AssetLinkError) as error:
            await apply_operation_action(session, operation, action="exclude")
        assert "Печь для пиццы" in str(error.value)

        await session.rollback()
        rows = await _operation_rows(session, operation_id)
        assert rows[0].quality_status != "excluded", "отказ не оставляет операцию исключённой"


@pytest.mark.parametrize("action", ["exclude", "mark_internal_transfer"])
def test_journal_stops_showing_the_excluded_row_as_an_expense(
    client: TestClient,
    async_session_factory: async_sessionmaker[AsyncSession],
    action: str,
) -> None:
    """Витрина владельца: строка по статье приходит «Исключено», а не «Размечено».

    Внутренний перевод проверяем тем же замком — ветка кода общая, и операция-перевод так же
    перестаёт быть расходом по статье.
    """

    async def seed() -> dict[str, str]:
        async with async_session_factory() as session:
            _wallet, article, operation = await _bank_fixture(session, wallet_code=f"w-{action}")
            await session.commit()
            await apply_operation_action(
                session, operation, action="set_article", article_id=article.id
            )
            await session.commit()
            return {"article_id": str(article.id), "operation_id": str(operation.id)}

    ids = _run(seed())

    def article_rows() -> list[dict]:
        response = client.get(
            "/api/v1/dds/journal",
            params={"status": "marked", "article_id": ids["article_id"], **WINDOW},
            headers=HEADERS,
        )
        assert response.status_code == 200, response.text
        return response.json()["items"]

    before = article_rows()
    assert [row["status"] for row in before] == ["classified"]

    async def act() -> None:
        async with async_session_factory() as session:
            operation = await session.get(BankOperation, ids["operation_id"])
            await apply_operation_action(session, operation, action=action)
            await session.commit()

    _run(act())

    after = article_rows()
    assert [row["status"] for row in after] == ["excluded"]
    assert [row["id"] for row in after] == [row["id"] for row in before]
