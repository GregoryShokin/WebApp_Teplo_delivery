from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class IikoCashShift(Base):
    """Закрытая кассовая смена iiko (`/resto/api/v2/cashshifts/list`).

    Витрина вкладки «Касса → Закрытие смены» и источник авто-проводки наличного
    контура в ДДС. Одна строка = одна смена кассы Черниковой. ``cash_remain`` —
    остаток наличных после закрытия, ``pay_out`` — суммарные изъятия (инкассация +
    ЗП курьеров + наличные Алисы); разнос изъятий по счетам-назначениям лежит в
    ``IikoCashShiftPayout``. ``posted`` — отметка, что наличный контур смены уже
    проведён в ДДС (барьер идемпотентности повторного синка).

    Смену сохраняем по наличию ``close_date``, а не по ``session_status`` — в проде
    закрытые смены приходят со статусом ``UNACCEPTED``. ``raw`` — исходный объект
    смены iiko как есть.
    """

    __tablename__ = "iiko_cash_shift"
    __table_args__ = (
        Index("uq_iiko_cash_shift_session", "iiko_session_id", unique=True),
        Index("ix_iiko_cash_shift_close_date", "close_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # GUID кассовой смены iiko — ключ upsert при повторном синке.
    iiko_session_id: Mapped[str] = mapped_column(String(128), nullable=False)
    session_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    point_of_sale_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    manager_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    open_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    close_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    session_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    session_start_cash: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    sales_cash: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    sales_card: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    pay_in: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    pay_out: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    cash_remain: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    cash_diff: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    posted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    raw: Mapped[dict] = mapped_column(JSONB, nullable=False)
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    payouts: Mapped[list[IikoCashShiftPayout]] = relationship(
        back_populates="shift", cascade="all, delete-orphan"
    )


class IikoCashShiftPayout(Base):
    """Изъятие наличных за смену (`payOutsRecords` из `/v2/cashshifts/payments/list`).

    Каждая запись — одно изъятие со счётом-назначением iiko (``account_id_iiko``),
    который задаёт ``category`` и правило проводки в ДДС:

    - ``main_cash`` — инкассация в кассу Черниковой → приход ДДС («Поступление денег
      с торг. точек», кошелёк ``tk_chernikova``);
    - ``courier_salary`` — ЗП курьеров → расход ДДС («Курьерская служба -»);
    - ``alisa`` — наличные у партнёра → дебиторка УДКЗ, движение ДДС НЕ создаётся;
    - ``unknown`` — прочий счёт назначения → движение не создаём, строку фиксируем.

    ``cashflow_transaction_id`` ссылается на созданное движение ДДС (NULL для
    ``alisa``/``unknown``).
    """

    __tablename__ = "iiko_cash_shift_payout"
    __table_args__ = (Index("ix_iiko_cash_shift_payout_shift", "shift_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    shift_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("iiko_cash_shift.id", ondelete="CASCADE"),
        nullable=False,
    )
    iiko_payout_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Счёт назначения изъятия в iiko (info.accountId) — определяет category.
    account_id_iiko: Mapped[str] = mapped_column(String(128), nullable=False)
    account_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    cashflow_transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("cashflow_transactions.id", ondelete="SET NULL"),
        nullable=True,
    )
    raw: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    shift: Mapped[IikoCashShift] = relationship(back_populates="payouts")
