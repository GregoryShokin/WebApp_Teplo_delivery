"""Два легаси-тумблера карточки выводятся из типа контрагента.

Revision ID: 0238_retire_legacy_toggles
Revises: 0237_billing_mode_agreement
Create Date: 2026-08-01

Схемы не меняет — только данные, чтобы существующие карточки соответствовали новой логике.

«Списания из банка пополняют баланс» давно ничего не гейтит (правило 1 канона универсально,
см. докстринг ``ensure_prepayment_from_bank_transaction``) — но включённый флаг описывал
модель Манго, а это ровно режим «счёт + УПД»: сумму расхода приносит закрывающий документ.
Переносим смысл в разметку.

«Требовать период оказания услуг» теперь выводится из типа: «счёт за период» без периода не
работает по определению, остальным типам требование вредно или бессмысленно. Приводим флаг
уже размеченных контрагентов в соответствие.
"""

from __future__ import annotations

from alembic import op

revision = "0238_retire_legacy_toggles"
down_revision = "0237_billing_mode_agreement"
branch_labels = None
depends_on = None

TABLE = "counterparty_payable_profile"


def upgrade() -> None:
    op.execute(
        f"update {TABLE} set service_billing_mode = 'per_invoice' "
        f"where bank_payments_create_prepayment and service_billing_mode is null"
    )
    op.execute(
        f"update {TABLE} set service_period_required = (service_billing_mode = 'fixed_tariff') "
        f"where service_billing_mode is not null"
    )


def downgrade() -> None:
    # Дата-фикс: старые значения флагов не восстановить, и не нужно — старый код их читал
    # так же, просто не выводил из типа.
    pass
