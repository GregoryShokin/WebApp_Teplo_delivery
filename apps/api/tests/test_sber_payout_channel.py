"""Сбер как банк-плательщик payout-черновиков: клиент, payload, фабрика провайдера (DB-free).

Проверяет ядро контура «Сбербанк»: SberClient создаёт НЕподписанный черновик (mock), билдер
тела `/v1/payments` формирует обязательные поля по схеме Сбера, а фабрика по коду канала/банка
резолвит клиент и счёт-плательщик."""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal

from app.core.config import get_settings
from app.services.banking.payout import channel_provider, payer_account_for, payout_client_for
from app.services.banking.sber import SberClient, _sber_external_id, build_sber_payment_payload
from app.services.banking.tbank import TbankClient


def _run(coro):
    return asyncio.run(coro)


def _mock_settings():
    return get_settings().model_copy(update={"teplo_bank_client_mode": "mock"})


def test_sber_create_draft_mock_returns_unsigned_draft() -> None:
    client = SberClient(None, settings=_mock_settings())
    result = _run(
        client.create_payment_draft(
            document_id="teplo-deposit-abc",
            amount=Decimal("100.00"),
            purpose="Тест",
            requisites={},
            payer_account="40802810252090056194",
        )
    )
    assert result.status == "created"
    # provider_ref = детерминированный externalId (UUID) из document_id.
    assert result.provider_ref == _sber_external_id("teplo-deposit-abc")
    uuid.UUID(result.provider_ref)  # валидный UUID
    # У Сбера нет вебхука → в mock статус не читается.
    assert _run(client.get_payment_status(result.provider_ref)) is None


def test_build_sber_payment_payload_required_fields() -> None:
    payload = build_sber_payment_payload(
        external_id="11111111-1111-1111-1111-111111111111",
        amount=Decimal("12600.00"),
        purpose="Выдача депозита (через Сейф). НДС не облагается",
        payee={
            "recipientName": "Шокина Кристина Юрьевна",
            "inn": "7707083893",
            "kpp": "616143002",
            "bankAcnt": "40817810552095257243",
            "bankBik": "046015602",
            "recipientCorrAccountNumber": "30101810600000000602",
        },
        payer={
            "payerName": "ИНДИВИДУАЛЬНЫЙ ПРЕДПРИНИМАТЕЛЬ ШОКИНА КРИСТИНА ЮРЬЕВНА",
            "payerInn": "890307589201",
            "payerKpp": "0",
            "payerBankBic": "046015602",
            "payerBankCorrAccount": "30101810600000000602",
        },
        payer_account="40802810252090056194",
    )
    # Обязательные по схеме Сбера (иначе 400 VALIDATION_FAULT).
    assert payload["operationCode"] == "01"
    assert payload["priority"] == "5"
    assert payload["externalId"] == "11111111-1111-1111-1111-111111111111"
    assert payload["payerName"].startswith("ИНДИВИДУАЛЬНЫЙ")
    assert payload["payerAccount"] == "40802810252090056194"
    assert payload["payerBankBic"] == "046015602"
    assert payload["payeeAccount"] == "40817810552095257243"
    assert payload["payeeBankBic"] == "046015602"
    # Без блока подписи → банк создаёт документ в статусе «черновик».
    assert "digestSignatures" not in payload


def test_channel_provider_mapping() -> None:
    assert channel_provider("bank_draft") == "tbank"
    assert channel_provider("bank_draft_sber") == "sber"
    assert channel_provider("cash_tk") is None
    assert channel_provider("cash_safe") is None
    assert channel_provider(None) is None


def test_payout_client_for_provider() -> None:
    assert isinstance(payout_client_for("sber", None), SberClient)
    assert isinstance(payout_client_for("tbank", None), TbankClient)


def test_payer_account_for_provider() -> None:
    # С настроенными разными счетами провайдеры дают РАЗНЫЕ счета-плательщики.
    both = get_settings().model_copy(
        update={
            "tbank_api_account_number": "40802810100002438573",
            "sber_api_account_number": "40802810252090056194",
        }
    )
    assert payer_account_for(both, "sber") == "40802810252090056194"
    assert payer_account_for(both, "tbank") == "40802810100002438573"
    # Счёт не настроен + mock → заглушка (боевой не дёргается); + live → None.
    unset = {"sber_api_account_number": None, "tbank_api_account_number": None}
    mock = get_settings().model_copy(update={"teplo_bank_client_mode": "mock", **unset})
    assert payer_account_for(mock, "sber") == "00000000000000000000"
    live = get_settings().model_copy(update={"teplo_bank_client_mode": "live", **unset})
    assert payer_account_for(live, "sber") is None
