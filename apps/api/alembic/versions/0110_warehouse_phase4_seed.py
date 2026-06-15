"""warehouse phase 4: staff DDS article + invoice↔bank match settings

Data-only seed (no DDL): a separate DDS article for the «персонал» part of a warehouse
invoice (so it never mixes with goods-purchase ``payment_to_supplier``), plus the
invoice↔bank time-match parameters surfaced on the «Настройки» page.

Revision ID: 0110_warehouse_phase4_seed
Revises: 0109_position_admin_oklad
Create Date: 2026-06-15
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0110_warehouse_phase4_seed"
down_revision = "0109_position_admin_oklad"
branch_labels = None
depends_on = None

STAFF_ARTICLE_CODE = "supplier_staff_payment"
SETTING_KEYS = (
    "warehouse.staff_payment_article_code",
    "warehouse.match_amount_tolerance_pct",
    "warehouse.match_window_hours",
    "warehouse.match_tight_window_minutes",
)


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            insert into dds_articles (id, code, name, movement_type, activity_type, description)
            values (
                '8627b172-b385-4153-9ae2-65856df09442',
                :code,
                'Оплата поставщику (персонал)',
                'outflow',
                'operating',
                'Денежная часть накладной по строкам «персонал» — не уходит в iiko, '
                'учитывается отдельной статьёй ДДС, не смешиваясь с закупкой товара.'
            )
            on conflict (code) do nothing
            """
        ),
        {"code": STAFF_ARTICLE_CODE},
    )
    bind.execute(
        sa.text(
            """
            insert into app_setting (
                id, key, value, value_type, category, display_name, description,
                widget_type, widget_options, unit, updated_at
            )
            values
            (
                '7336df20-c425-4230-bff0-2ad60ea98697',
                'warehouse.staff_payment_article_code',
                '"supplier_staff_payment"'::jsonb, 'string', 'warehouse',
                'Статья ДДС для персональной части накладной',
                'Код статьи ДДС, на которую относится денежная часть «персонал» накладной '
                'при оплате наличными/со счёта.',
                'text', null, null, now()
            ),
            (
                '6369c2f0-3a1f-453b-902c-8ff8902ebc23',
                'warehouse.match_amount_tolerance_pct',
                '10'::jsonb, 'number', 'warehouse',
                'Допуск суммы при сверке накладной с банком',
                'На сколько процентов сумма банковской операции может отличаться от остатка '
                'накладной, чтобы попасть в кандидаты сверки.',
                'number', null, '%', now()
            ),
            (
                'ca0d8901-c9b7-4353-a9ed-98031397efca',
                'warehouse.match_window_hours',
                '48'::jsonb, 'number', 'warehouse',
                'Окно по времени при сверке накладной с банком',
                'Максимальное расхождение между временем чека и проводкой банка (в часах) '
                'для попадания операции в кандидаты.',
                'number', null, 'ч', now()
            ),
            (
                'f8051753-f422-43af-84ad-a43b03b9b4f4',
                'warehouse.match_tight_window_minutes',
                '120'::jsonb, 'number', 'warehouse',
                'Узкое окно «точного» совпадения времени',
                'В пределах этого окна (в минутах) совпадение времени считается точным: только '
                'тогда кандидат с неточной суммой (в пределах допуска) считается уверенным.',
                'number', null, 'мин', now()
            )
            on conflict (key) do nothing
            """
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text("delete from app_setting where key = any(:keys)"),
        {"keys": list(SETTING_KEYS)},
    )
    bind.execute(
        sa.text("delete from dds_articles where code = :code"),
        {"code": STAFF_ARTICLE_CODE},
    )
