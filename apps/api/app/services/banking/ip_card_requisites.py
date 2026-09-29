"""Owner-approved recipient requisites for every bank → IP-card payout.

The database setting is kept for visibility and migration compatibility, but it is
not allowed to override the payment recipient at runtime.  This is deliberate:
changing a generic setting must never silently redirect payroll, Safe top-ups,
advances, deposits, or informal-counterparty payouts to another account.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AppSetting
from app.services.banking.payment_purpose import (
    OWNER_CARD_PAYMENT_PURPOSE,
    owner_card_payment_purpose,
)

logger = logging.getLogger(__name__)

PAYOUT_REQUISITES_KEY: Final = "payroll.bank_payout_requisites"

# OWNER-APPROVED FINANCIAL CONSTANT — DO NOT CHANGE WITHOUT AN EXPLICIT REQUEST
# FROM THE OWNER IN THE CURRENT TASK.  In particular, do not replace the recipient
# INN/KPP with the bank's INN/KPP and do not switch the account during refactors,
# migrations, provider work, or test cleanup.
#
# КПП intentionally does not belong to the canonical requisites. Bank payload
# builders add the protocol-required value "0" for an IP/individual themselves.
# The payment purpose intentionally describes a transfer of the owner's own funds.
# Do not replace it with payroll/salary wording without an explicit owner request.
OWNER_APPROVED_IP_CARD_REQUISITES: Final[Mapping[str, Any]] = MappingProxyType(
    {
        "recipientName": "Шокина Кристина Юрьевна",
        "inn": "890307589201",
        "bankAcnt": "40817810800023540968",
        "bankBik": "044525974",
        "bankName": 'АО "ТБанк"',
        "corrAccount": "30101810145250000974",
        "recipientCorrAccountNumber": "30101810145250000974",
        "executionOrder": 5,
        "paymentPurpose": OWNER_CARD_PAYMENT_PURPOSE,
    }
)


def owner_approved_ip_card_requisites() -> dict[str, Any]:
    """Return a mutable copy for a single bank-client call."""

    return dict(OWNER_APPROVED_IP_CARD_REQUISITES)


async def load_owner_approved_ip_card_requisites(
    session: AsyncSession,
) -> dict[str, Any]:
    """Return the code-locked recipient and report any database drift.

    AppSetting remains useful to show the value in settings and to keep existing
    deployments compatible.  It is intentionally diagnostic-only: even if a DB
    value is missing or was changed by a migration/agent, payment creation still
    uses the owner-approved constant above.
    """

    canonical = owner_approved_ip_card_requisites()
    setting = await session.scalar(
        select(AppSetting).where(AppSetting.key == PAYOUT_REQUISITES_KEY)
    )
    stored = setting.value if setting is not None and isinstance(setting.value, Mapping) else None
    # A previously stored purpose template is display metadata, not recipient drift.
    # The bank-facing description is enforced separately for every owner-card payment.
    purpose_keys = {"paymentPurpose", "paymentPurposeTemplate"}
    stored_recipient = (
        {key: value for key, value in stored.items() if key not in purpose_keys}
        if stored is not None
        else None
    )
    canonical_recipient = {
        key: value for key, value in canonical.items() if key not in purpose_keys
    }
    if stored_recipient != canonical_recipient:
        logger.error(
            "%s differs from the owner-approved code constant; ignoring database value",
            PAYOUT_REQUISITES_KEY,
        )
    return canonical


def bank_purpose_for_recipient(
    *, document_id: str, purpose: str, requisites: Mapping[str, Any]
) -> str:
    """Keep the owner-card rule at the provider boundary as well as in services."""
    account = str(
        requisites.get("bankAcnt")
        or requisites.get("bank_acnt")
        or requisites.get("payeeAccount")
        or ""
    )
    account = "".join(character for character in account if character.isdigit())
    if account == OWNER_APPROVED_IP_CARD_REQUISITES["bankAcnt"]:
        return owner_card_payment_purpose(document_id)
    return purpose
