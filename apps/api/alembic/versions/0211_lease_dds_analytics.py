"""Аналитика по помещению в ДДС: статья требует помещение, платёж несёт аренду.

Владелец: «подвязываем к помещению статью, а статья требует указывать тех арендодателей,
которые подвязаны к этому помещению». Отсюда три вещи:

* ``dds_articles.location_required`` — флаг у статьи (шаблон ``kassa_enabled``). Хардкод кодов
  хрупок: коды сгенерированы транслитом, а каталог правится владельцем.
* ``location_lease.dds_article_id`` — статья, по которой платят ЭТУ аренду.
* ``location_id``/``lease_id`` на носителях расхода: проводка, строка черновика, резерв Сейфа,
  правило авто-разметки. Помещение — свойство СТРОКИ, а не операции: один платёж арендодателю
  закрывает зал и склад разными строками (то же решение, что по контрагенту 20.07.2026).

Имена полей у резерва Сейфа строго ``location_id``: колонка ``location`` там уже занята
значениями ``safe``/``kassa`` (``ck_safe_allocations_location``).

Все колонки nullable: в ``cashflow_transactions`` лежит вся история без помещений, а арендные
статьи используются не только под аренду (мусор, охрана — 13 из 21 проводки без контрагента).

Revision ID: 0211_lease_dds_analytics
Revises: 0210_location_lease
Create Date: 2026-07-23
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0211_lease_dds_analytics"
down_revision = "0210_location_lease"
branch_labels = None
depends_on = None

# Статьи ищем по НАЗВАНИЮ: коды сгенерированы транслитом (`arenda_torgovyh_tochek`), и
# опечатка в коде тихо оставила бы статью без флага.
RENT_ARTICLE_NAMES = ("Аренда торговых точек", "Аренда склада", "Аренда офиса")


def upgrade() -> None:
    op.add_column(
        "dds_articles",
        sa.Column(
            "location_required",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "location_lease",
        sa.Column(
            "dds_article_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("dds_articles.id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )
    op.create_index("ix_location_lease_dds_article_id", "location_lease", ["dds_article_id"])

    for table in ("cashflow_transactions", "expense_draft_line", "safe_allocations"):
        op.add_column(
            table,
            sa.Column(
                "location_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("location.id", ondelete="RESTRICT"),
                nullable=True,
            ),
        )
        op.add_column(
            table,
            sa.Column(
                "lease_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("location_lease.id", ondelete="RESTRICT"),
                nullable=True,
            ),
        )

    op.create_index(
        "ix_cashflow_transactions_location_id",
        "cashflow_transactions",
        ["location_id", "operation_date"],
    )

    # Правило авто-разметки помнит помещение: иначе «запомнить как правило» теряло бы его,
    # и каждая следующая арендная операция падала бы в разбор вручную.
    op.add_column(
        "classification_rules",
        sa.Column(
            "location_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("location.id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )

    names = ", ".join("'" + name.replace("'", "''") + "'" for name in RENT_ARTICLE_NAMES)
    op.execute(f"update dds_articles set location_required = true where name in ({names})")


def downgrade() -> None:
    op.drop_column("classification_rules", "location_id")
    op.drop_index("ix_cashflow_transactions_location_id", table_name="cashflow_transactions")
    for table in ("safe_allocations", "expense_draft_line", "cashflow_transactions"):
        op.drop_column(table, "lease_id")
        op.drop_column(table, "location_id")
    op.drop_index("ix_location_lease_dds_article_id", table_name="location_lease")
    op.drop_column("location_lease", "dds_article_id")
    op.drop_column("dds_articles", "location_required")
