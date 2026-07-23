"""Аренда помещения: кто сдаёт, почём и на каких условиях.

Строка аренды связывает помещение с арендодателем-контрагентом. Активных строк на одном
помещении может быть несколько — площадь бывает поделена между собственниками, а склад и
торговый зал в одном адресе сдают разные лица.

Смена арендодателя — это не правка строки, а закрытие текущей (``ended_on``) и заведение
новой: иначе история платежей за прошлые месяцы задним числом переписалась бы на нового
собственника. По той же причине строки не удаляются.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class LocationLease(Base):
    __tablename__ = "location_lease"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    location_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("location.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    counterparty_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("counterparty.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    monthly_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    # Число месяца, когда платим. Пусто — договорённость «по факту», напоминания не строим.
    payment_day: Mapped[int | None] = mapped_column(nullable=True)
    # prepaid — платим за месяц вперёд, postpaid — по его окончании. От этого зависит, за какой
    # период считается начисленный долг в момент платежа.
    payment_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="prepaid", server_default="prepaid"
    )
    # official — арендодатель выставляет УПД, informal — документов нет и долг считаем сами.
    documents_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="informal", server_default="informal"
    )
    # Залог — не расход, а наши деньги у арендодателя: дебиторка до возврата или зачёта.
    deposit_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=Decimal("0"), server_default="0"
    )
    # Статья ДДС этой аренды: по ней платят и по ней же система предложит арендодателей
    # именно этого помещения при разборе платежа.
    dds_article_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("dds_articles.id", ondelete="RESTRICT"), nullable=True
    )
    # Выключатель ежемесячного начисления: не всякий договор должен порождать обязательство
    # (разовая аренда, договор, который ведут вне системы).
    accrual_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    started_on: Mapped[date] = mapped_column(Date, nullable=False)
    ended_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
