"""Аналитика по собственнику: движение по «его» статьям обязано называть, чьё оно.

Правило одно и живёт здесь по той же причине, что и правило помещения: статья и контрагент
сходятся в шести входах ДДС (разбор банковской операции, разбор ручной проводки, PATCH
проводки, черновик «Нового платежа», наличная оплата через Сейф, выплата из кассы). Проверяй
в каждом по-своему — и аналитика окажется дырявой ровно там, где о ней забыли.

Смысл правил:

* статья с ``owner_required`` (взнос собственника, возврат ему, дивиденды) без собственника
  бессмысленна: собственников двое, и «поступление от собственников» без имени — общий котёл,
  из которого нельзя вынуть, кто сколько внёс;
* названный контрагент обязан БЫТЬ собственником — с ролью ``owner`` в карточке. Иначе взнос
  запишется на поставщика, а расчёты с ним поедут в чужую сторону;
* обратное правило (собственник на обычной статье) НЕ вводим, в отличие от помещения. Человек
  бывает бизнесу и арендодателем, и подрядчиком; запретить ему появляться на других статьях
  значило бы решать за владельца, кем ещё этот человек может быть.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Counterparty, CounterpartyRole, DdsArticle

__all__ = ["OWNER_ROLE", "OwnerAnalyticsError", "ensure_owner_context", "list_owners"]

OWNER_ROLE = "owner"


class OwnerAnalyticsError(ValueError):
    """Нарушение правил аналитики по собственнику. Поднимается ДО любых записей."""


async def is_owner(session: AsyncSession, counterparty_id: uuid.UUID) -> bool:
    row = await session.scalar(
        select(CounterpartyRole.counterparty_id).where(
            CounterpartyRole.counterparty_id == counterparty_id,
            CounterpartyRole.role == OWNER_ROLE,
        )
    )
    return row is not None


async def ensure_owner_context(
    session: AsyncSession,
    *,
    article: DdsArticle | None,
    counterparty_id: uuid.UUID | None,
) -> None:
    """Проверить связку «статья ↔ собственник». Ничего не возвращает: достраивать нечего.

    В отличие от помещения, где аренда подставляет арендодателя, здесь вывести собственника
    неоткуда — его называет человек. Поэтому функция только отказывает, и отказывает ДО записи.
    """
    if article is None or not article.owner_required:
        return
    if counterparty_id is None:
        raise OwnerAnalyticsError(
            f"Для статьи «{article.name}» укажите собственника — деньги каждого учитываются "
            "отдельно"
        )
    if not await is_owner(session, counterparty_id):
        raise OwnerAnalyticsError(
            "Выбранный контрагент не заведён как собственник. Отметьте роль «Собственник» "
            "в его карточке или выберите другого"
        )


async def list_owners(session: AsyncSession) -> list[Counterparty]:
    """Реестр собственников — те же контрагенты, отобранные по роли.

    Отдельной таблицы у собственника нет намеренно: ему нужны ровно те же расчёты, что и любому
    контрагенту (он вносит деньги, ему возвращают, ему начисляют дивиденды), и второй реестр
    означал бы вторую, несовместимую историю долга.
    """
    return list(
        (
            await session.scalars(
                select(Counterparty)
                .join(CounterpartyRole, CounterpartyRole.counterparty_id == Counterparty.id)
                .where(CounterpartyRole.role == OWNER_ROLE)
                .order_by(Counterparty.name)
            )
        ).all()
    )
