from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from conftest_guard import TEST_DATABASE_URL
from fastapi.testclient import TestClient
from sqlalchemy import MetaData, Table, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from alembic import command

os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ.setdefault("TEPLO_BANK_CLIENT_MODE", "mock")
os.environ["TEPLO_ADMIN_EMAIL"] = "admin@teplo.local"
os.environ["TEPLO_ADMIN_PASSWORD"] = "test-admin-password"
# Планировщик в тестах ОБЯЗАН молчать: TestClient поднимает lifespan приложения, и живые
# джобы (agent_run-журнал, синки) пишут в тестовую БД параллельно с teardown-транкейтом —
# отсюда плавающие deadlock'и в случайных тестах (в полном прогоне — десятки ERROR'ов).
os.environ["SCHEDULER_ENABLED"] = "false"

from app.core.config import get_settings
from app.db.session import get_session
from app.main import create_app
from app.services import iiko_directory, iiko_location, position_registry
from app.services.couriers import iiko_attendance_sync
from app.services.sbis import client as sbis_client

API_DIR = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class BaselineSnapshot:
    tables: list[Table]
    rows_by_table: dict[str, list[dict[str, Any]]]


@pytest.fixture(scope="session")
def postgres_available() -> None:
    try:
        asyncio.run(_ping_database(TEST_DATABASE_URL))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"PostgreSQL test database is not available: {exc}")


@pytest.fixture(scope="session")
def alembic_cfg_session() -> Config:
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
    os.environ["TEPLO_BANK_CLIENT_MODE"] = "mock"
    os.environ["TEPLO_ADMIN_EMAIL"] = "admin@teplo.local"
    os.environ["TEPLO_ADMIN_PASSWORD"] = "test-admin-password"
    get_settings.cache_clear()
    cfg = Config(str(API_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(API_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    return cfg


@pytest.fixture()
def alembic_cfg(alembic_cfg_session: Config) -> Config:
    return alembic_cfg_session


@pytest.fixture(scope="session")
def _test_db_migrated(
    alembic_cfg_session: Config,
    postgres_available: None,
) -> Iterator[str]:
    asyncio.run(_reset_public_schema(TEST_DATABASE_URL))
    command.upgrade(alembic_cfg_session, "head")
    try:
        yield TEST_DATABASE_URL
    finally:
        asyncio.run(_reset_public_schema(TEST_DATABASE_URL))


@pytest.fixture(scope="session")
def _session_engine(_test_db_migrated: str):
    engine = create_async_engine(_test_db_migrated, poolclass=NullPool)
    yield engine
    asyncio.run(engine.dispose())


@pytest.fixture(scope="session")
def _baseline_snapshot(_session_engine) -> BaselineSnapshot:
    return asyncio.run(_capture_baseline(_session_engine))


@pytest.fixture()
def migrated_db(_test_db_migrated: str) -> str:
    return _test_db_migrated


@pytest.fixture()
def async_session_factory(
    _session_engine,
) -> Iterator[async_sessionmaker[AsyncSession]]:
    factory = async_sessionmaker(_session_engine, expire_on_commit=False)
    yield factory


@pytest.fixture()
def client(_session_engine) -> Iterator[TestClient]:
    session_factory = async_sessionmaker(_session_engine, expire_on_commit=False)
    app = create_app()

    async def override_session() -> AsyncIterator[Any]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.pop(get_session, None)


def _reset_process_global_caches() -> None:
    """Сбросить кэши, которые живут в памяти процесса, а не в БД.

    ``_truncate_between_tests`` чистит таблицы, но не память: снапшот реестра должностей,
    id точки iiko, справочник iiko, курьерские роли, токен СБИС и lru_cache настроек
    переживают тест и текут в следующий. Кто об этом знал — боролся сам (module-level
    autouse-фикстуры и ``try/finally`` в теле теста), кто не знал — молча зависел от
    порядка запуска. Здесь это делается один раз для всех.

    В приватные имена модулей лезем намеренно: это единственное место, где тесты знают
    про внутренности кэшей, зато продовый код не носит test-only хелперов.
    """
    position_registry._snapshot = position_registry._DEFAULT_SNAPSHOT
    position_registry._loaded_at = None
    iiko_location._cache.clear()
    iiko_directory._cache = None
    iiko_attendance_sync._courier_role_cache = None
    sbis_client._token_cache.clear()
    # Настройки перечитываются из env: тесты правят env через monkeypatch и зовут
    # cache_clear() уже ПОСЛЕ setenv — без сброса на входе мок доживал до чужого теста.
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clean_process_global_caches() -> Iterator[None]:
    _reset_process_global_caches()
    yield
    _reset_process_global_caches()


@pytest.fixture(autouse=True)
async def _truncate_between_tests(
    _session_engine,
    alembic_cfg_session: Config,
    _baseline_snapshot: BaselineSnapshot,
):
    yield

    if await _database_at_head(_session_engine, alembic_cfg_session):
        await _restore_baseline(_session_engine, _baseline_snapshot)
        return

    await _session_engine.dispose()
    await _reset_public_schema(TEST_DATABASE_URL)
    command.upgrade(alembic_cfg_session, "head")
    await _restore_baseline(_session_engine, _baseline_snapshot)


async def _ping_database(database_url: str) -> None:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    finally:
        await engine.dispose()


async def _reset_public_schema(database_url: str) -> None:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
            await conn.execute(text("GRANT ALL ON SCHEMA public TO public"))
    finally:
        await engine.dispose()


async def _database_at_head(engine, alembic_cfg: Config) -> bool:
    head_revisions = set(ScriptDirectory.from_config(alembic_cfg).get_heads())
    async with engine.connect() as conn:
        has_version_table = await conn.scalar(text("SELECT to_regclass('public.alembic_version')"))
        if not has_version_table:
            return False

        result = await conn.execute(text("SELECT version_num FROM alembic_version"))
        current_revisions = {row[0] for row in result.fetchall()}

    return current_revisions == head_revisions


async def _capture_baseline(engine) -> BaselineSnapshot:
    metadata = MetaData()
    async with engine.begin() as conn:
        table_names = await _public_table_names(conn)
        await conn.run_sync(
            lambda sync_conn: metadata.reflect(
                sync_conn,
                schema="public",
                only=table_names,
            )
        )
        tables = [table for table in metadata.sorted_tables if table.name != "alembic_version"]
        rows_by_table: dict[str, list[dict[str, Any]]] = {}
        for table in tables:
            rows = await conn.execute(select(table))
            rows_by_table[table.name] = [dict(row._mapping) for row in rows.fetchall()]

    return BaselineSnapshot(tables=tables, rows_by_table=rows_by_table)


async def _restore_baseline(engine, snapshot: BaselineSnapshot) -> None:
    async with engine.begin() as conn:
        await _truncate_tables(conn)
        for table in snapshot.tables:
            rows = snapshot.rows_by_table.get(table.name, [])
            if rows:
                await conn.execute(table.insert(), rows)
        await _reset_owned_sequences(conn)


async def _public_table_names(conn) -> list[str]:
    result = await conn.execute(
        text(
            """
            SELECT tablename
              FROM pg_tables
             WHERE schemaname = 'public'
             ORDER BY tablename
            """
        )
    )
    return [row[0] for row in result.fetchall()]


async def _truncate_tables(conn) -> None:
    tables = [table for table in await _public_table_names(conn) if table != "alembic_version"]
    if tables:
        quoted_tables = ", ".join(_quote_identifier(table) for table in tables)
        await conn.execute(text(f"TRUNCATE TABLE {quoted_tables} RESTART IDENTITY CASCADE"))


async def _reset_owned_sequences(conn) -> None:
    result = await conn.execute(
        text(
            """
            SELECT
                seq_ns.nspname AS sequence_schema,
                seq.relname AS sequence_name,
                table_ns.nspname AS table_schema,
                table_class.relname AS table_name,
                attr.attname AS column_name
            FROM pg_class seq
            JOIN pg_namespace seq_ns ON seq_ns.oid = seq.relnamespace
            JOIN pg_depend dep ON dep.objid = seq.oid AND dep.deptype = 'a'
            JOIN pg_class table_class ON table_class.oid = dep.refobjid
            JOIN pg_namespace table_ns ON table_ns.oid = table_class.relnamespace
            JOIN pg_attribute attr
              ON attr.attrelid = table_class.oid
             AND attr.attnum = dep.refobjsubid
            WHERE seq.relkind = 'S'
              AND table_ns.nspname = 'public'
            """
        )
    )
    for row in result.fetchall():
        sequence_name = _quote_qualified(row.sequence_schema, row.sequence_name)
        table_name = _quote_qualified(row.table_schema, row.table_name)
        column_name = _quote_identifier(row.column_name)
        sequence_literal = sequence_name.replace("'", "''")
        await conn.execute(
            text(
                f"""
                SELECT setval(
                    '{sequence_literal}'::regclass,
                    COALESCE((SELECT MAX({column_name}) FROM {table_name}), 1),
                    (SELECT MAX({column_name}) FROM {table_name}) IS NOT NULL
                )
                """
            )
        )


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _quote_qualified(schema: str, identifier: str) -> str:
    return f"{_quote_identifier(schema)}.{_quote_identifier(identifier)}"
