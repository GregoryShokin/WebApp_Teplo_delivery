"""seed counterparty ledger categories

Леджер-вкладки страницы «Контрагенты». Идемпотентно по коду категории —
повторный прогон обновляет имя/порядок, но не плодит дубли. Назначение
контрагентов в категории делает менеджер в карточке (или go-live bootstrap).

Revision ID: 0094_counterparty_ledger_seed
Revises: 0093_counterparty_permissions
Create Date: 2026-06-11
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0094_counterparty_ledger_seed"
down_revision = "0093_counterparty_permissions"
branch_labels = None
depends_on = None

CATEGORIES = (
    ("products_packaging", "Продукты и упаковка", 10),
    ("marketing", "Маркетинг и реклама", 20),
    ("automation", "Сервисы и автоматизация", 30),
    ("security", "Охрана и сигнализация", 40),
    ("telecom", "Связь и интернет", 50),
    ("utilities", "Коммунальные услуги", 60),
    ("taxes", "Налоги и сборы", 70),
    ("other", "Прочее", 80),
)


def upgrade() -> None:
    table = sa.table(
        "counterparty_ledger_category",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("code", sa.String()),
        sa.column("name", sa.String()),
        sa.column("sort_order", sa.Integer()),
    )
    rows = [
        {"id": uuid.uuid4(), "code": code, "name": name, "sort_order": sort_order}
        for code, name, sort_order in CATEGORIES
    ]
    insert_stmt = postgresql.insert(table).values(rows)
    op.get_bind().execute(
        insert_stmt.on_conflict_do_update(
            index_elements=["code"],
            set_={
                "name": insert_stmt.excluded.name,
                "sort_order": insert_stmt.excluded.sort_order,
            },
        )
    )


def downgrade() -> None:
    codes = ", ".join("'" + code + "'" for code, _name, _order in CATEGORIES)
    op.execute(f"delete from counterparty_ledger_category where code in ({codes})")
