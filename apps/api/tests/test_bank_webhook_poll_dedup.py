"""Дедуп вебхук↔поллинг по operationId и гейт авторизационных холдов.

operationId T-Банка — универсальный стабильный ключ операции (одинаков у выписки и
вебхука, подтверждено реальными данными). Одна операция, пришедшая из обоих источников,
остаётся одной строкой — баланс (= SUM(amount)) не задваивается. Авторизационные холды
(operationStatus=Authorization) не проведены и не пускаются в журнал ни поллингом, ни вебхуком.
"""

from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.models import BankOperation, ReconciliationCase
from app.scheduler import ingest_operations
from app.services.banking.tbank import (
    TbankClient,
    is_tbank_operation_hold,
    normalize_tbank_statement_row,
)


def _run(coro):
    return asyncio.run(coro)


def _statement_row(operation_id: str, *, amount: str = "50000") -> dict:
    return {
        "operationId": operation_id,
        "accountNumber": "40802810000000012345",
        "operationDate": "2026-06-23T11:48:06Z",
        "trxnPostDate": "2026-06-23T11:46:00Z",
        "typeOfOperation": "Credit",
        "operationAmount": amount,
        "accountAmount": amount,
        "rubleAmount": amount,
        "operationStatus": "Transaction",
        "documentNumber": "777",
        "payPurpose": "Отражение операции по договору 777",
        "description": "Оплата по договору 777",
        "payer": {
            "account": "40702810900000099999",
            "name": 'ООО "Контрагент"',
            "inn": "7700000000",
        },
        "receiver": {"account": "40802810000000012345", "name": "ИП Шокина Е.А."},
    }


def test_is_tbank_operation_hold() -> None:
    assert is_tbank_operation_hold({"operationStatus": "Authorization"}) is True
    assert is_tbank_operation_hold({"operationStatus": "authorization"}) is True  # casefold
    assert is_tbank_operation_hold({"operationStatus": "Transaction"}) is False
    assert is_tbank_operation_hold({}) is False  # нет статуса → не холд (ингестим)


@pytest.mark.asyncio
async def test_poll_statement_excludes_authorization_holds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEPLO_BANK_CLIENT_MODE", "mock")
    get_settings.cache_clear()
    operations = await TbankClient().fetch_statement(
        date_from=date(2026, 5, 26), date_to=date(2026, 5, 27)
    )
    ids = {op.provider_operation_id for op in operations}
    # Проведённые остаются, холд (Authorization) отфильтрован на уровне фетча.
    assert "tbank-20260527-005" in ids
    assert "tbank-20260527-hold" not in ids


def test_normalizer_keys_operation_by_operation_id() -> None:
    op = normalize_tbank_statement_row(
        _statement_row("op-xyz"), "40802810000000012345", date.today()
    )
    assert op.provider_operation_id == "op-xyz"
    assert op.direction == "in"
    assert op.amount == Decimal("50000")


async def _count(factory: async_sessionmaker[AsyncSession]) -> int:
    async with factory() as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(BankOperation)
                .where(BankOperation.provider == "tbank")
            )
            or 0
        )


def test_same_operation_id_dedup_across_sources(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    op_id = "shared-operation-id-1"

    async def _flow() -> None:
        # 1) вебхук принял операцию (нормализуем как строку выписки — один парсер)
        async with async_session_factory() as session:
            op = normalize_tbank_statement_row(
                _statement_row(op_id), "40802810000000012345", date.today()
            )
            await ingest_operations(session, provider="tbank", operations=[op])
            await session.commit()
        # 2) сверочный поллинг приносит ТУ ЖЕ операцию (тот же operationId)
        async with async_session_factory() as session:
            op = normalize_tbank_statement_row(
                _statement_row(op_id, amount="50000"), "40802810000000012345", date.today()
            )
            await ingest_operations(session, provider="tbank", operations=[op])
            await session.commit()

    _run(_flow())
    assert _run(_count(async_session_factory)) == 1  # одна строка, баланс не задвоился


def test_amount_change_on_classified_op_opens_reconciliation_case(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    op_id = "amount-change-1"
    acct = "40802810000000012345"

    async def _flow() -> None:
        async with async_session_factory() as session:
            op = normalize_tbank_statement_row(
                _statement_row(op_id, amount="50000"), acct, date.today()
            )
            await ingest_operations(session, provider="tbank", operations=[op])
            await session.commit()
        # Эмулируем «уже классифицирована» (как после ручной/авто разметки в журнал).
        async with async_session_factory() as session:
            row = (
                await session.scalars(
                    select(BankOperation).where(BankOperation.provider_operation_id == op_id)
                )
            ).one()
            row.classification_status = "classified"
            await session.commit()
        # Рефайр с ДРУГОЙ суммой (банк скорректировал проводку).
        async with async_session_factory() as session:
            op = normalize_tbank_statement_row(
                _statement_row(op_id, amount="49500"), acct, date.today()
            )
            await ingest_operations(session, provider="tbank", operations=[op])
            await session.commit()

    _run(_flow())

    async def _check() -> tuple[Decimal, ReconciliationCase | None]:
        async with async_session_factory() as session:
            row = (
                await session.scalars(
                    select(BankOperation).where(BankOperation.provider_operation_id == op_id)
                )
            ).one()
            case = await session.scalar(
                select(ReconciliationCase).where(
                    ReconciliationCase.kind == "operation_amount_changed",
                    ReconciliationCase.bank_operation_id == row.id,
                )
            )
            return row.amount, case

    amount, case = _run(_check())
    assert amount == Decimal("49500")  # баланс обновлён к правде банка
    assert case is not None  # расхождение баланс↔журнал зафлагано, не молча


def test_intra_batch_duplicate_operation_id_collapses(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Перекрывающиеся периоды выписки могут вернуть одну операцию дважды в одном батче.
    async def _flow() -> None:
        async with async_session_factory() as session:
            row = _statement_row("batch-dup-1")
            ops = [
                normalize_tbank_statement_row(row, "40802810000000012345", date.today()),
                normalize_tbank_statement_row(row, "40802810000000012345", date.today()),
            ]
            await ingest_operations(session, provider="tbank", operations=ops)
            await session.commit()

    _run(_flow())
    assert _run(_count(async_session_factory)) == 1
