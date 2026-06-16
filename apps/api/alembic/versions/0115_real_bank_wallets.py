"""real bank accounts and wallets (T-Bank x4, Sber, cash group)

Replaces the dev-placeholder accounts/wallets with the real structure agreed with the
owner:

- Tinkoff (group, API-fed via statement per account number):
  Рублёвый счёт, Бизнес-копилка, Гарант фонд, Накопительный фонд.
- Sber (group): расчётный счёт.
- Наличные (group, manual in-app, push to iiko): Сейф, Торговая касса Черникова.

All four T-Bank accounts are registered in own_accounts_registry with the real
legal-entity INN so intra-bank transfers (р/с ↔ копилка/гарант/накопительный) are
detected and matched. Balances are derived from movements; the bank ``otb`` balance
(inflated by the overdraft limit) is not used. Opening balances are the real snapshot
as of 2026-06-16.

Revision ID: 0115_real_bank_wallets
Revises: 0114_dds_articles_catalog
Create Date: 2026-06-16
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

from alembic import op
from sqlalchemy import text

revision = "0115_real_bank_wallets"
down_revision = "0114_dds_articles_catalog"
branch_labels = None
depends_on = None

INN = "890307589201"
LEGAL_ENTITY = "Индивидуальный предприниматель Шокина Кристина Юрьевна"
TBANK_BIC = "044525974"
SNAPSHOT_DATE = dt.date(2026, 6, 16)

# wallet_code -> (account_number, wallet_name, opening_balance)
TBANK_ACCOUNTS = {
    "tbank_main": ("40802810100002438573", "Тинькофф · Рублёвый счёт", Decimal("370721.31")),
    "tbank_kopilka": ("40802810500002492902", "Тинькофф · Бизнес-копилка", Decimal("0")),
    "guarant_fund": ("40802810000004059843", "Тинькофф · Гарант фонд", Decimal("47200")),
    "tbank_nakopit": ("40802810900006944739", "Тинькофф · Накопительный фонд", Decimal("99375.47")),
}
SBER_ACCOUNT_NUMBER = "40802810252090056194"
SBER_OPENING = Decimal("107042.93")  # closing balance as of 2026-06-16 (Sber API)
NEW_WALLET_CODES = ("tbank_kopilka", "tbank_nakopit")


def _account_id_for_wallet(conn, wallet_code: str):
    return conn.execute(
        text("SELECT account_id FROM wallet WHERE code = :code"), {"code": wallet_code}
    ).scalar()


def upgrade() -> None:
    conn = op.get_bind()

    # --- Tinkoff: main account (reuse existing tbank account behind tbank_main) ---
    main_account_id = _account_id_for_wallet(conn, "tbank_main")
    main_number, main_name, main_opening = TBANK_ACCOUNTS["tbank_main"]
    conn.execute(
        text(
            "UPDATE account SET account_number=:num, bic=:bic, legal_entity=:le,"
            " currency='RUB', status='active', updated_at=now() WHERE id=:id"
        ),
        {"num": main_number, "bic": TBANK_BIC, "le": LEGAL_ENTITY, "id": main_account_id},
    )
    conn.execute(
        text(
            "UPDATE wallet SET name=:name, type='bank', is_internal_transfer_eligible=true,"
            " opening_balance=:ob, opening_balance_date=:od WHERE code='tbank_main'"
        ),
        {"name": main_name, "ob": main_opening, "od": SNAPSHOT_DATE},
    )

    # --- Tinkoff: Гарант фонд (reuse existing tbank account behind guarant_fund) ---
    garant_account_id = _account_id_for_wallet(conn, "guarant_fund")
    garant_number, garant_name, garant_opening = TBANK_ACCOUNTS["guarant_fund"]
    conn.execute(
        text(
            "UPDATE account SET account_number=:num, bic=:bic, legal_entity=:le,"
            " currency='RUB', status='active', updated_at=now() WHERE id=:id"
        ),
        {"num": garant_number, "bic": TBANK_BIC, "le": LEGAL_ENTITY, "id": garant_account_id},
    )
    conn.execute(
        text(
            "UPDATE wallet SET name=:name, type='bank', is_internal_transfer_eligible=true,"
            " opening_balance=:ob, opening_balance_date=:od WHERE code='guarant_fund'"
        ),
        {"name": garant_name, "ob": garant_opening, "od": SNAPSHOT_DATE},
    )

    # --- Tinkoff: new accounts + wallets (Бизнес-копилка, Накопительный фонд) ---
    for wallet_code in NEW_WALLET_CODES:
        number, name, opening = TBANK_ACCOUNTS[wallet_code]
        account_id = str(uuid.uuid4())
        conn.execute(
            text(
                "INSERT INTO account (id, bank_code, account_number, bic, legal_entity,"
                " currency, status) VALUES (:id, 'tbank', :num, :bic, :le, 'RUB', 'active')"
            ),
            {"id": account_id, "num": number, "bic": TBANK_BIC, "le": LEGAL_ENTITY},
        )
        conn.execute(
            text(
                "INSERT INTO wallet (id, code, name, type, currency,"
                " is_internal_transfer_eligible, status, account_id, opening_balance,"
                " opening_balance_date)"
                " VALUES (:id, :code, :name, 'bank', 'RUB', true, 'active', :acc, :ob, :od)"
            ),
            {
                "id": str(uuid.uuid4()),
                "code": wallet_code,
                "name": name,
                "acc": account_id,
                "ob": opening,
                "od": SNAPSHOT_DATE,
            },
        )

    # --- Sber: real settlement account number on the existing account ---
    sber_account_id = _account_id_for_wallet(conn, "sber_main")
    conn.execute(
        text(
            "UPDATE account SET account_number=:num, legal_entity=:le, currency='RUB',"
            " status='active', updated_at=now() WHERE id=:id"
        ),
        {"num": SBER_ACCOUNT_NUMBER, "le": LEGAL_ENTITY, "id": sber_account_id},
    )
    conn.execute(
        text(
            "UPDATE wallet SET name='Сбербанк', type='bank', is_internal_transfer_eligible=true,"
            " opening_balance=:ob, opening_balance_date=:od WHERE code='sber_main'"
        ),
        {"ob": SBER_OPENING, "od": SNAPSHOT_DATE},
    )

    # --- Cash group naming ---
    conn.execute(text("UPDATE wallet SET name='Торговая касса Черникова' WHERE code='tk_chernikova'"))
    conn.execute(text("UPDATE wallet SET name='Сейф' WHERE code='cash_safe'"))

    # --- own_accounts_registry: rebuild clean for sber + tbank with real INN ---
    conn.execute(text("DELETE FROM own_accounts_registry WHERE bank_code IN ('sber', 'tbank')"))
    registry = [("sber", SBER_ACCOUNT_NUMBER, sber_account_id)]
    for wallet_code, (number, _name, _ob) in TBANK_ACCOUNTS.items():
        registry.append(("tbank", number, _account_id_for_wallet(conn, wallet_code)))
    for bank_code, number, account_id in registry:
        conn.execute(
            text(
                "INSERT INTO own_accounts_registry (id, bank_code, account_number, bic,"
                " legal_entity_inn, account_id, is_active, metadata)"
                " VALUES (:id, :bank, :num, :bic, :inn, :acc, true, :meta)"
            ),
            {
                "id": str(uuid.uuid4()),
                "bank": bank_code,
                "num": number,
                "bic": TBANK_BIC if bank_code == "tbank" else None,
                "inn": INN,
                "acc": account_id,
                "meta": '{"source": "owner_confirmed"}',
            },
        )


def downgrade() -> None:
    conn = op.get_bind()
    # Remove the two newly added Tinkoff wallets + their accounts and registry rows.
    for wallet_code in NEW_WALLET_CODES:
        account_id = _account_id_for_wallet(conn, wallet_code)
        conn.execute(text("DELETE FROM wallet WHERE code=:code"), {"code": wallet_code})
        if account_id is not None:
            conn.execute(
                text("DELETE FROM own_accounts_registry WHERE account_id=:id"), {"id": account_id}
            )
            conn.execute(text("DELETE FROM account WHERE id=:id"), {"id": account_id})
    # Field updates on reused rows are not reversed (data migration).
