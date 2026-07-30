"""Связь денежной проводки с основным средством: одно правило на все входы ДДС.

Статья вешается на проводку из многих мест — разбор банковской операции, разбор по строкам,
ручная разметка, разбор владельцем, PATCH проводки, черновик «Нового платежа», выплата из
кассы, авто-правила по мерчанту. Если требовать карточку в каждом по-своему, дыра появится
ровно там, где о ней забыли: покупка ОС уйдёт в расход мимо баланса. Поэтому правило живёт
здесь, а входы его только зовут — так же, как ``location_analytics`` для помещения.

Что решает статья (``DdsArticle.asset_link_kind``):

* ``purchase`` — платёж покупает объект: карточка обязательна;
* ``repair`` — расход на существующий объект, капитализация по правилу 15% (меньше доли —
  расход периода, больше — увеличивает первоначальную стоимость);
* ``maintenance`` — текущий ремонт: НИКОГДА не капитализируется, но объект указать надо,
  иначе не собрать историю ремонтов и не понять, что технику пора менять;
* пусто — статья к основным средствам отношения не имеет, и объект на ней отвергается так же,
  как помещение на статье без ``location_required``.

Обратное правило держим симметрично: объект на статье без признака — мусор в учёте, потому
что по такой связи ничего не считается и никто её не читает.

Проверка НИЧЕГО не пишет: она отвечает «можно ли и что именно получится», а записи делает
``link_transaction_to_asset``. Так вход может отказать пользователю до того, как в базе
появится хоть одна строка.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AssetCashflowLink, CashflowTransaction, DdsArticle, FixedAsset
from app.services.fixed_assets import (
    INACTIVE_STATUSES,
    classify_asset_expense,
    upgrade_share_threshold,
)


class AssetLinkError(ValueError):
    """Нарушение правил связи проводки с основным средством. Поднимается ДО любых записей."""


@dataclass(frozen=True)
class AssetContext:
    asset_id: uuid.UUID | None
    # Что писать в ``AssetCashflowLink.kind``: purchase / repair / upgrade.
    link_kind: str | None
    # Расход увеличивает первоначальную стоимость объекта (модернизация по правилу 15%).
    capitalize: bool
    # Решение неочевидно — объект уходит владельцу на разбор с этой причиной.
    review_reason: str | None


_EMPTY = AssetContext(asset_id=None, link_kind=None, capitalize=False, review_reason=None)


async def resolve_asset_context(
    session: AsyncSession,
    *,
    article: DdsArticle | None,
    asset_id: uuid.UUID | None,
    amount: Decimal,
) -> AssetContext:
    """Проверить связку «статья ↔ объект ↔ сумма» и решить, что с ней делать."""
    if article is None:
        if asset_id is not None:
            raise AssetLinkError("Основное средство указывается только вместе со статьёй ДДС")
        return _EMPTY

    kind = article.asset_link_kind
    if kind is None:
        if asset_id is not None:
            raise AssetLinkError(
                f"Статья «{article.name}» не ведёт учёт основных средств — уберите объект"
            )
        return _EMPTY

    if asset_id is None:
        raise AssetLinkError(f"Для статьи «{article.name}» укажите основное средство")

    asset = await session.get(FixedAsset, asset_id)
    if asset is None:
        raise AssetLinkError("Основное средство не найдено")
    if asset.status in INACTIVE_STATUSES and asset.status != "not_working":
        # Неработающий объект чинить и покупать к нему запчасти можно — выбывший нельзя.
        raise AssetLinkError(
            f"Объект «{asset.name}» выбыл из учёта — расход по нему провести нельзя"
        )

    if kind == "purchase":
        return _purchase_context(asset, amount)
    if kind == "maintenance":
        # Текущий ремонт: привязка ради истории объекта, стоимость не трогаем никогда.
        return AssetContext(
            asset_id=asset.id, link_kind="repair", capitalize=False, review_reason=None
        )
    if kind == "repair":
        return await _repair_context(session, asset, amount)

    raise AssetLinkError(f"Неизвестный вид связи статьи с ОС: {kind}")


def _purchase_context(asset: FixedAsset, amount: Decimal) -> AssetContext:
    """Платёж покупает объект.

    Стоимость карточки НЕ переписываем суммой платежа: карточку завёл человек, и молча менять
    у неё базу амортизации — значит задним числом сдвинуть весь график. Но расхождение прячем
    тоже нельзя: по методологии для покупки первоначальная стоимость и есть сумма платежа
    (``valuation_basis='payment'``). Поэтому расхождение уходит владельцу на разбор.
    """
    reason: str | None = None
    booked = Decimal(str(asset.initial_cost))
    paid = Decimal(str(amount))
    if booked != paid:
        reason = (
            f"Платёж {paid:.2f} ₽ не сходится с первоначальной стоимостью карточки "
            f"{booked:.2f} ₽ — проверьте, что оплачено именно это"
        )
    return AssetContext(
        asset_id=asset.id, link_kind="purchase", capitalize=False, review_reason=reason
    )


async def _repair_context(
    session: AsyncSession, asset: FixedAsset, amount: Decimal
) -> AssetContext:
    """Ремонт или модернизация — по доле расхода от первоначальной стоимости."""
    verdict = classify_asset_expense(
        asset.initial_cost,
        amount,
        share_threshold=await upgrade_share_threshold(session),
    )
    if verdict == "upgrade":
        return AssetContext(
            asset_id=asset.id, link_kind="upgrade", capitalize=True, review_reason=None
        )
    if verdict == "repair":
        return AssetContext(
            asset_id=asset.id, link_kind="repair", capitalize=False, review_reason=None
        )
    # Ровно на границе или нулевая база — решает владелец. Расход при этом проводим как
    # ремонт: занизить стоимость объекта безопаснее, чем завысить баланс на спорную сумму.
    return AssetContext(
        asset_id=asset.id,
        link_kind="repair",
        capitalize=False,
        review_reason=(
            f"Расход {Decimal(str(amount)):.2f} ₽ на границе правила 15% — решите, "
            f"ремонт это или модернизация"
        ),
    )


async def link_transaction_to_asset(
    session: AsyncSession,
    *,
    context: AssetContext,
    transaction_id: uuid.UUID,
    amount: Decimal,
) -> AssetCashflowLink | None:
    """Записать связь платежа с объектом.

    Идемпотентно по тройке «объект, проводка, вид связи» — уникальный индекс из ``0201``.

    КАПИТАЛИЗАЦИЯ ЗДЕСЬ НЕ ПРИМЕНЯЕТСЯ, и это осознанно. Соблазн был: правило 15% уже дало
    вердикт, остаётся прибавить сумму к первоначальной стоимости. Но переразбор операции
    УДАЛЯЕТ прежние проводки и заводит новые с другими идентификаторами — идемпотентность по
    проводке при этом теряется, и вторая капитализация легла бы поверх первой. Откатить её
    нельзя: она могла уже уехать в закрытый месяц.

    Плюс сам сценарий нездоровый: база амортизации объекта менялась бы из экрана разбора
    банковской выписки, где про основные средства речи нет, и владелец узнал бы об этом из
    графика через месяц.

    Поэтому вердикт правила ЗАПИСЫВАЕТСЯ (вид связи ``upgrade``), а объект уходит владельцу
    на подтверждение с готовой цифрой. Стоимость меняет человек — в карточке объекта, где
    пересчитается и хвост остатков.
    """
    if context.asset_id is None or context.link_kind is None:
        return None

    existing = await session.scalar(
        select(AssetCashflowLink).where(
            AssetCashflowLink.asset_id == context.asset_id,
            AssetCashflowLink.cashflow_transaction_id == transaction_id,
            AssetCashflowLink.kind == context.link_kind,
        )
    )
    if existing is not None:
        return existing

    link = AssetCashflowLink(
        asset_id=context.asset_id,
        cashflow_transaction_id=transaction_id,
        kind=context.link_kind,
        amount=Decimal(str(amount)),
    )
    session.add(link)

    asset = await session.get(FixedAsset, context.asset_id)
    if asset is not None:
        reason = context.review_reason
        if context.capitalize:
            new_cost = Decimal(str(asset.initial_cost)) + Decimal(str(amount))
            reason = (
                f"Работы на {Decimal(str(amount)):.2f} ₽ — это больше 15% стоимости, то есть "
                f"модернизация. Подтвердите новую первоначальную стоимость "
                f"{new_cost:.2f} ₽ — после этого амортизация пойдёт от неё"
            )
        if reason:
            asset.review_status = "requires_owner_review"
            asset.review_reason = reason

    await session.flush()
    return link


async def unlink_transaction(session: AsyncSession, transaction_id: uuid.UUID) -> int:
    """Снять привязки проводки к объектам перед ПЕРЕразбором.

    Переразбор переписывает раскладку операции с нуля, и старые следы обязаны исчезнуть —
    иначе объект унаследует связь с суммой, которой на нём больше нет. Тот же порядок, что
    у прочих следов разбора: сначала снять прежние, потом класть новые.

    Капитализацию откатить нельзя и не пытаемся: она уже могла попасть в закрытый месяц.
    Возвращаем число снятых связей, чтобы вызывающий мог сказать об этом в логе.
    """
    links = (
        await session.scalars(
            select(AssetCashflowLink).where(
                AssetCashflowLink.cashflow_transaction_id == transaction_id
            )
        )
    ).all()
    for link in links:
        await session.delete(link)
    await session.flush()
    return len(links)


async def assets_linked_to_transaction(
    session: AsyncSession, transaction_id: uuid.UUID
) -> list[AssetCashflowLink]:
    """Связи проводки с объектами — для карточки операции и для защиты от переразметки."""
    return list(
        (
            await session.scalars(
                select(AssetCashflowLink).where(
                    AssetCashflowLink.cashflow_transaction_id == transaction_id
                )
            )
        ).all()
    )


async def ensure_asset_link_survives(
    session: AsyncSession,
    *,
    transaction_id: uuid.UUID,
    next_article: DdsArticle | None,
) -> None:
    """Не дать увести проводку со статьи ОС, пока на ней висит объект.

    Дыра обратной стороны гейта, и она опаснее прямой. Проводку, уже привязанную к объекту,
    сегодня можно одним PATCH переразметить в «Продукты» или исключить — связь останется
    висеть, и объект будет считать своей стоимостью деньги, которые в учёте стали платежом за
    овощи. Капитализация при этом могла уже уехать в закрытый месяц, и отменить её нельзя.

    Поэтому не чиним молча, а отказываем и говорим, что сделать: сначала снять привязку в
    карточке объекта, потом менять статью. Решение о судьбе объекта принимает человек — код
    угадать его не может.
    """
    links = await assets_linked_to_transaction(session, transaction_id)
    if not links:
        return

    next_kind = next_article.asset_link_kind if next_article is not None else None
    if next_kind is not None:
        return

    names = (
        await session.scalars(
            select(FixedAsset.name).where(FixedAsset.id.in_([link.asset_id for link in links]))
        )
    ).all()
    listed = ", ".join(f"«{name}»" for name in names) or "основным средством"
    raise AssetLinkError(
        f"К этому платежу привязано основное средство ({listed}). Снимите привязку в карточке "
        f"объекта, иначе он останется со стоимостью из денег, которых в учёте больше нет"
    )


@dataclass(frozen=True)
class UnlinkedPayment:
    """Платёж по статье основных средств, за которым не стоит ни одного объекта."""

    transaction_id: uuid.UUID
    operation_date: date
    amount: Decimal
    article_name: str
    article_link_kind: str
    payment_purpose: str | None


async def find_unlinked_asset_payments(
    session: AsyncSession, *, since: date | None = None
) -> list[UnlinkedPayment]:
    """Найти платежи по статьям ОС, к которым не привязан ни один объект.

    ЗАЧЕМ ЭТО ЕСТЬ. Статья вешается на проводку из шестидесяти с лишним мест: разбор выписки,
    касса, склад, оплата счетов, предоплаты, правила по мерчанту, подтверждение банка, ночные
    джобы, CLI-скрипты. Держать жёсткий гард в каждом — значит писать диф по всему финансовому
    контуру и всё равно проиграть: точка, добавленная через полгода, обойдёт его молча. Так уже
    случилось с помещением — флаг ``location_required`` есть, а разбор владельцем и целевые
    резервы Сейфа его не спрашивают.

    Поэтому гард стоит там, где человек действительно может указать объект, а эта сверка ловит
    ВСЁ остальное — включая автоматические контуры, где спрашивать некого, и любую будущую
    точку. Она ничего не чинит и не блокирует: показывает список, по которому владелец заводит
    недостающие карточки. Прятать такой платёж нельзя — это ровно та покупка, которая уходит
    мимо баланса.

    ``since`` ограничивает окно: исторические проводки до внедрения контура карточек не имеют
    законно, и тащить их в список каждый месяц — шум, который заставит перестать смотреть.
    """
    linked = select(AssetCashflowLink.cashflow_transaction_id)
    query = (
        select(CashflowTransaction, DdsArticle)
        .join(DdsArticle, CashflowTransaction.article_id == DdsArticle.id)
        .where(
            DdsArticle.asset_link_kind.is_not(None),
            CashflowTransaction.id.not_in(linked),
            # Исключённая проводка — уже признанная ошибкой разбора, звать по ней второй раз
            # незачем.
            CashflowTransaction.quality_status != "excluded",
        )
    )
    if since is not None:
        query = query.where(CashflowTransaction.operation_date >= since)

    rows = (await session.execute(query.order_by(CashflowTransaction.operation_date.desc()))).all()
    return [
        UnlinkedPayment(
            transaction_id=transaction.id,
            operation_date=transaction.operation_date,
            amount=Decimal(str(transaction.amount)),
            article_name=article.name,
            article_link_kind=article.asset_link_kind or "",
            payment_purpose=transaction.payment_purpose,
        )
        for transaction, article in rows
    ]
