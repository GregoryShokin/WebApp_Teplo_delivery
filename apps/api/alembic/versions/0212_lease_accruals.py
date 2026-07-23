"""Ежемесячное обязательство по аренде и залог как дебиторка.

Аренда без документов всё равно долг: договор известен, сумма известна, срок известен.
Обязательство заводим НЕ своей таблицей, а закрывающим документом ``SupplierInvoice``
(``doc_kind='closing'``, ``operational_scope='finance'``, ``source='lease'``) — только так оно
попадает в готовые витрины: ``list_counterparty_balances`` считает КЗ по накладным, а
``list_supplier_accounting`` — признание расхода по accrual с ``invoice_id``. Standalone-accrual
без накладной ни в одну из них не попал бы.

Идемпотентность генератора достаётся бесплатно: ``external_id = 'lease:{id}:{YYYY-MM}'`` под
существующим частичным уникумом ``uq_supplier_invoice_source_external``.

``accrual_enabled`` — выключатель на строке аренды: не всякий договор должен начислять
(например, аренда, которую платят разово или ведут вне системы).

Залог — не расход, а наши деньги у арендодателя, поэтому он живёт как ``SupplierPrepayment``
с привязкой к договору; ``kind='deposit'`` добавляется в EARMARKED_PREPAYMENT_KINDS в коде,
иначе первая же ежемесячная накладная съела бы залог авто-зачётом.

Значение enum добавляем ОТДЕЛЬНЫМ шагом с autocommit: PostgreSQL не даёт использовать новое
значение в той же транзакции, где оно создано (тот же капкан, что с ролью landlord в 0210).

Revision ID: 0212_lease_accruals
Revises: 0211_lease_dds_analytics
Create Date: 2026-07-23
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0212_lease_accruals"
down_revision = "0211_lease_dds_analytics"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ADD VALUE не работает внутри транзакции миграции — выходим в autocommit.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE counterparty_invoice_source ADD VALUE IF NOT EXISTS 'lease'")

    op.add_column(
        "location_lease",
        sa.Column(
            "accrual_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )
    op.add_column(
        "supplier_prepayment",
        sa.Column(
            "lease_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("location_lease.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_supplier_prepayment_lease_id", "supplier_prepayment", ["lease_id"])


def downgrade() -> None:
    op.drop_index("ix_supplier_prepayment_lease_id", table_name="supplier_prepayment")
    op.drop_column("supplier_prepayment", "lease_id")
    op.drop_column("location_lease", "accrual_enabled")
    # Значение enum не снимаем: PostgreSQL не умеет DROP VALUE, а пересоздание типа задело бы
    # существующие накладные.
