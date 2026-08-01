"""Абонентские платежи: помесячное признание расхода без закрывающего документа.

ЗАЧЕМ. Часть контрагентов работает по абонентской плате помесячно, а закрывающие документы
присылает не всегда: Наумченко не присылает вовсе, Микроэль присылает, но за май пропустила.
Оплата при этом бывает вперёд за несколько месяцев — 9 000 ₽ за апрель-июнь одним платежом.
Такие деньги висели дебиторкой целиком до конца всего периода, а расход не признавался ни в
одном месяце: на 01.08.2026 так стояло 311 969 ₽ у десяти контрагентов.

ЧТО ДЕЛАЕМ. Раз в месяц по такому платежу заводится ВНУТРЕННИЙ закрывающий документ на долю
периода (9 000 за три месяца → 3 000 в апреле, 3 000 в мае, 3 000 в июне). Он гасит дебиторку
и признаёт расход в своём месяце — ровно тот же механизм, которым уже живёт аренда
(``source='lease'``): арендодатель документов не выставляет, но долг известен и считается сам.

ПОЧЕМУ ОТДЕЛЬНЫЙ ИСТОЧНИК, А НЕ 'lease'. Это документ, которого не существует в природе:
контрагент его не выставлял и не подтверждал. Такой расход нельзя брать в налоговую базу УСН
(нет первички — инспекция снимет), поэтому он обязан быть отличим одним полем, а не по
косвенным признакам. ``self_billed`` и есть этот признак: витрины показывают «признано без
первички», налоговый контур такие строки не берёт.

ЗАМЕЩЕНИЕ. Если настоящий УПД за тот же период всё-таки приходит, внутренний документ
аннулируется, а дебиторку гасит настоящий (см. ``subscription_accruals.supersede``). Иначе
расход задвоился бы: один раз по самоакту, второй — по УПД.

Revision ID: 0233_subscription_self_billed
Revises: 0232_settlement_ledger_profile
Create Date: 2026-08-01
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0233_subscription_self_billed"
down_revision = "0232_settlement_ledger_profile"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE вне транзакции: в PostgreSQL новое значение enum нельзя
    # использовать в той же транзакции, где оно добавлено, а alembic держит миграцию в одной.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE counterparty_invoice_source ADD VALUE IF NOT EXISTS 'self_billed'")

    # Сколько месяцев покрывает платёж. 1 (или NULL) — обычный платёж за свой месяц, разбивать
    # нечего. >1 — абонентская оплата вперёд, которую делим помесячно.
    op.add_column(
        "supplier_prepayment",
        sa.Column("service_period_months", sa.Integer(), nullable=True),
    )
    op.create_check_constraint(
        "ck_supplier_prepayment_period_months",
        "supplier_prepayment",
        "service_period_months IS NULL OR service_period_months BETWEEN 1 AND 36",
    )
    # Признание помесячно включается на самом платеже, а не в карточке контрагента: один и тот
    # же поставщик может выставить и абонентский счёт, и разовую работу (решение владельца
    # 01.08.2026 — «тариф не сущность карточки, а переменная платежа»).
    op.add_column(
        "supplier_prepayment",
        sa.Column(
            "auto_recognize_monthly",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    # Те же два поля на строке расхода: период спрашивается в окне «Новый платёж», а на
    # дебиторку переезжает при оплате (supplier_prepayments._expense_line_period). Без них
    # квартальный платёж терял бы разбивку по дороге от окна до признания.
    op.add_column(
        "expense_draft_line",
        sa.Column("service_period_months", sa.Integer(), nullable=True),
    )
    op.create_check_constraint(
        "ck_expense_draft_line_period_months",
        "expense_draft_line",
        "service_period_months IS NULL OR service_period_months BETWEEN 1 AND 36",
    )
    op.add_column(
        "expense_draft_line",
        sa.Column(
            "auto_recognize_monthly",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("expense_draft_line", "auto_recognize_monthly")
    op.drop_constraint("ck_expense_draft_line_period_months", "expense_draft_line")
    op.drop_column("expense_draft_line", "service_period_months")
    op.drop_column("supplier_prepayment", "auto_recognize_monthly")
    op.drop_constraint("ck_supplier_prepayment_period_months", "supplier_prepayment")
    op.drop_column("supplier_prepayment", "service_period_months")
    # Значение enum не удаляем: PostgreSQL этого не умеет без пересоздания типа, а строки
    # с source='self_billed' могли остаться.
