"""Слияние линий миграций: налоговый контур + основная ветка.

Модуль «Налоги» рос параллельно main от общего предка ``0207_iiko_payment_verify``: у нас
цепочка 0208_taxes → … → 0215_tax_unique_slots, у main своя — 0207_fund_forfeit_settlement →
… → 0214_dds_article_lease_bound (на ней стоит прод). После ребейза в дереве ДВЕ головы, и
``alembic upgrade head`` без слияния упадёт с «Multiple head revisions».

Миграция пустая по DDL — она только связывает линии в одну. Приём в проекте уже применялся:
так же собран ``0208_merge_fund_iiko``.

Revision ID: 0216_merge_taxes_into_main
Revises: 0215_tax_unique_slots, 0214_dds_article_lease_bound
"""

from __future__ import annotations

revision = "0216_merge_taxes_into_main"
down_revision = ("0215_tax_unique_slots", "0214_dds_article_lease_bound")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Слияние линий — схему не трогает."""


def downgrade() -> None:
    """Разделение линий обратно — схему не трогает."""
