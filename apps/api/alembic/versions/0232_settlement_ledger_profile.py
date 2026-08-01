"""Сверка платежей и УПД: контур контрагента и срок ожидания закрывающего документа.

ЗАЧЕМ. Мы платим сервисным контрагентам (связь, реклама, подписки, аренда) и ждём от них
закрывающий документ. Если он не приходит, деньги навсегда остаются дебиторкой, а расход
не признаётся: на 31.07.2026 так висело 311 969,51 ₽ у десяти контрагентов, и заметить это
было негде — разрыв виден только если открыть карточку и сверить платежи с УПД глазами.
Микроэль не прислала УПД за май, и это выяснилось через два месяца случайно.

ДВА ПОЛЯ, КОТОРЫХ НЕ ХВАТАЛО.

``closing_doc_expected_day`` — до какого числа месяца, следующего за периодом услуги,
контрагент обычно присылает закрывающий документ. Микроэль присылает 1-го, кто-то 5-го,
кто-то 10-го. Без этого числа сверка может ругаться либо слишком рано (1-го числа у всех,
хотя половина присылает позже и это норма), либо никогда. NULL = ждём с 1-го числа.

``settlement_contour`` — товарный контрагент или сервисный. У товарных закрывающие приходят
накладными и гасятся складским контуром iiko автоматически, ручной контроль им не нужен и в
сводку разрывов они не попадают. У сервисных гашение ручное — это и есть зона внимания.
NULL = определяем по факту: есть складские накладные → товарный. Явное значение перебивает
факт: контрагент может возить товар и параллельно оказывать услуги.

ПОЧЕМУ NULLABLE, А НЕ ЗНАЧЕНИЕ ПО УМОЛЧАНИЮ. Оба поля отличают «владелец решил» от «мы пока
не знаем». Проставить всем `service` на миграции значило бы соврать про 5 товарных
контрагентов, а `goods` — спрятать от сводки как раз тех, ради кого она делается.

Revision ID: 0232_settlement_ledger_profile
Revises: 0231_repair_articles_named
Create Date: 2026-08-01
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0232_settlement_ledger_profile"
down_revision = "0231_repair_articles_named"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "counterparty_payable_profile",
        sa.Column("closing_doc_expected_day", sa.Integer(), nullable=True),
    )
    op.add_column(
        "counterparty_payable_profile",
        sa.Column("settlement_contour", sa.String(length=16), nullable=True),
    )
    # Число месяца, а не произвольное целое: 29-31 нет в феврале, и «ждём до 31-го» в коротком
    # месяце означало бы «не ждём вовсе». Гард в БД, потому что поле правится и через API,
    # и руками при разборе.
    op.create_check_constraint(
        "ck_cpp_closing_doc_expected_day",
        "counterparty_payable_profile",
        "closing_doc_expected_day IS NULL OR closing_doc_expected_day BETWEEN 1 AND 28",
    )
    op.create_check_constraint(
        "ck_cpp_settlement_contour",
        "counterparty_payable_profile",
        "settlement_contour IS NULL OR settlement_contour IN ('goods', 'service')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_cpp_settlement_contour", "counterparty_payable_profile")
    op.drop_constraint("ck_cpp_closing_doc_expected_day", "counterparty_payable_profile")
    op.drop_column("counterparty_payable_profile", "settlement_contour")
    op.drop_column("counterparty_payable_profile", "closing_doc_expected_day")
