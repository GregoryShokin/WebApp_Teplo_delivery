"""Сервисные тесты «Внесение в кассу» по пресетам (ТК Черникова).

Пресет = имя → приходная статья ДДС + фиксированный контрагент + шаблон комментария.
Проверяем: приход книжится с ``source_kind='kassa_payin'``; комментарий берётся из
пресета при пустом вводе; пресет нельзя привязать к расходной/служебной статье;
внесение видно в журнале как правимая своя запись; правка/удаление только своей
сегодняшней; неактивный пресет и чужая запись — отказ.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import CashflowTransaction, Counterparty, DdsArticle, User, Wallet
from app.services.kassa.payins import (
    KassaPayinError,
    create_payin,
    create_preset,
    delete_payin,
    ensure_article_payin_eligible,
    list_active_presets,
    list_all_presets,
    list_payin_options,
    update_payin,
)
from app.services.kassa.payouts import (
    KASSA_PAYIN_SOURCE_KIND,
    KASSA_WALLET_CODE,
    kassa_journal,
    kassa_today,
)


async def _make_user(session: AsyncSession, *, name: str = "Кассир Тест") -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"payin-{uuid.uuid4().hex[:10]}@test.local",
        hashed_password="hashed",
        full_name=name,
    )
    session.add(user)
    await session.flush()
    return user


async def _make_inflow_article(
    session: AsyncSession, *, activity_type: str = "operating", kassa_enabled: bool = False
) -> DdsArticle:
    article = DdsArticle(
        code=f"prihod_test_{uuid.uuid4().hex[:6]}",
        name="Прочие поступления (тест)",
        movement_type="inflow",
        activity_type=activity_type,
        kassa_enabled=kassa_enabled,
    )
    session.add(article)
    await session.flush()
    return article


async def _make_counterparty(session: AsyncSession) -> Counterparty:
    counterparty = Counterparty(
        name=f"Партнёр {uuid.uuid4().hex[:6]}",
        type="legal_entity",
        status="active",
    )
    session.add(counterparty)
    await session.flush()
    return counterparty


async def _tk_wallet(session: AsyncSession) -> Wallet:
    wallet = await session.scalar(select(Wallet).where(Wallet.code == KASSA_WALLET_CODE))
    assert wallet is not None, "кошелёк ТК Черникова должен быть в сидах"
    return wallet


# --------------------------------------------------------------------------- #
# Приход по пресету: проводка, статья, контрагент, комментарий
# --------------------------------------------------------------------------- #


async def test_payin_books_inflow_transaction(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        user = await _make_user(session)
        article = await _make_inflow_article(session)
        counterparty = await _make_counterparty(session)
        await session.commit()

        preset = await create_preset(
            session,
            name="Приход от партнёра",
            article_id=article.id,
            counterparty_id=counterparty.id,
            comment_template="Наличные от партнёра",
            actor_user_id=user.id,
        )
        result = await create_payin(
            session,
            preset_id=preset.id,
            amount=Decimal("5000"),
            comment="Забрали за две недели",
            actor_user_id=user.id,
        )
        wallet = await _tk_wallet(session)
        transaction = await session.get(CashflowTransaction, result["transaction_id"])
        assert transaction is not None
        assert transaction.wallet_id == wallet.id
        assert transaction.direction == "in"
        assert transaction.source_kind == KASSA_PAYIN_SOURCE_KIND
        assert transaction.article_id == article.id
        assert transaction.counterparty_id == counterparty.id
        assert transaction.amount == Decimal("5000.00")
        assert transaction.operation_date == kassa_today()
        assert transaction.created_by_user_id == user.id
        # Введённый кассиром комментарий приоритетнее шаблона пресета.
        assert transaction.payment_purpose == "Забрали за две недели"


async def test_payin_comment_falls_back_to_template(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        user = await _make_user(session)
        article = await _make_inflow_article(session)
        await session.commit()

        preset = await create_preset(
            session,
            name="Деньги за масло",
            article_id=article.id,
            comment_template="Плата за масло",
            actor_user_id=user.id,
        )
        result = await create_payin(
            session, preset_id=preset.id, amount=Decimal("1200"), actor_user_id=user.id
        )
        transaction = await session.get(CashflowTransaction, result["transaction_id"])
        assert transaction is not None
        assert transaction.payment_purpose == "Плата за масло"
        assert transaction.counterparty_id is None


async def test_kassa_enabled_inflow_article_is_direct_payin_option(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        user = await _make_user(session)
        enabled = await _make_inflow_article(session, kassa_enabled=True)
        disabled = await _make_inflow_article(session)
        await session.commit()

        options = await list_payin_options(session)
        by_id = {item["id"]: item for item in options}
        assert by_id[enabled.id]["kind"] == "article"
        assert disabled.id not in by_id

        result = await create_payin(
            session,
            article_id=enabled.id,
            amount=Decimal("750"),
            comment="Прямое внесение по статье",
            actor_user_id=user.id,
        )
        transaction = await session.get(CashflowTransaction, result["transaction_id"])
        assert transaction is not None
        assert transaction.article_id == enabled.id
        assert transaction.direction == "in"
        assert transaction.payment_purpose == "Прямое внесение по статье"
        assert result["preset_id"] is None


async def test_direct_payin_rejects_article_without_kassa_flag(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        article = await _make_inflow_article(session)
        await session.commit()

        with pytest.raises(KassaPayinError, match="не разрешена"):
            await create_payin(session, article_id=article.id, amount=Decimal("100"))


# --------------------------------------------------------------------------- #
# Гард статьи пресета: только приход, не служебная
# --------------------------------------------------------------------------- #


def test_preset_article_guard_rejects_outflow_and_technical() -> None:
    outflow = DdsArticle(
        code=f"rashod_{uuid.uuid4().hex[:6]}",
        name="Расход (тест)",
        movement_type="outflow",
        activity_type="operating",
    )
    with pytest.raises(KassaPayinError, match="приходными"):
        ensure_article_payin_eligible(outflow)

    technical = DdsArticle(
        code=f"perevod_{uuid.uuid4().hex[:6]}",
        name="Перевод между счетами (тест)",
        movement_type="inflow",
        activity_type="technical",
    )
    with pytest.raises(KassaPayinError, match="переводы"):
        ensure_article_payin_eligible(technical)


async def test_create_preset_denies_outflow_article(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        outflow = DdsArticle(
            code=f"rashod_{uuid.uuid4().hex[:6]}",
            name="Расход (тест)",
            movement_type="outflow",
            activity_type="operating",
        )
        session.add(outflow)
        await session.commit()

        with pytest.raises(KassaPayinError, match="приходными"):
            await create_preset(session, name="Кривой", article_id=outflow.id)


# --------------------------------------------------------------------------- #
# Журнал: приход виден зелёной строкой, правится своей сегодняшней
# --------------------------------------------------------------------------- #


async def test_payin_appears_in_journal_editable(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        user = await _make_user(session)
        article = await _make_inflow_article(session)
        await session.commit()

        preset = await create_preset(
            session, name="Прочий приход", article_id=article.id, actor_user_id=user.id
        )
        result = await create_payin(
            session,
            preset_id=preset.id,
            amount=Decimal("800"),
            comment="Возврат подотчёта",
            actor_user_id=user.id,
        )

        today = kassa_today()
        journal = await kassa_journal(
            session,
            date_from=today.replace(day=1),
            date_to=today,
            actor_user_id=user.id,
        )
        by_id = {item["id"]: item for item in journal["items"]}
        row = by_id[result["transaction_id"]]
        assert row["direction"] == "in"
        assert row["kassa_flow"] == "payin"
        assert row["editable"] is True
        assert journal["period_in"] >= 800.0

        # Чужому этот приход править нельзя.
        stranger = await _make_user(session, name="Другой")
        await session.commit()
        other_journal = await kassa_journal(
            session,
            date_from=today.replace(day=1),
            date_to=today,
            actor_user_id=stranger.id,
        )
        other_row = {item["id"]: item for item in other_journal["items"]}[result["transaction_id"]]
        assert other_row["editable"] is False


async def test_edit_and_delete_own_today_payin(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        user = await _make_user(session)
        article = await _make_inflow_article(session)
        await session.commit()

        preset = await create_preset(
            session, name="Прочий приход", article_id=article.id, actor_user_id=user.id
        )
        result = await create_payin(
            session, preset_id=preset.id, amount=Decimal("700"), actor_user_id=user.id
        )
        await update_payin(
            session,
            transaction_id=result["transaction_id"],
            amount=Decimal("750"),
            comment="Исправлено",
            actor_user_id=user.id,
        )
        transaction = await session.get(CashflowTransaction, result["transaction_id"])
        assert transaction is not None
        assert transaction.amount == Decimal("750.00")
        assert transaction.payment_purpose == "Исправлено"
        # Статья пресета не меняется правкой.
        assert transaction.article_id == article.id

        await delete_payin(
            session, transaction_id=result["transaction_id"], actor_user_id=user.id
        )
        assert await session.get(CashflowTransaction, result["transaction_id"]) is None


async def test_payin_edit_denied_for_other_and_yesterday(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        author = await _make_user(session, name="Автор")
        stranger = await _make_user(session, name="Чужой")
        article = await _make_inflow_article(session)
        await session.commit()

        preset = await create_preset(
            session, name="Прочий приход", article_id=article.id, actor_user_id=author.id
        )
        result = await create_payin(
            session, preset_id=preset.id, amount=Decimal("100"), actor_user_id=author.id
        )

        with pytest.raises(KassaPayinError, match="свои"):
            await delete_payin(
                session, transaction_id=result["transaction_id"], actor_user_id=stranger.id
            )

        transaction = await session.get(CashflowTransaction, result["transaction_id"])
        assert transaction is not None
        transaction.operation_date = kassa_today() - timedelta(days=1)
        await session.commit()
        with pytest.raises(KassaPayinError, match="день создания"):
            await update_payin(
                session,
                transaction_id=result["transaction_id"],
                amount=Decimal("1"),
                actor_user_id=author.id,
            )


# --------------------------------------------------------------------------- #
# Каталог: активные vs все; неактивный пресет не проводится
# --------------------------------------------------------------------------- #


async def test_active_presets_exclude_inactive(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        user = await _make_user(session)
        article = await _make_inflow_article(session)
        await session.commit()

        active = await create_preset(
            session, name="Активный", article_id=article.id, actor_user_id=user.id
        )
        inactive = await create_preset(
            session,
            name="Выключенный",
            article_id=article.id,
            is_active=False,
            actor_user_id=user.id,
        )

        active_ids = {item["id"] for item in await list_active_presets(session)}
        all_ids = {item["id"] for item in await list_all_presets(session)}
        assert active.id in active_ids
        assert inactive.id not in active_ids
        assert {active.id, inactive.id} <= all_ids

        with pytest.raises(KassaPayinError, match="выключен"):
            await create_payin(
                session, preset_id=inactive.id, amount=Decimal("10"), actor_user_id=user.id
            )
