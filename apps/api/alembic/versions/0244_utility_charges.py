"""Коммунальные услуги помещения: поток «помещение × ресурс» и приёмка платёжек.

ЗАЧЕМ. Вода, газ и электричество по торговой точке шли мимо учёта обязательств: счета
ресурсников выставлены на арендодателя-физлицо, бизнес возмещает ему затраты наличными и
переводами, а в системе это оставалось разрозненными платежами под статьёй аренды. Расход
попадал в тот месяц, когда заплатили, — а не в тот, когда потребили; долг перед арендодателем
не считался вовсе.

ПОЧЕМУ НЕ ХВАТИЛО СУЩЕСТВУЮЩИХ МЕХАНИЗМОВ. Аренда и договор услуги начисляют долг сами, потому
что месячная сумма известна заранее. У коммуналки она известна только из документа: счётчик.
Поэтому обязательство здесь рождается не по расписанию, а из принесённой бумажки — и таблица
приёмки нужна, чтобы бумажка сначала попала человеку на проверку, а не сразу в кредиторку.

СОБСТВЕННОГО ДОЛГА ТАБЛИЦЫ НЕТ. Проведённая платёжка становится обычным ``supplier_invoice``
(``doc_kind='closing'``, ``source='utility'``) — так долг попадает в готовые витрины ДЗ/КЗ и в
признание расхода, ровно как у аренды. Новый источник ``utility`` нужен, чтобы отличать эти
документы от первички на ИП: расход по ним идёт в управленческий P&L, но не в налоговую базу.

Revision ID: 0244_utility_charges
Revises: 0243_accrual_location
Create Date: 2026-08-02
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0244_utility_charges"
down_revision = "0243_accrual_location"
branch_labels = None
depends_on = None

UTILITIES_ARTICLE_NAME = "Коммунальные платежи"


def _ensure_utilities_article() -> None:
    """Завести статью «Коммунальные платежи», если её нет.

    На проде она существует, но её завёл владелец руками через интерфейс — в каталоге
    миграции 0114 её нет, и 0213 только проставляет ей признак помещения ПО ИМЕНИ. Из-за
    этого на любой чистой базе (тесты, новый стенд, гринфилд) статьи не существует вовсе, и
    модуль коммунальных услуг там не завести: поток без статьи создать нельзя.

    Ищем по ИМЕНИ, а не по коду: прод-код сгенерён транслитом со случайным хвостом
    (``kommunalnye_platezhi_0b0b4c``), совпасть с ним нельзя, а завести вторую статью с тем же
    именем — значит расколоть расход надвое.
    """
    conn = op.get_bind()
    existing = conn.execute(
        text("SELECT id FROM dds_articles WHERE name = :name"),
        {"name": UTILITIES_ARTICLE_NAME},
    ).first()
    if existing is not None:
        return
    conn.execute(
        text(
            "INSERT INTO dds_articles (id, code, name, movement_type, activity_type,"
            " parent_id, is_active, description, location_required)"
            " VALUES (:id, :code, :name, 'outflow', 'operating', NULL, true, :description, true)"
        ),
        {
            "id": str(uuid.uuid4()),
            "code": "kommunalnye_platezhi",
            "name": UTILITIES_ARTICLE_NAME,
            "description": (
                "Вода, газ, электричество по торговым точкам — возмещение арендодателю по его "
                "расчёту. Счёт ресурсника выставлен на него, первички на ИП нет."
            ),
        },
    )


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE вне транзакции: в PostgreSQL новое значение enum нельзя
    # использовать в той же транзакции, где оно добавлено, а alembic держит миграцию в одной.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE counterparty_invoice_source ADD VALUE IF NOT EXISTS 'utility'")

    op.create_table(
        "utility_account",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("location_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("counterparty_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("dds_article_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("expected_day", sa.Integer(), nullable=True),
        sa.Column("started_on", sa.Date(), nullable=False),
        sa.Column("ended_on", sa.Date(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "kind in ('water', 'gas', 'electricity')", name="ck_utility_account_kind"
        ),
        sa.CheckConstraint(
            "expected_day IS NULL OR expected_day BETWEEN 1 AND 28",
            name="ck_utility_account_expected_day",
        ),
        # RESTRICT, а не CASCADE: удаление помещения или контрагента не должно уносить настройку,
        # по которой считался долг.
        sa.ForeignKeyConstraint(["location_id"], ["location.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["counterparty_id"], ["counterparty.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["dds_article_id"], ["dds_articles.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("location_id", "kind", name="uq_utility_account_location_kind"),
    )
    op.create_index("ix_utility_account_location", "utility_account", ["location_id"])
    op.create_index("ix_utility_account_counterparty", "utility_account", ["counterparty_id"])

    op.create_table(
        "utility_intake",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="new"),
        sa.Column("filename", sa.String(length=255), nullable=True),
        sa.Column("mime", sa.String(length=128), nullable=True),
        sa.Column("attachment_sha256", sa.String(length=64), nullable=True),
        sa.Column("attachment_size", sa.Integer(), nullable=True),
        sa.Column("content", sa.LargeBinary(), nullable=True),
        sa.Column("period_start", sa.Date(), nullable=True),
        sa.Column("period_end", sa.Date(), nullable=True),
        sa.Column("amount", sa.Numeric(14, 2), nullable=True),
        sa.Column("document_number", sa.String(length=64), nullable=True),
        sa.Column("document_date", sa.Date(), nullable=True),
        sa.Column("recognition", postgresql.JSONB(), nullable=True),
        sa.Column("invoice_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("uploaded_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "status in ('new', 'needs_review', 'ready', 'promoted', 'rejected')",
            name="ck_utility_intake_status",
        ),
        sa.CheckConstraint(
            "period_end IS NULL OR period_start IS NULL OR period_end >= period_start",
            name="ck_utility_intake_period",
        ),
        sa.CheckConstraint("amount IS NULL OR amount > 0", name="ck_utility_intake_amount"),
        # RESTRICT на профиль: пока по нему есть приёмки, настройку не удалить — иначе исчезнет
        # ответ на вопрос «за что был этот долг».
        sa.ForeignKeyConstraint(["account_id"], ["utility_account.id"], ondelete="RESTRICT"),
        # SET NULL на документ: аннулирование документа не должно стирать историю приёмки.
        sa.ForeignKeyConstraint(["invoice_id"], ["supplier_invoice.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["uploaded_by_user_id"], ["user.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    # Дедуп по содержимому — только там, где содержимое есть: одну и ту же квитанцию нельзя
    # завести дважды, а ручных строк без файла у месяца может быть несколько.
    op.create_index(
        "uq_utility_intake_sha",
        "utility_intake",
        ["attachment_sha256"],
        unique=True,
        postgresql_where=sa.text("attachment_sha256 IS NOT NULL"),
    )
    op.create_index("ix_utility_intake_status", "utility_intake", ["status"])
    op.create_index(
        "ix_utility_intake_account_period", "utility_intake", ["account_id", "period_start"]
    )
    op.create_index("ix_utility_intake_invoice", "utility_intake", ["invoice_id"])

    _ensure_utilities_article()


def downgrade() -> None:
    op.drop_index("ix_utility_intake_invoice", table_name="utility_intake")
    op.drop_index("ix_utility_intake_account_period", table_name="utility_intake")
    op.drop_index("ix_utility_intake_status", table_name="utility_intake")
    op.drop_index("uq_utility_intake_sha", table_name="utility_intake")
    op.drop_table("utility_intake")
    op.drop_index("ix_utility_account_counterparty", table_name="utility_account")
    op.drop_index("ix_utility_account_location", table_name="utility_account")
    op.drop_table("utility_account")
    # Статью «Коммунальные платежи» НЕ удаляем: на проде она существовала и до этой миграции,
    # а на ней могли уже висеть проводки ДДС. Повторный upgrade её просто не задвоит — вставка
    # идёт только когда статьи с таким именем нет.
    #
    # Значение enum не удаляем тоже: PostgreSQL этого не умеет без пересоздания типа, а строки
    # с source='utility' могли остаться.
