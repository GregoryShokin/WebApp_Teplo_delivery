"""Контроль ошибочных цен накладных по скользящему среднему.

Для каждой товарной позиции считаем среднюю цену закупки этого товара (по ``product_guid``) за
последние ``lookback_days`` дней по ВСЕМ поставщикам (только payable, без персонала/возвратов/
бартера). Если у товара достаточно истории (≥ ``min_samples`` закупок) и цена в новой строке
отклонилась от среднего больше порога — вверх на +``upper_pct``% (переплата) или вниз на
−``lower_pct``% — строка помечается аномальной, а вся накладная «подозрительной»
(``price_control_status='flagged'``). Пока накладная не подтверждена, оплата и отправка в банк
заблокированы (:func:`assert_price_cleared`); человек сверяет цены и жмёт «ОК, всё верно»
(:func:`confirm_prices`) → статус ``confirmed`` разблокирует. Любая правка позиций пересчитывает
контроль и сбрасывает прежнее подтверждение.

Пороги ассиметричны намеренно: «дорого» (переплата) ловим жёстче (10%), «дёшево» — мягче (15%),
т.к. скидка/акция вероятнее реальной ошибки, чем внезапно возросшая цена. Значения по умолчанию
переопределяются ключами ``AppSetting`` ``invoice.price_control.*`` (без сида — код читает их,
если они заведены на «Настройках»).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AppSetting, IikoProduct, InvoiceLineItem, SupplierInvoice


class PriceControlError(RuntimeError):
    """Попытка оплатить / отправить в банк подозрительную (неподтверждённую) накладную."""


# --- дефолты (переопределяются AppSetting-ключами invoice.price_control.*) ---
DEFAULT_UPPER_PCT = Decimal("10")  # порог отклонения ВВЕРХ (переплата) — жёстче
DEFAULT_LOWER_PCT = Decimal("15")  # порог отклонения ВНИЗ (подозрительно дёшево) — мягче
DEFAULT_LOOKBACK_DAYS = 60
DEFAULT_MIN_SAMPLES = 3

SETTING_UPPER = "invoice.price_control.upper_pct"
SETTING_LOWER = "invoice.price_control.lower_pct"
SETTING_LOOKBACK = "invoice.price_control.lookback_days"
SETTING_MIN = "invoice.price_control.min_samples"


@dataclass(frozen=True)
class PriceControlConfig:
    upper_pct: Decimal
    lower_pct: Decimal
    lookback_days: int
    min_samples: int


@dataclass(frozen=True)
class PriceCheckLine:
    """Строка накладной для проверки цены — минимум, что нужно контролю (без ORM-объекта)."""

    name: str
    product_guid: str | None
    unit: str | None
    price: Decimal
    is_staff: bool = False
    is_return: bool = False


def _money(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _deviation_pct(price: Decimal, avg: Decimal) -> Decimal:
    """Отклонение цены от среднего в процентах (знак = направление), 1 знак после запятой."""
    return ((price - avg) / avg * Decimal("100")).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


async def load_config(session: AsyncSession) -> PriceControlConfig:
    rows = (
        await session.execute(
            select(AppSetting.key, AppSetting.value).where(
                AppSetting.key.in_(
                    (SETTING_UPPER, SETTING_LOWER, SETTING_LOOKBACK, SETTING_MIN)
                )
            )
        )
    ).all()
    raw: dict[str, Any] = {key: value for key, value in rows}

    def _dec(key: str, default: Decimal) -> Decimal:
        if raw.get(key) is not None:
            try:
                value = Decimal(str(raw[key]))
                if value > 0:
                    return value
            except (ValueError, ArithmeticError):
                return default
        return default

    def _int(key: str, default: int) -> int:
        if raw.get(key) is not None:
            try:
                value = int(raw[key])
                if value > 0:
                    return value
            except (ValueError, TypeError):
                return default
        return default

    return PriceControlConfig(
        upper_pct=_dec(SETTING_UPPER, DEFAULT_UPPER_PCT),
        lower_pct=_dec(SETTING_LOWER, DEFAULT_LOWER_PCT),
        lookback_days=_int(SETTING_LOOKBACK, DEFAULT_LOOKBACK_DAYS),
        min_samples=_int(SETTING_MIN, DEFAULT_MIN_SAMPLES),
    )


async def _moving_averages(
    session: AsyncSession,
    *,
    product_guids: set[str],
    as_of: date,
    cfg: PriceControlConfig,
    exclude_invoice_id: uuid.UUID | None,
) -> dict[tuple[str, str | None], tuple[Decimal, int]]:
    """Средняя цена и число закупок по (product_guid, unit) в окне [as_of − lookback; as_of].

    Только payable, не бартер, без персонала/возвратов, цена > 0. Ось времени — дата чека
    (``issued_at``) с фолбэком на ``invoice_date`` (у iiko-синканных времени нет). Текущая
    накладная исключается по id, чтобы её собственные строки не искажали базу при правке.
    """
    if not product_guids:
        return {}
    start = as_of - timedelta(days=cfg.lookback_days)
    date_expr = func.coalesce(func.date(SupplierInvoice.issued_at), SupplierInvoice.invoice_date)
    query = (
        select(
            InvoiceLineItem.product_guid,
            InvoiceLineItem.unit,
            func.avg(InvoiceLineItem.price).label("avg_price"),
            func.count().label("n"),
        )
        .join(SupplierInvoice, SupplierInvoice.id == InvoiceLineItem.invoice_id)
        .where(
            InvoiceLineItem.product_guid.in_(product_guids),
            InvoiceLineItem.is_staff.is_(False),
            InvoiceLineItem.is_return.is_(False),
            InvoiceLineItem.price > 0,
            SupplierInvoice.direction == "payable",
            SupplierInvoice.barter_role.is_(None),
            date_expr >= start,
            date_expr <= as_of,
        )
        .group_by(InvoiceLineItem.product_guid, InvoiceLineItem.unit)
    )
    if exclude_invoice_id is not None:
        query = query.where(SupplierInvoice.id != exclude_invoice_id)
    rows = (await session.execute(query)).all()
    return {(guid, unit): (_money(avg_price), int(n)) for guid, unit, avg_price, n in rows}


async def evaluate_lines(
    session: AsyncSession,
    *,
    lines: list[PriceCheckLine],
    as_of: date,
    cfg: PriceControlConfig,
    exclude_invoice_id: uuid.UUID | None,
) -> list[dict[str, Any]]:
    """Список аномальных строк (снимок для БД/UI). Товар без истории (< min_samples) НЕ флагается."""
    goods = [
        line
        for line in lines
        if line.product_guid and not line.is_staff and not line.is_return and line.price > 0
    ]
    if not goods:
        return []
    stats = await _moving_averages(
        session,
        product_guids={line.product_guid for line in goods if line.product_guid},
        as_of=as_of,
        cfg=cfg,
        exclude_invoice_id=exclude_invoice_id,
    )
    anomalies: list[dict[str, Any]] = []
    for line in goods:
        avg, count = stats.get((line.product_guid, line.unit), (None, 0))
        if avg is None or count < cfg.min_samples or avg <= 0:
            continue
        price = _money(line.price)
        deviation = _deviation_pct(price, avg)
        if deviation > cfg.upper_pct:
            direction = "high"
        elif deviation < -cfg.lower_pct:
            direction = "low"
        else:
            continue
        anomalies.append(
            {
                "name": line.name,
                "product_guid": line.product_guid,
                "unit": line.unit,
                "price": float(price),
                "avg_price": float(avg),
                "sample_count": count,
                "deviation_pct": float(deviation),
                "direction": direction,
            }
        )
    return anomalies


async def apply_price_control(
    session: AsyncSession, invoice: SupplierInvoice, lines: list[PriceCheckLine]
) -> None:
    """Пересчитать контроль цен и обновить ``price_control_*`` на накладной (без commit).

    Контроль применяется только к обычным приходным (payable, не бартер). Любой пересчёт
    сбрасывает прежнее подтверждение — изменившиеся цены нужно сверить заново.
    """
    invoice.price_confirmed_by_user_id = None
    invoice.price_confirmed_at = None
    if invoice.direction != "payable" or invoice.barter_role is not None:
        invoice.price_control_status = "clean"
        invoice.price_anomalies = []
        return
    cfg = await load_config(session)
    as_of = (invoice.issued_at.date() if invoice.issued_at else invoice.invoice_date) or date.today()
    anomalies = await evaluate_lines(
        session, lines=lines, as_of=as_of, cfg=cfg, exclude_invoice_id=invoice.id
    )
    invoice.price_anomalies = anomalies
    invoice.price_control_status = "flagged" if anomalies else "clean"


async def confirm_prices(
    session: AsyncSession, invoice: SupplierInvoice, *, actor_user_id: uuid.UUID | None
) -> None:
    """Подтвердить подозрительные цены («ОК, всё верно») — разблокирует оплату/отправку в банк."""
    if invoice.price_control_status == "clean":
        raise PriceControlError("У накладной нет подозрительных цен — подтверждать нечего")
    if invoice.price_control_status == "confirmed":
        raise PriceControlError("Цены уже подтверждены")
    invoice.price_control_status = "confirmed"
    invoice.price_confirmed_by_user_id = actor_user_id
    invoice.price_confirmed_at = datetime.now(tz=UTC)
    await session.commit()
    await session.refresh(invoice)


def assert_price_cleared(invoice: SupplierInvoice) -> None:
    """Гейт перед оплатой/отправкой в банк: не пускаем неподтверждённую подозрительную накладную."""
    if invoice.price_control_status == "flagged":
        count = len(invoice.price_anomalies or [])
        raise PriceControlError(
            f"Накладная содержит подозрительные цены ({count} поз.) — оплата и отправка в банк "
            "заблокированы, пока цены не подтверждены («ОК, всё верно»)."
        )


async def price_stats_for_product(
    session: AsyncSession, *, product_id: uuid.UUID
) -> dict[str, Any]:
    """Статистика цены товара для подсказки в форме ввода строки (живая подсветка отклонения)."""
    cfg = await load_config(session)
    product = await session.get(IikoProduct, product_id)
    empty = {
        "avg_price": None,
        "sample_count": 0,
        "unit": product.unit if product else None,
        "upper_pct": float(cfg.upper_pct),
        "lower_pct": float(cfg.lower_pct),
        "lookback_days": cfg.lookback_days,
        "min_samples": cfg.min_samples,
    }
    if product is None:
        return empty
    stats = await _moving_averages(
        session,
        product_guids={product.iiko_id},
        as_of=date.today(),
        cfg=cfg,
        exclude_invoice_id=None,
    )
    # Берём срез по основной единице товара; если её нет в истории — самый представительный.
    entry = stats.get((product.iiko_id, product.unit))
    if entry is None and stats:
        entry = max(stats.values(), key=lambda pair: pair[1])
    if entry is None:
        return empty
    avg, count = entry
    if count < cfg.min_samples:
        return {**empty, "sample_count": count}
    return {**empty, "avg_price": float(avg), "sample_count": count}
