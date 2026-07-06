"""Касса: этап 3 целевых выплат — передача целёвок в кассу и разрешения на авансы.

- ``safe_allocations.location`` — где живёт целевой резерв: ``safe`` (на карте
  «Сейф», по умолчанию) или ``kassa`` (передан в Торговую кассу Черникова вместе
  с деньгами двухногим перемещением). Переданный резерв меняет и ``wallet_id``
  на кассу, поэтому инвариант Сейфа (баланс = свободно + резервы) не ломается,
  а выдаётся резерв уже наличными из кассы (вкладка «К выдаче»).
- ``salary_advance.kassa_cancelled_at`` — отметка «разрешение отменено кассой»:
  админ отклонил pending-выдачу аванса/займа через кассу (создатель видит статус
  «отменено кассой» на странице авансов). NULL у отозванных самим создателем.

Права не добавляются: создание разрешения покрыто правом выдачи авансов/займов,
исполнение и отмена в кассе — ``kassa.payouts.create``, передача целёвки —
``finance.safe.confirm_paid``.

Revision ID: 0160_kassa_target_stage3
Revises: 0159_kassa_journal_payout
Create Date: 2026-07-06
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0160_kassa_target_stage3"
down_revision = "0159_kassa_journal_payout"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "safe_allocations",
        sa.Column(
            "location",
            sa.String(length=8),
            nullable=False,
            server_default="safe",
        ),
    )
    op.create_check_constraint(
        "ck_safe_allocations_location",
        "safe_allocations",
        "location in ('safe', 'kassa')",
    )
    op.add_column(
        "salary_advance",
        sa.Column("kassa_cancelled_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("salary_advance", "kassa_cancelled_at")
    op.drop_constraint("ck_safe_allocations_location", "safe_allocations", type_="check")
    op.drop_column("safe_allocations", "location")
