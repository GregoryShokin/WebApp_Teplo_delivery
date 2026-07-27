"""Живая выписка T-Bank: чужой счёт в нашем реестре не должен валить весь проход.

Реальный случай 27.07.2026: в ``account`` осталась строка демо-счёта 40702…0002, на который
банк отвечает 422 ``NO_EXISTING_ACCOUNT``. Одна такая строка обрывала выборку по ВСЕМ счетам
— выписка не приходила вообще, налоговый факт-слой стоял пустой.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from app.services.banking.base import AccountMeta
from app.services.banking.exceptions import BankFetchError
from app.services.banking.tbank import TbankClient, _is_unknown_account_error

LIVE_ACCOUNT = "40802810100002438573"
STALE_ACCOUNT = "40702810800000000002"


def _row(account: str) -> dict[str, Any]:
    return {
        "operationId": "op-1",
        "accountNumber": account,
        "operationDate": "2026-06-23T10:00:00Z",
        "typeOfOperation": "Debit",
        "amount": "15209.20",
        "currency": "RUB",
        "documentNumber": "287",
        "paymentPurpose": "Единый налоговый платеж",
        "receiver": {"name": "Казначейство России (ФНС России)", "inn": "7727406020"},
    }


class _StubClient(TbankClient):
    """Клиент с подменённым транспортом: один счёт живой, второй банку неизвестен."""

    def __init__(self, accounts: list[str]) -> None:
        super().__init__(session=None)
        self._accounts = accounts
        self.asked: list[str] = []

    async def fetch_account_metadata(self) -> list[AccountMeta]:
        return [AccountMeta(account_number=number) for number in self._accounts]

    async def _get_json(self, client: Any, path: str, params: dict[str, Any]) -> Any:
        account = str(params["accountNumber"])
        self.asked.append(account)
        if account == STALE_ACCOUNT:
            raise BankFetchError(
                "tbank",
                "T-Bank API returned 422",
                status_code=422,
                detail="NO_EXISTING_ACCOUNT",
            )
        return {"operations": [_row(account)]}


def test_unknown_account_error_detected_by_code() -> None:
    unknown = BankFetchError("tbank", "x", status_code=422, detail="NO_EXISTING_ACCOUNT")
    other_422 = BankFetchError("tbank", "x", status_code=422, detail="VALIDATION_ERROR")
    server = BankFetchError("tbank", "x", status_code=500, detail=None)
    assert _is_unknown_account_error(unknown)
    assert not _is_unknown_account_error(other_422)
    assert not _is_unknown_account_error(server)


async def test_stale_account_is_skipped_and_live_one_is_fetched(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.banking.tbank.required_credential",
        _fake_credential,
    )
    client = _StubClient([STALE_ACCOUNT, LIVE_ACCOUNT])

    operations = await client._fetch_live_statement(
        date_from=date(2026, 6, 1), date_to=date(2026, 6, 30)
    )

    assert [op.amount for op in operations] == [_decimal("15209.20")]
    assert client.asked == [STALE_ACCOUNT, LIVE_ACCOUNT]  # проход не оборвался


async def test_all_accounts_unknown_still_raises(monkeypatch) -> None:
    """Если живых счётов не осталось — это сбой настройки, а не мусор в реестре."""
    monkeypatch.setattr(
        "app.services.banking.tbank.required_credential",
        _fake_credential,
    )
    client = _StubClient([STALE_ACCOUNT])

    with pytest.raises(BankFetchError):
        await client._fetch_live_statement(
            date_from=date(2026, 6, 1), date_to=date(2026, 6, 30)
        )


async def _fake_credential(*args: Any, **kwargs: Any) -> str:
    return "token"


def _decimal(value: str):
    from decimal import Decimal

    return Decimal(value)
