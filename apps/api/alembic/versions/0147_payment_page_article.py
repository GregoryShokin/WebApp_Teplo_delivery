"""«Страница на оплату»: статья ДДС на счёте + закреплённая статья контрагента

Не-складские счета (ПО, реклама, техподдержка) при оплате в банк должны идти по СВОЕЙ статье
ДДС, а не по дефолтной «Оплата поставщикам». Оператор выбирает статью в окне отправки;
``supplier_invoice.dds_article_id`` хранит её для гашения (``apply_payment_status``), а
``counterparty_payable_profile.default_dds_article_id`` — закреплённую за контрагентом статью,
которой предзаполняется окно при следующей оплате. Оба nullable, ondelete=SET NULL.

Revision ID: 0147_payment_page_article
Revises: 0146_freelancer_cooks
Create Date: 2026-06-29
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0147_payment_page_article"
down_revision = "0146_freelancer_cooks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "supplier_invoice",
        sa.Column("dds_article_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_supplier_invoice_dds_article",
        "supplier_invoice",
        "dds_articles",
        ["dds_article_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column(
        "counterparty_payable_profile",
        sa.Column("default_dds_article_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_payable_profile_default_dds_article",
        "counterparty_payable_profile",
        "dds_articles",
        ["default_dds_article_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_payable_profile_default_dds_article", "counterparty_payable_profile", type_="foreignkey"
    )
    op.drop_column("counterparty_payable_profile", "default_dds_article_id")
    op.drop_constraint(
        "fk_supplier_invoice_dds_article", "supplier_invoice", type_="foreignkey"
    )
    op.drop_column("supplier_invoice", "dds_article_id")
