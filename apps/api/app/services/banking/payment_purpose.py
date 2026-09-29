"""Bank-facing descriptions and stable internal payment identifiers."""

from __future__ import annotations

import hashlib
import re
import uuid
from contextlib import suppress
from typing import Final

OWNER_CARD_PAYMENT_PURPOSE: Final = "Вывод собственных средств на карту ИП"
_MATCH_MARKER_RE = re.compile(r"\[\s*TPL\s*-\s*([0-9A-F]{12})\s*\]", re.IGNORECASE)


def payment_match_marker(document_id: str) -> str:
    """Identify a payment document, including payroll retry/top-up suffixes.

    Counterparty documents keep their existing marker so previously sent drafts
    remain matchable. Other document types use the full identifier rather than
    a run/employee identifier shared by several separate bank payments.
    """
    document_id = str(document_id).strip()
    if not document_id:
        raise ValueError("Payment document identifier is required")
    code = hashlib.sha256(document_id.encode("utf-8")).hexdigest()[:12]
    if document_id.startswith("teplo-cp-"):
        with suppress(ValueError):
            code = uuid.UUID(document_id.removeprefix("teplo-cp-")).hex[:12]
    return f"[TPL-{code.upper()}]"


def owner_card_payment_purpose(document_id: str) -> str:
    """A transfer of the owner's own funds, with no internal expense details."""
    return f"{OWNER_CARD_PAYMENT_PURPOSE} {payment_match_marker(document_id)}"


def extract_payment_match_markers(purpose: str | None) -> frozenset[str]:
    """Read bank-returned markers without depending on case or surrounding text."""
    return frozenset(f"[TPL-{code.upper()}]" for code in _MATCH_MARKER_RE.findall(purpose or ""))


def is_owner_card_payment_purpose(purpose: str | None) -> bool:
    """Recognize the common bank description independently of technical markers."""
    base = " ".join(_MATCH_MARKER_RE.sub("", purpose or "").split())
    return base.casefold() == OWNER_CARD_PAYMENT_PURPOSE.casefold()
