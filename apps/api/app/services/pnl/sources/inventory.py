"""Ревизии и инвентаризация как источник ОПиУ.

СТРОКА «РЕЗУЛЬТАТЫ РЕВИЗИИ» СОСТАВНАЯ — так решил владелец ещё в мае 2026:
результат продуктовых инвентаризаций за месяц МИНУС штрафы по ревизиям за тот же месяц.
Штрафы идут отрицательным компонентом, потому что компенсируют ревизионные потери; их не
вычитают из зарплаты повторно (зарплатный адаптер их намеренно пропускает).

БЕРЁТСЯ «НЕДОСТАЧА ВСЕГО», А НЕ НЕТТО И НЕ ШТРАФНАЯ БАЗА — решение владельца 04.08.2026.
У ревизии три разных числа, и перепутать их легко:

* ``sum(item.shortage_amount)`` — недостача всего, ровно то, что экран ревизии подписывает
  «Недостача всего (из iiko)». За 13.07.2026 это 24 364,24 ₽;
* ``audit.total_shortage_amount`` — «Учитываемая в штрафе»: только активные позиции,
  15 044,18 ₽ по той же ревизии. Это база расчёта штрафа, а не потеря бизнеса;
* ``sum(item.amount)`` — нетто, недостачи минус излишки: −16 847,52 ₽. Раньше строка считала
  именно его, и владелец сказал, что так неверно.

Излишки в потерю не зачитываются: чаще всего это пересорт и ошибки учёта, а не найденный
товар, и вычитая их, мы прячем настоящую недостачу. ``shortage_amount`` неотрицателен по
CHECK-констрейнту, поэтому инвертировать здесь больше нечего — величина сразу положительный
расход. У СОСЕДНИХ строк (инвентаризация упаковки и коробок для пиццы) источник другой:
там зеркало iiko, и знак разворачивается в проекторе.

РЕВИЗИЙ НА ТОЧКЕ ДВЕ, И ЭТА СТРОКА — ПРО ПОВАРСКУЮ. Владелец описал контур 05.08.2026:
ПОВАРСКАЯ ревизия идёт каждую неделю и считает сырьё — её и ведут в модуле «Ревизии», отсюда
берётся эта строка. БАРНАЯ идёт раз в месяц по первым числам, её проводят кассиры, и считает
она упаковку, диппоты и напитки — всё, что стоит на барной стойке. В ОПиУ барная ревизия
приходит другим путём, инвентаризационным документом iiko, и питает строки
``packaging_inventory``, ``pizza_box_inventory`` и ``beverage_inventory``.

ПОЭТОМУ ФИЛЬТР ПО НОМЕНКЛАТУРЕ ОБЯЗАТЕЛЕН, ХОТЯ НА ИЮЛЕ 2026 ПОЧТИ НИЧЕГО НЕ ФИЛЬТРУЕТ.
Разделения по виду ревизии в самом модуле нет: барную ревизию тоже можно завести сюда, и
однажды её завели — документ от 01.06.2026 на 34 позиции упаковки и напитков (32 720,78 ₽)
лежит со статусом ``cancelled`` и в отчёт не идёт. Проведи его кто-нибудь — и расхождение
барной стойки попало бы разом в две строки отчёта, в разные блоки. Фильтр по ``line_code``
(``load_packaging_guids``) снимает ровно этот риск и стоит дёшево.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.inventory import InventoryAudit, InventoryAuditItem

#: Ревизия считается фактом только после проведения: черновик пересчитают, отменённая не
#: существует. Статусы модуля — draft / applied / cancelled.
APPLIED_STATUS = "applied"


@dataclass(slots=True)
class InventoryMonth:
    """Итоги инвентаризаций месяца."""

    #: Недостача всего за месяц — всегда неотрицательная величина расхода.
    product_result: Decimal | None = None
    audits_count: int = 0
    audit_dates: list[date] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.audit_dates is None:
            self.audit_dates = []


#: Строки ОПиУ, которые считают расхождение БАРНОЙ ревизии — упаковка, коробки для пиццы и
#: напитки. Их товары обязаны выпасть из ``audit_results``: иначе одно и то же расхождение
#: попадёт и в поварскую строку, и в свою.
BAR_AUDIT_LINES = ("packaging_inventory", "pizza_box_inventory", "beverage_inventory")


async def load_packaging_guids(session: AsyncSession) -> set[str]:
    """Товары, чьё расхождение НЕ идёт в строку поварской ревизии. Две разные причины.

    ПЕРВАЯ — расхождение уже посчитано в своей строке барной ревизии. Такие товары размечены
    как складские (``stocked``, это нужно балансу) и при этом имеют ``line_code``. Пара
    «складской + со строкой» и означает «сюда его брать нельзя, он уже учтён рядом».

    ВТОРАЯ — товар вообще не в складском учёте: его расход признан ПО ФАКТУ ПОКУПКИ. Правило
    владельца от 05.08.2026 про перчатки, шпажки, полотенца, чековую ленту и им подобных:
    «эти товары не участвуют в складском учёте, расход признаётся по факту покупки, и в
    балансе их тоже учитывать не нужно». Складские метрики их и так не берут (``stocked``
    у них не стоит), а вот в ревизионную потерю они попасть могли: барную ревизию заводили
    и в модуль «Ревизии», а фильтр смотрел только на ``line_code``. Проведи кто-нибудь такой
    документ — за перчатки заплатили бы дважды: расходом по накладной и недостачей по ревизии.

    На данных июля 2026 второе множество ничего не отсекает — этих товаров в проведённых
    поварских ревизиях нет. Защита стоит заранее: пробел здесь молчит, он не ломает сходимость
    и виден только сверкой с накладными.
    """
    from app.models.pnl import PnlProductWhitelist

    rows = await session.execute(
        select(PnlProductWhitelist.iiko_product_guid).where(
            (
                (PnlProductWhitelist.source_kind == "inventory")
                & PnlProductWhitelist.line_code.in_(BAR_AUDIT_LINES)
            )
            | (
                (PnlProductWhitelist.source_kind == "incoming_invoice")
                & (PnlProductWhitelist.include_status == "include")
            )
        )
    )
    return {guid for guid in rows.scalars() if guid}


async def build_inventory_month(
    session: AsyncSession,
    month_start: date,
    month_end: date,
    packaging_guids: set[str] | None = None,
) -> InventoryMonth:
    """Результат продуктовых ревизий месяца, относящихся к нему по дате ревизии.

    Месяц определяется ``business_date`` самой ревизии, а не датой удержания штрафа: штраф
    списывается следующим днём и может уехать в другой месяц, но потеря товара случилась
    тогда, когда её обнаружили.
    """
    packaging_guids = packaging_guids or set()

    audits = (
        (
            await session.execute(
                select(InventoryAudit).where(
                    InventoryAudit.status == APPLIED_STATUS,
                    InventoryAudit.business_date >= month_start,
                    InventoryAudit.business_date <= month_end,
                )
            )
        )
        .scalars()
        .all()
    )
    if not audits:
        return InventoryMonth()

    audit_ids = [audit.id for audit in audits]
    conditions = [InventoryAuditItem.audit_id.in_(audit_ids)]
    if packaging_guids:
        conditions.append(
            InventoryAuditItem.iiko_product_guid.not_in(packaging_guids)
            | InventoryAuditItem.iiko_product_guid.is_(None)
        )
    shortage_total = await session.scalar(
        select(func.coalesce(func.sum(InventoryAuditItem.shortage_amount), 0)).where(*conditions)
    )

    return InventoryMonth(
        product_result=Decimal(shortage_total or 0),
        audits_count=len(audits),
        audit_dates=sorted(audit.business_date for audit in audits),
    )
