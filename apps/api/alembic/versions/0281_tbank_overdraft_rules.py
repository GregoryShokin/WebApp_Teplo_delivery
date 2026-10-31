"""Строки овердрафта Т-Банка разбираются сами, статья комиссии названа как её строка ОПиУ.

Решение владельца 31.10.2026: выдача и погашение овердрафта — тело долга, в ОПиУ они не
входят, в строку «Комиссия за овердрафт» идёт только плата банка. Правил на эти строки не
было: их разносили вручную, а статья комиссии называлась просто «Овердрафт» и стояла рядом
с «Получением овердрафта». Так 30.10.2026 выдача на 36 916,03 ₽ попала в комиссию и подняла
чистую прибыль. Настоящую плату без своего правила забирало «плата за» → РКО, поэтому
новые правила стоят раньше него.

Revision ID: 0281_tbank_overdraft_rules
Revises: 0280_payroll_line_penalty_debt
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa

from alembic import op

revision = "0281_tbank_overdraft_rules"
down_revision = "0280_payroll_line_penalty_debt"
branch_labels = None
depends_on = None

FEE_ARTICLE_CODE = "overdraft"
RULE_PRIORITY = 20
RULE_COMMENT = "Решение владельца 31.10.2026: тело овердрафта вне ОПиУ, плата банка — комиссия"
# Заметки к статьям называют статью комиссии старым именем.
ARTICLE_NOTES = (
    (
        FEE_ARTICLE_CODE,
        "Перечисление процентов по овердрафту",
        "Плата банка за овердрафт",
    ),
    (
        "pogashenie_overdrafta",
        "Тело овердрафта; проценты — статья «Овердрафт»",
        "Тело овердрафта; плата банка — статья «Комиссия за овердрафт»",
    ),
    (
        "overdraft_fee",
        "Деактивированный плейсхолдер: проценты идут «Овердрафтом»",
        "Деактивированный плейсхолдер: плата банка идёт статьёй «Комиссия за овердрафт»",
    ),
)
RULES = (
    (
        "T-Bank: Овердрафт — выдача",
        "in",
        "Предоставление овердрафта",
        "poluchenie_overdrafta",
    ),
    (
        "T-Bank: Овердрафт — погашение",
        "out",
        "Погашение разрешенного овердрафта",
        "pogashenie_overdrafta",
    ),
    (
        "T-Bank: Овердрафт — плата за активный овердрафт",
        "out",
        "Плата за активный овердрафт",
        FEE_ARTICLE_CODE,
    ),
    (
        "T-Bank: Овердрафт — плата за использованный лимит",
        "out",
        "Плата за использованный лимит овердрафта",
        FEE_ARTICLE_CODE,
    ),
)


def _set_article_note(article_code: str, current: str, wanted: str) -> None:
    op.get_bind().execute(
        sa.text(
            """
            UPDATE pnl_article_rule AS rule
            SET note = :wanted
            FROM dds_articles AS article
            WHERE article.id = rule.article_id
              AND article.code = :article_code
              AND rule.note = :current
            """
        ),
        {"article_code": article_code, "current": current, "wanted": wanted},
    )


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            UPDATE dds_articles
            SET name = 'Комиссия за овердрафт',
                description = 'Плата банка за овердрафт. Выдача и погашение идут статьями '
                              '«Получение овердрафта» и «Погашение овердрафта»'
            WHERE code = :code
            """
        ),
        {"code": FEE_ARTICLE_CODE},
    )
    bind.execute(
        sa.text(
            """
            UPDATE pnl_line
            SET source_note = 'ДДС, статья «Комиссия за овердрафт»: плата банка'
            WHERE code = 'overdraft_fee'
            """
        )
    )
    for article_code, old_note, new_note in ARTICLE_NOTES:
        _set_article_note(article_code, old_note, new_note)
    for name, direction, pattern, article_code in RULES:
        bind.execute(
            sa.text(
                """
                INSERT INTO classification_rules (
                    id, name, priority, is_active, provider, direction,
                    purpose_pattern, action, article_id, comment
                )
                SELECT :id, :name, :priority, true, 'tbank', :direction,
                       :pattern, 'set_article', id, :comment
                FROM dds_articles
                WHERE code = :article_code
                """
            ),
            {
                "id": uuid.uuid4(),
                "name": name,
                "priority": RULE_PRIORITY,
                "direction": direction,
                "pattern": pattern,
                "comment": RULE_COMMENT,
                "article_code": article_code,
            },
        )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text("DELETE FROM classification_rules WHERE name = ANY(:names)"),
        {"names": [rule[0] for rule in RULES]},
    )
    for article_code, old_note, new_note in ARTICLE_NOTES:
        _set_article_note(article_code, new_note, old_note)
    bind.execute(
        sa.text(
            """
            UPDATE pnl_line
            SET source_note = 'ДДС, статья «Овердрафт»: перечисление процентов'
            WHERE code = 'overdraft_fee'
            """
        )
    )
    bind.execute(
        sa.text(
            """
            UPDATE dds_articles
            SET name = 'Овердрафт', description = 'Перечисление %% по овердрафту'
            WHERE code = :code
            """
        ),
        {"code": FEE_ARTICLE_CODE},
    )
