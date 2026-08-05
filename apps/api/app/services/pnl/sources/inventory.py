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

ПОЧЕМУ ФИЛЬТР ПО НОМЕНКЛАТУРЕ ЕСТЬ, ХОТЯ СЕЙЧАС НИЧЕГО НЕ ФИЛЬТРУЕТ. Продуктовая ревизия,
инвентаризация упаковки и коробки для пиццы приходят из одного эндпоинта iiko, а загрузчик
документов по номенклатуре не разделяет. На данных прода за июль 2026 упаковки в ревизиях
нет — ни по идентификатору товара, ни по названию, там только сырьё. Но если её начнут
загружать, без этого фильтра она попадёт и сюда, и в свою строку, то есть посчитается
дважды и в двух разных блоках отчёта. Фильтр стоит заранее и стоит дёшево.
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


async def load_packaging_guids(session: AsyncSession) -> set[str]:
    """Товары, чьё расхождение уходит в СВОИ строки инвентаризации упаковки.

    Берём по ``line_code``, а не по статусу: упаковка размечена как складская (``stocked``,
    это нужно балансу) и при этом имеет строку ОПиУ. Именно пара «складской + со строкой» и
    означает «его расхождение уже посчитано отдельно, сюда его брать нельзя».
    """
    from app.models.pnl import PnlProductWhitelist

    rows = await session.execute(
        select(PnlProductWhitelist.iiko_product_guid).where(
            PnlProductWhitelist.source_kind == "inventory",
            PnlProductWhitelist.line_code.in_(("packaging_inventory", "pizza_box_inventory")),
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
