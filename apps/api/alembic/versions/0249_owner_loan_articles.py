"""Займы собственникам тоже обязаны называть имя.

ПОЧЕМУ ЭТИХ СТАТЕЙ НЕ БЫЛО В 0247. Тогда флаг получили три статьи, которые владелец назвал
прямо: взнос, возврат, дивиденды. Но долг собственника перед бизнесом растёт не ими, а ВЫДАЧЕЙ
ЗАЙМА — и именно на такой проводке (14.07.2026, 30 000 ₽ Павлу) контрагент оказался не указан:
механизма собственников тогда не существовало. Оставь статью без флага — и следующий займ снова
уйдёт безымянным, а долг не вырастет ни на рубль.

ПОЧЕМУ ЭТО НЕ ЛОМАЕТ ЗАЙМЫ СОТРУДНИКАМ. У них своя статья («Выдача займов сотрудникам»,
operating), и на проде она живёт отдельно: 2 проводки против одной у «Выдачи кредитов и займов».
Кредит от банка тоже не задет — он приходит по «Получению кредитов и займов», а гасится
«Оплатами по кредитам и займам»; флага у них нет.

ЕСЛИ БИЗНЕС КОГДА-НИБУДЬ ЗАЙМЁТ НЕ СОБСТВЕННИКУ, эта статья потребует собственника и откажет.
Тогда правильным будет развести статьи, а не снимать флаг: «займ партнёру» и «займ
собственнику» — разные строки баланса.

Revision ID: 0249_owner_loan_articles
Revises: 0248_business_owner
Create Date: 2026-08-03
"""

from __future__ import annotations

from sqlalchemy import text

from alembic import op

revision = "0249_owner_loan_articles"
down_revision = "0248_business_owner"
branch_labels = None
depends_on = None

LOAN_ARTICLES: tuple[tuple[str, str], ...] = (
    ("vydacha_kreditov_i_zaimov", "Выдача кредитов и займов"),
    ("vozvrat_kreditov_i_zaimov", "Возврат кредитов и займов"),
)


def upgrade() -> None:
    conn = op.get_bind()
    for code, name in LOAN_ARTICLES:
        conn.execute(
            text(
                "UPDATE dds_articles SET owner_required = true WHERE code = :code OR name = :name"
            ),
            {"code": code, "name": name},
        )


def downgrade() -> None:
    conn = op.get_bind()
    for code, name in LOAN_ARTICLES:
        conn.execute(
            text(
                "UPDATE dds_articles SET owner_required = false WHERE code = :code OR name = :name"
            ),
            {"code": code, "name": name},
        )
