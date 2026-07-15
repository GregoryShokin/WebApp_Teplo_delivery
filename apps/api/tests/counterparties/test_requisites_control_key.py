"""Контрольный разряд счёта получателя + осмысленный ответ на отказ банка.

Регресс прод-инцидента 2026-07-09: «Отправить в банк» по накладной ООО «ТОРА» падало
голой 500 — Т-Банк отклонял платёжку (422 «Неверный контрольный разряд счета»), а
``BankFetchError`` не ловился роутом. Теперь: битый счёт не проходит «подтверждение»
реквизитов, а отказ банка превращается в 422 с причиной (не 500).
"""

from __future__ import annotations

import pytest
from cp_helpers import make_counterparty, make_invoice
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.v1.routes.counterparties import _bank_rejected
from app.models import CounterpartyPaymentDraft
from app.services.banking.exceptions import BankCredentialsError, BankFetchError
from app.services.banking.requisites import account_control_key_valid, payee_account_error
from app.services.counterparty_payments import create_payment_draft_for_invoices
from app.services.counterparty_registry import CounterpartyRegistryError, set_requisites

# Валидная пара р/с + БИК (контрольный разряд сходится).
VALID_ACCOUNT = "40702810200000012345"
VALID_BIK = "044525225"
# Реальные реквизиты ООО «ТОРА» из прод-инцидента: р/с с битым контрольным разрядом.
INVALID_ACCOUNT = "40702810826000036193"
INVALID_BIK = "044525974"


# --- контрольный разряд (чистая функция) --------------------------------------


def test_control_key_valid_for_correct_pair() -> None:
    assert account_control_key_valid(VALID_ACCOUNT, VALID_BIK) is True


def test_control_key_invalid_for_incident_pair() -> None:
    assert account_control_key_valid(INVALID_ACCOUNT, INVALID_BIK) is False


def test_control_key_rejects_bad_length() -> None:
    assert account_control_key_valid("123", VALID_BIK) is False
    assert account_control_key_valid(VALID_ACCOUNT, "0445") is False


def test_control_key_ignores_separators() -> None:
    assert account_control_key_valid("4070 2810 2000 0001 2345", VALID_BIK) is True


def test_payee_account_error_flags_invalid() -> None:
    message = payee_account_error({"bankAcnt": INVALID_ACCOUNT, "bankBik": INVALID_BIK})
    assert message is not None
    assert "контрольн" in message.lower()


def test_payee_account_error_none_for_valid() -> None:
    assert payee_account_error({"bankAcnt": VALID_ACCOUNT, "bankBik": VALID_BIK}) is None


def test_payee_account_error_none_when_incomplete() -> None:
    assert payee_account_error({}) is None
    assert payee_account_error({"bankBik": VALID_BIK}) is None


# --- отказ банка → HTTP (роут-хелпер) -----------------------------------------


def test_bank_rejected_maps_4xx_to_422_with_reason() -> None:
    exc = BankFetchError(
        "tbank",
        "T-Bank API returned 422",
        status_code=422,
        detail="Счет получателя — Неверный контрольный разряд счета",
    )
    http = _bank_rejected(exc)
    assert http.status_code == 422
    assert "Неверный контрольный разряд" in http.detail


def test_bank_rejected_maps_credentials_to_502() -> None:
    http = _bank_rejected(BankCredentialsError("tbank", "invalid token"))
    assert http.status_code == 502


def test_bank_rejected_maps_unknown_to_502() -> None:
    http = _bank_rejected(BankFetchError("tbank", "network down"))
    assert http.status_code == 502


# --- «подтверждение» реквизитов не пускает битый счёт -------------------------


async def test_set_requisites_rejects_invalid_account_when_verified(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name='ООО "ТОРА"', inn="6165233720")
        await session.commit()

        with pytest.raises(CounterpartyRegistryError, match="контрольный разряд"):
            await set_requisites(
                session,
                cp.id,
                requisites={"bankAcnt": INVALID_ACCOUNT, "bankBik": INVALID_BIK},
                verified=True,
                actor_user_id=None,
            )


async def test_set_requisites_allows_invalid_account_when_unverified(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="ООО Черновик", inn="6165233720")
        await session.commit()

        profile = await set_requisites(
            session,
            cp.id,
            requisites={"bankAcnt": INVALID_ACCOUNT, "bankBik": INVALID_BIK},
            verified=False,
            actor_user_id=None,
        )
        assert profile.requisites_verified is False


async def test_set_requisites_accepts_valid_account_when_verified(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="ООО Валидный", inn="7701234567")
        await session.commit()

        profile = await set_requisites(
            session,
            cp.id,
            # Набор полный: подтвердить можно только то, чем реально можно заплатить.
            requisites={
                "bankAcnt": VALID_ACCOUNT,
                "bankBik": VALID_BIK,
                "inn": "7701234567",
                "recipientCorrAccountNumber": "30101810400000000225",
            },
            verified=True,
            actor_user_id=None,
        )
        assert profile.requisites_verified is True


async def test_set_requisites_rejects_verified_with_incomplete_requisites(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Официального поставщика нельзя пометить «реквизиты проверены» на пустом наборе.

    Раньше проходило: контрагента, которого нельзя СОЗДАТЬ без реквизитов, можно было
    получить правкой — с отметкой о проверке поверх пустоты. Гард «реквизиты не
    подтверждены» после этого пропускал платёж в банк.
    """
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="ООО Пустой", inn="7701234599")
        await session.commit()

        with pytest.raises(CounterpartyRegistryError, match="обязательные реквизиты"):
            await set_requisites(session, cp.id, requisites={}, verified=True, actor_user_id=None)


async def test_set_requisites_allows_incomplete_requisites_when_unverified(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Черновик реквизитов сохранять можно — гард стоит на подтверждении, а не на вводе."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="ООО Наполовину", inn="7701234588")
        await session.commit()

        profile = await set_requisites(
            session,
            cp.id,
            requisites={"bankBik": VALID_BIK},
            verified=False,
            actor_user_id=None,
        )
        assert profile.requisites_verified is False
        assert profile.requisites == {"bankBik": VALID_BIK}


# --- отказ банка при отправке накладной не роняет запрос в 500 ----------------


class _RejectingBank:
    """Мок банк-клиента, который отклоняет черновик, как реальный Т-Банк на битых реквизитах."""

    provider = "tbank"

    async def create_payment_draft(self, **_kwargs: object) -> object:
        raise BankFetchError(
            "tbank",
            "T-Bank API returned 422",
            status_code=422,
            detail="Счет получателя — Неверный контрольный разряд счета",
        )


async def test_create_draft_reraises_bank_rejection_and_saves_failed_draft(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        supplier = await make_counterparty(
            session,
            name="ООО Поставщик",
            inn="7701234567",
            requisites={
                "bankAcnt": VALID_ACCOUNT,
                "bankBik": VALID_BIK,
                "recipientCorrAccountNumber": "30101810400000000225",
            },
            requisites_verified=True,
        )
        invoice = await make_invoice(session, counterparty_id=supplier.id, amount="500.00")
        await session.commit()

        with pytest.raises(BankFetchError):
            await create_payment_draft_for_invoices(
                session,
                invoice_ids=[invoice.id],
                actor_user_id=None,
                bank_client=_RejectingBank(),
            )

        failed = (
            await session.execute(
                select(CounterpartyPaymentDraft).where(
                    CounterpartyPaymentDraft.status == "failed"
                )
            )
        ).scalars().all()
        assert len(failed) == 1
        assert failed[0].counterparty_id == supplier.id
