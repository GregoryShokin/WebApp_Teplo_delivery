from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from app.core.config import get_settings
from app.db.session import get_session
from app.main import create_app

API_DIR = Path(__file__).resolve().parents[1]
TEST_DATABASE_URL = os.environ.get(
    "TEPLO_TEST_DATABASE_URL",
    os.environ.get("DATABASE_URL", "postgresql+asyncpg://teplo:teplo@localhost:5432/teplo"),
)

DDS_TABLES = {
    "source_credential",
    "account",
    "wallet",
    "own_accounts_registry",
    "dds_articles",
    "dds_article_aliases",
    "counterparty_alias",
    "bank_operations",
    "cashflow_transactions",
    "transfer_groups",
    "classification_rules",
    "wallet_balance_snapshots",
}

EXPECTED_COLUMNS = {
    "source_credential": {
        "provider",
        "credential_kind",
        "value_encrypted",
        "expires_at",
        "is_active",
        "metadata",
        "created_at",
        "updated_at",
    },
    "account": {"bank_code", "account_number", "bic", "legal_entity", "currency", "status"},
    "wallet": {"account_id", "opening_balance", "opening_balance_date"},
    "bank_operations": {
        "provider",
        "provider_operation_id",
        "operation_date",
        "classification_status",
        "cashflow_transaction_id",
    },
    "cashflow_transactions": {"wallet_id", "article_id", "transfer_group_id", "source_kind"},
    "classification_rules": {"priority", "action", "article_id", "purpose_pattern"},
}

EXPECTED_INDEXES = {
    "source_credential": {"uq_source_credential_active_kind"},
    "account": {"uq_account_bank_code_account_number"},
    "dds_article_aliases": {"uq_dds_article_aliases_alias_ci"},
    "counterparty_alias": {"uq_counterparty_alias_alias_ci"},
    "bank_operations": {
        "uq_bank_operations_provider_operation",
        "ix_bank_operations_operation_date",
        "ix_bank_operations_classification_status",
    },
    "cashflow_transactions": {
        "ix_cashflow_transactions_operation_date",
        "ix_cashflow_transactions_wallet_id",
        "ix_cashflow_transactions_article_id",
    },
    "wallet_balance_snapshots": {"uq_wallet_balance_snapshots_wallet_date_source"},
}


@pytest.fixture()
def postgres_available() -> None:
    try:
        asyncio.run(_ping_database(TEST_DATABASE_URL))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"PostgreSQL test database is not available: {exc}")


@pytest.fixture()
def alembic_cfg(monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    get_settings.cache_clear()
    cfg = Config(str(API_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(API_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    return cfg


@pytest.fixture()
def migrated_db(alembic_cfg: Config, postgres_available: None) -> str:
    command.downgrade(alembic_cfg, "base")
    command.upgrade(alembic_cfg, "head")
    try:
        yield TEST_DATABASE_URL
    finally:
        command.downgrade(alembic_cfg, "base")


@pytest.fixture()
def client(migrated_db: str) -> Iterator[TestClient]:
    engine = create_async_engine(migrated_db)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    app = create_app()

    async def override_session() -> AsyncIterator[Any]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    with TestClient(app) as test_client:
        yield test_client
    asyncio.run(engine.dispose())


def test_dds_tables_columns_and_indexes_exist(migrated_db: str) -> None:
    snapshot = asyncio.run(_schema_snapshot(migrated_db))

    assert snapshot["tables"] >= DDS_TABLES
    for table, columns in EXPECTED_COLUMNS.items():
        assert columns <= snapshot["columns"][table]
    for table, indexes in EXPECTED_INDEXES.items():
        assert indexes <= snapshot["indexes"][table]


def test_guarant_fund_wallet_seeded_with_opening_balance(migrated_db: str) -> None:
    row = asyncio.run(
        _fetch_one(
            migrated_db,
            "SELECT code, opening_balance FROM wallet WHERE code = 'guarant_fund'",
        )
    )

    assert row["code"] == "guarant_fund"
    assert row["opening_balance"] == 47200


def test_overdraft_fee_article_is_renamed_commission(migrated_db: str) -> None:
    row = asyncio.run(
        _fetch_one(migrated_db, "SELECT name FROM dds_articles WHERE code = 'overdraft_fee'")
    )

    assert row["name"] == "Овердрафт комиссия"


def test_customer_refund_article_is_outflow(migrated_db: str) -> None:
    row = asyncio.run(
        _fetch_one(
            migrated_db,
            "SELECT movement_type FROM dds_articles WHERE code = 'customer_refund'",
        )
    )

    assert row["movement_type"] == "outflow"


def test_classification_rules_seed_contains_base_rules(migrated_db: str) -> None:
    row = asyncio.run(_fetch_one(migrated_db, "SELECT count(*) AS count FROM classification_rules"))

    assert row["count"] >= 2


def test_get_dds_wallets_returns_seeded_wallets(client: TestClient) -> None:
    response = client.get("/api/v1/dds/wallets", headers={"X-User-Role": "finance_manager"})

    assert response.status_code == 200
    rows = response.json()
    assert len(rows) == 5
    assert {row["code"] for row in rows} == {
        "cash_safe",
        "guarant_fund",
        "sber_main",
        "tbank_main",
        "tk_chernikova",
    }


def test_get_dds_articles_returns_seeded_articles(client: TestClient) -> None:
    response = client.get("/api/v1/dds/articles", headers={"X-User-Role": "finance_manager"})

    assert response.status_code == 200
    rows = response.json()
    assert len(rows) >= 10
    assert "overdraft_fee" in {row["code"] for row in rows}


def test_bank_sync_stub_returns_accepted(client: TestClient) -> None:
    response = client.post(
        "/api/v1/dds/bank-sync/sber",
        headers={"X-User-Role": "finance_manager"},
        json={"date_from": "2026-05-01", "date_to": "2026-05-31"},
    )

    assert response.status_code == 202
    assert response.json()["status"] == "not_implemented"


async def _ping_database(database_url: str) -> None:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    finally:
        await engine.dispose()


async def _schema_snapshot(database_url: str) -> dict[str, Any]:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as conn:
            tables = {
                row[0]
                for row in (
                    await conn.execute(
                        text(
                            """
                            SELECT table_name
                            FROM information_schema.tables
                            WHERE table_schema = 'public'
                            """
                        )
                    )
                )
            }
            columns: dict[str, set[str]] = {}
            indexes: dict[str, set[str]] = {}
            for table in DDS_TABLES:
                columns[table] = {
                    row[0]
                    for row in (
                        await conn.execute(
                            text(
                                """
                                SELECT column_name
                                FROM information_schema.columns
                                WHERE table_schema = 'public' AND table_name = :table
                                """
                            ),
                            {"table": table},
                        )
                    )
                }
                indexes[table] = {
                    row[0]
                    for row in (
                        await conn.execute(
                            text(
                                """
                                SELECT indexname
                                FROM pg_indexes
                                WHERE schemaname = 'public' AND tablename = :table
                                """
                            ),
                            {"table": table},
                        )
                    )
                }
        return {"tables": tables, "columns": columns, "indexes": indexes}
    finally:
        await engine.dispose()


async def _fetch_one(database_url: str, query: str) -> dict[str, Any]:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(text(query))
            return dict(result.mappings().one())
    finally:
        await engine.dispose()
