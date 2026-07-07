"""Каталог пресетов «Внесение в кассу» (``kassa_payin_preset``) + стартовые пресеты.

Именованные пресеты внесения: человеческое имя («Деньги за масло», «Приход от
„В гостях у Алисы“») → статья ДДС + фиксированный контрагент + шаблон комментария. Кассир выбирает
имя, бэкенд проводит приход на ТК Черникова (``source_kind='kassa_payin'``), не показывая
кассиру статью ДДС. Каталог курирует владелец в «Настройках → Касса».

Стартовые пресеты (``mechanism='income'`` — голый приход по статье):
- «Деньги за масло» → «Прочие поступления»;
- «Взнос собственника» → «Поступление денег от собственников»;
- «Приход от „В гостях у Алисы“» → «Поступление денег с торг. точек», контрагент —
  best-effort по имени (если партнёр заведён в реестре), иначе ``NULL``.

Сид-вставки идут через ``INSERT ... SELECT`` от ``dds_articles`` по имени: если статьи в
каталоге нет — пресет просто не создаётся (без падения). ``NOT EXISTS`` — защита от
повторного прогона.

Revision ID: 0163_kassa_payin_presets
Revises: 0162_employee_dismissing
Create Date: 2026-07-07
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0163_kassa_payin_presets"
down_revision = "0162_employee_dismissing"
branch_labels = None
depends_on = None


# (имя пресета, имя статьи ДДС, шаблон комментария, привязать ли контрагента-партнёра, sort)
SEED_PRESETS: tuple[tuple[str, str, str, bool, int], ...] = (
    ("Деньги за масло", "Прочие поступления", "Плата за масло", False, 10),
    ("Взнос собственника", "Поступление денег от собственников", "Взнос собственника", False, 20),
    (
        "Приход от «В гостях у Алисы»",
        "Поступление денег с торг. точек",
        "Наличные от «В гостях у Алисы»",
        True,
        30,
    ),
)

# Партнёр «В гостях у Алисы» может быть не заведён контрагентом (в iiko это счёт-аккаунт),
# поэтому привязку контрагента делаем best-effort по имени; иначе counterparty_id = NULL.
_ALISA_COUNTERPARTY_SQL = (
    "(SELECT id FROM counterparty "
    "WHERE name ILIKE '%алис%' OR name ILIKE '%гостев%' "
    "ORDER BY name LIMIT 1)"
)


def upgrade() -> None:
    op.create_table(
        "kassa_payin_preset",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "mechanism", sa.String(length=32), nullable=False, server_default="income"
        ),
        sa.Column("article_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("counterparty_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("comment_template", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("mechanism in ('income')", name="ck_kassa_payin_preset_mechanism"),
        sa.ForeignKeyConstraint(["article_id"], ["dds_articles.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["counterparty_id"], ["counterparty.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["user.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_kassa_payin_preset_active_sort",
        "kassa_payin_preset",
        ["is_active", "sort_order"],
    )

    for name, article_name, comment_template, link_partner, sort_order in SEED_PRESETS:
        counterparty_expr = _ALISA_COUNTERPARTY_SQL if link_partner else "NULL"
        op.execute(
            sa.text(
                f"""
                INSERT INTO kassa_payin_preset
                    (id, name, mechanism, article_id, counterparty_id,
                     comment_template, is_active, sort_order)
                SELECT gen_random_uuid(), :name, 'income', a.id, {counterparty_expr},
                       :comment_template, true, :sort_order
                FROM dds_articles a
                WHERE a.name = :article_name
                  AND NOT EXISTS (
                      SELECT 1 FROM kassa_payin_preset p WHERE p.name = :name
                  )
                """
            ).bindparams(
                name=name,
                comment_template=comment_template,
                sort_order=sort_order,
                article_name=article_name,
            )
        )


def downgrade() -> None:
    op.drop_index("ix_kassa_payin_preset_active_sort", table_name="kassa_payin_preset")
    op.drop_table("kassa_payin_preset")
