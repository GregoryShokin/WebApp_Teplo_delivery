"""Чек Кассы: пометка «позиция возвращена» на строке накладной.

Возвращённая строка остаётся в чеке (сверка gross-суммы с бумажным чеком и
карт-операцией копейка в копейку), но не проводится: ДДС/iiko/аллокации считаются
по net-части. Ожидаемый от банка возврат = сумма помеченных строк.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0157_cheque_line_return"
down_revision = "0156_assistant_mgr"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "invoice_line_item",
        sa.Column("is_return", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )


def downgrade() -> None:
    op.drop_column("invoice_line_item", "is_return")
