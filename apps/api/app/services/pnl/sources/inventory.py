"""Ревизии и инвентаризация как источник ОПиУ.

СТРОКА «РЕЗУЛЬТАТЫ РЕВИЗИИ» СОСТАВНАЯ — так решил владелец ещё в мае 2026:
результат продуктовых инвентаризаций за месяц МИНУС штрафы по ревизиям за тот же месяц.
Штрафы идут отрицательным компонентом, потому что компенсируют ревизионные потери; их не
вычитают из зарплаты повторно (зарплатный адаптер их намеренно пропускает).

ЗНАК ИНВЕРТИРУЕТСЯ, И ЭТО ГЛАВНАЯ ЛОВУШКА ФАЙЛА. В базе недостача отрицательна
(``amount < 0``), излишек положителен. В отчёте наоборот: недостача — положительный расход,
излишек уменьшает его. Ошибка в знаке здесь стоит двойной величины недостачи и выглядит
правдоподобно, поэтому инверсия сделана явно и один раз. У СОСЕДНИХ строк — инвентаризации
упаковки и коробок для пиццы — политика ДРУГАЯ: там знак iiko сохраняется как есть.

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

    #: Знак УЖЕ инвертирован: недостача положительна, излишек отрицателен.
    product_result: Decimal | None = None
    audits_count: int = 0
    audit_dates: list[date] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.audit_dates is None:
            self.audit_dates = []


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
    signed_total = await session.scalar(
        select(func.coalesce(func.sum(InventoryAuditItem.amount), 0)).where(*conditions)
    )

    return InventoryMonth(
        # Инверсия: в базе недостача отрицательна, в отчёте она положительный расход.
        product_result=Decimal(signed_total or 0) * Decimal("-1"),
        audits_count=len(audits),
        audit_dates=sorted(audit.business_date for audit in audits),
    )
