"""Сообщение менеджера о состоянии объекта и предложение модели по переоценке.

Решение владельца 2026-07-30: менеджер открывает карточку основного средства и пишет
свободным текстом, что с объектом («сломался компрессор», «поцарапана дверь, холод не
держит»). В фоне модель предлагает новую оценку, владелец подтверждает или отклоняет.

Почему не детерминированный скрипт. Классификацию покупки владелец сознательно оставил на
правилах — там сравнение с порогом и больше ничего. Здесь вход другой: свободный текст, из
которого надо понять, что именно сломалось, насколько это меняет стоимость и надо ли вообще
её менять. Правилами это не берётся.

Почему ОТДЕЛЬНАЯ таблица, а не поле в карточке.

Поля ``note`` и ``review_reason`` в карточке — одиночные и перезаписываемые. Второе сообщение
затёрло бы первое, а история состояния объекта и есть главная ценность: по ней видно, что
техника ломается третий раз за полгода и её пора менять, а не чинить.

Плюс здесь живёт СЛЕД МОДЕЛИ. Предложение ИИ нельзя хранить как факт: это гипотеза, у неё есть
уверенность, обоснование, модель и время. Без следа нельзя ни проверить решение через полгода,
ни понять, почему стоимость упала на 40 тысяч.

Статусы отвечают на вопрос «где сейчас эта запись»:

``pending``   — менеджер написал, модель ещё не смотрела. Джоба берёт такие в работу.
``proposed``  — модель предложила оценку, ждёт решения владельца.
``applied``   — владелец согласился, стоимость карточки изменена.
``dismissed`` — владелец отклонил: объект остался при своей стоимости.
``failed``    — модель не ответила (нет ключа, таймаут, регион). Запись НЕ теряется: сообщение
                менеджера остаётся в истории, а прогон можно повторить.

Уникальность частичным индексом по ``pending``: одна необработанная запись на объект. Иначе
менеджер, нажавший «Сохранить» дважды, получил бы два параллельных вызова модели и два
предложения по одному и тому же поводу.

Revision ID: 0225_asset_condition_report
Revises: 0224_dds_article_asset_link
Create Date: 2026-07-30
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0225_asset_condition_report"
down_revision = "0224_dds_article_asset_link"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "asset_condition_report",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("asset_id", postgresql.UUID(as_uuid=True), nullable=False),
        # Что написал менеджер, дословно. Текст не нормализуем и не режем: он вход модели и
        # одновременно свидетельство для владельца.
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        # Стоимость на момент обращения — чтобы предложение модели можно было прочитать через
        # полгода, когда стоимость уже другая.
        sa.Column("cost_before", sa.Numeric(14, 2), nullable=False),
        sa.Column("proposed_cost", sa.Numeric(14, 2), nullable=True),
        sa.Column("proposed_reason", sa.Text(), nullable=True),
        # Уверенность модели 0..1. Порога автоприменения НЕТ сознательно: стоимость актива
        # меняет только человек, какой бы уверенной модель ни была.
        sa.Column("confidence", sa.Numeric(4, 3), nullable=True),
        sa.Column("model", sa.String(64), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("reported_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("decided_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["asset_id"], ["fixed_asset.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["reported_by_user_id"], ["user.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["decided_by_user_id"], ["user.id"], ondelete="SET NULL"),
        sa.CheckConstraint(
            "status IN ('pending','proposed','applied','dismissed','failed')",
            name="ck_asset_condition_report_status",
        ),
        sa.CheckConstraint(
            "proposed_cost IS NULL OR proposed_cost >= 0",
            name="ck_asset_condition_report_cost_non_negative",
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_asset_condition_report_confidence",
        ),
    )
    op.create_index(
        "ix_asset_condition_report_asset", "asset_condition_report", ["asset_id", "created_at"]
    )
    op.create_index("ix_asset_condition_report_status", "asset_condition_report", ["status"])
    # Одна необработанная запись на объект: двойное нажатие «Сохранить» не должно дать два
    # параллельных вызова модели по одному поводу.
    op.create_index(
        "uq_asset_condition_report_pending",
        "asset_condition_report",
        ["asset_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index("uq_asset_condition_report_pending", table_name="asset_condition_report")
    op.drop_index("ix_asset_condition_report_status", table_name="asset_condition_report")
    op.drop_index("ix_asset_condition_report_asset", table_name="asset_condition_report")
    op.drop_table("asset_condition_report")
