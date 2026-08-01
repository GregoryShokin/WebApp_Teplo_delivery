"""Режим признания «по договору» — четвёртый тип услугового контрагента.

Revision ID: 0237_billing_mode_agreement
Revises: 0236_expense_reversal
Create Date: 2026-08-01

Владелец назвал четыре типа: счёт + УПД, счёт за период, договор с фиксированной суммой и
разовые работы. Первых двух и четвёртого в перечислении не хватало только третьего — договор
существовал отдельной таблицей, но в карточке его нельзя было выбрать как тип, и «где я указываю
3 000 ₽ в месяц» оставалось без ответа.

Сам режим ставки не хранит: сумма живёт в ``counterparty_service_agreement`` (у одного
контрагента их бывает несколько — у АО «АЙКО» две лицензии). Режим отвечает на другой вопрос —
ждать ли от контрагента закрывающие документы; по договору их не ждут.
"""

from __future__ import annotations

from alembic import op

revision = "0237_billing_mode_agreement"
down_revision = "0236_expense_reversal"
branch_labels = None
depends_on = None

CONSTRAINT = "ck_payable_profile_service_billing_mode"
TABLE = "counterparty_payable_profile"


def upgrade() -> None:
    op.drop_constraint(CONSTRAINT, TABLE, type_="check")
    op.create_check_constraint(
        CONSTRAINT,
        TABLE,
        "service_billing_mode is null or service_billing_mode in "
        "('fixed_tariff', 'per_invoice', 'agreement', 'one_off')",
    )


def downgrade() -> None:
    # Значение убираем из данных, иначе старое ограничение не встанет.
    op.execute(
        f"update {TABLE} set service_billing_mode = null where service_billing_mode = 'agreement'"
    )
    op.drop_constraint(CONSTRAINT, TABLE, type_="check")
    op.create_check_constraint(
        CONSTRAINT,
        TABLE,
        "service_billing_mode is null or service_billing_mode in "
        "('fixed_tariff', 'per_invoice', 'one_off')",
    )
