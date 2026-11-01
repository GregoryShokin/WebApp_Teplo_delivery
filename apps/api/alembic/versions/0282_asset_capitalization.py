"""Dated capital repair decisions; no legacy payments are capitalized automatically.

Revision ID: 0282_asset_capitalization
Revises: 0281_tbank_overdraft_rules
"""

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0282_asset_capitalization"
down_revision = "0281_tbank_overdraft_rules"
branch_labels = None
depends_on = None
CODE = "accounting.fixed_assets.capitalize"


def upgrade():
    op.create_table(
        "asset_capitalization",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "asset_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("fixed_asset.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "link_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("asset_cashflow_link.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("cost_before", sa.Numeric(14, 2)),
        sa.Column("cost_after", sa.Numeric(14, 2)),
        sa.Column("monthly_before", sa.Numeric(14, 2)),
        sa.Column("monthly_after", sa.Numeric(14, 2)),
        sa.Column("effective_on", sa.Date()),
        sa.Column("depreciation_from", sa.Date()),
        sa.Column(
            "decided_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
        ),
        sa.Column("note", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("link_id", name="uq_asset_capitalization_link"),
        sa.CheckConstraint(
            "status IN ('confirmed','dismissed')", name="ck_asset_capitalization_status"
        ),
        sa.CheckConstraint("amount > 0", name="ck_asset_capitalization_amount"),
        sa.CheckConstraint(
            (
                "(status = 'confirmed' AND effective_on IS NOT NULL AND "
                "depreciation_from IS NOT NULL) OR (status = 'dismissed' AND "
                "effective_on IS NULL AND depreciation_from IS NULL)"
            ),
            name="ck_asset_capitalization_dates",
        ),
    )
    op.create_index(
        "ix_asset_capitalization_asset_date", "asset_capitalization", ["asset_id", "effective_on"]
    )
    op.execute("""
    CREATE FUNCTION protect_asset_repair_decision() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_TABLE_NAME = 'cashflow_transactions' THEN
        IF EXISTS (SELECT 1 FROM asset_capitalization c
          JOIN asset_cashflow_link l ON l.id=c.link_id
          WHERE l.cashflow_transaction_id=OLD.id)
          AND (TG_OP='DELETE'
          OR NEW.amount IS DISTINCT FROM OLD.amount
          OR NEW.article_id IS DISTINCT FROM OLD.article_id
         
          OR NEW.direction IS DISTINCT FROM OLD.direction
          OR NEW.operation_date IS DISTINCT FROM OLD.operation_date
         
          OR NEW.quality_status IS DISTINCT FROM OLD.quality_status
          OR NEW.wallet_id IS DISTINCT FROM OLD.wallet_id
         
          OR NEW.source_kind IS DISTINCT FROM OLD.source_kind
          OR NEW.source_id IS DISTINCT FROM OLD.source_id) THEN
          RAISE EXCEPTION 'Решение о ремонте уже принято: изменение или удаление платежа запрещено'
          USING ERRCODE='23514';
        END IF;
      ELSE
        IF EXISTS (SELECT 1 FROM asset_capitalization WHERE link_id=OLD.id) THEN
          RAISE EXCEPTION 'Решение о ремонте уже принято: изменение или удаление связи запрещено'
          USING ERRCODE='23514';
        END IF;
      END IF;
      IF TG_OP='DELETE' THEN RETURN OLD; END IF;
      RETURN NEW;
    END $$;
    """)
    op.execute("""CREATE TRIGGER protect_asset_repair_tx
      BEFORE UPDATE OR DELETE ON cashflow_transactions
      FOR EACH ROW EXECUTE FUNCTION protect_asset_repair_decision();
    """)
    op.execute("""CREATE TRIGGER protect_asset_repair_link
      BEFORE UPDATE OR DELETE ON asset_cashflow_link
      FOR EACH ROW EXECUTE FUNCTION protect_asset_repair_decision();
    """)
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "INSERT INTO permission(id,code,module,description) VALUES "
            "(:id,:code,'Учёт','Подтверждать капитализацию ремонта ОС') ON "
            "CONFLICT (code) DO NOTHING"
        ),
        {"id": uuid.uuid5(uuid.NAMESPACE_URL, CODE), "code": CODE},
    )
    bind.execute(
        sa.text(
            "INSERT INTO role_permission(role_id,permission_id) SELECT "
            "r.id,p.id FROM role r CROSS JOIN permission p WHERE r.code IN "
            "('admin','owner') AND p.code=:code ON CONFLICT DO NOTHING"
        ),
        {"code": CODE},
    )


def downgrade():
    op.execute("DROP TRIGGER protect_asset_repair_tx ON cashflow_transactions")
    op.execute("DROP TRIGGER protect_asset_repair_link ON asset_cashflow_link")
    op.execute("DROP FUNCTION protect_asset_repair_decision()")
    op.drop_table("asset_capitalization")
    op.execute(sa.text("DELETE FROM permission WHERE code='accounting.fixed_assets.capitalize'"))
