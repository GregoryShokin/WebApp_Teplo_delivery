"""Коммунальные услуги помещения: поток «помещение × ресурс» и его связь с приёмкой.

ЗАЧЕМ. Вода, газ и электричество по торговой точке шли мимо учёта обязательств: счета
ресурсников выставлены на арендодателя-физлицо, бизнес возмещает ему затраты наличными и
переводами, а в системе это оставалось разрозненными платежами под статьёй аренды. Расход
попадал в тот месяц, когда заплатили, — а не в тот, когда потребили; долг перед арендодателем
не считался вовсе.

ЧТО ХРАНИТ ПОТОК. Ответы, которых в самой квитанции нет и которые различаются в пределах одной
точки: помещение (иначе расход осядет «без помещения», и прибыль точки посчитается без
коммуналки), получателя денег и статью расхода. По решению владельца от 02.08.2026 вода и газ
возмещаются одному арендодателю, электричество — другому, поэтому получатель хранится на потоке,
а не выводится из документа: в квитанции стоят реквизиты ресурсника, а платим мы не ему.

ПРИЁМКА СВОЕЙ ТАБЛИЦЫ НЕ ПОЛУЧАЕТ. Платёжка приходит на «Страницу на оплату» третьим источником
рядом с почтой и ЭДО, в общий журнал ``email_invoice_intake`` — оттуда и колонка
``utility_account_id``. Отдельный экран с собственным хранилищем был первой версией этой ветки и
оказался ошибкой: документ становился виден только тому, кто знает про специальную страницу, а
оплатить его из очереди оплат было нечем.

СОБСТВЕННОГО ДОЛГА ТАБЛИЦЫ НЕТ. Разобранная платёжка становится обычными ``supplier_invoice`` —
так долг попадает в готовые витрины ДЗ/КЗ и в признание расхода, ровно как у аренды. Новый
источник ``utility`` нужен, чтобы отличать эти документы от первички на ИП: расход по ним идёт
в управленческий P&L, но не в налоговую базу.

Revision ID: 0246_utility_charges
Revises: 0245_vacation_paid_by_run
Create Date: 2026-08-02
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0246_utility_charges"
down_revision = "0245_vacation_paid_by_run"
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

    # Связь журнала «Страницы на оплату» с коммунальным потоком. Колонка живёт в общей таблице
    # приёмки, потому что коммунальная платёжка приходит туда же, куда счета из почты и ЭДО, —
    # и именно из потока берутся ответы, которых в самой квитанции нет: помещение (иначе расход
    # осядет «без помещения»), получатель денег и статья расхода.
    op.add_column(
        "email_invoice_intake",
        sa.Column("utility_account_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_email_invoice_intake_utility_account",
        "email_invoice_intake",
        "utility_account",
        ["utility_account_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_email_invoice_intake_utility_account",
        "email_invoice_intake",
        ["utility_account_id"],
    )

    _ensure_utilities_article()


def downgrade() -> None:
    op.drop_index("ix_email_invoice_intake_utility_account", table_name="email_invoice_intake")
    op.drop_constraint(
        "fk_email_invoice_intake_utility_account", "email_invoice_intake", type_="foreignkey"
    )
    op.drop_column("email_invoice_intake", "utility_account_id")
    op.drop_index("ix_utility_account_counterparty", table_name="utility_account")
    op.drop_index("ix_utility_account_location", table_name="utility_account")
    op.drop_table("utility_account")
    # Статью «Коммунальные платежи» НЕ удаляем: на проде она существовала и до этой миграции,
    # а на ней могли уже висеть проводки ДДС. Повторный upgrade её просто не задвоит — вставка
    # идёт только когда статьи с таким именем нет.
    #
    # Значение enum не удаляем тоже: PostgreSQL этого не умеет без пересоздания типа, а строки
    # с source='utility' могли остаться.
