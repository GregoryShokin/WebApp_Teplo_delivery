"""Категория решает, какие поля спрашивать; карточка помнит, новым куплен объект или б/у.

ЗАЧЕМ ПРОФИЛЬ. Форма заведения карточки начинается с категории (решение владельца 2026-07-31),
и дальше поля зависят от неё: у рисоварки и кондиционера смысл имеют МАРКА и МОДЕЛЬ, у
производственного стола — МАТЕРИАЛ и РАЗМЕРЫ, а марки у него обычно нет вовсе. Спрашивать
модель у стола — это учить человека, что половину полей можно не заполнять; спрашивать материал
у пароконвектомата — то же самое с другой стороны.

ПОЧЕМУ ФЛАГ НА КАТЕГОРИИ, А НЕ СПИСОК ИМЁН ВО ФРОНТЕ. Тот же приём, что у ``location_required``
и ``asset_link_kind`` на статьях ДДС: список категорий владелец правит из интерфейса, а
захардкоженный во фронте перечень «вот эти шесть — техника» после добавления одиннадцатой
категории молча начнёт врать. Профиль живёт рядом со сроком службы — там же, где и остальное
знание о категории.

ЗАЧЕМ СОСТОЯНИЕ. Б/У объект и новый — это разные объекты с точки зрения оценки: у купленного
с рук пароконвектомата износ уже есть, и он не виден ни в сумме платежа, ни в сроке из
категории. Признак ``condition`` отвечает на вопрос «а вообще было ли что оценивать», а само
описание состояния уходит в ``asset_condition_report`` — туда же, куда пишет менеджер при
поломке, и оттуда его забирает контур оценки моделью.

NULL в ``condition`` — честное «неизвестно»: 149 карточек реестра инвентаризации 2026 заведены
описью, и чем они были в момент покупки, не знает никто. Ставить им 'used' значило бы выдумать
факт, а 'new' — соврать.

Revision ID: 0229_asset_spec_profile
Revises: 0228_asset_through_payment
Create Date: 2026-07-31
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0229_asset_spec_profile"
down_revision = "0228_asset_through_payment"
branch_labels = None
depends_on = None


# Категория → профиль полей.
#
# equipment — марка и модель обязательны: без них объект не опознать при инвентаризации и не
#             оценить б/у (цена «холодильника» без марки не значит ничего);
# furniture — материал и размеры: нержавейка или дерево, и сколько сантиметров. Марки нет;
# other     — свободная строка характеристик: категория-мешок, единого набора полей у неё нет.
CATEGORY_PROFILES: dict[str, str] = {
    "Тепловое оборудование": "equipment",
    "Холодильное/морозильное оборудование": "equipment",
    "Кассовое оборудование": "equipment",
    "Электромеханическое оборудование": "equipment",
    "Электроника и оргтехника": "equipment",
    "Системы кондиционирования": "equipment",
    "Оборудование торговых залов": "furniture",
    "Вспомогательное оборудование": "furniture",
    "Мебель и предметы интерьера": "furniture",
    "Прочий кухонный инвентарь": "other",
}


def upgrade() -> None:
    # Умолчание 'other', а не 'equipment': новая категория, о которой мы ничего не знаем, не
    # должна требовать марку и модель — форма заблокировалась бы на поле, которого у объекта
    # может не быть, и покупку снова понесли бы мимо баланса.
    op.add_column(
        "asset_category",
        sa.Column(
            "spec_profile",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'other'"),
        ),
    )
    op.create_check_constraint(
        "ck_asset_category_spec_profile",
        "asset_category",
        "spec_profile IN ('equipment','furniture','other')",
    )
    for name, profile in CATEGORY_PROFILES.items():
        op.execute(
            sa.text(
                "UPDATE asset_category SET spec_profile = :profile WHERE name = :name"
            ).bindparams(profile=profile, name=name)
        )

    op.add_column("fixed_asset", sa.Column("condition", sa.String(length=8), nullable=True))
    op.create_check_constraint(
        "ck_fixed_asset_condition",
        "fixed_asset",
        "condition IS NULL OR condition IN ('new','used')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_fixed_asset_condition", "fixed_asset", type_="check")
    op.drop_column("fixed_asset", "condition")
    op.drop_constraint("ck_asset_category_spec_profile", "asset_category", type_="check")
    op.drop_column("asset_category", "spec_profile")
