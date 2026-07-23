"""Webhook «Статус платежа» T-Банка: авторизация, сопоставление по provider_ref, гашение.

Статус платёжного документа доводит черновик по ``provider_ref`` — это основной путь.

На тот же URL приходит и тело «операция по счёту» (с ``operationId``): оно уходит в общий
ингест выписки (``ingested=true``) — иначе card-операции выпадают из баланса банка и пикера
карт-оплат Кассы (84406d4 после разворота 59b1451). Черновик такая операция закрывает только
при точном многофакторном матче в ``settle_counterparty_draft_from_operation`` (сумма,
назначение, счёт, documentNumber) — near-realtime доводка оплаты (8b91bc6). Анти-дубль ДДС —
prebooked-механизм классификатора.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date
from decimal import Decimal

from cp_helpers import make_counterparty, make_draft, make_invoice
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_payroll_payouts import create_payroll_run

from app.core.config import Settings, get_settings
from app.models import CounterpartyPaymentDraft, PayrollBankDraft, SupplierInvoice
from app.services.banking.tbank import _document_number

BASE = "/api/v1/webhooks/tbank/payment-status"


def _run(coro):
    return asyncio.run(coro)


async def _seed(factory, *, provider_ref: str = "pay-1") -> tuple[uuid.UUID, uuid.UUID]:
    async with factory() as session:
        cp = await make_counterparty(session, name="Поставщик")
        draft = await make_draft(session, counterparty_id=cp.id, amount="1000.00")
        draft.provider_ref = provider_ref
        await session.flush()
        inv = await make_invoice(
            session, counterparty_id=cp.id, amount="1000.00", draft_id=draft.id
        )
        await session.commit()
        return draft.id, inv.id


def test_webhook_settles_invoice_on_executed(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _, invoice_id = _run(_seed(async_session_factory))
    resp = client.post(BASE, json={"paymentId": "pay-1", "status": "executed"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["matched"] is True and body["draft_status"] == "paid"

    async def _check() -> str:
        async with async_session_factory() as session:
            inv = await session.get(SupplierInvoice, invoice_id)
            return inv.payment_status

    assert _run(_check()) == "paid"


def test_webhook_unknown_payment_acked_not_matched(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _run(_seed(async_session_factory))
    resp = client.post(BASE, json={"paymentId": "does-not-exist", "status": "executed"})
    assert resp.status_code == 200  # 200, чтобы банк не ретраил
    assert resp.json()["matched"] is False


def test_webhook_missing_payment_id_is_422(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _run(_seed(async_session_factory))
    resp = client.post(BASE, json={"status": "executed"})
    assert resp.status_code == 422


def test_webhook_is_idempotent(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _run(_seed(async_session_factory))
    first = client.post(BASE, json={"paymentId": "pay-1", "status": "executed"})
    second = client.post(BASE, json={"paymentId": "pay-1", "status": "executed"})
    assert first.status_code == 200 and second.status_code == 200
    assert second.json()["draft_status"] == "paid"


def test_webhook_deleted_status_reverts_invoice_to_unpaid(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Статус DELETED (черновик отозван в банке) → накладная возвращается в «неоплачено»
    (draft_id снят), черновик становится deleted. Деньги не двигались."""
    draft_id, invoice_id = _run(_seed(async_session_factory))
    resp = client.post(BASE, json={"paymentId": "pay-1", "status": "DELETED"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["draft_status"] == "deleted"

    async def _check() -> tuple[str, object]:
        async with async_session_factory() as session:
            inv = await session.get(SupplierInvoice, invoice_id)
            return inv.payment_status, inv.draft_id

    inv_status, inv_draft_id = _run(_check())
    assert inv_status != "paid"
    assert inv_draft_id is None  # накладная снова доступна к оплате


async def _seed_payroll_draft(factory) -> uuid.UUID:
    async with factory() as session:
        _period, run, _employees = await create_payroll_run(session)
        draft = PayrollBankDraft(
            id=uuid.uuid4(),
            run_id=run.id,
            document_id=f"teplo-payroll-{run.id}",
            amount=Decimal("1000.00"),
            status="created",
            bank_provider="tbank",
            provider_ref="payroll-pay-1",
        )
        session.add(draft)
        await session.commit()
        return draft.id


def test_webhook_deleted_payroll_draft_becomes_retriable(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    draft_id = _run(_seed_payroll_draft(async_session_factory))

    resp = client.post(BASE, json={"paymentId": "payroll-pay-1", "status": "DELETED"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["matched"] is True
    assert resp.json()["draft_status"] == "deleted"

    async def _check() -> tuple[str, str | None]:
        async with async_session_factory() as session:
            draft = await session.get(PayrollBankDraft, draft_id)
            return draft.status, draft.last_error

    assert _run(_check()) == ("deleted", "Черновик удалён в банке")


_PAYER_ACCOUNT = "40802810000000012345"
_DRAFT_PURPOSE = "Оплата поставщику по счёту 1"


def _debit_body(
    operation_id: str,
    *,
    doc_number: str = "654321",
    amount: str = "1000.00",
    purpose: str = _DRAFT_PURPOSE,
) -> dict:
    """Расходная операция по счёту (формат выписки T-Банка). На статусный URL такое тело
    приходит как строка выписки: вливается в bank_operations, а черновик доводит только при
    точном матче по сумме/назначению/счёту/documentNumber.

    Назначение кладём и в ``payPurpose``, и в ``description`` — банк в живых телах шлёт оба
    (в переводах они различаются только типографикой). Матч читает ``description``:
    ``normalize_tbank_statement_row`` берёт ``paymentPurpose|purpose|description``,
    ``payPurpose`` в этом списке нет.
    """
    return {
        "operationId": operation_id,
        "typeOfOperation": "Debit",
        "accountNumber": _PAYER_ACCOUNT,
        "documentNumber": doc_number,
        "operationAmount": amount,
        "accountAmount": amount,
        "rubleAmount": amount,
        "operationStatus": "Transaction",
        # Дата не раньше created_at черновика — иначе матч отсекается как «операция до платежа».
        "operationDate": f"{date.today().isoformat()}T10:00:00Z",
        "payPurpose": purpose,
        "description": purpose,
        "payer": {"account": _PAYER_ACCOUNT, "name": "ИП Шокина Е.А."},
        "receiver": {"account": "40702810900000099999", "name": "Поставщик", "inn": "7700000000"},
    }


async def _seed_op(
    factory, *, amount: str = "1000.00", purpose: str = _DRAFT_PURPOSE
) -> tuple[uuid.UUID, uuid.UUID, str]:
    """Открытый черновик «банк по реквизитам» с накладной.

    Возвращает и его documentNumber: матч операции идёт по номеру, детерминированному из
    ``document_id`` (тот же ``_document_number``, что уходит в банк), а не по payload.
    """
    async with factory() as session:
        cp = await make_counterparty(session, name="Поставщик", inn="7700000000")
        draft = await make_draft(session, counterparty_id=cp.id, amount=amount)
        draft.provider_ref = "bank-doc-id-1"
        draft.payload = {"paymentPurpose": purpose, "accountNumber": _PAYER_ACCOUNT}
        await session.flush()
        inv = await make_invoice(session, counterparty_id=cp.id, amount=amount, draft_id=draft.id)
        await session.commit()
        return draft.id, inv.id, _document_number(draft.document_id)


def test_operation_like_body_ingested_but_draft_untouched_without_match(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Операция по счёту (есть operationId) на статусном URL вливается в выписку, но черновик
    без точного матча не трогает: назначение операции чужое → ждём статус документа/поллинг."""
    draft_id, invoice_id, doc_number = _run(_seed_op(async_session_factory))
    resp = client.post(
        BASE, json=_debit_body("op-settle-1", doc_number=doc_number, purpose="Оплата по счёту 42")
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Выписка наполняется всегда: иначе пустой баланс банка и пикер карт-оплат Кассы.
    assert body["ingested"] is True and body["stage"] == "transaction"
    assert body["inserted"] == 1

    async def _check() -> tuple[str, str]:
        async with async_session_factory() as session:
            inv = await session.get(SupplierInvoice, invoice_id)
            draft = await session.get(CounterpartyPaymentDraft, draft_id)
            return inv.payment_status, draft.status

    inv_status, draft_status = _run(_check())
    assert inv_status != "paid"
    assert draft_status in ("created", "updated")


def test_operation_like_body_settles_draft_on_exact_match(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Near-realtime доводка через HTTP-вход: сумма + назначение + счёт + documentNumber сошлись
    → черновик и накладная закрываются сразу, не дожидаясь статуса платёжного документа."""
    draft_id, invoice_id, doc_number = _run(_seed_op(async_session_factory))
    resp = client.post(BASE, json=_debit_body("op-settle-2", doc_number=doc_number))
    assert resp.status_code == 200, resp.text
    assert resp.json()["ingested"] is True

    async def _check() -> tuple[str, str]:
        async with async_session_factory() as session:
            inv = await session.get(SupplierInvoice, invoice_id)
            draft = await session.get(CounterpartyPaymentDraft, draft_id)
            return inv.payment_status, draft.status

    assert _run(_check()) == ("paid", "paid")


def test_operation_like_body_without_account_is_422(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Тело-операция без accountNumber отвергается ингестом выписки (иначе account_id=NULL и
    операция выпадает из баланса). 422 именно из ингест-ветки — тело не ушло в статусную."""
    body = _debit_body("op-nomatch-1", doc_number="999999")
    del body["accountNumber"]
    resp = client.post(BASE, json=body)
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == "Нет номера счёта в операции"


def test_webhook_token_enforced_when_configured(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _run(_seed(async_session_factory))
    client.app.dependency_overrides[get_settings] = lambda: Settings(tbank_webhook_token="s3cret")
    try:
        # без заголовка → 401
        assert (
            client.post(BASE, json={"paymentId": "pay-1", "status": "executed"}).status_code == 401
        )
        # неверный токен → 401
        bad = client.post(
            BASE,
            json={"paymentId": "pay-1", "status": "executed"},
            headers={"Authorization": "Bearer wrong"},
        )
        assert bad.status_code == 401
        # верный токен → 200
        ok = client.post(
            BASE,
            json={"paymentId": "pay-1", "status": "executed"},
            headers={"Authorization": "Bearer s3cret"},
        )
        assert ok.status_code == 200 and ok.json()["matched"] is True
        # «голый» токен без префикса Bearer (так шлёт T-Банк) → тоже 200
        ok_bare = client.post(
            BASE,
            json={"paymentId": "pay-1", "status": "executed"},
            headers={"Authorization": "s3cret"},
        )
        assert ok_bare.status_code == 200 and ok_bare.json()["matched"] is True
    finally:
        client.app.dependency_overrides.pop(get_settings, None)
