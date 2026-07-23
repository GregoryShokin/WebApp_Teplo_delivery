"""Аналитика расхода по помещению: одно правило на все входы ДДС.

Помещение попадает в расход из шести мест — разбор банковской операции, разбор ручной
проводки, PATCH проводки, черновик окна «Новый платёж», наличная оплата через Сейф и выплата
из кассы. Если проверять в каждом по-своему, аналитика будет дырявой ровно там, где о ней
забыли, поэтому правило живёт здесь, а входы его только зовут.

Смысл правил:

* статья с ``location_required`` (аренда, коммуналка, охрана) без помещения бессмысленна —
  именно ради ответа «сколько стоит точка» поле и заводится;
* обратное тоже верно: помещение на статье без флага — мусор в аналитике, как ``employee_id``
  на незарплатной статье;
* аренда (``lease_id``) необязательна: по арендной статье платят и вывоз мусора, и охрану —
  тогда помещение есть, а договора нет;
* если аренда выбрана, контрагент обязан быть её арендодателем — иначе платёж уедет чужому
  лицу под видом аренды. Пустой контрагент наследуется от аренды, как доля наследует его от
  привязанной накладной.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DdsArticle, Location, LocationLease


class LocationAnalyticsError(ValueError):
    """Нарушение правил аналитики по помещению. Поднимается ДО любых записей."""


@dataclass(frozen=True)
class LocationContext:
    location_id: uuid.UUID | None
    lease_id: uuid.UUID | None
    counterparty_id: uuid.UUID | None


async def resolve_location_context(
    session: AsyncSession,
    *,
    article: DdsArticle | None,
    location_id: uuid.UUID | None,
    lease_id: uuid.UUID | None,
    counterparty_id: uuid.UUID | None,
    on_date: date,
) -> LocationContext:
    """Проверить связку «статья ↔ помещение ↔ аренда ↔ контрагент» и достроить контрагента."""
    if article is None:
        if location_id is not None or lease_id is not None:
            raise LocationAnalyticsError("Помещение указывается только вместе со статьёй ДДС")
        return LocationContext(None, None, counterparty_id)

    if lease_id is not None and location_id is None:
        raise LocationAnalyticsError("Выбрана аренда без помещения")

    if location_id is None:
        if article.location_required:
            raise LocationAnalyticsError(f"Для статьи «{article.name}» укажите помещение")
        return LocationContext(None, None, counterparty_id)

    if not article.location_required:
        raise LocationAnalyticsError(
            f"Статья «{article.name}» не ведёт аналитику по помещению — уберите помещение"
        )

    location = await session.get(Location, location_id)
    if location is None:
        raise LocationAnalyticsError("Помещение не найдено")
    # Закрытую точку не запрещаем: по Гагариной остаются исторические разборы, и запрет
    # сломал бы переразбор старых выписок.

    if lease_id is None:
        return LocationContext(location.id, None, counterparty_id)

    lease = await session.get(LocationLease, lease_id)
    if lease is None:
        raise LocationAnalyticsError("Аренда не найдена")
    if lease.location_id != location.id:
        raise LocationAnalyticsError("Выбранная аренда относится к другому помещению")
    if (
        lease.dds_article_id is not None
        and article.id is not None
        and lease.dds_article_id != article.id
    ):
        raise LocationAnalyticsError(
            "Эта аренда оплачивается другой статьёй ДДС — проверьте статью платежа"
        )
    if lease.started_on > on_date or (lease.ended_on is not None and lease.ended_on < on_date):
        raise LocationAnalyticsError(
            "На дату платежа этот договор аренды не действовал — выберите действующий"
        )
    if counterparty_id is not None and counterparty_id != lease.counterparty_id:
        raise LocationAnalyticsError("Выбранный контрагент не является арендодателем помещения")

    return LocationContext(location.id, lease.id, lease.counterparty_id)


async def leases_for_location(
    session: AsyncSession,
    location_id: uuid.UUID,
    *,
    article_id: uuid.UUID | None = None,
    on_date: date | None = None,
) -> list[LocationLease]:
    """Арендодатели помещения — источник выбора для платежа по арендной статье.

    Аренда без своей статьи подходит любой арендной статье: владелец мог не проставить её,
    и прятать такого арендодателя из списка было бы хуже, чем показать.
    """
    query = select(LocationLease).where(LocationLease.location_id == location_id)
    if on_date is not None:
        query = query.where(
            LocationLease.started_on <= on_date,
            (LocationLease.ended_on.is_(None)) | (LocationLease.ended_on >= on_date),
        )
    else:
        query = query.where(LocationLease.ended_on.is_(None))
    if article_id is not None:
        query = query.where(
            (LocationLease.dds_article_id == article_id)
            | (LocationLease.dds_article_id.is_(None))
        )
    return list((await session.scalars(query.order_by(LocationLease.started_on.desc()))).all())
