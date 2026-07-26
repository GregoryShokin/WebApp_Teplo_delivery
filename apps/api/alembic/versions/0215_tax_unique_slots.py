"""Защита от двойного клика: частичные уникальные индексы налоговых слотов.

Писатели работают по схеме «select слот → insert»: два параллельных запроса (двойной
клик, две вкладки, ручная кнопка одновременно с фоновым джобом) оба видят пусто и оба
пишут. Цена дубля — двойной счёт: лишнее плановое обязательство в долге, задвоенный
разнос в вычете УСН, вторая платёжка в банке. Единственность слота гарантирует БД.

``NULLS NOT DISTINCT`` (PostgreSQL 15+) обязателен: ``for_period``/``for_year`` бывают
NULL, и по умолчанию PG считает NULL ≠ NULL — индекс бы такие слоты не защитил.

ПЕРЕД ДЕПЛОЕМ на окружение с данными убедиться, что дублей нет (иначе CREATE INDEX
упадёт — это лучше молчаливого дубля):

    select for_year, kind, for_period, count(*) from tax_payment
    where status = 'planned' group by 1,2,3 having count(*) > 1;

Revision ID: 0215_tax_unique_slots
Revises: 0214_official_children
"""

from __future__ import annotations

from alembic import op

revision = "0215_tax_unique_slots"
down_revision = "0214_official_children"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Слот планового обязательства (promote_intake): одна платёжка бухгалтера —
    # одно обязательство на (год, вид, период).
    op.execute(
        "create unique index uq_tax_payment_planned_slot on tax_payment "
        "(for_year, kind, for_period) nulls not distinct where status = 'planned'"
    )
    # Слот разноса ЕНП из оборотки (rebuild_payroll_enp_split): по одной строке
    # взносов и НДФЛ на месяц. Слот идентифицируется МЕСЯЦЕМ — строки без периода
    # (сид, исторический ручной ввод) слотом не являются и не ограничиваются.
    op.execute(
        "create unique index uq_tax_payment_split_slot on tax_payment "
        "(for_year, for_period, kind) "
        "where source_kind = 'tax_notice' and for_period is not null "
        "and kind in ('contrib_employees', 'ndfl')"
    )
    # Факт-слой (bank_facts): одна банковская операция — максимум одна строка каждого
    # вида. Две строки на операцию законны только как разнос ЕНП (взносы + НДФЛ).
    op.execute(
        "create unique index uq_tax_payment_bank_operation_kind on tax_payment "
        "(bank_operation_id, kind) where bank_operation_id is not null"
    )
    # Один активный черновик платёжки на обязательство (create_tax_payment_draft).
    op.execute(
        "create unique index uq_tax_bank_draft_active_slot on tax_bank_draft "
        "(tax_kind, for_year, for_period) nulls not distinct "
        "where status in ('ready_to_send', 'in_bank')"
    )


def downgrade() -> None:
    op.execute("drop index if exists uq_tax_bank_draft_active_slot")
    op.execute("drop index if exists uq_tax_payment_bank_operation_kind")
    op.execute("drop index if exists uq_tax_payment_split_slot")
    op.execute("drop index if exists uq_tax_payment_planned_slot")
