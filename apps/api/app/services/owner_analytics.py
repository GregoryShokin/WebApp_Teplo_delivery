"""Аналитика по собственнику: движение по «его» статьям обязано называть, чьё оно.

Правило одно и живёт здесь по той же причине, что и правило помещения: статья и контрагент
сходятся в шести входах ДДС (разбор банковской операции, разбор ручной проводки, PATCH
проводки, черновик «Нового платежа», наличная оплата через Сейф, выплата из кассы). Проверяй
в каждом по-своему — и аналитика окажется дырявой ровно там, где о ней забыли.

Смысл правил:

* статья с ``owner_required`` (взнос собственника, возврат ему, дивиденды) без собственника
  бессмысленна: собственников двое, и «поступление от собственников» без имени — общий котёл,
  из которого нельзя вынуть, кто сколько внёс;
* названный контрагент обязан числиться в РЕЕСТРЕ (``business_owner``), а не просто носить роль
  ``owner`` в карточке. Роль — пометка для списков, её можно проставить мимо реестра, и она не
  несёт ни доли, ни даты входа. Источник правды один, и это тот, в котором есть всё нужное;
* обратное правило (собственник на обычной статье) НЕ вводим, в отличие от помещения. Человек
  бывает бизнесу и арендодателем, и подрядчиком; запретить ему появляться на других статьях
  значило бы решать за владельца, кем ещё этот человек может быть.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import BusinessOwner, Counterparty, CounterpartyRole, DdsArticle

__all__ = [
    "OWNER_LOAN_KIND",
    "OWNER_ROLE",
    "OwnerAnalyticsError",
    "OwnerRow",
    "ensure_owner_context",
    "is_owner_settlement",
    "list_owners",
    "settlement_article_ids",
    "shares_total",
]

OWNER_ROLE = "owner"

# Вид дебиторки «собственник должен бизнесу»: заём выданный и входящий остаток по нему. Отдельный
# вид, а не ``subscription``, потому что услугой он не станет никогда — расход по нему не
# признаётся, документа от собственника не ждут, и очередь признания расходов такие строки
# обязана пропускать (иначе 1,25 млн ₽ входящих остатков вечно висят в «Ждём документ»).
OWNER_LOAN_KIND = "owner_loan"


async def settlement_article_ids(session: AsyncSession) -> set[uuid.UUID]:
    """Статьи расчётов с собственниками: заём, его возврат, взнос, дивиденды."""
    return set(
        (
            await session.scalars(select(DdsArticle.id).where(DdsArticle.owner_required.is_(True)))
        ).all()
    )


def is_owner_settlement(
    *, kind: str | None, article_id: uuid.UUID | None, owner_articles: Collection[uuid.UUID]
) -> bool:
    """Платёж — расчёт с собственником, а не услуга: документа по нему не будет никогда.

    Один признак для очереди признания, сверки с контрагентом и сводки разрывов. Пока очередь
    проверяла его сама, а сверка — нет, входящие остатки собственников (1 020 000 и 200 000 ₽) и
    июльский заём Павла ушли из «Ждём документ», но в карточке продолжали краснеть «документа
    нет · 15 дн.». ОПиУ отсекает такие строки раньше и по-своему: статьи собственника в отчёт не
    входят (``in_pnl=false``), и ожидание по ним до строки отчёта не доходит.

    Отбор по СТАТЬЕ, а не по контрагенту: собственник бывает бизнесу ещё и арендодателем, и
    подрядчиком (см. докстринг модуля), и выкинуть все его платежи значило бы спрятать настоящую
    услугу вместе с займом. ``kind`` ловит входящий остаток: у него статьи нет вовсе.
    """
    return kind == OWNER_LOAN_KIND or (article_id is not None and article_id in owner_articles)


class OwnerAnalyticsError(ValueError):
    """Нарушение правил аналитики по собственнику. Поднимается ДО любых записей."""


@dataclass(frozen=True)
class OwnerRow:
    """Строка реестра: человек и его доля."""

    counterparty: Counterparty
    registration: BusinessOwner

    @property
    def share_percent(self) -> Decimal:
        return Decimal(self.registration.share_percent)


async def is_owner(
    session: AsyncSession, counterparty_id: uuid.UUID, *, on_date: date | None = None
) -> bool:
    """Числится ли человек собственником. Вышедший из состава — уже нет.

    Дата нужна не для строгости: после выхода человек остаётся в базе со всеми прошлыми
    расчётами, и без проверки новый взнос записался бы на того, кто больше не владеет.
    """
    row = await session.scalar(
        select(BusinessOwner).where(BusinessOwner.counterparty_id == counterparty_id)
    )
    if row is None:
        return False
    if on_date is None:
        return row.ended_on is None
    return row.started_on <= on_date and (row.ended_on is None or row.ended_on >= on_date)


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
            "Выбранный контрагент не значится в реестре собственников. Заведите его в "
            "«Настройках» с указанием доли или выберите другого"
        )


async def list_owners(session: AsyncSession, *, include_former: bool = False) -> list[OwnerRow]:
    """Реестр собственников с долями.

    Вышедших из состава по умолчанию не показываем, но и не удаляем: прошлые начисления и
    расчёты с ними остаются, и реестр обязан объяснять, откуда они взялись.
    """
    query = (
        select(BusinessOwner, Counterparty)
        .join(Counterparty, Counterparty.id == BusinessOwner.counterparty_id)
        .order_by(Counterparty.name)
    )
    if not include_former:
        query = query.where(BusinessOwner.ended_on.is_(None))
    rows = (await session.execute(query)).all()
    return [OwnerRow(counterparty=counterparty, registration=owner) for owner, counterparty in rows]


def shares_total(owners: list[OwnerRow]) -> Decimal:
    """Сумма долей действующих собственников.

    Показывается человеку, а не навязывается: пока реестр заполняется, промежуточные 50 % —
    нормальное состояние, и отказ в сохранении мешал бы вводу. Ошибку видно числом.
    """
    return sum((row.share_percent for row in owners), Decimal("0"))


async def ensure_role_marker(session: AsyncSession, counterparty_id: uuid.UUID) -> None:
    """Проставить карточке роль «Собственник» — пометка для списков и фильтров.

    Решать «собственник ли это» роль не может: её ставят руками мимо реестра, и доли у неё нет.
    Но без неё человек не найдётся там, где контрагентов фильтруют по роли.
    """
    exists = await session.scalar(
        select(CounterpartyRole.counterparty_id).where(
            CounterpartyRole.counterparty_id == counterparty_id,
            CounterpartyRole.role == OWNER_ROLE,
        )
    )
    if exists is None:
        session.add(CounterpartyRole(counterparty_id=counterparty_id, role=OWNER_ROLE))
        await session.flush()
