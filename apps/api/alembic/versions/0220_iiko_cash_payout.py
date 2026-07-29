"""Журнал денежных проводок в iiko вне накладных: выдачи авансов и депозитов.

Аванс налом, выдача производственного депозита и движение депозита курьера дублируются в
iiko изъятием/внесением (``payInOuts/addPayOut``), потому что «Главная касса» iiko — это
наш кошелёк ТК Черникова. Проводка необратима, а её судьба нигде не фиксировалась: любой
сбой (сеть, таймаут, лимит частоты, iiko недоступна) терял изъятие молча, оставляя после
себя только ``warning`` в логах контейнера — а они ротируются и обнуляются при пересборке.
Остаток кассы в iiko при этом тихо расходился с ДДС.

Таблица даёт проводке durable-след по образцу ``iiko_invoice_payment_push``: строка
``pending`` пишется ДО HTTP, после ответа переходит в ``posted``/``failed``. Уникальность
``(kind, source_id)`` — барьер идемпотентности: одна операция = одна проводка. ``pending``,
оставшийся после краша, означает «исход неизвестен» — авто-повтор по нему запрещён (задвоил
бы деньги), разбор ручной через owner-review.

Revision ID: 0220_iiko_cash_payout
Revises: 0219_freelancer_shift_void
Create Date: 2026-07-29
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0220_iiko_cash_payout"
down_revision = "0219_freelancer_shift_void"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "iiko_cash_payout",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("payout_date", sa.Date(), nullable=False),
        sa.Column("pay_out_type_id", sa.String(length=128), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reason_code", sa.String(length=32), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        # Пост-сверка по OLAP: ответ addPayOut не доказывает проводку (та же история, что с
        # add_payment по накладным), а «потерянную» выдачу иначе не увидеть.
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "verify_attempts", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("amount > 0", name="ck_iiko_cash_payout_amount_positive"),
        sa.CheckConstraint(
            "status in ('pending', 'posted', 'failed')", name="ck_iiko_cash_payout_status"
        ),
        sa.UniqueConstraint("kind", "source_id", name="uq_iiko_cash_payout_kind_source"),
    )
    op.create_index("ix_iiko_cash_payout_status", "iiko_cash_payout", ["status"])
    # Изъятия чека Кассы: та же беда, что у выдач — «failed» не различал «точно не прошло»
    # (тип не настроен) и «неизвестно» (обрыв на отправке). Повтор проведения чека вслепую
    # переотправлял и второе, задваивая изъятие в учёте iiko.
    op.add_column(
        "kassa_cheque_iiko_payout", sa.Column("reason_code", sa.String(length=32), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("kassa_cheque_iiko_payout", "reason_code")
    op.drop_index("ix_iiko_cash_payout_status", table_name="iiko_cash_payout")
    op.drop_table("iiko_cash_payout")
