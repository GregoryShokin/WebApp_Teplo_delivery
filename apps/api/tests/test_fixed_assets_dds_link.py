"""Связь денежной проводки с основным средством: правило, общее для всех входов ДДС.

Расчётная часть модуля лежит в ``tests/counterparties/test_fixed_assets.py``, HTTP-слой —
в ``tests/test_fixed_assets_api.py``. Здесь — только граница «статья ДДС ↔ карточка ОС».
"""

from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

sys.path.append(str(Path(__file__).parent / "counterparties"))

from cp_helpers import (  # noqa: E402
    make_account,
    make_bank_operation,
    make_expense_article,
    make_wallet,
)

from app.models import (  # noqa: E402
    AssetCashflowLink,
    CashflowTransaction,
    DdsArticle,
    FixedAsset,
    Wallet,
)
from app.services.asset_analytics import (  # noqa: E402
    AssetLinkError,
    ensure_asset_link_survives,
    find_unlinked_asset_payments,
    link_transaction_to_asset,
    resolve_asset_context,
    unlink_transaction,
)
from app.services.banking.classifier import (  # noqa: E402
    OperationSplitLine,
    apply_operation_split,
)


async def _article(session: AsyncSession, *, name: str, kind: str | None) -> DdsArticle:
    article = DdsArticle(
        code=f"test_{name.lower().replace(' ', '_')}",
        name=name,
        movement_type="outflow",
        activity_type="investing",
        asset_link_kind=kind,
    )
    session.add(article)
    await session.flush()
    return article


async def _asset(session: AsyncSession, *, cost: str, status: str = "in_use") -> FixedAsset:
    asset = FixedAsset(
        name="Печь для пиццы",
        initial_cost=Decimal(cost),
        useful_life_months=84,
        commissioned_on=date(2026, 8, 1),
        status=status,
        valuation_basis="payment",
    )
    session.add(asset)
    await session.flush()
    return asset


async def _transaction(session: AsyncSession, amount: str) -> CashflowTransaction:
    wallet = await session.scalar(select(Wallet))
    assert wallet is not None, "в базовом наборе должен быть хотя бы один кошелёк"
    transaction = CashflowTransaction(
        wallet_id=wallet.id,
        direction="out",
        amount=Decimal(amount),
        operation_date=date(2026, 8, 15),
        source_kind="test_asset_link",
        payment_purpose="Оплата оборудования",
        quality_status="final",
    )
    session.add(transaction)
    await session.flush()
    return transaction


async def test_purchase_article_demands_an_asset_card(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Без карточки покупка ОС уходит в расход мимо баланса — ради этого гейт и заводился."""
    async with async_session_factory() as session:
        article = await _article(session, name="Покупка ОС", kind="purchase")

        with pytest.raises(AssetLinkError) as error:
            await resolve_asset_context(
                session, article=article, asset_id=None, amount=Decimal("95000.00")
            )
        assert "Покупка ОС" in str(error.value)


async def test_asset_on_unrelated_article_is_rejected(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Обратное правило: объект на статье без признака — мусор, его никто не читает.

    Ровно так же ``location_analytics`` отвергает помещение на статье без ``location_required``.
    """
    async with async_session_factory() as session:
        article = await _article(session, name="Аренда помещения", kind=None)
        asset = await _asset(session, cost="95000.00")

        with pytest.raises(AssetLinkError):
            await resolve_asset_context(
                session, article=article, asset_id=asset.id, amount=Decimal("1000.00")
            )


async def test_disposed_asset_cannot_take_new_money(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Выбывший объект расход не принимает, а неработающий — принимает: его ещё чинят."""
    async with async_session_factory() as session:
        article = await _article(session, name="Ремонт ОС", kind="repair")
        sold = await _asset(session, cost="95000.00", status="sold")
        broken = await _asset(session, cost="95000.00", status="not_working")

        with pytest.raises(AssetLinkError):
            await resolve_asset_context(
                session, article=article, asset_id=sold.id, amount=Decimal("1000.00")
            )

        context = await resolve_asset_context(
            session, article=article, asset_id=broken.id, amount=Decimal("1000.00")
        )
        assert context.asset_id == broken.id


async def test_repair_article_splits_by_the_fifteen_percent_rule(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Правило владельца: меньше 15% — расход периода, больше — капитализация."""
    async with async_session_factory() as session:
        article = await _article(session, name="Ремонт ОС", kind="repair")
        asset = await _asset(session, cost="100000.00")

        small = await resolve_asset_context(
            session, article=article, asset_id=asset.id, amount=Decimal("14999.00")
        )
        assert (small.link_kind, small.capitalize) == ("repair", False)

        big = await resolve_asset_context(
            session, article=article, asset_id=asset.id, amount=Decimal("30000.00")
        )
        assert (big.link_kind, big.capitalize) == ("upgrade", True)

        # Ровно на границе решает владелец, а расход проводим как ремонт: занизить стоимость
        # объекта безопаснее, чем завысить баланс на спорную сумму.
        edge = await resolve_asset_context(
            session, article=article, asset_id=asset.id, amount=Decimal("15000.00")
        )
        assert (edge.link_kind, edge.capitalize) == ("repair", False)
        assert edge.review_reason is not None


async def test_maintenance_article_never_capitalizes(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """«Ремонт оборудования» — операционная статья: объект указать надо, стоимость не трогаем.

    Даже расход в половину стоимости объекта остаётся расходом периода: правило 15% относится
    к капитальному ремонту, а не к текущему.
    """
    async with async_session_factory() as session:
        article = await _article(session, name="Ремонт оборудования", kind="maintenance")
        asset = await _asset(session, cost="100000.00")

        context = await resolve_asset_context(
            session, article=article, asset_id=asset.id, amount=Decimal("50000.00")
        )
        assert (context.link_kind, context.capitalize) == ("repair", False)

        with pytest.raises(AssetLinkError):
            await resolve_asset_context(
                session, article=article, asset_id=None, amount=Decimal("500.00")
            )


async def test_upgrade_asks_the_owner_instead_of_raising_the_base_silently(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Вердикт «модернизация» записывается, а стоимость меняет человек.

    Прибавить сумму к стоимости прямо здесь было заманчиво, но переразбор операции удаляет
    прежние проводки и заводит новые с другими идентификаторами — идемпотентность по проводке
    теряется, и вторая капитализация легла бы поверх первой. Откатить её нельзя: она могла уже
    уехать в закрытый месяц. Поэтому объект уходит владельцу с готовой цифрой.
    """
    async with async_session_factory() as session:
        article = await _article(session, name="Ремонт ОС", kind="repair")
        asset = await _asset(session, cost="100000.00")
        transaction = await _transaction(session, "30000.00")

        context = await resolve_asset_context(
            session, article=article, asset_id=asset.id, amount=Decimal("30000.00")
        )
        await link_transaction_to_asset(
            session, context=context, transaction_id=transaction.id, amount=Decimal("30000.00")
        )
        await session.commit()

        assert Decimal(str(asset.initial_cost)) == Decimal("100000.00")
        assert asset.review_status == "requires_owner_review"
        assert "130000.00" in (asset.review_reason or "")
        link = await session.scalar(select(AssetCashflowLink))
        assert link is not None
        assert link.kind == "upgrade"


async def test_link_is_idempotent(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Повторный разбор той же проводки не задваивает связь."""
    async with async_session_factory() as session:
        article = await _article(session, name="Ремонт ОС", kind="repair")
        asset = await _asset(session, cost="100000.00")
        transaction = await _transaction(session, "30000.00")

        for _ in range(2):
            context = await resolve_asset_context(
                session, article=article, asset_id=asset.id, amount=Decimal("30000.00")
            )
            await link_transaction_to_asset(
                session,
                context=context,
                transaction_id=transaction.id,
                amount=Decimal("30000.00"),
            )
        await session.commit()

        links = (await session.scalars(select(AssetCashflowLink))).all()
        assert len(links) == 1


async def test_purchase_mismatch_goes_to_owner_review(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Платёж не сошёлся со стоимостью карточки — это к владельцу, а не молча наружу.

    Стоимость карточки суммой платежа НЕ переписываем: у объекта уже может идти амортизация,
    и смена базы задним числом сдвинула бы весь график.
    """
    async with async_session_factory() as session:
        article = await _article(session, name="Покупка ОС", kind="purchase")
        asset = await _asset(session, cost="95000.00")
        transaction = await _transaction(session, "97000.00")

        context = await resolve_asset_context(
            session, article=article, asset_id=asset.id, amount=Decimal("97000.00")
        )
        assert context.review_reason is not None
        await link_transaction_to_asset(
            session, context=context, transaction_id=transaction.id, amount=Decimal("97000.00")
        )
        await session.commit()

        assert Decimal(str(asset.initial_cost)) == Decimal("95000.00")
        assert asset.review_status == "requires_owner_review"


async def test_net_catches_a_payment_without_an_asset(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Сеть-ловушка: платёж по статье ОС, за которым не стоит ни один объект.

    Статья попадает на проводку из шести десятков мест, и держать жёсткий гард в каждом
    нереально — точка, добавленная через полгода, обойдёт его молча. Эта сверка ловит всё
    остальное, включая автоматические контуры, где спрашивать некого.
    """
    async with async_session_factory() as session:
        purchase = await _article(session, name="Покупка ОС", kind="purchase")
        neutral = await _article(session, name="Продукты", kind=None)
        caught = await _transaction(session, "95000.00")
        caught.article_id = purchase.id
        ignored = await _transaction(session, "1200.00")
        ignored.article_id = neutral.id
        await session.commit()

        found = await find_unlinked_asset_payments(session)
        assert [row.transaction_id for row in found] == [caught.id]
        assert found[0].article_name == "Покупка ОС"
        assert found[0].article_link_kind == "purchase"


async def test_net_goes_quiet_once_the_asset_is_linked(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Привязали объект — платёж уходит из списка. Иначе на него перестанут смотреть."""
    async with async_session_factory() as session:
        article = await _article(session, name="Покупка ОС", kind="purchase")
        asset = await _asset(session, cost="95000.00")
        transaction = await _transaction(session, "95000.00")
        transaction.article_id = article.id
        await session.commit()

        assert len(await find_unlinked_asset_payments(session)) == 1

        context = await resolve_asset_context(
            session, article=article, asset_id=asset.id, amount=Decimal("95000.00")
        )
        await link_transaction_to_asset(
            session, context=context, transaction_id=transaction.id, amount=Decimal("95000.00")
        )
        await session.commit()

        assert await find_unlinked_asset_payments(session) == []


async def test_article_cannot_be_moved_away_from_a_linked_asset(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Обратная дыра, и она опаснее прямой.

    Проводку, привязанную к объекту, нельзя одним движением переразметить в «Продукты»: связь
    осталась бы висеть, и объект считал бы своей стоимостью деньги, которые в учёте стали
    платежом за овощи. Капитализация при этом могла уже уехать в закрытый месяц.
    """
    async with async_session_factory() as session:
        repair = await _article(session, name="Ремонт ОС", kind="repair")
        neutral = await _article(session, name="Продукты", kind=None)
        asset = await _asset(session, cost="100000.00")
        transaction = await _transaction(session, "30000.00")

        context = await resolve_asset_context(
            session, article=repair, asset_id=asset.id, amount=Decimal("30000.00")
        )
        await link_transaction_to_asset(
            session, context=context, transaction_id=transaction.id, amount=Decimal("30000.00")
        )
        await session.commit()

        with pytest.raises(AssetLinkError) as error:
            await ensure_asset_link_survives(
                session, transaction_id=transaction.id, next_article=neutral
            )
        assert "Печь для пиццы" in str(error.value)

        # Статью ОС на статью ОС менять можно: объект остаётся при своих деньгах.
        await ensure_asset_link_survives(
            session, transaction_id=transaction.id, next_article=repair
        )


async def test_unlinked_transaction_is_free_to_reclassify(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Проводка без привязки переразмечается как угодно — гард не должен мешать обычной работе."""
    async with async_session_factory() as session:
        neutral = await _article(session, name="Продукты", kind=None)
        transaction = await _transaction(session, "1200.00")
        await session.commit()

        await ensure_asset_link_survives(
            session, transaction_id=transaction.id, next_article=neutral
        )


async def test_split_line_carries_its_own_asset(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Объект указывается НА СТРОКЕ разбора: один платёж покупает три стеллажа.

    Именно поэтому объект — свойство доли, а не операции: три стеллажа из одного перевода это
    три разные карточки со своими инвентарными номерами, и списать один из них надо уметь
    отдельно.
    """
    async with async_session_factory() as session:
        account = await make_account(session)
        await make_wallet(session, wallet_type="bank", account_id=account.id)
        article = await make_expense_article(session, code="test_pokupka_os", name="Покупка ОС")
        article.asset_link_kind = "purchase"
        first = await _asset(session, cost="30000.00")
        second = await _asset(session, cost="20000.00")
        operation = await make_bank_operation(
            session, amount="50000.00", direction="out", account_id=account.id
        )
        await session.commit()

        created = await apply_operation_split(
            session,
            operation,
            splits=[
                OperationSplitLine(
                    article_id=article.id, amount=Decimal("30000.00"), asset_id=first.id
                ),
                OperationSplitLine(
                    article_id=article.id, amount=Decimal("20000.00"), asset_id=second.id
                ),
            ],
        )
        await session.commit()

        assert len(created) == 2
        links = (await session.scalars(select(AssetCashflowLink))).all()
        assert {link.asset_id for link in links} == {first.id, second.id}
        assert {Decimal(str(link.amount)) for link in links} == {
            Decimal("30000.00"),
            Decimal("20000.00"),
        }


async def test_split_without_asset_is_refused_before_any_write(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Разбор со статьёй ОС без объекта отвергается, и ни одной проводки не остаётся.

    Проверка идёт ДО записей — как и у помещения. Иначе половина разбора легла бы в базу, а
    вторая упала, и операция осталась бы в неопределённом состоянии.
    """
    async with async_session_factory() as session:
        account = await make_account(session)
        await make_wallet(session, wallet_type="bank", account_id=account.id)
        article = await make_expense_article(session, code="test_pokupka_os2", name="Покупка ОС")
        article.asset_link_kind = "purchase"
        operation = await make_bank_operation(
            session, amount="95000.00", direction="out", account_id=account.id
        )
        await session.commit()

        with pytest.raises(ValueError, match="укажите основное средство"):
            await apply_operation_split(
                session,
                operation,
                splits=[
                    OperationSplitLine(article_id=article.id, amount=Decimal("95000.00")),
                ],
            )
        await session.rollback()

        assert (await session.scalars(select(AssetCashflowLink))).all() == []


async def test_resplit_drops_previous_links(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Переразбор снимает прежние следы: объект не должен нести сумму, которой на нём нет."""
    async with async_session_factory() as session:
        article = await _article(session, name="Ремонт ОС", kind="repair")
        asset = await _asset(session, cost="100000.00")
        transaction = await _transaction(session, "5000.00")

        context = await resolve_asset_context(
            session, article=article, asset_id=asset.id, amount=Decimal("5000.00")
        )
        await link_transaction_to_asset(
            session, context=context, transaction_id=transaction.id, amount=Decimal("5000.00")
        )
        await session.commit()

        removed = await unlink_transaction(session, transaction.id)
        await session.commit()

        assert removed == 1
        assert (await session.scalars(select(AssetCashflowLink))).all() == []
