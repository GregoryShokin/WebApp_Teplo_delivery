"""Схождение двух веток миграций: контур ОПиУ и оплата приёмки через Сейф.

Обе ветки выросли из ``0250_allocation_origin`` параллельно. В main приехала
``0256_intake_pays_via_safe`` (оплата приёмки через Сейф), в ветке ОПиУ — вся линия
``0251_pnl_lines`` … ``0268_pnl_workup_posted``.

ДВЕ ГОЛОВЫ — НЕ КОСМЕТИКА: при них ``alembic upgrade head`` падает с «Multiple head revisions
are present», то есть выкатка физически невозможна, пока линии не сведены. Именно этот блокер
уже останавливал выкатку 02.08.2026 (тогда разошлись 0232-е), и аудит замкнутости 05.08.2026
нашёл его снова — на новой паре номеров.

Схему ревизия не трогает: она только объявляет, что дальше обе линии идут одной. Порядок
внутри пары важен лишь для читаемости — alembic применит недостающие ревизии обеих ветвей
до этой точки в порядке их собственных зависимостей.

Revision ID: 0269_merge_pnl_into_main
Revises: 0268_pnl_workup_posted, 0256_intake_pays_via_safe
Create Date: 2026-08-06
"""

from __future__ import annotations

revision = "0269_merge_pnl_into_main"
down_revision = ("0268_pnl_workup_posted", "0256_intake_pays_via_safe")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
