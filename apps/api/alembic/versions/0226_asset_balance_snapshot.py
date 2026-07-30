"""Снимок строк баланса по основным средствам на конец закрытого месяца.

Модуль ОС считает амортизацию, но цифры некуда отдать: балансового модуля и ОПиУ в приложении
нет — они ведутся в таблицах, и по методологии реестра «эта цифра переносится вручную пару раз
в год». Эта таблица делает перенос возможным: после закрытия месяца в ней лежат готовые
строки баланса, которые не поедут.

ПОЧЕМУ СНИМОК, А НЕ РАСЧЁТ НА ЛЕТУ. Остаточная стоимость выводится из первоначальной, а та
меняется ЗАДНИМ ЧИСЛОМ тремя путями: ручная коррекция начисления, применённая переоценка по
сообщению менеджера и обычная правка карточки. Каждый зовёт ``recompute_residuals``, который
переписывает остатки по ВСЕЙ истории объекта. Без снимка баланс за июль, уже перенесённый в
отчётность, в сентябре тихо стал бы другим — и расхождение всплыло бы через полгода при сверке.

Тот же приём применён в налогах (``TaxPeriodClose``): замороженный расчёт периода плюс
возможность сравнить его с текущим состоянием. Проверку расхождения делает сервис — он
пересчитывает строку заново и сопоставляет со снимком; разошлось значит прошлое двигали, и
владелец должен об этом узнать, а не обнаружить случайно.

ОДИННАДЦАТАЯ СТРОКА. Строк баланса по ОС одиннадцать, а категорий в справочнике десять:
«Не работающее оборудование» — это СТАТУС карточки, а не категория (решение владельца
2026-05-25, подтверждено 2026-07-30). Поэтому объект со статусом ``not_working`` уходит в свою
строку и ИСКЛЮЧАЕТСЯ из строки своей категории — иначе он посчитался бы дважды. Ключ строки
поэтому текстовый, а не ссылка на категорию: одна из одиннадцати строк категории не имеет.

Revision ID: 0226_asset_balance_snapshot
Revises: 0225_asset_condition_report
Create Date: 2026-07-30
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0226_asset_balance_snapshot"
down_revision = "0225_asset_condition_report"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "asset_balance_snapshot",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        # Первое число закрытого месяца — тот же ключ, что у начисления.
        sa.Column("period_month", sa.Date(), nullable=False),
        # Название строки баланса. Текстом, а не ссылкой на категорию: строка «Не работающее
        # оборудование» собирается по статусу и категории не имеет.
        sa.Column("line_name", sa.String(length=160), nullable=False),
        sa.Column("category_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("asset_count", sa.Integer(), nullable=False),
        sa.Column("initial_cost", sa.Numeric(14, 2), nullable=False),
        # Накоплено ЗА ВСЁ ВРЕМЯ по эту дату — то, что вычитается из первоначальной.
        sa.Column("accumulated", sa.Numeric(14, 2), nullable=False),
        sa.Column("residual", sa.Numeric(14, 2), nullable=False),
        # Начислено ИМЕННО В ЭТОМ МЕСЯЦЕ — строка «УчОС Амортизация» в ОПиУ.
        sa.Column("depreciation", sa.Numeric(14, 2), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["category_id"], ["asset_category.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("period_month", "line_name", name="uq_asset_balance_snapshot_line"),
    )
    op.create_index("ix_asset_balance_snapshot_period", "asset_balance_snapshot", ["period_month"])


def downgrade() -> None:
    op.drop_index("ix_asset_balance_snapshot_period", table_name="asset_balance_snapshot")
    op.drop_table("asset_balance_snapshot")
