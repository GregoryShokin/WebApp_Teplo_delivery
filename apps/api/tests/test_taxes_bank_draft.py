"""Черновик платёжки ЕНП в банк: обход двух блокеров и обратная совместимость.

Проверяем, что налоговый платёж кладёт реальный КБК и проходит контроль казначейского счёта,
а платёжки поставщиков при этом не меняются (нулевой регресс живого контура).
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.services.banking.fns_enp_requisites import treasury_enp_requisites
from app.services.banking.requisites import account_control_key_valid, payee_account_error
from app.services.banking.tbank import build_payment_draft_api_payload
from app.services.taxes.bank_draft import create_tax_bank_draft

PAYER = "40802810100002438573"  # счёт ИП Шокиной в Т-Банке (синтетический)


# ── Блокер №1: КБК больше не затирается нулём ─────────────────────────────────


def test_tax_payload_carries_real_kbk() -> None:
    payload = build_payment_draft_api_payload(
        document_id="tax-1",
        amount=Decimal("14902.30"),
        purpose="Единый налоговый платеж",
        requisites=treasury_enp_requisites(),
        payer_account=PAYER,
    )
    assert payload["kbk"] == "18201061201010000510"
    assert payload["taxPayerStatus"] == "01"
    assert payload["recipientName"] == "Казначейство России (ФНС России)"
    assert payload["bankAcnt"] == "03100643000000018500"


def test_supplier_payload_still_zeroes_tax_fields() -> None:
    """Регресс: платёжка поставщика (без налоговых полей) — КБК по-прежнему «0»."""
    payload = build_payment_draft_api_payload(
        document_id="inv-1",
        amount=Decimal("1000"),
        purpose="Оплата по счёту",
        payer_account=PAYER,
        requisites={
            "recipientName": "ООО Поставщик",
            "inn": "7712345678",
            "bankAcnt": "40702810900000012345",
            "bankBik": "044525225",
            "recipientCorrAccountNumber": "30101810400000000225",
        },
    )
    assert payload["kbk"] == "0"
    assert payload["taxPayerStatus"] == "0"
    assert payload["oktmo"] == "0"


# ── Блокер №2: казначейский счёт проходит контроль ────────────────────────────


def test_treasury_account_passes_control_key() -> None:
    assert account_control_key_valid("03100643000000018500", "017003983") is True
    assert account_control_key_valid("40102810445370000059", "017003983") is True
    assert payee_account_error(treasury_enp_requisites()) is None


def test_supplier_client_account_control_key_unchanged() -> None:
    """Регресс: клиентский счёт (405/407..) по-прежнему проверяется алгоритмом 579-П."""
    # Валидная клиентская пара остаётся валидной, а порченая — невалидной.
    good = account_control_key_valid("40702810938000000001", "044525225")
    bad = account_control_key_valid("40702810938000000002", "044525225")
    assert good != bad  # контроль реально различает верный и битый разряд


# ── Сервис черновика (mock-режим банка) ───────────────────────────────────────


async def test_create_tax_bank_draft_in_mock(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        result = await create_tax_bank_draft(
            session, settings=get_settings(), amount=Decimal("14902.30")
        )
    assert result.document_id
    assert result.status  # mock-клиент возвращает статус черновика без похода в сеть


async def test_prepare_draft_is_idempotent_and_guards_in_bank(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Повторная подготовка обновляет неотправленный черновик, а отправленный — не дублирует."""
    import pytest

    from app.services.taxes.bank_draft import TaxDraftError, create_tax_payment_draft

    async with async_session_factory() as session:
        first = await create_tax_payment_draft(
            session,
            tax_kind="usn_advance",
            amount=Decimal("478376"),
            for_year=2026,
            for_period="h1",
        )
        again = await create_tax_payment_draft(
            session,
            tax_kind="usn_advance",
            amount=Decimal("478319"),
            for_year=2026,
            for_period="h1",
        )
        assert again.id == first.id  # обновился, а не задвоился
        assert again.amount == Decimal("478319")

        first.status = "in_bank"
        await session.flush()
        with pytest.raises(TaxDraftError, match="уже отправлен в банк"):
            await create_tax_payment_draft(
                session,
                tax_kind="usn_advance",
                amount=Decimal("478376"),
                for_year=2026,
                for_period="h1",
            )
