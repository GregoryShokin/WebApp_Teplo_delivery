"""seed dds reference data

Revision ID: 0048_dds_seed
Revises: 0047_dds_classification
Create Date: 2026-06-03
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0048_dds_seed"
down_revision = "0047_dds_classification"
branch_labels = None
depends_on = None

LEGAL_ENTITY = "ИП Шокина Е.А."

WALLETS = [
    {
        "code": "sber_main",
        "name": "Сбербанк",
        "type": "bank",
        "bank_code": "sber",
        "opening_balance": Decimal("0"),
        "is_internal_transfer_eligible": True,
    },
    {
        "code": "tbank_main",
        "name": "Тинькофф",
        "type": "bank",
        "bank_code": "tbank",
        "opening_balance": Decimal("0"),
        "is_internal_transfer_eligible": True,
    },
    {
        "code": "tk_chernikova",
        "name": "ТК Черникова",
        "type": "store_cash",
        "bank_code": "iiko_cashbox",
        "opening_balance": Decimal("0"),
        "is_internal_transfer_eligible": False,
    },
    {
        "code": "cash_safe",
        "name": "Сейф",
        "type": "cash_safe",
        "bank_code": "cash",
        "opening_balance": Decimal("0"),
        "is_internal_transfer_eligible": False,
    },
    {
        "code": "guarant_fund",
        "name": "ГарантФонд",
        "type": "reserve",
        "bank_code": "tbank",
        "opening_balance": Decimal("47200.00"),
        "is_internal_transfer_eligible": False,
    },
]

ARTICLES = [
    {
        "code": "revenue_acquiring_sber",
        "name": "Выручка эквайринг Sber",
        "movement_type": "inflow",
        "activity_type": "operating",
        "description": "Поступления эквайринга Сбербанка.",
    },
    {
        "code": "revenue_acquiring_tbank",
        "name": "Выручка эквайринг T-Bank",
        "movement_type": "inflow",
        "activity_type": "operating",
        "description": "Поступления собственного эквайринга T-Bank.",
    },
    {
        "code": "revenue_cash",
        "name": "Выручка наличная",
        "movement_type": "inflow",
        "activity_type": "operating",
        "description": "Поступление денег с торговых точек наличными.",
    },
    {
        "code": "internal_transfer",
        "name": "Внутренний перевод",
        "movement_type": "internal",
        "activity_type": "internal",
        "description": "Перевод между собственными счетами и кошельками.",
    },
    {
        "code": "overdraft_fee",
        "name": "Овердрафт комиссия",
        "movement_type": "outflow",
        "activity_type": "financing",
        "description": "Проценты и комиссии по овердрафту; тело долга не включается.",
    },
    {
        "code": "loan_principal_payment",
        "name": "Погашение тела кредита",
        "movement_type": "outflow",
        "activity_type": "financing",
        "description": "Погашение тела кредита или овердрафта.",
    },
    {
        "code": "customer_refund",
        "name": "Возврат покупателю",
        "movement_type": "outflow",
        "activity_type": "operating",
        "description": "Возвраты клиентам за ранее купленный товар.",
    },
    {
        "code": "payment_to_supplier",
        "name": "Оплата поставщикам",
        "movement_type": "outflow",
        "activity_type": "operating",
        "description": "Закупка товара, сырья и материалов для торговых точек.",
    },
    {
        "code": "payroll_payout",
        "name": "Выплата ЗП",
        "movement_type": "outflow",
        "activity_type": "operating",
        "description": "Выплаты сотрудникам и payroll cash-fact.",
    },
    {
        "code": "tax_payment",
        "name": "Налоги",
        "movement_type": "outflow",
        "activity_type": "operating",
        "description": "Налоговые платежи и взносы.",
    },
    {
        "code": "rent",
        "name": "Аренда",
        "movement_type": "outflow",
        "activity_type": "operating",
        "description": "Аренда торговых точек, склада, офиса и связанные платежи.",
    },
    {
        "code": "bank_service_fee",
        "name": "РКО",
        "movement_type": "outflow",
        "activity_type": "operating",
        "description": "Абонентская плата и комиссии за банковское обслуживание.",
    },
    {
        "code": "unknown",
        "name": "Не размечено",
        "movement_type": "outflow",
        "activity_type": "operating",
        "description": "Временная статья для операций, требующих разметки.",
    },
]

ARTICLE_ALIASES = [
    {"article_code": "overdraft_fee", "alias": "Овердрафт", "source": "legacy_sheets"},
    {
        "article_code": "payment_to_supplier",
        "alias": "Оплата поставщикам ",
        "source": "legacy_sheets",
    },
    {"article_code": "customer_refund", "alias": "Возвраты клиентам", "source": "legacy_sheets"},
]

CLASSIFICATION_RULES = [
    {
        "name": "Зачисление эквайринга T-Bank",
        "priority": 10,
        "is_active": True,
        "provider": "tbank",
        "direction": "in",
        "counterparty_name_pattern": 'АО "ТБанк"',
        "purpose_pattern": "Зачисление средств по терминалам эквайринга",
        "action": "set_article",
        "article_code": "revenue_acquiring_tbank",
        "comment": "Seed-rule для первого распознавания собственного эквайринга T-Bank.",
    },
    {
        "name": "Internal transfer placeholder",
        "priority": 5,
        "is_active": False,
        "provider": None,
        "direction": None,
        "counterparty_name_pattern": None,
        "purpose_pattern": None,
        "action": "mark_internal_transfer",
        "article_code": None,
        "comment": "Будет активировано после заполнения own_accounts_registry из банковских API.",
    },
]


def upgrade() -> None:
    _seed_wallets()
    _seed_articles()
    _seed_article_aliases()
    _seed_classification_rules()


def downgrade() -> None:
    bind = op.get_bind()
    rule_names = [rule["name"] for rule in CLASSIFICATION_RULES]
    article_codes = [article["code"] for article in ARTICLES]
    wallet_codes = [wallet["code"] for wallet in WALLETS]

    bind.execute(
        sa.text("DELETE FROM classification_rules WHERE name = ANY(:names)"),
        {"names": rule_names},
    )
    bind.execute(
        sa.text(
            """
            DELETE FROM dds_article_aliases
            WHERE article_id IN (SELECT id FROM dds_articles WHERE code = ANY(:codes))
            """
        ),
        {"codes": article_codes},
    )
    bind.execute(
        sa.text("DELETE FROM dds_articles WHERE code = ANY(:codes)"),
        {"codes": article_codes},
    )

    account_ids = [
        row[0]
        for row in bind.execute(
            sa.text(
                """
                SELECT account_id
                FROM wallet
                WHERE code = ANY(:codes) AND account_id IS NOT NULL
                """
            ),
            {"codes": wallet_codes},
        )
    ]
    bind.execute(sa.text("DELETE FROM wallet WHERE code = ANY(:codes)"), {"codes": wallet_codes})
    if account_ids:
        bind.execute(
            sa.text(
                """
                DELETE FROM account
                WHERE id = ANY(:ids)
                  AND NOT EXISTS (SELECT 1 FROM wallet WHERE wallet.account_id = account.id)
                """
            ),
            {"ids": account_ids},
        )


def _seed_wallets() -> None:
    bind = op.get_bind()
    for wallet in WALLETS:
        account_id = uuid.uuid4()
        bind.execute(
            sa.text(
                """
                INSERT INTO account (id, bank_code, legal_entity, currency, status)
                VALUES (:id, :bank_code, :legal_entity, 'RUB', 'active')
                """
            ),
            {
                "id": account_id,
                "bank_code": wallet["bank_code"],
                "legal_entity": LEGAL_ENTITY,
            },
        )
        bind.execute(
            sa.text(
                """
                INSERT INTO wallet (
                    id, code, name, type, currency, is_internal_transfer_eligible,
                    status, account_id, opening_balance
                )
                VALUES (
                    :id, :code, :name, CAST(:type AS wallet_type), 'RUB',
                    :is_internal_transfer_eligible, 'active', :account_id, :opening_balance
                )
                ON CONFLICT (code) DO UPDATE
                SET
                    name = EXCLUDED.name,
                    type = EXCLUDED.type,
                    currency = EXCLUDED.currency,
                    is_internal_transfer_eligible = EXCLUDED.is_internal_transfer_eligible,
                    account_id = COALESCE(wallet.account_id, EXCLUDED.account_id),
                    opening_balance = EXCLUDED.opening_balance,
                    status = EXCLUDED.status
                """
            ),
            {
                "id": uuid.uuid4(),
                "code": wallet["code"],
                "name": wallet["name"],
                "type": wallet["type"],
                "is_internal_transfer_eligible": wallet["is_internal_transfer_eligible"],
                "account_id": account_id,
                "opening_balance": wallet["opening_balance"],
            },
        )
        bind.execute(
            sa.text(
                """
                DELETE FROM account
                WHERE id = :id
                  AND NOT EXISTS (SELECT 1 FROM wallet WHERE wallet.account_id = account.id)
                """
            ),
            {"id": account_id},
        )


def _seed_articles() -> None:
    bind = op.get_bind()
    existing_codes = {
        row[0]
        for row in bind.execute(
            sa.text("SELECT code FROM dds_articles WHERE code = ANY(:codes)"),
            {"codes": [article["code"] for article in ARTICLES]},
        )
    }
    rows = [
        {"id": uuid.uuid4(), **article}
        for article in ARTICLES
        if str(article["code"]) not in existing_codes
    ]
    if not rows:
        return

    dds_articles = sa.table(
        "dds_articles",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("code", sa.String),
        sa.column("name", sa.String),
        sa.column("movement_type", sa.String),
        sa.column("activity_type", sa.String),
        sa.column("description", sa.Text),
    )
    op.bulk_insert(dds_articles, rows)


def _seed_article_aliases() -> None:
    bind = op.get_bind()
    article_ids = {
        row["code"]: row["id"]
        for row in bind.execute(
            sa.text("SELECT code, id FROM dds_articles WHERE code = ANY(:codes)"),
            {"codes": [alias["article_code"] for alias in ARTICLE_ALIASES]},
        ).mappings()
    }
    existing_aliases = {
        row[0]
        for row in bind.execute(
            sa.text(
                """
                SELECT lower(alias)
                FROM dds_article_aliases
                WHERE lower(alias) = ANY(:aliases)
                """
            ),
            {"aliases": [str(alias["alias"]).lower() for alias in ARTICLE_ALIASES]},
        )
    }
    rows = [
        {
            "id": uuid.uuid4(),
            "article_id": article_ids[alias["article_code"]],
            "alias": alias["alias"],
            "source": alias["source"],
        }
        for alias in ARTICLE_ALIASES
        if str(alias["alias"]).lower() not in existing_aliases
        and alias["article_code"] in article_ids
    ]
    if not rows:
        return

    dds_article_aliases = sa.table(
        "dds_article_aliases",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("article_id", postgresql.UUID(as_uuid=True)),
        sa.column("alias", sa.String),
        sa.column("source", sa.String),
    )
    op.bulk_insert(dds_article_aliases, rows)


def _seed_classification_rules() -> None:
    bind = op.get_bind()
    existing_names = {
        row[0]
        for row in bind.execute(
            sa.text("SELECT name FROM classification_rules WHERE name = ANY(:names)"),
            {"names": [rule["name"] for rule in CLASSIFICATION_RULES]},
        )
    }
    article_ids = {
        row["code"]: row["id"]
        for row in bind.execute(
            sa.text("SELECT code, id FROM dds_articles WHERE code = ANY(:codes)"),
            {
                "codes": [
                    rule["article_code"]
                    for rule in CLASSIFICATION_RULES
                    if rule["article_code"] is not None
                ]
            },
        ).mappings()
    }
    for rule in CLASSIFICATION_RULES:
        if rule["name"] in existing_names:
            continue
        bind.execute(
            sa.text(
                """
                INSERT INTO classification_rules (
                    id, name, priority, is_active, provider, direction,
                    counterparty_name_pattern, purpose_pattern, action, article_id, comment
                )
                VALUES (
                    :id, :name, :priority, :is_active, :provider, :direction,
                    :counterparty_name_pattern, :purpose_pattern, :action, :article_id, :comment
                )
                """
            ),
            {
                "id": uuid.uuid4(),
                "name": rule["name"],
                "priority": rule["priority"],
                "is_active": rule["is_active"],
                "provider": rule["provider"],
                "direction": rule["direction"],
                "counterparty_name_pattern": rule["counterparty_name_pattern"],
                "purpose_pattern": rule["purpose_pattern"],
                "action": rule["action"],
                "article_id": article_ids.get(rule["article_code"]),
                "comment": rule["comment"],
            },
        )
