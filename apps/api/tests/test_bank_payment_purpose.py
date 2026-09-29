"""Bank-facing owner-card purpose and document identity contract."""

from __future__ import annotations

import json
import re
import uuid
from decimal import Decimal

import httpx
import pytest

from app.core.config import get_settings
from app.services.banking.ip_card_requisites import owner_approved_ip_card_requisites
from app.services.banking.payment_purpose import (
    extract_payment_match_markers,
    is_owner_card_payment_purpose,
    owner_card_payment_purpose,
    payment_match_marker,
)
from app.services.banking.sber import SberClient
from app.services.banking.tbank import build_payment_draft_api_payload


def test_owner_card_documents_have_generic_purpose_and_separate_stable_codes() -> None:
    payment_id = uuid.uuid4()
    documents = [
        f"teplo-cp-{payment_id}",
        f"teplo-payroll-{payment_id}",
        f"teplo-payroll-{payment_id}-retry-1",
        f"teplo-payroll-{payment_id}-retry-2",
        f"teplo-payroll-{payment_id}-topup-1",
        f"teplo-advance-{payment_id}",
        f"teplo-emppayout-{payment_id}",
        f"teplo-deposit-{payment_id}",
        "teplo-courier-deposit-123",
    ]
    purposes = [owner_card_payment_purpose(document) for document in documents]
    assert len(set(purposes)) == len(documents)
    for document, purpose in zip(documents, purposes, strict=True):
        assert re.fullmatch(
            r"Вывод собственных средств на карту ИП \[TPL-[0-9A-F]{12}\]", purpose
        )
        assert len(purpose) <= 210
        assert purpose == owner_card_payment_purpose(document)
    assert payment_match_marker(documents[0]) == f"[TPL-{payment_id.hex[:12].upper()}]"


def test_returned_marker_accepts_bank_case_and_spacing() -> None:
    purpose = "  вывод собственных средств на карту ИП  [ tpl - aBcDeF012345 ] "
    assert extract_payment_match_markers(purpose) == frozenset({"[TPL-ABCDEF012345]"})
    assert is_owner_card_payment_purpose(purpose)
    assert not is_owner_card_payment_purpose("Оплата поставщику [TPL-ABCDEF012345]")
    assert extract_payment_match_markers("[TPL-ABC] Неполный код") == frozenset()
    with pytest.raises(ValueError, match="identifier"):
        owner_card_payment_purpose("  ")


@pytest.mark.parametrize("owner_card", [True, False])
def test_tbank_payload_enforces_owner_card_purpose_only_for_owner_recipient(
    owner_card: bool,
) -> None:
    requisites = owner_approved_ip_card_requisites()
    if not owner_card:
        requisites["bankAcnt"] = "40702810400000012349"
    document = f"teplo-deposit-{uuid.uuid4()}"
    original = "Выдача депозита Иванову. НДС не облагается"
    payload = build_payment_draft_api_payload(
        document_id=document,
        amount=Decimal("2000.00"),
        purpose=original,
        requisites=requisites,
        payer_account="40802810100002438573",
    )
    assert payload["paymentPurpose"] == (
        owner_card_payment_purpose(document) if owner_card else original
    )
    assert payload["bankAcnt"] == requisites["bankAcnt"]
    assert payload["amount"] == 2000


@pytest.mark.parametrize("owner_card", [True, False])
@pytest.mark.parametrize("account_key", ["bankAcnt", "payeeAccount"])
async def test_sber_request_enforces_owner_card_purpose_only_for_owner_recipient(
    owner_card: bool, account_key: str
) -> None:
    sent: list[dict] = []

    class RecordingSberClient(SberClient):
        async def _payer_requisites(self) -> dict:
            return {
                "payerName": "ИП Шокина Кристина Юрьевна",
                "payerInn": "890307589201",
                "payerBankBic": "046015602",
                "payerBankCorrAccount": "30101810600000000602",
            }

        async def _authorized_client(self) -> httpx.AsyncClient:
            def handler(request: httpx.Request) -> httpx.Response:
                payload = json.loads(request.content)
                sent.append(payload)
                return httpx.Response(200, json={"externalId": payload["externalId"]})

            return httpx.AsyncClient(
                base_url="https://bank.invalid", transport=httpx.MockTransport(handler)
            )

    settings = get_settings().model_copy(update={"teplo_bank_client_mode": "live"})
    bank = RecordingSberClient(settings=settings)
    requisites = owner_approved_ip_card_requisites()
    if not owner_card:
        requisites["bankAcnt"] = "40702810400000012349"
    document = f"teplo-deposit-{uuid.uuid4()}"
    original = "Выдача депозита Иванову. НДС не облагается"
    recipient_account = requisites.pop("bankAcnt")
    requisites[account_key] = recipient_account
    result = await bank.create_payment_draft(
        document_id=document,
        amount=Decimal("2000.00"),
        purpose=original,
        requisites=requisites,
        payer_account="40802810252090056194",
    )
    assert len(sent) == 1
    assert sent[0]["purpose"] == (
        owner_card_payment_purpose(document) if owner_card else original
    )
    assert sent[0]["payeeAccount"] == recipient_account
    assert Decimal(str(sent[0]["amount"])) == Decimal("2000.00")
    assert result.provider_ref == sent[0]["externalId"]
