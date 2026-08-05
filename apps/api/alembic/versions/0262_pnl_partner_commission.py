"""Расчёты с партнёрами из сохранённого OLAP-отчёта iiko.

Комиссия партнёра не является кассовым расходом месяца. Договорная база — выручка со
скидкой из OLAP «Отчёт о партнёрах», комиссия «В гостях у Алисы» — 20 %. Поэтому денежную
статью отдаём потоку iiko, а рядом с контрольным итогом храним месячную расшифровку:
партнёр → выручка → ставка → комиссия.

Revision ID: 0262_pnl_partner_commission
Revises: 0261_pnl_revision_result
Create Date: 2026-08-04
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0262_pnl_partner_commission"
down_revision = "0261_pnl_revision_result"
branch_labels = None
depends_on = None


PARTNER_PRESET_ID = "28b87ee7-0af5-41ad-baae-de735189169c"


def upgrade() -> None:
    op.create_table(
        "pnl_partner_commission_rule",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("partner_key", sa.String(255), nullable=False),
        sa.Column("partner_name", sa.String(255), nullable=False),
        sa.Column("commission_rate", sa.Numeric(7, 6), nullable=False),
        sa.Column("source_preset_id", sa.String(64), nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "commission_rate >= 0 and commission_rate <= 1",
            name="ck_pnl_partner_commission_rule_rate",
        ),
    )
    op.create_index(
        "uq_pnl_partner_commission_rule_key",
        "pnl_partner_commission_rule",
        ["partner_key"],
        unique=True,
    )

    op.create_table(
        "pnl_partner_commission_fact",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("period_month", sa.Date, nullable=False),
        sa.Column(
            "rule_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("pnl_partner_commission_rule.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("partner_key", sa.String(255), nullable=False),
        sa.Column("partner_name", sa.String(255), nullable=False),
        sa.Column("revenue_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("commission_rate", sa.Numeric(7, 6), nullable=False),
        sa.Column("commission_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("rows_count", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("source_ref", sa.Text, nullable=False),
        sa.Column(
            "synced_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "commission_rate >= 0 and commission_rate <= 1",
            name="ck_pnl_partner_commission_fact_rate",
        ),
    )
    op.create_index(
        "uq_pnl_partner_commission_fact_slot",
        "pnl_partner_commission_fact",
        ["period_month", "rule_id"],
        unique=True,
    )

    op.execute(
        sa.text(
            """
            INSERT INTO pnl_partner_commission_rule (
                id, partner_key, partner_name, commission_rate, source_preset_id
            )
            VALUES (
                gen_random_uuid(), 'в гостях у алисы', 'В гостях у Алисы', 0.20, :preset_id
            )
            ON CONFLICT (partner_key) DO UPDATE SET
                partner_name = EXCLUDED.partner_name,
                commission_rate = EXCLUDED.commission_rate,
                source_preset_id = EXCLUDED.source_preset_id,
                is_active = true,
                updated_at = now()
            """
        ).bindparams(preset_id=PARTNER_PRESET_ID)
    )

    # Денежная выплата комиссии — расчёт с уже начисленным обязательством, не база расхода.
    op.execute(
        """
        UPDATE pnl_article_rule AS rule
        SET line_code = NULL,
            in_pnl = false,
            owner_stream = 'iiko',
            note = 'Комиссия начисляется как выручка партнёра из OLAP × договорная ставка; '
                   'кассовая выплата не меняет расход месяца'
        FROM dds_articles AS article
        WHERE article.id = rule.article_id
          AND (article.code = 'komissiya_partneram' OR article.name = 'Комиссия партнерам')
          AND rule.is_active
        """
    )

    op.execute(
        """
        INSERT INTO pnl_source_rule (
            id, line_code, stream, component, selector, sign, sign_policy, priority, note
        )
        SELECT gen_random_uuid(), 'partner_commission', 'iiko', 'main',
               '{"metric": "partner_commission"}'::jsonb,
               1, 'expense_positive', 10,
               'Выручка со скидкой из OLAP «Отчёт о партнёрах» × ставка партнёра'
        WHERE NOT EXISTS (
            SELECT 1
            FROM pnl_source_rule
            WHERE line_code = 'partner_commission'
              AND stream = 'iiko'
              AND component = 'main'
              AND selector = '{"metric": "partner_commission"}'::jsonb
              AND is_active
        )
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DELETE FROM pnl_source_rule
        WHERE line_code = 'partner_commission'
          AND stream = 'iiko'
          AND selector = '{"metric": "partner_commission"}'::jsonb
        """
    )
    op.execute(
        """
        UPDATE pnl_article_rule AS rule
        SET line_code = 'partner_commission',
            in_pnl = true,
            owner_stream = NULL,
            note = NULL
        FROM dds_articles AS article
        WHERE article.id = rule.article_id
          AND (article.code = 'komissiya_partneram' OR article.name = 'Комиссия партнерам')
          AND rule.is_active
        """
    )
    op.drop_index(
        "uq_pnl_partner_commission_fact_slot",
        table_name="pnl_partner_commission_fact",
    )
    op.drop_table("pnl_partner_commission_fact")
    op.drop_index(
        "uq_pnl_partner_commission_rule_key",
        table_name="pnl_partner_commission_rule",
    )
    op.drop_table("pnl_partner_commission_rule")
