"""Договор услуги: сколько контрагент берёт в месяц и считаем ли долг сами.

То же, что договор аренды (``LocationLease``), только ось не «где» (помещение), а «кто»
(контрагент). Смысл общий: услуга оказывается каждый месяц, документов может не быть вовсе,
но долг известен заранее — договор, ставка, срок.

Строк на контрагента может быть несколько: у АО «АЙКО» две лицензии по 16 430 и 4 260 ₽, и
каждая живёт своей ставкой и своим сроком. Поэтому таблица, а не поля в профиле контрагента —
профиль единственный (``uq_counterparty_payable_profile_cp``).

Смена ставки — закрытие текущей строки (``ended_on``) и заведение новой, как у аренды: иначе
уже начисленные месяцы задним числом пересчитались бы по новой цене, а это подделка истории.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CounterpartyServiceAgreement(Base):
    __tablename__ = "counterparty_service_agreement"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    counterparty_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("counterparty.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    # Что оказывают. Попадает в номер документа начисления: у контрагента с двумя услугами
    # иначе обе строки в реестре ДЗ/КЗ выглядели бы одинаково.
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    monthly_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    # Статья ДДС услуги: по ней признаётся расход и по ней же платят.
    dds_article_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("dds_articles.id", ondelete="RESTRICT"), nullable=True
    )
    # informal — закрывающих документов не будет, долг считаем сами (ради этого договор и
    # заводится); official — документы приходят, начисление держим выключенным и ждём первичку,
    # иначе расход задвоится: один раз нашим начислением, второй — пришедшим УПД.
    documents_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="informal", server_default="informal"
    )
    # Выключатель ежемесячного начисления — как у аренды: не всякий договор должен порождать
    # обязательство (услуга на паузе, договор ведут вне системы).
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
