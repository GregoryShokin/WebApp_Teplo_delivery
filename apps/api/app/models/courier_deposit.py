from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import BigInteger, CheckConstraint, Date, DateTime, ForeignKey, Integer, Text, func
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CourierDepositTransactionType(StrEnum):
    TOP_UP = "top_up"
    RETURN = "return"
    FORFEIT = "forfeit"


courier_deposit_transaction_type_enum = SQLEnum(
    CourierDepositTransactionType,
    name="courier_deposit_transaction_type",
    values_callable=lambda values: [item.value for item in values],
)


class CourierDepositAccount(Base):
    __tablename__ = "courier_deposit_account"

    employee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("employee.id", ondelete="CASCADE"),
        primary_key=True,
    )
    target_amount_cents: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=500_000,
        server_default="500000",
    )
    opening_balance_cents: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        server_default="0",
    )
    opening_date: Mapped[date] = mapped_column(
        Date,
        nullable=False,
        server_default=func.current_date(),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class CourierDepositTransaction(Base):
    __tablename__ = "courier_deposit_transaction"
    __table_args__ = (
        CheckConstraint(
            "amount_cents > 0",
            name="ck_courier_deposit_transaction_amount_positive",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_employee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("courier_deposit_account.employee_id", ondelete="CASCADE"),
        nullable=False,
    )
    transaction_type: Mapped[CourierDepositTransactionType] = mapped_column(
        courier_deposit_transaction_type_enum,
        nullable=False,
    )
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    transaction_date: Mapped[date] = mapped_column(Date, nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Канал выдачи для RETURN: NULL/'cash_tk' = ТК Черникова (legacy, + iiko-изъятие),
    # 'cash_safe' = с Сейфа, 'bank_draft' = безнал через Сейф (этап 3). Для TOP_UP/FORFEIT NULL.
    payout_method: Mapped[str | None] = mapped_column(Text, nullable=True)
    payout_wallet_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("wallet.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
