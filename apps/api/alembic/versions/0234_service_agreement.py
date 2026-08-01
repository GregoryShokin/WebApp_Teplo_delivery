"""Договор услуги: ежемесячный тариф контрагента, по которому долг считается сам.

ЗАЧЕМ. Часть поставщиков услуг закрывающих документов не присылает вовсе (бухгалтер, охрана,
мелкое обслуживание). Платежи им есть, документов нет — и деньги навсегда оседают ложной
дебиторкой, а расход не признаётся ни в одном месяце. На 01.08.2026 так висело 311 969,51 ₽
у десяти контрагентов.

РЕШЕНИЕ — ТО ЖЕ, ЧТО У АРЕНДЫ. Арендодатель тоже документов не выставляет, но долг известен:
договор, ставка, срок. ``lease_accruals`` раз в месяц заводит закрывающий документ сам, и
контур ДЗ/КЗ работает как обычно. Эта таблица — тот же ``LocationLease``, только ось не
«где» (помещение), а «кто» (контрагент): у ИП Наумченко услуга одна, у АО «АЙКО» их две
(две лицензии по 16 430 и 4 260 ₽), поэтому таблица, а не колонки в профиле — профиль
единственный на контрагента (``uq_counterparty_payable_profile_cp``).

``service_billing_mode`` на профиле — подтип услугового контрагента, разный по ОЖИДАНИЯМ:

* ``fixed_tariff`` — абонентка фиксированной суммой (Синапсис 13 000, ДоксИнБокс 15 580,
  Манго 5 000). Тариф известен заранее, долг можно считать самим;
* ``per_invoice`` — сумма плавает от месяца к месяцу (реклама О.О: 28 000 / 48 000 / 68 000,
  ЭкоЦентр по объёму). Тариф поставить нельзя, ждём счёт и документ;
* ``one_off`` — разовые работы (ремонт, юрист, типография). Ежемесячных документов от них
  не ждём вовсе, и сводка разрывов не должна по ним краснеть: тишина — это норма.

Revision ID: 0234_service_agreement
Revises: 0233_subscription_self_billed
Create Date: 2026-08-01
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0234_service_agreement"
down_revision = "0233_subscription_self_billed"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "counterparty_service_agreement",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "counterparty_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("counterparty.id", ondelete="RESTRICT"),
            nullable=False,
            index=True,
        ),
        # Что именно оказывают: «Ведение бухгалтерии», «Лицензия iikoCloud», «Охрана объекта».
        # Идёт в номер документа начисления — иначе в реестре ДЗ/КЗ у контрагента с двумя
        # услугами обе строки выглядели бы одинаково.
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("monthly_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column(
            "dds_article_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("dds_articles.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        # informal — документов не будет, долг считаем сами (ради этого таблица и заводится);
        # official — документы приходят, начисление держим выключенным и ждём первичку.
        sa.Column(
            "documents_mode",
            sa.String(16),
            nullable=False,
            server_default="informal",
        ),
        sa.Column("accrual_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("started_on", sa.Date(), nullable=False),
        sa.Column("ended_on", sa.Date(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("monthly_amount > 0", name="ck_service_agreement_amount_positive"),
        sa.CheckConstraint(
            "documents_mode in ('official', 'informal')",
            name="ck_service_agreement_documents_mode",
        ),
        sa.CheckConstraint(
            "ended_on is null or ended_on >= started_on",
            name="ck_service_agreement_period",
        ),
    )

    op.add_column(
        "counterparty_payable_profile",
        sa.Column("service_billing_mode", sa.String(16), nullable=True),
    )
    op.create_check_constraint(
        "ck_payable_profile_service_billing_mode",
        "counterparty_payable_profile",
        "service_billing_mode is null or service_billing_mode in "
        "('fixed_tariff', 'per_invoice', 'one_off')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_payable_profile_service_billing_mode",
        "counterparty_payable_profile",
        type_="check",
    )
    op.drop_column("counterparty_payable_profile", "service_billing_mode")
    op.drop_table("counterparty_service_agreement")
