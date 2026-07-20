"""Учёт основных средств: карточки, категории (СПИ), движения, связь с ДДС, амортизация.

Первый этап закрытия контура баланса (постановка владельца 2026-07-19). Методология принята
ранее: порог признания 5 000 ₽, единый линейный помесячный метод, СПИ по категориям с
переопределением в карточке, старт амортизации с месяца ввода в эксплуатацию.

Первоначальная стоимость (``valuation_basis``) имеет ДВА источника и это принципиально:
объекты инвентаризации оцениваются по РЫНКУ на дату инвентаризации (историю платежей для них
не восстанавливаем), новые покупки берут фактическую сумму платежа ДДС.

``asset_cashflow_link`` отдельной таблицей — один платёж может купить несколько объектов, а
объект обрастает расходами (ремонт, затем модернизация). ``kind`` отвечает на вопрос «что именно
ремонтировалось»: без него не собрать историю объекта и не применить правило 15%.

Revision ID: 0201_fixed_assets
Revises: 0200_barter_alloc_link
Create Date: 2026-07-19
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0201_fixed_assets"
down_revision = "0200_barter_alloc_link"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "asset_category",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("useful_life_months", sa.Integer(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("name", name="uq_asset_category_name"),
        sa.CheckConstraint("useful_life_months > 0", name="ck_asset_category_life_positive"),
    )

    op.create_table(
        "fixed_asset",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("inventory_number", sa.String(64), nullable=True),
        sa.Column(
            "category_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("asset_category.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("initial_cost", sa.Numeric(14, 2), nullable=False),
        sa.Column("valuation_basis", sa.String(16), nullable=False, server_default="payment"),
        sa.Column("valued_on", sa.Date(), nullable=True),
        sa.Column("commissioned_on", sa.Date(), nullable=True),
        sa.Column("useful_life_months", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(24), nullable=False, server_default="in_use"),
        sa.Column("location", sa.String(160), nullable=True),
        sa.Column("review_status", sa.String(24), nullable=False, server_default="ok"),
        sa.Column("review_reason", sa.Text(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("initial_cost >= 0", name="ck_fixed_asset_cost_non_negative"),
        sa.CheckConstraint(
            "useful_life_months IS NULL OR useful_life_months > 0",
            name="ck_fixed_asset_life_positive",
        ),
        sa.CheckConstraint(
            "status IN ('in_use','in_storage','not_working','disposed','sold')",
            name="ck_fixed_asset_status",
        ),
        sa.CheckConstraint(
            "valuation_basis IN ('market','payment')", name="ck_fixed_asset_valuation_basis"
        ),
        sa.CheckConstraint(
            "review_status IN ('ok','requires_owner_review')", name="ck_fixed_asset_review_status"
        ),
    )
    op.create_index("ix_fixed_asset_status", "fixed_asset", ["status"])
    op.create_index("ix_fixed_asset_review", "fixed_asset", ["review_status"])
    op.create_index("ix_fixed_asset_category", "fixed_asset", ["category_id"])

    op.create_table(
        "asset_movement",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "asset_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("fixed_asset.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("movement_type", sa.String(24), nullable=False),
        sa.Column("occurred_on", sa.Date(), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_by_user_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "movement_type IN ('commission','transfer','upgrade','writeoff','sale')",
            name="ck_asset_movement_type",
        ),
        sa.CheckConstraint("amount IS NULL OR amount >= 0", name="ck_asset_movement_amount"),
    )
    op.create_index("ix_asset_movement_asset", "asset_movement", ["asset_id", "occurred_on"])

    op.create_table(
        "asset_cashflow_link",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "asset_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("fixed_asset.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "cashflow_transaction_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("cashflow_transactions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "kind IN ('purchase','repair','upgrade','sale')", name="ck_asset_cashflow_link_kind"
        ),
        sa.CheckConstraint("amount >= 0", name="ck_asset_cashflow_link_amount"),
        sa.UniqueConstraint(
            "asset_id", "cashflow_transaction_id", "kind", name="uq_asset_cashflow_link"
        ),
    )
    op.create_index("ix_asset_cashflow_link_tx", "asset_cashflow_link", ["cashflow_transaction_id"])

    op.create_table(
        "depreciation_entry",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "asset_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("fixed_asset.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("period_month", sa.Date(), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("residual_after", sa.Numeric(14, 2), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("asset_id", "period_month", name="uq_depreciation_asset_month"),
        sa.CheckConstraint("amount >= 0", name="ck_depreciation_amount_non_negative"),
    )
    op.create_index("ix_depreciation_period", "depreciation_entry", ["period_month"])

    # Статьи ДДС контура ОС. «Ремонт ОС» уже есть (remont_os); добавляем покупку и модернизацию —
    # именно выбор статьи «Покупка ОС» обязывает завести карточку объекта.
    # activity_type='investing' — как у существующей «Ремонт ОС»: движение внеоборотных активов,
    # не операционный расход периода.
    op.execute(
        """
        INSERT INTO dds_articles
            (id, code, name, movement_type, activity_type, is_active, created_at, updated_at)
        SELECT gen_random_uuid(), v.code, v.name, 'outflow', 'investing', true, now(), now()
        FROM (VALUES
            ('pokupka_os', 'Покупка основных средств'),
            ('modernizaciya_os', 'Модернизация ОС')
        ) AS v(code, name)
        WHERE NOT EXISTS (SELECT 1 FROM dds_articles a WHERE a.code = v.code)
        """
    )


def downgrade() -> None:
    # Удаляем ТОЛЬКО свою статью: 'pokupka_os' («Покупка ОС») существовала в каталоге до этой
    # миграции, upgrade её не создавал (WHERE NOT EXISTS) — снос увёл бы чужую статью вместе с
    # алиасами по каскаду либо упал на FK от проводок ДДС.
    op.execute("DELETE FROM dds_articles WHERE code = 'modernizaciya_os'")
    op.drop_index("ix_depreciation_period", "depreciation_entry")
    op.drop_table("depreciation_entry")
    op.drop_index("ix_asset_cashflow_link_tx", "asset_cashflow_link")
    op.drop_table("asset_cashflow_link")
    op.drop_index("ix_asset_movement_asset", "asset_movement")
    op.drop_table("asset_movement")
    op.drop_index("ix_fixed_asset_category", "fixed_asset")
    op.drop_index("ix_fixed_asset_review", "fixed_asset")
    op.drop_index("ix_fixed_asset_status", "fixed_asset")
    op.drop_table("fixed_asset")
    op.drop_table("asset_category")
