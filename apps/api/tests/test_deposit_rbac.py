"""Гранулярные права депозитов + права на каналы выдачи (миграция 0135).

Проверяем РАЗДЕЛЕНИЕ через реальные guard'ы FastAPI:
- производство: выдача и списание — отдельные права; немедленная выдача требует ещё и право
  на канал (Сейф/ТК Черникова/банк-черновик);
- курьеры: пополнение/возврат/списание — отдельные права; возврат требует право на канал;
- зарплата: банк-черновик и выбор наличного счёта требуют право на канал.

Тесты граничные (403): проверяют, что без нужного права операция запрещена, а с нужным —
guard пропускает (дальше 404/409 по отсутствию сущности — значит проверка прав пройдена).
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_access_rights_backend_guards import _headers_for_permissions

DEPOSITS = "/api/v1/deposits"
COURIERS = "/api/v1/couriers"
PAYROLL = "/api/v1/payroll"


def _h(factory: async_sessionmaker[AsyncSession], email: str, codes: list[str]) -> dict[str, str]:
    return _headers_for_permissions(factory, email, codes)


# --- Производственные депозиты: выдача / списание / каналы ------------------------------


def test_production_payout_requires_payout_permission(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    # Только списание — выдача запрещена.
    writeoff_only = _h(
        async_session_factory,
        "prod-dep-writeoff@test.local",
        ["payroll.production_deposits.write_off", "finance.payout_channel.cash_tk"],
    )
    denied = client.post(
        f"{DEPOSITS}/{uuid.uuid4()}/payout",
        json={"payout_method": "cash_tk"},
        headers=writeoff_only,
    )
    assert denied.status_code == 403


def test_production_payout_requires_channel_permission(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    # Право выдавать есть, права на канал нет → 403.
    payout_no_channel = _h(
        async_session_factory,
        "prod-dep-payout-nochannel@test.local",
        ["payroll.production_deposits.payout"],
    )
    denied = client.post(
        f"{DEPOSITS}/{uuid.uuid4()}/payout",
        json={"payout_method": "cash_tk"},
        headers=payout_no_channel,
    )
    assert denied.status_code == 403

    # С правом на канал ТК guard пропускает (дальше 404 — сотрудника нет).
    payout_cash_tk = _h(
        async_session_factory,
        "prod-dep-payout-cashtk@test.local",
        ["payroll.production_deposits.payout", "finance.payout_channel.cash_tk"],
    )
    passed = client.post(
        f"{DEPOSITS}/{uuid.uuid4()}/payout",
        json={"payout_method": "cash_tk"},
        headers=payout_cash_tk,
    )
    assert passed.status_code == 404
    # Но другой канал (банк-черновик) тем же юзером запрещён.
    denied_other = client.post(
        f"{DEPOSITS}/{uuid.uuid4()}/payout",
        json={"payout_method": "bank_draft"},
        headers=payout_cash_tk,
    )
    assert denied_other.status_code == 403


def test_production_writeoff_requires_writeoff_permission(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    payout_only = _h(
        async_session_factory,
        "prod-dep-payout-only@test.local",
        ["payroll.production_deposits.payout", "finance.payout_channel.cash_tk"],
    )
    denied = client.post(
        f"{DEPOSITS}/{uuid.uuid4()}/writeoff",
        json={"amount": "100", "reason": "x"},
        headers=payout_only,
    )
    assert denied.status_code == 403


# --- Курьерские депозиты: пополнение / возврат / списание / каналы ----------------------


def test_courier_return_requires_return_permission(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    top_up_only = _h(
        async_session_factory,
        "courier-dep-topup@test.local",
        ["couriers.deposits.top_up", "finance.payout_channel.cash_tk"],
    )
    denied = client.post(
        f"{COURIERS}/{uuid.uuid4()}/deposit/transactions",
        json={
            "transaction_type": "return",
            "amount_cents": 1000,
            "transaction_date": "2026-06-01",
            "payout_method": "cash_tk",
        },
        headers=top_up_only,
    )
    assert denied.status_code == 403


def test_courier_return_requires_channel_permission(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    return_no_channel = _h(
        async_session_factory,
        "courier-dep-return-nochannel@test.local",
        ["couriers.deposits.return"],
    )
    denied = client.post(
        f"{COURIERS}/{uuid.uuid4()}/deposit/transactions",
        json={
            "transaction_type": "return",
            "amount_cents": 1000,
            "transaction_date": "2026-06-01",
            "payout_method": "cash_tk",
        },
        headers=return_no_channel,
    )
    assert denied.status_code == 403


def test_courier_topup_requires_topup_permission(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    forfeit_only = _h(
        async_session_factory,
        "courier-dep-forfeit@test.local",
        ["couriers.deposits.forfeit"],
    )
    denied = client.post(
        f"{COURIERS}/{uuid.uuid4()}/deposit/transactions",
        json={
            "transaction_type": "top_up",
            "amount_cents": 1000,
            "transaction_date": "2026-06-01",
        },
        headers=forfeit_only,
    )
    assert denied.status_code == 403


# --- Зарплатные каналы: банк-черновик / наличный счёт -----------------------------------


def test_payroll_bank_draft_requires_channel_permission(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    no_channel = _h(
        async_session_factory,
        "payroll-draft-nochannel@test.local",
        ["payroll.runs.bank_draft"],
    )
    denied = client.post(f"{PAYROLL}/runs/{uuid.uuid4()}/bank-draft", headers=no_channel)
    assert denied.status_code == 403

    with_channel = _h(
        async_session_factory,
        "payroll-draft-channel@test.local",
        ["payroll.runs.bank_draft", "finance.payout_channel.bank_draft"],
    )
    # Право на канал есть → guard пропускает (дальше 404 — прогона нет).
    passed = client.post(f"{PAYROLL}/runs/{uuid.uuid4()}/bank-draft", headers=with_channel)
    assert passed.status_code == 404


def test_payroll_cash_wallet_requires_channel_permission(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    # Может работать с черновиком, но нет права на канал «Сейф» → выбор Сейфа запрещён.
    no_safe = _h(
        async_session_factory,
        "payroll-cash-nosafe@test.local",
        ["payroll.runs.bank_draft", "finance.payout_channel.cash_tk"],
    )
    denied = client.patch(
        f"{PAYROLL}/runs/{uuid.uuid4()}/payout-cash",
        json={"amount_cash": "100", "cash_wallet_code": "cash_safe"},
        headers=no_safe,
    )
    assert denied.status_code == 403
