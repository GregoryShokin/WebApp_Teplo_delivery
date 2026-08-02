"""Реестр собственников бизнеса: кто владеет и какой долей.

ПОЧЕМУ ОТДЕЛЬНАЯ ТАБЛИЦА, ЕСЛИ СОБСТВЕННИК — КОНТРАГЕНТ. Личность и расчёты живут в карточке
контрагента: он вносит деньги, ему возвращают, ему начисляют дивиденды — механика ровно та же,
что у любого контрагента, и второй истории долга нам не нужно. А вот ДОЛЯ — свойство участия в
бизнесе, а не человека: у поставщика её не бывает, и колонка в общей таблице была бы пустой у
всех, кроме двоих. Поэтому долю держит эта запись, а личность — та.

ЭТА ТАБЛИЦА И ЕСТЬ РЕЕСТР. Роль ``owner`` в карточке контрагента остаётся пометкой для списков
и фильтров, но вопрос «собственник ли это» решается здесь: у роли нет ни доли, ни даты входа, и
проставить её можно мимо реестра. Один источник правды — тот, в котором есть всё нужное.

ДОЛЯ ХРАНИТСЯ, А НЕ СЧИТАЕТСЯ. Сумма долей обязана давать 100 %, но система это ПОКАЗЫВАЕТ, а не
навязывает: пока реестр заполняется, промежуточные 50 % — нормальное состояние, и запрет на
сохранение мешал бы вводу. Ошибку человек видит числом, а не ловит отказом.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Numeric,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class BusinessOwner(Base):
    """Одна строка = один собственник с его долей."""

    __tablename__ = "business_owner"
    __table_args__ = (
        CheckConstraint(
            "share_percent > 0 AND share_percent <= 100", name="ck_business_owner_share"
        ),
        # Один человек — одна запись: доля у него одна, а две строки означали бы спор о том,
        # какая из них настоящая.
        UniqueConstraint("counterparty_id", name="uq_business_owner_counterparty"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # RESTRICT: удаление карточки не должно уносить факт владения — по нему считаются дивиденды
    # и долг бизнеса перед человеком.
    counterparty_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("counterparty.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    share_percent: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    # С какой даты человек владеет долей. Нужна не для красоты: дивиденды за период, в котором
    # он ещё не входил в состав, ему не причитаются.
    started_on: Mapped[date] = mapped_column(Date, nullable=False)
    # Выход из состава. Запись остаётся: прошлые начисления и расчёты с ним никуда не деваются.
    ended_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
