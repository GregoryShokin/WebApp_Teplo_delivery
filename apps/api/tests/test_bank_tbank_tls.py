"""TLS до T-Банка: корень НУЦ Минцифры в доверии всех клиентов ``business.tbank.ru``.

Реальный случай 23–24.09.2026: Т-Банк перевёл ``business.tbank.ru`` на сертификат от
``Russian Trusted Sub CA``. Корня Минцифры нет в ``certifi`` → каждый запрос падал
``CERTIFICATE_VERIFY_FAILED``: выписка не приходила сутки, «Отправить в банк» отвечал
«Банк временно недоступен», а в логе api не было ни строчки о причине.
"""

from __future__ import annotations

import hashlib
import ssl
from datetime import date
from decimal import Decimal
from typing import Any

import certifi
import httpx
import pytest

from app.services.banking import tbank as tbank_module
from app.services.banking.base import AccountMeta
from app.services.banking.exceptions import BankFetchError
from app.services.banking.tbank import TbankClient
from app.services.banking.tls import RUSSIAN_TRUSTED_ROOT_CA, russian_trusted_ssl_context

# SHA-256 DER корня с Госуслуг; сверен с цепочкой business.tbank.ru и CA-бандлом Сбера.
ROOT_SHA256 = "d26d2d0231b7c39f92cc738512ba54103519e4405d68b5bd703e9788ca8ecf31"
ROOT_CN = "Russian Trusted Root CA"


def _bundled_root_der() -> bytes:
    pem = RUSSIAN_TRUSTED_ROOT_CA.read_text(encoding="ascii")
    start = pem.index("-----BEGIN CERTIFICATE-----")
    return ssl.PEM_cert_to_DER_cert(pem[start:])


def _subject_cns(cert: dict[str, Any]) -> set[str]:
    return {value for rdn in cert.get("subject", ()) for key, value in rdn if key == "commonName"}


def test_bundled_root_is_the_mintsifry_root_by_fingerprint() -> None:
    """Файл — ровно тот корень, что сверен по трём источникам; подмену ловит отпечаток."""
    assert hashlib.sha256(_bundled_root_der()).hexdigest() == ROOT_SHA256


def test_context_trusts_mintsifry_root_on_top_of_certifi() -> None:
    context = russian_trusted_ssl_context()
    roots = context.get_ca_certs()
    assert any(ROOT_CN in _subject_cns(cert) for cert in roots)

    # Международные УЦ никуда не делись: если банк вернёт прежний сертификат, связь не порвётся.
    certifi_only = ssl.create_default_context(cafile=certifi.where())
    assert len(roots) == len(certifi_only.get_ca_certs()) + 1
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_mintsifry_root_is_not_in_certifi() -> None:
    """Причина инцидента: без явного добавления корня httpx его не знает."""
    certifi_only = ssl.create_default_context(cafile=certifi.where())
    assert not any(ROOT_CN in _subject_cns(cert) for cert in certifi_only.get_ca_certs())


class _FakeResponse:
    def __init__(self, status_code: int = 200, payload: Any = None) -> None:
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = ""

    def json(self) -> Any:
        return self._payload


class _RecordingClient:
    """Подмена ``httpx.AsyncClient``: запоминает kwargs конструктора, в сеть не ходит."""

    created: list[dict[str, Any]] = []
    fail_with: Exception | None = None

    def __init__(self, **kwargs: Any) -> None:
        type(self).created.append(kwargs)

    async def __aenter__(self) -> _RecordingClient:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def post(self, path: str, **_kwargs: Any) -> _FakeResponse:
        if self.fail_with is not None:
            raise self.fail_with
        if path.endswith("/payment/status"):
            return _FakeResponse(payload={"result": [{"documentId": "doc-1", "status": "DRAFT"}]})
        return _FakeResponse(payload={"documentId": "doc-1", "status": "created"})

    async def get(self, _path: str, **_kwargs: Any) -> _FakeResponse:
        if self.fail_with is not None:
            raise self.fail_with
        return _FakeResponse(payload={"operations": []})


class _LiveTbank(TbankClient):
    def __init__(self) -> None:
        super().__init__(session=None)
        self.settings = self.settings.model_copy(update={"teplo_bank_client_mode": "live"})

    async def fetch_account_metadata(self) -> list[AccountMeta]:
        return [AccountMeta(account_number="40802810100002438573")]


@pytest.fixture
def recording_client(monkeypatch: pytest.MonkeyPatch) -> type[_RecordingClient]:
    _RecordingClient.created = []
    _RecordingClient.fail_with = None

    async def _fake_credential(*_args: Any, **_kwargs: Any) -> str:
        return "test-token"

    monkeypatch.setattr("app.services.banking.tbank.httpx.AsyncClient", _RecordingClient)
    monkeypatch.setattr("app.services.banking.tbank.required_credential", _fake_credential)
    return _RecordingClient


_REQUISITES = {
    "recipient_name": "ООО Ромашка",
    "inn": "7707083893",
    "kpp": "770701001",
    "bank_acnt": "40702810900000000001",
    "bik": "044525974",
    "corr_account": "30101810145250000974",
}


async def _call_every_tbank_endpoint(client: TbankClient) -> None:
    await client.create_payment_draft(
        document_id="doc-1",
        amount=Decimal("100.00"),
        purpose="Оплата по счёту 1",
        requisites=_REQUISITES,
        payer_account="40802810100002438573",
    )
    await client.get_payment_status("doc-1")
    await client.fetch_statement(date_from=date(2026, 9, 22), date_to=date(2026, 9, 24))


async def test_every_tbank_client_verifies_with_mintsifry_context(
    recording_client: type[_RecordingClient],
) -> None:
    """Черновик, статус и выписка — все три клиента получают один и тот же контекст."""
    await _call_every_tbank_endpoint(_LiveTbank())

    assert len(recording_client.created) == 3
    expected = russian_trusted_ssl_context()
    assert all(kwargs.get("verify") is expected for kwargs in recording_client.created)


async def test_payment_draft_transport_error_keeps_cause(
    recording_client: type[_RecordingClient],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Причина сетевого сбоя попадает и в лог api, и в ``last_error`` черновика."""
    # ``fileConfig`` из alembic/env.py (миграции тестовой базы) гасит уже созданные логгеры.
    monkeypatch.setattr(tbank_module.logger, "disabled", False)
    recording_client.fail_with = httpx.ConnectError(
        "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"
    )

    with (
        caplog.at_level("WARNING", logger="app.services.banking.tbank"),
        pytest.raises(BankFetchError) as excinfo,
    ):
        await _LiveTbank().create_payment_draft(
            document_id="doc-1",
            amount=Decimal("100.00"),
            purpose="Оплата по счёту 1",
            requisites=_REQUISITES,
            payer_account="40802810100002438573",
        )

    assert "CERTIFICATE_VERIFY_FAILED" in str(excinfo.value)
    assert excinfo.value.status_code is None  # → роут отдаёт 502, а не 422 «по реквизитам»
    assert "CERTIFICATE_VERIFY_FAILED" in caplog.text


async def test_statement_transport_error_is_bank_fetch_error(
    recording_client: type[_RecordingClient],
) -> None:
    """Сетевой сбой выписки — ``BankFetchError``: ``run_bank_sync_job`` изолирует провайдера."""
    recording_client.fail_with = httpx.ConnectError(
        "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"
    )

    with pytest.raises(BankFetchError) as excinfo:
        await _LiveTbank().fetch_statement(date_from=date(2026, 9, 22), date_to=date(2026, 9, 24))

    assert "CERTIFICATE_VERIFY_FAILED" in str(excinfo.value)
