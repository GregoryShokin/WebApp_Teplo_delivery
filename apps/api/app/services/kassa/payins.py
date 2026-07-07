"""«Внесение в кассу» по именованным пресетам + каталог пресетов (ТК Черникова).

Кассир выбирает пресет по человеческому имени («Деньги за масло», «Приход от
„В гостях у Алисы“»), а бэкенд подставляет статью ДДС, фиксированного контрагента и шаблон
комментария и проводит приход на ТК Черникова (``CashflowTransaction`` с
``direction='in'``, ``source_kind='kassa_payin'``). Никакой статьи ДДС кассир не видит.

Пресеты v1 — контур ``income`` (голый приход по статье): просто приходная проводка, iiko
и сверку смены НЕ трогаем. Каталог курирует владелец в «Настройках → Касса» (CRUD под
правом ``settings.general.*``); создание внесения — под ``kassa.payouts.create`` (то же
право, что и «Выплата из кассы»).

Пресет вяжется только к приходной (``inflow``) статье и не к ``technical`` (переводы между
счетами нельзя — иначе повиснет полунога перевода). Приход по «Поступление с торг. точек»
(партнёр «Алиса») не задваивает авто-контур смены: тот пишет ``source_kind='kassa_cashshift'``,
а внесение — ``kassa_payin`` (Алиса из контура смены исключена, см. iiko_cashshift_sync).

Правка/удаление внесения — только СВОЯ запись и только в день создания (как у выплаты).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    CashflowTransaction,
    Counterparty,
    DdsArticle,
    KassaPayinPreset,
)
from app.services.kassa.payouts import (
    KASSA_PAYIN_SOURCE_KIND,
    get_kassa_wallet,
    kassa_today,
)

_CENTS = Decimal("0.01")


class KassaPayinError(Exception):
    """Внесение/правка/каталог пресетов невозможны (валидация, права на запись, состояние)."""


def _money(value: object) -> Decimal:
    return Decimal(str(value or 0)).quantize(_CENTS, rounding=ROUND_HALF_UP)


def ensure_article_payin_eligible(article: DdsArticle) -> None:
    """Можно ли привязать пресет к этой статье (422 в API, если нет).

    Внесение — только приход; статьи-переводы (``technical``) исключены, иначе повиснет
    полунога перевода между счетами.
    """
    if article.movement_type != "inflow":
        raise KassaPayinError(
            "Внесение проводится только приходными статьями — расходные идут «Выплатой из кассы»"
        )
    if article.activity_type == "technical":
        raise KassaPayinError(
            "Служебные статьи-переводы между счетами в пресеты внесения включать нельзя"
        )


async def _validated_preset_article(session: AsyncSession, article_id: uuid.UUID) -> DdsArticle:
    article = await session.get(DdsArticle, article_id)
    if article is None:
        raise KassaPayinError("Статья не найдена")
    if not article.is_active:
        raise KassaPayinError("Статья деактивирована")
    ensure_article_payin_eligible(article)
    return article


# --- Каталог пресетов (Настройки → Касса) --------------------------------------


async def list_preset_articles(session: AsyncSession) -> list[dict[str, Any]]:
    """Приходные статьи ДДС, доступные для привязки к пресету (для выпадашки в Настройках)."""
    articles = (
        await session.scalars(
            select(DdsArticle)
            .where(
                DdsArticle.is_active.is_(True),
                DdsArticle.movement_type == "inflow",
                DdsArticle.activity_type != "technical",
            )
            .order_by(DdsArticle.name)
        )
    ).all()
    return [
        {
            "id": article.id,
            "code": article.code,
            "name": article.name,
            "activity_type": article.activity_type,
        }
        for article in articles
    ]


async def list_preset_counterparties(session: AsyncSession) -> list[dict[str, Any]]:
    """Активные контрагенты для привязки к пресету (выпадашка в Настройках).

    Отдельный от ``/kassa/counterparties`` (право ``kassa.refs.read``) список под правом
    Настроек — весь контур каталога пресетов живёт под ``settings.general.*``.
    """
    rows = (
        await session.scalars(
            select(Counterparty)
            .where(Counterparty.status == "active")
            .order_by(Counterparty.name)
        )
    ).all()
    return [{"id": counterparty.id, "name": counterparty.name} for counterparty in rows]


async def list_active_presets(session: AsyncSession) -> list[dict[str, Any]]:
    """Активные пресеты для формы кассира: имя + шаблон комментария (без статьи ДДС)."""
    presets = (
        await session.scalars(
            select(KassaPayinPreset)
            .where(KassaPayinPreset.is_active.is_(True))
            .order_by(KassaPayinPreset.sort_order, KassaPayinPreset.name)
        )
    ).all()
    return [
        {
            "id": preset.id,
            "name": preset.name,
            "comment_template": preset.comment_template,
        }
        for preset in presets
    ]


async def list_all_presets(session: AsyncSession) -> list[dict[str, Any]]:
    """Все пресеты каталога (вкл. выключенные) с именами статьи/контрагента — для Настроек."""
    rows = (
        await session.execute(
            select(KassaPayinPreset, DdsArticle.name, Counterparty.name)
            .join(DdsArticle, DdsArticle.id == KassaPayinPreset.article_id)
            .outerjoin(Counterparty, Counterparty.id == KassaPayinPreset.counterparty_id)
            .order_by(KassaPayinPreset.sort_order, KassaPayinPreset.name)
        )
    ).all()
    return [
        {
            "id": preset.id,
            "name": preset.name,
            "mechanism": preset.mechanism,
            "article_id": preset.article_id,
            "article_name": article_name,
            "counterparty_id": preset.counterparty_id,
            "counterparty_name": counterparty_name,
            "comment_template": preset.comment_template,
            "is_active": preset.is_active,
            "sort_order": preset.sort_order,
        }
        for preset, article_name, counterparty_name in rows
    ]


async def _ensure_counterparty(session: AsyncSession, counterparty_id: uuid.UUID) -> None:
    exists = await session.scalar(
        select(Counterparty.id).where(Counterparty.id == counterparty_id)
    )
    if exists is None:
        raise KassaPayinError("Контрагент не найден")


async def create_preset(
    session: AsyncSession,
    *,
    name: str,
    article_id: uuid.UUID,
    counterparty_id: uuid.UUID | None = None,
    comment_template: str | None = None,
    is_active: bool = True,
    sort_order: int = 0,
    actor_user_id: uuid.UUID | None = None,
) -> KassaPayinPreset:
    """Завести пресет внесения (владелец, из Настроек)."""
    clean_name = (name or "").strip()
    if not clean_name:
        raise KassaPayinError("Укажите название пресета")
    await _validated_preset_article(session, article_id)
    if counterparty_id is not None:
        await _ensure_counterparty(session, counterparty_id)
    preset = KassaPayinPreset(
        name=clean_name,
        mechanism="income",
        article_id=article_id,
        counterparty_id=counterparty_id,
        comment_template=(comment_template or "").strip() or None,
        is_active=is_active,
        sort_order=sort_order,
        created_by_user_id=actor_user_id,
    )
    session.add(preset)
    await session.commit()
    await session.refresh(preset)
    return preset


async def update_preset(
    session: AsyncSession,
    *,
    preset_id: uuid.UUID,
    name: str,
    article_id: uuid.UUID,
    counterparty_id: uuid.UUID | None = None,
    comment_template: str | None = None,
    is_active: bool = True,
    sort_order: int = 0,
) -> KassaPayinPreset:
    """Изменить пресет (владелец). Уже проведённые внесения не трогает — они хранят свою статью."""
    preset = await session.get(KassaPayinPreset, preset_id)
    if preset is None:
        raise KassaPayinError("Пресет не найден")
    clean_name = (name or "").strip()
    if not clean_name:
        raise KassaPayinError("Укажите название пресета")
    await _validated_preset_article(session, article_id)
    if counterparty_id is not None:
        await _ensure_counterparty(session, counterparty_id)
    preset.name = clean_name
    preset.article_id = article_id
    preset.counterparty_id = counterparty_id
    preset.comment_template = (comment_template or "").strip() or None
    preset.is_active = is_active
    preset.sort_order = sort_order
    await session.commit()
    await session.refresh(preset)
    return preset


async def delete_preset(session: AsyncSession, *, preset_id: uuid.UUID) -> None:
    """Удалить пресет (владелец). Проведённые по нему внесения остаются в журнале."""
    preset = await session.get(KassaPayinPreset, preset_id)
    if preset is None:
        raise KassaPayinError("Пресет не найден")
    await session.delete(preset)
    await session.commit()


# --- Внесение по пресету (форма кассира) ---------------------------------------


async def create_payin(
    session: AsyncSession,
    *,
    preset_id: uuid.UUID,
    amount: Decimal,
    comment: str | None = None,
    actor_user_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Провести приход на ТК Черникова по пресету (дата — всегда сегодня).

    Комментарий — введённый кассиром, иначе шаблон пресета. Статья ДДС и фиксированный
    контрагент берутся из пресета; iiko и сверку смены не трогаем.
    """
    preset = await session.get(KassaPayinPreset, preset_id)
    if preset is None or not preset.is_active:
        raise KassaPayinError("Пресет внесения не найден или выключен")
    amount = _money(amount)
    if amount <= 0:
        raise KassaPayinError("Сумма должна быть больше нуля")
    # Статья пресета могла быть деактивирована владельцем после создания пресета.
    await _validated_preset_article(session, preset.article_id)
    wallet = await get_kassa_wallet(session)
    purpose = (comment or "").strip() or preset.comment_template

    transaction = CashflowTransaction(
        wallet_id=wallet.id,
        direction="in",
        amount=amount,
        operation_date=kassa_today(),
        article_id=preset.article_id,
        counterparty_id=preset.counterparty_id,
        source_kind=KASSA_PAYIN_SOURCE_KIND,
        payment_purpose=purpose,
        quality_status="final",
        created_by_user_id=actor_user_id,
    )
    session.add(transaction)
    await session.commit()
    await session.refresh(transaction)
    return {"transaction_id": transaction.id, "preset_id": preset.id}


async def _own_today_payin(
    session: AsyncSession,
    transaction_id: uuid.UUID,
    *,
    actor_user_id: uuid.UUID | None,
) -> CashflowTransaction:
    """Найти своё сегодняшнее внесение или бросить понятную ошибку (правка/удаление)."""
    wallet = await get_kassa_wallet(session)
    transaction = await session.get(CashflowTransaction, transaction_id)
    if (
        transaction is None
        or transaction.wallet_id != wallet.id
        or transaction.source_kind != KASSA_PAYIN_SOURCE_KIND
    ):
        raise KassaPayinError("Запись внесения не найдена")
    if actor_user_id is None or transaction.created_by_user_id != actor_user_id:
        raise KassaPayinError("Можно править только свои кассовые записи")
    if transaction.operation_date != kassa_today():
        raise KassaPayinError(
            "Правка доступна только в день создания записи — за прошлые даты обратитесь к владельцу"
        )
    return transaction


async def update_payin(
    session: AsyncSession,
    *,
    transaction_id: uuid.UUID,
    amount: Decimal,
    comment: str | None = None,
    actor_user_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Исправить сумму/комментарий своего сегодняшнего внесения (статья пресета неизменна)."""
    transaction = await _own_today_payin(session, transaction_id, actor_user_id=actor_user_id)
    amount = _money(amount)
    if amount <= 0:
        raise KassaPayinError("Сумма должна быть больше нуля")
    transaction.amount = amount
    transaction.payment_purpose = (comment or "").strip() or None
    await session.commit()
    return {"transaction_id": transaction.id, "preset_id": None}


async def delete_payin(
    session: AsyncSession,
    *,
    transaction_id: uuid.UUID,
    actor_user_id: uuid.UUID | None = None,
) -> None:
    """Удалить своё сегодняшнее внесение (приход снимается с кассы)."""
    transaction = await _own_today_payin(session, transaction_id, actor_user_id=actor_user_id)
    await session.delete(transaction)
    await session.commit()
