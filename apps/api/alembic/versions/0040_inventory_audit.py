"""add inventory audit penalties

Revision ID: 0040_inventory_audit
Revises: 0039_vacation_periods
Create Date: 2026-06-02
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0040_inventory_audit"
down_revision = "0039_vacation_periods"
branch_labels = None
depends_on = None


SEED_POSITIONS = [
    ("beacon", "Бекон", "chefs"),
    ("hunters_sausages", "Колбаски охот.", "chefs"),
    ("masago", "Масаго", "chefs"),
    ("potato_wedges", "Дольки", "chefs"),
    ("french_fries", "Фри", "chefs"),
    ("shrimp", "Креветки", "chefs"),
    ("chicken", "Курица", "chefs"),
    ("salmon_frozen", "Лосось с/м", "chefs"),
    ("salmon_cold_smoked", "Лосось х/к", "chefs"),
    ("mayo", "Майонез", "chefs"),
    ("mozzarella", "Моцарелла", "chefs"),
    ("nuggets", "Наггетсы", "chefs"),
    ("cucumbers", "Огурцы", "chefs"),
    ("napa_cabbage", "Пекинская капуста", "chefs"),
    ("tomatoes", "Помидоры", "chefs"),
    ("cream_cheese", "Творожный сыр", "chefs"),
    ("snow_crab", "Снежный краб", "chefs"),
    ("cheddar", "Чеддер", "chefs"),
    ("fry_fat", "Фрит Жир", "chefs"),
    ("smoked_chicken", "Копченая курица", "chefs"),
    ("beef_mince", "Говяжий фарш", "chefs"),
    ("pork_beef_mince", "Свин-гов фарш", "chefs"),
    ("escolar", "Масляная рыба", "chefs"),
    ("mussels_cocktail", "Мидии кокт.", "chefs"),
    ("parmesan", "Пармезан", "chefs"),
    ("tilapia", "Тилапия", "chefs"),
    ("eel", "Угорь", "chefs"),
    ("tuna_cold_smoked", "Тунец хк", "chefs"),
    ("flour", "Мука", "chefs"),
    ("rice", "Рис", "chefs"),
    ("milk", "Молоко", "chefs"),
    ("pickled_cucumbers", "Марин.огурцы", "chefs"),
    ("avocado", "Авокадо", "chefs"),
    ("bell_pepper", "Перец болгарский", "chefs"),
    ("ham", "Ветчина", "chefs"),
    ("eggs", "Яйца", "chefs"),
    ("kombu", "Комбу", "chefs"),
    ("pork", "Свинина", "chefs"),
    ("soy_sauce", "Соевый соус", "soy"),
    ("wasabi", "Васаби", "admins"),
    ("ginger", "Имбирь", "admins"),
]


def upgrade() -> None:
    op.create_table(
        "inventory_audit_position",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("allocation_group", sa.String(16), nullable=False),
        sa.Column("iiko_product_guid", sa.String(64), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("100")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "allocation_group IN ('chefs','soy','admins')",
            name="ck_inventory_audit_position_group",
        ),
        sa.UniqueConstraint("code", name="uq_inventory_audit_position_code"),
    )
    op.create_index(
        "ix_inventory_audit_position_active",
        "inventory_audit_position",
        ["is_active", "sort_order"],
    )
    op.create_index(
        "ix_inventory_audit_position_guid",
        "inventory_audit_position",
        ["iiko_product_guid"],
    )

    position_table = sa.table(
        "inventory_audit_position",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("code", sa.String(64)),
        sa.column("display_name", sa.String(200)),
        sa.column("allocation_group", sa.String(16)),
        sa.column("iiko_product_guid", sa.String(64)),
        sa.column("is_active", sa.Boolean()),
        sa.column("sort_order", sa.Integer()),
    )
    op.bulk_insert(
        position_table,
        [
            {
                "id": uuid.uuid5(
                    uuid.UUID("4a3ffcff-9f82-4b6d-a967-154ba542db72"),
                    code,
                ),
                "code": code,
                "display_name": display_name,
                "allocation_group": allocation_group,
                "iiko_product_guid": None,
                "is_active": True,
                "sort_order": index * 10,
            }
            for index, (code, display_name, allocation_group) in enumerate(SEED_POSITIONS, start=1)
        ],
    )

    op.create_table(
        "inventory_audit",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("previous_audit_date", sa.Date(), nullable=True),
        sa.Column("iiko_document_id", sa.String(64), nullable=True),
        sa.Column("iiko_document_num", sa.String(32), nullable=True),
        sa.Column("source", sa.String(16), nullable=False, server_default=sa.text("'iiko'")),
        sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'draft'")),
        sa.Column(
            "total_shortage_amount",
            sa.Numeric(14, 2),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "total_penalty_amount",
            sa.Numeric(14, 2),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("computation_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("applied_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["user.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["applied_by_user_id"], ["user.id"], ondelete="SET NULL"),
        sa.CheckConstraint("source IN ('iiko','manual')", name="ck_inventory_audit_source"),
        sa.CheckConstraint(
            "status IN ('draft','applied','cancelled')",
            name="ck_inventory_audit_status",
        ),
        sa.UniqueConstraint("business_date", name="uq_inventory_audit_business_date"),
    )
    op.create_index("ix_inventory_audit_date", "inventory_audit", ["business_date"])

    op.create_table(
        "inventory_audit_item",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("audit_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("position_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("iiko_product_guid", sa.String(64), nullable=True),
        sa.Column("product_name_snapshot", sa.String(200), nullable=False),
        sa.Column("shortage_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["audit_id"], ["inventory_audit.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["position_id"],
            ["inventory_audit_position.id"],
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "shortage_amount >= 0",
            name="ck_inventory_audit_item_amount_non_negative",
        ),
    )
    op.create_index("ix_inventory_audit_item_audit", "inventory_audit_item", ["audit_id"])
    op.create_index("ix_inventory_audit_item_position", "inventory_audit_item", ["position_id"])

    op.execute(
        """
        INSERT INTO payroll_adjustment_category
            (id, type, code, display_name, is_active, sort_order, created_at, updated_at)
        VALUES
            ('58f965d4-9739-4abc-af91-5c01dd0f33f8',
             'penalty', 'inventory_shortage', 'Недостача по ревизии', true, 50, now(), now())
        ON CONFLICT (code) DO NOTHING
        """
    )

    op.execute(
        sa.text(
            """
            insert into app_setting (
                id,
                key,
                value,
                value_type,
                category,
                display_name,
                description,
                widget_type,
                widget_options,
                unit,
                updated_at
            )
            values
                (
                    '78220317-f30a-4837-a178-5cc77f5ee8d3',
                    'inventory.threshold_zero',
                    '5000'::jsonb,
                    'number',
                    'inventory',
                    'Порог без штрафа',
                    'Сумма недостачи по группе, ниже которой штраф не начисляется.',
                    'number',
                    null,
                    '₽',
                    now()
                ),
                (
                    'afbcdf74-ebfb-451e-a571-2a7f030376c2',
                    'inventory.threshold_50pct',
                    '10000'::jsonb,
                    'number',
                    'inventory',
                    'Порог 50%',
                    'Сумма недостачи по группе, с которой применяется ставка 50%.',
                    'number',
                    null,
                    '₽',
                    now()
                ),
                (
                    'd1709b43-713a-44b4-8dac-c02e95980d8e',
                    'inventory.rate_tier_40pct',
                    '0.40'::jsonb,
                    'number',
                    'inventory',
                    'Ставка среднего порога',
                    'Процент штрафа для группы между порогами 5 000 и 10 000.',
                    'percent',
                    null,
                    null,
                    now()
                ),
                (
                    '22481f1e-a6f1-436b-8ea0-941a83906c08',
                    'inventory.rate_tier_50pct',
                    '0.50'::jsonb,
                    'number',
                    'inventory',
                    'Ставка верхнего порога',
                    'Процент штрафа для группы от 10 000.',
                    'percent',
                    null,
                    null,
                    now()
                )
            on conflict (key) do nothing
            """
        )
    )


def downgrade() -> None:
    op.execute(
        "delete from app_setting where key in "
        "('inventory.threshold_zero', 'inventory.threshold_50pct', "
        "'inventory.rate_tier_40pct', 'inventory.rate_tier_50pct', 'iiko.products_cache')"
    )
    op.execute("delete from payroll_adjustment_category where code = 'inventory_shortage'")
    op.drop_index("ix_inventory_audit_item_position", table_name="inventory_audit_item")
    op.drop_index("ix_inventory_audit_item_audit", table_name="inventory_audit_item")
    op.drop_table("inventory_audit_item")
    op.drop_index("ix_inventory_audit_date", table_name="inventory_audit")
    op.drop_table("inventory_audit")
    op.drop_index("ix_inventory_audit_position_guid", table_name="inventory_audit_position")
    op.drop_index("ix_inventory_audit_position_active", table_name="inventory_audit_position")
    op.drop_table("inventory_audit_position")
