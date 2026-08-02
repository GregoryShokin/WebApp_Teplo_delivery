"""Отпуск помнит, КАКАЯ ведомость его оплатила — иначе он выплачивается дважды.

Отпускные выдаются одним траншем по совпадению ``vacation_period.payout_date`` с
``payroll_period.payroll_date``. Сама запись отпуска до сих пор не хранила ни следа того,
что деньги уже ушли: статус ``paid`` сбрасывался обратно в ``planned`` при любой правке
(``update_vacation_period``), а валидация новой даты выплаты проверяла только НОВУЮ дату.
Достаточно было перенести ``payout_date`` на другой незакрытый вторник — и те же 10 000 ₽
начислялись вторым разом, попадая заодно в окно аванса следующей недели.

Статуса для этого мало по существу: ``paid`` ставится на РАСЧЁТЕ ведомости
(``run_payroll``), а не на финализации, то есть он говорит «попало в расчёт», а не «деньги
ушли». Ссылка на конкретный период отвечает ровно на нужный вопрос — какая ведомость несёт
этот транш — и позволяет спросить у неё, финализирована ли она.

БЭКФИЛЛ по точному совпадению ``payout_date`` с ``payroll_date`` ФИНАЛИЗИРОВАННОЙ недельной
ведомости. Без него вся уже накопленная история осталась бы с прежней дырой: заморозка
работала бы только для отпусков, рассчитанных новым кодом.

Совпадение однозначно: ``payout_date`` может быть только вторником (``_validate_payout_date``),
а вторник — это ``payroll_date`` ровно одной недельной ведомости. Направление безопасное:
привязка морозит запись, а не разрешает лишнюю выплату; ошибочно привязанный отпуск чинится
дефинализацией, тогда как пропущенный — это второй транш.

Revision ID: 0245_vacation_paid_by_run
Revises: 0244_asset_disposal
Create Date: 2026-08-02
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0245_vacation_paid_by_run"
down_revision = "0244_asset_disposal"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "vacation_period",
        sa.Column("paid_period_id", UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "vacation_period",
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
    )
    # SET NULL, а не CASCADE: удаление ведомости не должно уносить сам отпуск.
    op.create_foreign_key(
        # Имя — по naming_convention из db/base.py, иначе autogenerate видит расхождение.
        "fk_vacation_period_paid_period_id_payroll_period",
        "vacation_period",
        "payroll_period",
        ["paid_period_id"],
        ["id"],
        ondelete="SET NULL",
    )
    # Дефинализация ищет отпуска своего периода — по этому индексу.
    op.create_index(
        "ix_vacation_period_paid_period",
        "vacation_period",
        ["paid_period_id"],
    )
    # Бэкфилл: чья ведомость платила исторический отпуск. Только финализированные недельные
    # периоды — открытая ведомость ничего ещё не выплатила и морозить отпуск не должна.
    op.execute(
        """
        UPDATE vacation_period AS v
        SET paid_period_id = p.id,
            paid_at = p.finalized_at
        FROM payroll_period AS p
        WHERE p.period_type = 'week'
          AND p.status = 'finalized'
          AND p.payroll_date = v.payout_date
          AND v.payout_date IS NOT NULL
          AND v.status <> 'cancelled'
          AND v.paid_period_id IS NULL
        """
    )


def downgrade() -> None:
    op.drop_index("ix_vacation_period_paid_period", table_name="vacation_period")
    op.drop_constraint(
        "fk_vacation_period_paid_period_id_payroll_period",
        "vacation_period",
        type_="foreignkey",
    )
    op.drop_column("vacation_period", "paid_at")
    op.drop_column("vacation_period", "paid_period_id")
