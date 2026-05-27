"""setting display metadata

Revision ID: 0005_setting_display_metadata
Revises: 0004_payroll
Create Date: 2026-05-27
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0005_setting_display_metadata"
down_revision = "0004_payroll"
branch_labels = None
depends_on = None


SETTING_DISPLAY_METADATA = {
    "schedule.target_payroll_revenue_ratio": {
        "category": "schedule",
        "display_name": "Целевой ФОТ % от выручки",
        "description": (
            "Целевая доля ФОТ в выручке "
            "для планирования графика."
        ),
        "widget_type": "percent",
        "widget_options": None,
        "unit": "%",
    },
    "schedule.payroll_plan_fact_deviation_threshold": {
        "category": "schedule",
        "display_name": "Порог отклонения план-факт ФОТ",
        "description": (
            "Отклонение от планового ФОТ, "
            "после которого смена требует внимания."
        ),
        "widget_type": "percent",
        "widget_options": None,
        "unit": "%",
    },
    "schedule.atypical_days": {
        "category": "schedule",
        "display_name": "Праздничные и нетиповые дни",
        "description": (
            "Даты и периоды с нетиповым "
            "прогнозом выручки и графика."
        ),
        "widget_type": "date_array",
        "widget_options": {"format": "MM-DD", "fixed_year": False},
        "unit": None,
    },
    "payroll.open_shift_cap_hours": {
        "category": "payroll",
        "display_name": "Максимальная длительность смены",
        "description": (
            "Лимит часов для открытой смены "
            "перед авто-закрытием."
        ),
        "widget_type": "number",
        "widget_options": {"min": 1, "step": 1},
        "unit": "часов",
    },
    "payroll.open_shift_auto_close_time": {
        "category": "payroll",
        "display_name": "Время авто-закрытия открытой смены",
        "description": (
            "Время по Москве для закрытия смены "
            "без отметки окончания."
        ),
        "widget_type": "time",
        "widget_options": None,
        "unit": None,
    },
    "payroll.weekly_payment_window": {
        "category": "payroll",
        "display_name": "День начала payroll-недели",
        "description": (
            "День, с которого начинается "
            "расчётная неделя зарплаты."
        ),
        "widget_type": "select",
        "widget_options": {
            "options": [
                {
                    "value": {
                        "payday": "monday",
                        "period_start": "monday",
                        "period_end": "sunday",
                    },
                    "label": "Понедельник",
                },
                {
                    "value": {
                        "payday": "tuesday",
                        "period_start": "tuesday",
                        "period_end": "monday",
                    },
                    "label": "Вторник",
                },
                {
                    "value": {
                        "payday": "wednesday",
                        "period_start": "wednesday",
                        "period_end": "tuesday",
                    },
                    "label": "Среда",
                },
                {
                    "value": {
                        "payday": "thursday",
                        "period_start": "thursday",
                        "period_end": "wednesday",
                    },
                    "label": "Четверг",
                },
                {
                    "value": {
                        "payday": "friday",
                        "period_start": "friday",
                        "period_end": "thursday",
                    },
                    "label": "Пятница",
                },
                {
                    "value": {
                        "payday": "saturday",
                        "period_start": "saturday",
                        "period_end": "friday",
                    },
                    "label": "Суббота",
                },
                {
                    "value": {
                        "payday": "sunday",
                        "period_start": "sunday",
                        "period_end": "saturday",
                    },
                    "label": "Воскресенье",
                },
            ]
        },
        "unit": None,
    },
    "payroll.deposit_fund_payment_date": {
        "category": "payroll",
        "display_name": "Дата выплаты накопительного фонда",
        "description": (
            "Ежегодная дата выплаты "
            "накопительного фонда сотрудникам."
        ),
        "widget_type": "date",
        "widget_options": {"format": "MM-DD", "fixed_year": False},
        "unit": None,
    },
    "payroll.role_category_rates": {
        "category": "payroll",
        "display_name": "Ставки смен по ролям и категориям",
        "description": (
            "Базовые ставки полной 12-часовой "
            "смены по роли и категории."
        ),
        "widget_type": "json",
        "widget_options": None,
        "unit": "₽",
    },
    "payroll.category_rules": {
        "category": "payroll",
        "display_name": "Правила payroll-категорий",
        "description": (
            "Коэффициенты процента, депозитная "
            "цель и удержание по категориям."
        ),
        "widget_type": "json",
        "widget_options": None,
        "unit": None,
    },
    "payroll.revenue_percent_tiers": {
        "category": "payroll",
        "display_name": "Процент от дневной выручки",
        "description": (
            "Пороговая таблица процента "
            "от дневной выручки."
        ),
        "widget_type": "json",
        "widget_options": None,
        "unit": "%",
    },
    "payroll.allowances": {
        "category": "payroll",
        "display_name": "Надбавки к смене",
        "description": (
            "Доплаты старшему и заместителю "
            "старшего за смену."
        ),
        "widget_type": "json",
        "widget_options": None,
        "unit": "₽",
    },
    "payroll.fund_rates_by_tenure": {
        "category": "payroll",
        "display_name": "Ставки накопительного фонда по стажу",
        "description": (
            "Процент накопительного фонда "
            "от оклада по стажу."
        ),
        "widget_type": "json",
        "widget_options": None,
        "unit": "%",
    },
    "payroll.mock_daily_revenue": {
        "category": "payroll",
        "display_name": "Временная дневная выручка",
        "description": (
            "Ручной источник дневной выручки "
            "до подключения iiko OLAP."
        ),
        "widget_type": "json",
        "widget_options": None,
        "unit": "₽",
    },
    "payroll.deposit_auto_withholding_enabled": {
        "category": "payroll",
        "display_name": "Автоудержание депозита",
        "description": (
            "Включает автоматическое удержание "
            "депозита по категории."
        ),
        "widget_type": "boolean",
        "widget_options": None,
        "unit": None,
    },
    "payroll.deposit_write_offs": {
        "category": "payroll",
        "display_name": "Ручные списания депозитов",
        "description": (
            "Списания депозитов до отдельного "
            "журнала payroll-событий."
        ),
        "widget_type": "json",
        "widget_options": None,
        "unit": "₽",
    },
    "payment_calendar.auto_match_tolerance": {
        "category": "payment_calendar",
        "display_name": "Tolerance суммы для auto-match",
        "description": (
            "Допустимое отклонение суммы "
            "при сопоставлении план/факт ДДС."
        ),
        "widget_type": "percent",
        "widget_options": {"value_path": "relative"},
        "unit": "%",
    },
    "payment_calendar.revenue_seasonality_coefficients": {
        "category": "payment_calendar",
        "display_name": "Сезонные коэффициенты выручки",
        "description": (
            "Коэффициенты сезонности "
            "для прогноза выручки."
        ),
        "widget_type": "json",
        "widget_options": None,
        "unit": None,
    },
    "balance.close_day": {
        "category": "balance",
        "display_name": "Срок закрытия баланса (день месяца)",
        "description": (
            "Последний день месяца, "
            "до которого нужно закрыть баланс."
        ),
        "widget_type": "number",
        "widget_options": {"min": 1, "max": 31, "step": 1},
        "unit": "число",
    },
    "fixed_assets.capitalization_threshold_rub": {
        "category": "fixed_assets",
        "display_name": "Порог признания ОС",
        "description": (
            "Минимальная стоимость актива "
            "для признания основным средством."
        ),
        "widget_type": "number",
        "widget_options": {"min": 0, "step": 100},
        "unit": "₽",
    },
    "fixed_assets.repair_modernization_threshold_ratio": {
        "category": "fixed_assets",
        "display_name": "Граница ремонт/модернизация",
        "description": (
            "Доля стоимости, отделяющая "
            "ремонт от модернизации."
        ),
        "widget_type": "percent",
        "widget_options": None,
        "unit": "%",
    },
}


def upgrade() -> None:
    op.add_column("app_setting", sa.Column("display_name", sa.Text(), nullable=True))
    op.add_column(
        "app_setting",
        sa.Column("widget_type", sa.Text(), nullable=False, server_default=sa.text("'text'")),
    )
    op.add_column(
        "app_setting",
        sa.Column("widget_options", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column("app_setting", sa.Column("unit", sa.Text(), nullable=True))

    conn = op.get_bind()
    for key, metadata in SETTING_DISPLAY_METADATA.items():
        conn.execute(
            sa.text(
                """
                update app_setting
                   set category = :category,
                       display_name = :display_name,
                       description = :description,
                       widget_type = :widget_type,
                       widget_options = cast(:widget_options as jsonb),
                       unit = :unit
                 where key = :key
                """
            ),
            {
                "key": key,
                **metadata,
                "widget_options": json.dumps(metadata["widget_options"])
                if metadata["widget_options"] is not None
                else None,
            },
        )

    conn.execute(sa.text("update app_setting set display_name = key where display_name is null"))
    op.alter_column("app_setting", "display_name", existing_type=sa.Text(), nullable=False)


def downgrade() -> None:
    op.drop_column("app_setting", "unit")
    op.drop_column("app_setting", "widget_options")
    op.drop_column("app_setting", "widget_type")
    op.drop_column("app_setting", "display_name")
