from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class KdsTapType(StrEnum):
    """Три захватываемых на цеху упаковки момента."""

    READY = "ready"  # «Готово» — админ забрал готовый заказ у повара
    WAITING_COURIER = "waiting_courier"  # «Ждёт курьера» — упаковано, в сумке
    HANDED = "handed"  # «Передан курьеру» — курьер забрал


class KdsEventAction(StrEnum):
    SET = "set"  # поставить отметку
    ROLLBACK = "rollback"  # откатить ошибочную отметку
    EDIT = "edit"  # исправить время отметки


class KdsEventSource(StrEnum):
    KIOSK = "kiosk"  # планшет упаковки
    MANAGER = "manager"  # ручная правка из приложения


TAP_TYPE_VALUES = tuple(item.value for item in KdsTapType)
EVENT_ACTION_VALUES = tuple(item.value for item in KdsEventAction)
EVENT_SOURCE_VALUES = tuple(item.value for item in KdsEventSource)


def _sql_in(values: tuple[str, ...]) -> str:
    return ", ".join("'" + value + "'" for value in values)


class KdsOrder(Base):
    """Снимок активного заказа доставки + текущее состояние трёх тапов упаковки.

    Источник снимка — iikoCloud (вебхук ``DeliveryOrderUpdate`` + сверочный поллинг).
    Тапы пишутся с планшета упаковки (см. ``KdsOrderEvent``); денормализованные
    колонки ``*_at`` пересчитываются сворачиванием журнала событий по ``effective_at``.
    Без FK на ``delivery_order``: тап может опередить ~20-мин OLAP-синк курьеров.
    """

    __tablename__ = "kds_order"
    __table_args__ = (
        Index("ix_kds_order_work_date", "work_date"),
        Index("ix_kds_order_active_work_date", "is_active", "work_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    iiko_order_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    order_number: Mapped[str | None] = mapped_column(Text, nullable=True)
    work_date: Mapped[date] = mapped_column(Date, nullable=False)

    # snapshot из iikoCloud
    status: Mapped[str | None] = mapped_column(Text, nullable=True)
    complete_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    when_created: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_preorder: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    has_hot_item: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    # eventTime последнего обработанного снимка — защита от out-of-order вебхуков
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # три тапа упаковки (свёртка журнала)
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    waiting_courier_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    handed_to_courier_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    last_actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    raw: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class KdsOrderEvent(Base):
    """Append-only журнал тапов упаковки (источник истины + откаты/лог изменений).

    Идемпотентность офлайн-буфера — по ``client_event_id`` (UNIQUE). Откат — новая
    строка ``action=rollback`` (не DELETE); ``previous_value`` хранит отменённое.
    """

    __tablename__ = "kds_order_event"
    __table_args__ = (
        CheckConstraint(
            f"event_type in ({_sql_in(TAP_TYPE_VALUES)})",
            name="ck_kds_order_event_event_type",
        ),
        CheckConstraint(
            f"action in ({_sql_in(EVENT_ACTION_VALUES)})",
            name="ck_kds_order_event_action",
        ),
        CheckConstraint(
            f"source in ({_sql_in(EVENT_SOURCE_VALUES)})",
            name="ck_kds_order_event_source",
        ),
        Index("ix_kds_order_event_order_effective", "iiko_order_id", "effective_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    kds_order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("kds_order.id", ondelete="CASCADE"), nullable=False
    )
    iiko_order_id: Mapped[str] = mapped_column(Text, nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False, server_default="set")
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    source: Mapped[str] = mapped_column(String(16), nullable=False, server_default="kiosk")
    client_event_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    previous_value: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
