"""Плановая отправка помнит согласие «без реквизитов — на карту ИП».

``email_invoice_intake.scheduled_pays_via_safe`` — оператор подтвердил в окне отправки, что у
получателя банковских реквизитов нет и платёж выписывается на карту ИП → Сейф.

Зачем колонка, а не параметр запроса. Немедленную отправку делает человек и согласие живёт
ровно в этом запросе. Плановую отправку делает джоба ``send_scheduled_payments`` — через день
или неделю, когда спросить некого. Без сохранённого согласия плановый счёт получателя без
реквизитов вечно висел бы в «пропущено», ожидая реквизитов, которых не будет.

Согласие разовое: снимается вместе с плановой датой, когда платёж ушёл или план отменили.

Revision ID: 0256_intake_pays_via_safe
Revises: 0250_allocation_origin
Create Date: 2026-08-04
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0256_intake_pays_via_safe"
down_revision = "0250_allocation_origin"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "email_invoice_intake",
        sa.Column(
            "scheduled_pays_via_safe",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("email_invoice_intake", "scheduled_pays_via_safe")
