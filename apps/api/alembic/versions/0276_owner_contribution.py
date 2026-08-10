"""Вклад собственника в капитал — суммой и датой, в реестре собственников.

ЗАЧЕМ. «От чего вообще считается баланс» — вопрос владельца 09.08.2026, и до этой миграции
ответа не было: вклады собственников в системе не хранились нигде. Денежных проводок по статье
«Поступление денег от собственников» ноль, а 5 000 000 ₽ учредительного вклада (по 2 500 000 на
каждого из двоих) существуют только как знание владельца. Вся эта величина молча сидела внутри
балансирующей строки капитала, и баланс отвечал «капитал такой, потому что актив минус
обязательства» вместо «капитал такой, потому что внесли столько и заработали столько».

ПОЧЕМУ В РЕЕСТРЕ СОБСТВЕННИКОВ, А НЕ НАСТРОЙКОЙ. Вклад — свойство КОНКРЕТНОГО человека, как и
доля: их двое, и «5 млн одной цифрой» не отвечает на вопрос, кто сколько внёс. Реестр
``business_owner`` уже хранит долю и даты участия — вклад ложится туда же и правится там же,
в «Настройках → Собственники».

ПОЧЕМУ ОТДЕЛЬНАЯ ДАТА. Учредительный вклад сделан ДО начала управленческого учёта, и в балансе
он часть входящего капитала. Вклад, внесённый после 01.07.2026, приходит деньгами и виден в ДДС
своей статьёй. Дата разводит эти два случая, иначе один и тот же вклад посчитался бы дважды:
и как строка реестра, и как проводка.

Revision ID: 0276_owner_contribution
Revises: 0275_partner_cash_cp
Create Date: 2026-08-09
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0276_owner_contribution"
down_revision = "0275_partner_cash_cp"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "business_owner",
        sa.Column(
            "contribution_amount",
            sa.Numeric(16, 2),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "business_owner",
        sa.Column("contributed_on", sa.Date(), nullable=True),
    )
    # Отрицательный вклад — не «изъятие», а опечатка: изъятия ходят своими статьями ДДС
    # (возврат вкладов, дивиденды) и в реестре не живут.
    op.create_check_constraint(
        "ck_business_owner_contribution",
        "business_owner",
        "contribution_amount >= 0",
    )


def downgrade() -> None:
    op.drop_constraint("ck_business_owner_contribution", "business_owner", type_="check")
    op.drop_column("business_owner", "contributed_on")
    op.drop_column("business_owner", "contribution_amount")
