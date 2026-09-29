"""DDS must display deposit recipients and cannot edit their money independently of the ledger."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    Account,
    BankOperation,
    CashflowTransaction,
    ClassificationRule,
    Counterparty,
    CounterpartyPayableProfile,
    CounterpartyPaymentDraft,
    DdsArticle,
    DepositAccount,
    DepositBankDraft,
    DepositTransaction,
    Employee,
    EmployeePayout,
    ReconciliationCase,
    SafeAllocation,
    Wallet,
)
from app.services.banking.classifier import (
    OperationSplitLine,
    absorb_auto_classified_counterparty_payment,
    apply_operation_action,
    apply_operation_split,
    book_safe_topup,
    run_classification_rules,
)
from app.services.banking.safe_allocations import book_safe_topup_reserves
from app.services.banking.tbank import _document_number
from app.services.deposit_cashflow_integrity import DEPOSIT_PAYOUT_ARTICLE_CODE
from app.services.wallet_balance_as_of import wallet_balance_as_of

HEADERS = {"X-User-Role": "finance_manager"}
WINDOW = {"from": "2026-09-01", "to": "2026-09-30"}
DAY = date(2026, 9, 8)


def _run(coro):
    return asyncio.run(coro)


def _seed(
    factory: async_sessionmaker[AsyncSession], source_kind: str = "production_deposit_payout"
):
    async def go():
        async with factory() as session:
            article = await session.scalar(
                select(DdsArticle).where(DdsArticle.code == DEPOSIT_PAYOUT_ARTICLE_CODE)
            )
            if article is None:
                article = DdsArticle(
                    code=DEPOSIT_PAYOUT_ARTICLE_CODE,
                    name="Выдача депозита сотруднику",
                    movement_type="outflow",
                    activity_type="operating",
                )
                session.add(article)
                await session.flush()
            plain_article = DdsArticle(
                code=f"dds_deposit_plain_{uuid.uuid4().hex[:8]}",
                name="Обычный расход",
                movement_type="outflow",
                activity_type="operating",
            )
            wallet = Wallet(
                code=f"dds_deposit_{uuid.uuid4().hex[:8]}",
                name="Сейф проверки депозита",
                type="cash_safe",
                opening_balance=Decimal("10000"),
            )
            employee = Employee(
                full_name="Абдурахманов Сергей",
                iiko_id=f"dds-deposit-{uuid.uuid4()}",
                status="active",
            )
            other_employee = Employee(
                full_name="Другой сотрудник",
                iiko_id=f"dds-deposit-other-{uuid.uuid4()}",
                status="active",
            )
            session.add_all([wallet, employee, other_employee, plain_article])
            await session.flush()
            account = DepositAccount(
                employee_id=employee.id,
                balance=Decimal("4000"),
                initial_balance=Decimal("6000"),
                last_updated=datetime(2026, 9, 8, 18, 8, tzinfo=UTC),
            )
            event = DepositTransaction(
                employee_id=employee.id,
                transaction_type="payout",
                amount=Decimal("2000"),
                happened_on=DAY,
                created_at=datetime(2026, 9, 8, 18, 8, tzinfo=UTC),
            )
            session.add_all([account, event])
            await session.flush()
            source_id = event.id
            if source_kind in {
                "production_deposit_payout_draft",
                "safe_payout",
                "kassa_target_payout",
            }:
                allocation = SafeAllocation(
                    wallet_id=wallet.id,
                    article_id=article.id,
                    employee_id=None,
                    amount=Decimal("2000"),
                    amount_paid=Decimal("2000"),
                    status="paid",
                    location="safe" if source_kind != "kassa_target_payout" else "kassa",
                )
                session.add(allocation)
                await session.flush()
                draft = DepositBankDraft(
                    recipient_kind="production",
                    employee_id=employee.id,
                    deposit_transaction_id=event.id,
                    document_id=f"DDS-DEPOSIT-{uuid.uuid4().hex[:10]}",
                    amount=Decimal("2000"),
                    status="disbursed",
                    safe_allocation_id=allocation.id,
                    bank_provider="tbank",
                )
                session.add(draft)
                await session.flush()
                source_id = (
                    draft.id if source_kind == "production_deposit_payout_draft" else allocation.id
                )
            txn = CashflowTransaction(
                wallet_id=wallet.id,
                direction="out",
                amount=Decimal("2000"),
                operation_date=DAY,
                article_id=article.id,
                counterparty_id=None,
                source_kind=source_kind,
                source_id=source_id if source_kind != "manual" else None,
                payment_purpose=f"Выдача депозита сотруднику (операция {event.id})",
                quality_status="final",
            )
            session.add(txn)
            await session.commit()
            return {
                "employee_id": str(employee.id),
                "other_employee_id": str(other_employee.id),
                "account_id": str(account.id),
                "event_id": str(event.id),
                "txn_id": str(txn.id),
                "wallet_id": str(wallet.id),
                "source_id": str(txn.source_id) if txn.source_id else None,
                "article_id": str(article.id),
                "plain_article_id": str(plain_article.id),
            }

    return _run(go())


def _snapshot(factory, ids):
    async def go():
        async with factory() as session:
            txn = await session.get(CashflowTransaction, uuid.UUID(ids["txn_id"]))
            account = await session.get(DepositAccount, uuid.UUID(ids["account_id"]))
            event = await session.get(DepositTransaction, uuid.UUID(ids["event_id"]))
            return {
                "cashflow": (
                    txn.wallet_id,
                    txn.article_id,
                    txn.amount,
                    txn.source_kind,
                    txn.source_id,
                    txn.quality_status,
                ),
                "deposit": (
                    account.balance,
                    event.employee_id,
                    event.amount,
                    event.transaction_type,
                ),
                "cashflow_count": await session.scalar(
                    select(func.count())
                    .select_from(CashflowTransaction)
                    .where(CashflowTransaction.wallet_id == txn.wallet_id)
                ),
                "payout_count": await session.scalar(
                    select(func.count())
                    .select_from(EmployeePayout)
                    .where(
                        EmployeePayout.employee_id.in_(
                            [uuid.UUID(ids["employee_id"]), uuid.UUID(ids["other_employee_id"])]
                        )
                    )
                ),
            }

    return _run(go())


@pytest.mark.parametrize(
    "source_kind",
    [
        "production_deposit_payout",
        "production_deposit_payout_draft",
        "safe_payout",
        "kassa_target_payout",
    ],
)
def test_journal_resolves_deposit_employee_without_counterparty_or_salary_payout(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession], source_kind: str
):
    ids = _seed(async_session_factory, source_kind)
    before = _snapshot(async_session_factory, ids)
    response = client.get("/api/v1/dds/journal", params=WINDOW, headers=HEADERS)
    assert response.status_code == 200, response.text
    row = next(row for row in response.json()["items"] if row["id"] == ids["txn_id"])
    assert row["employee_id"] == ids["employee_id"]
    assert row["employee_name"] == "Абдурахманов Сергей"
    assert row["counterparty_id"] is None
    assert row["source_kind"] == source_kind
    assert "Депозиты" in row["classification_blocked_reason"]
    assert _snapshot(async_session_factory, ids) == before
    assert before["cashflow_count"] == 1
    assert before["payout_count"] == 0


@pytest.mark.parametrize(
    "source_kind",
    [
        "production_deposit_payout",
        "production_deposit_payout_draft",
        "safe_payout",
        "kassa_target_payout",
        "manual",
    ],
)
@pytest.mark.parametrize("action", ["exclude", "split", "patch"])
def test_generic_dds_cannot_change_deposit_money_recipient_or_source(
    client: TestClient,
    async_session_factory: async_sessionmaker[AsyncSession],
    source_kind: str,
    action: str,
):
    ids = _seed(async_session_factory, source_kind)
    before = _snapshot(async_session_factory, ids)
    url = f"/api/v1/dds/transactions/{ids['txn_id']}"
    if action == "patch":
        response = client.patch(url, json={"article_id": ids["plain_article_id"]}, headers=HEADERS)
    else:
        payload = {"action": action}
        if action == "split":
            payload["splits"] = [
                {
                    "article_id": ids["plain_article_id"],
                    "amount": "1000",
                    "employee_id": ids["other_employee_id"],
                }
            ]
        response = client.post(f"{url}/classify", json=payload, headers=HEADERS)
    assert response.status_code == 409, response.text
    assert "Депозиты" in response.json()["detail"]
    assert _snapshot(async_session_factory, ids) == before


@pytest.mark.parametrize("with_employee", [False, True])
def test_generic_classification_cannot_create_unlinked_deposit_payout(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession], with_employee: bool
):
    ids = _seed(async_session_factory)

    async def make_plain():
        async with async_session_factory() as session:
            txn = await session.get(CashflowTransaction, uuid.UUID(ids["txn_id"]))
            txn.article_id = uuid.UUID(ids["plain_article_id"])
            txn.source_kind = "manual"
            txn.source_id = None
            await session.commit()

    _run(make_plain())
    before = _snapshot(async_session_factory, ids)
    split = {"article_id": ids["article_id"], "amount": "2000"}
    if with_employee:
        split["employee_id"] = ids["employee_id"]
    response = client.post(
        f"/api/v1/dds/transactions/{ids['txn_id']}/classify",
        json={"action": "split", "splits": [split]},
        headers=HEADERS,
    )
    assert response.status_code == 400, response.text
    assert "сотрудником" in response.json()["detail"]
    assert "Депозиты" in response.json()["detail"]
    patch = client.patch(
        f"/api/v1/dds/transactions/{ids['txn_id']}",
        json={"article_id": ids["article_id"]},
        headers=HEADERS,
    )
    assert patch.status_code == 409, patch.text
    assert _snapshot(async_session_factory, ids) == before


def test_generic_bank_classification_requires_deposit_domain(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
):
    ids = _seed(async_session_factory)

    async def make_operation():
        async with async_session_factory() as session:
            operation = BankOperation(
                provider="tbank",
                provider_operation_id=f"dds-deposit-{uuid.uuid4()}",
                operation_date=DAY,
                direction="out",
                amount=Decimal("2000"),
                currency="RUB",
                classification_status="needs_review",
                raw_payload={},
            )
            session.add(operation)
            await session.commit()
            return str(operation.id)

    operation_id = _run(make_operation())
    before = _snapshot(async_session_factory, ids)
    response = client.post(
        f"/api/v1/dds/operations/{operation_id}/classify",
        json={
            "action": "split",
            "splits": [
                {
                    "article_id": ids["article_id"],
                    "amount": "2000",
                    "employee_id": ids["employee_id"],
                }
            ],
        },
        headers=HEADERS,
    )
    assert response.status_code == 409, response.text
    assert "Депозиты" in response.json()["detail"]
    assert _snapshot(async_session_factory, ids) == before


@pytest.mark.parametrize("mode", ["legacy_rule", "merchant_default"])
def test_legacy_auto_classification_does_not_create_unbound_deposit_money(
    async_session_factory: async_sessionmaker[AsyncSession], mode: str
):
    ids = _seed(async_session_factory)
    before = _snapshot(async_session_factory, ids)

    async def classify():
        async with async_session_factory() as session:
            account = Account(
                bank_code="tbank", account_number=uuid.uuid4().hex[:20], legal_entity="Тест ДДС"
            )
            session.add(account)
            await session.flush()
            wallet = Wallet(
                code=f"dds_deposit_auto_{uuid.uuid4().hex[:8]}",
                name="Банк проверки депозита",
                type="bank_account",
                account_id=account.id,
            )
            operation = BankOperation(
                provider="tbank",
                provider_operation_id=f"dds-deposit-auto-{uuid.uuid4()}",
                account_id=account.id,
                operation_date=DAY,
                direction="out",
                amount=Decimal("2000"),
                currency="RUB",
                classification_status="needs_review",
                payment_purpose="Оплата в DDSDEPOSIT Moskva RUS",
                raw_payload={},
            )
            session.add_all([wallet, operation])
            if mode == "legacy_rule":
                session.add(
                    ClassificationRule(
                        name="Старое правило выдачи депозита",
                        priority=-100000,
                        purpose_pattern="DDSDEPOSIT",
                        action="set_article",
                        article_id=uuid.UUID(ids["article_id"]),
                        is_active=True,
                    )
                )
            else:
                merchant = Counterparty(name="DDSDEPOSIT", type="legal_entity", status="active")
                session.add(merchant)
                await session.flush()
                session.add(
                    CounterpartyPayableProfile(
                        counterparty_id=merchant.id,
                        default_dds_article_id=uuid.UUID(ids["article_id"]),
                    )
                )
            await session.flush()
            result = await run_classification_rules(session, [operation])
            assert result.needs_review == 1
            assert result.classified == 0
            assert operation.classification_status == "needs_review"
            assert operation.cashflow_transaction_id is None
            case = await session.scalar(
                select(ReconciliationCase).where(
                    ReconciliationCase.bank_operation_id == operation.id
                )
            )
            assert case is not None
            assert case.payload["reason"] == "deposit_recipient_required"
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(CashflowTransaction)
                    .where(CashflowTransaction.wallet_id == wallet.id)
                )
                == 0
            )
            await session.commit()

    _run(classify())
    assert _snapshot(async_session_factory, ids) == before


def test_bank_confirmation_keeps_prebooked_deposit_cashflow_and_ledger(
    async_session_factory: async_sessionmaker[AsyncSession],
):
    ids = _seed(async_session_factory, "production_deposit_payout_draft")

    async def classify():
        async with async_session_factory() as session:
            account = Account(
                bank_code="tbank", account_number=uuid.uuid4().hex[:20], legal_entity="Тест ДДС"
            )
            session.add(account)
            await session.flush()
            wallet = await session.get(Wallet, uuid.UUID(ids["wallet_id"]))
            wallet.type = "bank_account"
            wallet.account_id = account.id
            operation = BankOperation(
                provider="tbank",
                provider_operation_id=f"dds-deposit-prebooked-{uuid.uuid4()}",
                account_id=account.id,
                operation_date=DAY,
                direction="out",
                amount=Decimal("2000"),
                currency="RUB",
                classification_status="needs_review",
                payment_purpose="Банк подтвердил перевод под выдачу депозита",
                raw_payload={},
            )
            session.add(operation)
            await session.flush()
            result = await run_classification_rules(session, [operation])
            assert result.classified == 1
            assert operation.cashflow_transaction_id == uuid.UUID(ids["txn_id"])
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(CashflowTransaction)
                    .where(CashflowTransaction.wallet_id == wallet.id)
                )
                == 1
            )
            await session.commit()

    before = _snapshot(async_session_factory, ids)
    _run(classify())
    assert _snapshot(async_session_factory, ids) == before


def test_direct_manual_set_article_cannot_bypass_deposit_recipient(
    async_session_factory: async_sessionmaker[AsyncSession],
):
    ids = _seed(async_session_factory)
    before = _snapshot(async_session_factory, ids)

    async def classify():
        async with async_session_factory() as session:
            account = Account(
                bank_code="tbank", account_number=uuid.uuid4().hex[:20], legal_entity="Тест ДДС"
            )
            session.add(account)
            await session.flush()
            wallet = Wallet(
                code=f"dds_deposit_manual_{uuid.uuid4().hex[:8]}",
                name="Банк",
                type="bank_account",
                account_id=account.id,
            )
            operation = BankOperation(
                provider="tbank",
                provider_operation_id=f"dds-deposit-manual-{uuid.uuid4()}",
                account_id=account.id,
                operation_date=DAY,
                direction="out",
                amount=Decimal("2000"),
                currency="RUB",
                classification_status="needs_review",
                raw_payload={},
            )
            session.add_all([wallet, operation])
            await session.flush()
            with pytest.raises(ValueError, match="Депозиты"):
                await apply_operation_action(
                    session,
                    operation,
                    action="set_article",
                    article_id=uuid.UUID(ids["article_id"]),
                    counterparty_id=None,
                    quality_status="owner_review",
                )
            assert operation.cashflow_transaction_id is None
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(CashflowTransaction)
                    .where(CashflowTransaction.wallet_id == wallet.id)
                )
                == 0
            )

    _run(classify())
    assert _snapshot(async_session_factory, ids) == before


def _banked_deposit_flow(factory, *, source_kind="bank_operation", placement="anchor"):
    """Legacy bank deposit shares and domain-linked bank anchors use the same guards."""
    ids = _seed(
        factory, source_kind if source_kind != "bank_operation" else "production_deposit_payout"
    )

    async def go():
        async with factory() as session:
            account = Account(
                bank_code="tbank", account_number=uuid.uuid4().hex[:20], legal_entity="Тест ДДС"
            )
            session.add(account)
            await session.flush()
            wallet = Wallet(
                code=f"dds_deposit_guard_{uuid.uuid4().hex[:8]}",
                name="Банк проверки источника",
                type="bank_account",
                account_id=account.id,
            )
            operation = BankOperation(
                provider="tbank",
                provider_operation_id=f"dds-deposit-guard-{uuid.uuid4()}",
                account_id=account.id,
                operation_date=DAY,
                direction="out",
                amount=Decimal("2000"),
                currency="RUB",
                classification_status="classified",
                payment_purpose="DDSDEPOSITGUARD",
                raw_payload={},
            )
            session.add_all([wallet, operation])
            await session.flush()
            if source_kind == "bank_operation":
                deposit_share = CashflowTransaction(
                    wallet_id=wallet.id,
                    direction="out",
                    amount=Decimal("1000" if placement == "non_anchor" else "2000"),
                    operation_date=DAY,
                    article_id=uuid.UUID(ids["article_id"]),
                    source_kind="bank_operation",
                    source_id=operation.id,
                    payment_purpose="Старая депозитная доля без сотрудника",
                    quality_status="auto",
                )
                session.add(deposit_share)
                await session.flush()
                if placement == "non_anchor":
                    anchor = CashflowTransaction(
                        wallet_id=wallet.id,
                        direction="out",
                        amount=Decimal("1000"),
                        operation_date=DAY,
                        article_id=uuid.UUID(ids["plain_article_id"]),
                        source_kind="bank_operation",
                        source_id=operation.id,
                        payment_purpose="Обычная якорная доля",
                        quality_status="auto",
                    )
                    session.add(anchor)
                    await session.flush()
                    operation.cashflow_transaction_id = anchor.id
                elif placement != "unanchored":
                    operation.cashflow_transaction_id = deposit_share.id
            else:
                deposit_share = await session.get(CashflowTransaction, uuid.UUID(ids["txn_id"]))
                deposit_share.wallet_id = wallet.id
                # Даже ранее заменённая статья не отменяет доменную связь с сотрудником.
                deposit_share.article_id = uuid.UUID(ids["plain_article_id"])
                operation.cashflow_transaction_id = deposit_share.id
            await session.commit()
            return {**ids, "operation_id": str(operation.id), "bank_wallet_id": str(wallet.id)}

    return _run(go())


async def _bank_cashflow_state(session, ids):
    rows = (
        await session.scalars(
            select(CashflowTransaction)
            .where(CashflowTransaction.wallet_id == uuid.UUID(ids["bank_wallet_id"]))
            .order_by(CashflowTransaction.id)
        )
    ).all()
    return [
        (
            row.id,
            row.wallet_id,
            row.direction,
            row.amount,
            row.operation_date,
            row.article_id,
            row.counterparty_id,
            row.source_kind,
            row.source_id,
            row.quality_status,
            row.payment_purpose,
        )
        for row in rows
    ]


@pytest.mark.parametrize("placement", ["anchor", "non_anchor", "unanchored"])
@pytest.mark.parametrize("action", ["exclude", "mark_internal_transfer", "set_article", "split"])
def test_existing_bank_deposit_share_blocks_every_generic_mutation(
    async_session_factory: async_sessionmaker[AsyncSession], placement: str, action: str
):
    ids = _banked_deposit_flow(async_session_factory, placement=placement)
    before = _snapshot(async_session_factory, ids)

    async def classify():
        async with async_session_factory() as session:
            operation = await session.get(BankOperation, uuid.UUID(ids["operation_id"]))
            before_rows = await _bank_cashflow_state(session, ids)
            before_operation = (operation.classification_status, operation.cashflow_transaction_id)
            with pytest.raises(ValueError, match="Депозиты"):
                if action == "split":
                    # Налоговая проекция вызывает этот сервис с PROJECTOR_QUALITY=final.
                    await apply_operation_split(
                        session,
                        operation,
                        splits=[
                            OperationSplitLine(uuid.UUID(ids["plain_article_id"]), Decimal("2000"))
                        ],
                        quality_status="final",
                    )
                else:
                    await apply_operation_action(
                        session,
                        operation,
                        action=action,
                        article_id=uuid.UUID(ids["plain_article_id"]),
                        quality_status="owner_review",
                    )
            assert await _bank_cashflow_state(session, ids) == before_rows
            assert (
                operation.classification_status,
                operation.cashflow_transaction_id,
            ) == before_operation
            await session.commit()

    _run(classify())
    assert _snapshot(async_session_factory, ids) == before


@pytest.mark.parametrize("action", ["exclude", "mark_internal_transfer", "set_article", "split"])
@pytest.mark.parametrize("source_kind", ["production_deposit_payout_draft", "safe_payout"])
def test_bank_action_preserves_domain_link_even_after_article_was_changed(
    async_session_factory: async_sessionmaker[AsyncSession], action: str, source_kind: str
):
    ids = _banked_deposit_flow(async_session_factory, source_kind=source_kind)
    before = _snapshot(async_session_factory, ids)

    async def classify():
        async with async_session_factory() as session:
            operation = await session.get(BankOperation, uuid.UUID(ids["operation_id"]))
            before_rows = await _bank_cashflow_state(session, ids)
            before_operation = (operation.classification_status, operation.cashflow_transaction_id)
            with pytest.raises(ValueError, match="Депозиты"):
                if action == "split":
                    await apply_operation_split(
                        session,
                        operation,
                        splits=[
                            OperationSplitLine(uuid.UUID(ids["plain_article_id"]), Decimal("2000"))
                        ],
                    )
                else:
                    await apply_operation_action(
                        session,
                        operation,
                        action=action,
                        article_id=uuid.UUID(ids["plain_article_id"]),
                        quality_status="owner_review",
                    )
            assert await _bank_cashflow_state(session, ids) == before_rows
            assert (
                operation.classification_status,
                operation.cashflow_transaction_id,
            ) == before_operation
            await session.commit()

    _run(classify())
    assert _snapshot(async_session_factory, ids) == before


@pytest.mark.parametrize("action", ["exclude", "mark_internal_transfer", "set_article"])
@pytest.mark.parametrize(
    "source_kind", ["bank_operation", "production_deposit_payout_draft", "safe_payout"]
)
def test_background_rule_keeps_every_deposit_share_and_recipient(
    async_session_factory: async_sessionmaker[AsyncSession], action: str, source_kind: str
):
    ids = _banked_deposit_flow(
        async_session_factory, source_kind=source_kind, placement="non_anchor"
    )
    before = _snapshot(async_session_factory, ids)

    async def classify():
        async with async_session_factory() as session:
            operation = await session.get(BankOperation, uuid.UUID(ids["operation_id"]))
            before_rows = await _bank_cashflow_state(session, ids)
            anchor_id = operation.cashflow_transaction_id
            session.add(
                ClassificationRule(
                    name="Правило поверх старой депозитной доли",
                    priority=-100000,
                    purpose_pattern="DDSDEPOSITGUARD",
                    action=action,
                    article_id=uuid.UUID(ids["plain_article_id"]),
                    is_active=True,
                )
            )
            await session.flush()
            result = await run_classification_rules(session, [operation])
            assert result.excluded == 0
            assert result.internal_transfer == 0
            assert operation.cashflow_transaction_id == anchor_id
            assert await _bank_cashflow_state(session, ids) == before_rows
            if source_kind == "bank_operation":
                assert result.needs_review == 1
                assert result.classified == 0
                case = await session.scalar(
                    select(ReconciliationCase).where(
                        ReconciliationCase.bank_operation_id == operation.id
                    )
                )
                assert case.payload["reason"] == "deposit_recipient_required"
            else:
                assert result.classified == 1
                assert result.needs_review == 0
            await session.commit()

    _run(classify())
    assert _snapshot(async_session_factory, ids) == before


def test_direct_bank_split_cannot_create_deposit_even_with_employee(
    async_session_factory: async_sessionmaker[AsyncSession],
):
    ids = _banked_deposit_flow(async_session_factory)
    before = _snapshot(async_session_factory, ids)

    async def classify():
        async with async_session_factory() as session:
            operation = await session.get(BankOperation, uuid.UUID(ids["operation_id"]))
            # Обычная исходная строка: отказ должен проверять целевую статью самого сервиса.
            rows = await session.scalars(
                select(CashflowTransaction).where(CashflowTransaction.source_id == operation.id)
            )
            for row in rows:
                row.article_id = uuid.UUID(ids["plain_article_id"])
            await session.flush()
            before_rows = await _bank_cashflow_state(session, ids)
            with pytest.raises(ValueError, match="Депозиты"):
                await apply_operation_split(
                    session,
                    operation,
                    splits=[
                        OperationSplitLine(
                            uuid.UUID(ids["article_id"]),
                            Decimal("2000"),
                            employee_id=uuid.UUID(ids["employee_id"]),
                        )
                    ],
                )
            assert await _bank_cashflow_state(session, ids) == before_rows
            assert operation.classification_status == "classified"
            await session.commit()

    _run(classify())
    assert _snapshot(async_session_factory, ids) == before


@pytest.mark.parametrize("placement", ["anchor", "non_anchor"])
def test_prebooked_absorption_cannot_relink_or_delete_deposit_share(
    async_session_factory: async_sessionmaker[AsyncSession], placement: str
):
    ids = _banked_deposit_flow(async_session_factory, placement=placement)
    before = _snapshot(async_session_factory, ids)

    async def classify():
        async with async_session_factory() as session:
            operation = await session.get(BankOperation, uuid.UUID(ids["operation_id"]))
            draft = CounterpartyPaymentDraft(
                document_id=f"teplo-cp-deposit-guard-{uuid.uuid4()}",
                amount=Decimal("2000"),
                status="paid",
            )
            session.add(draft)
            await session.flush()
            operation.document_number = _document_number(draft.document_id)
            prebooked = CashflowTransaction(
                wallet_id=uuid.UUID(ids["bank_wallet_id"]),
                direction="out",
                amount=Decimal("2000"),
                operation_date=DAY,
                article_id=uuid.UUID(ids["plain_article_id"]),
                source_kind="counterparty_payment",
                source_id=draft.id,
                payment_purpose="Оплата с тем же document_number",
                quality_status="final",
            )
            session.add(prebooked)
            await session.flush()
            anchor_id = operation.cashflow_transaction_id
            before_rows = await _bank_cashflow_state(session, ids)
            result = await run_classification_rules(session, [operation])
            assert result.needs_review == 1
            # Scheduler invokes absorption after the regular classification/reconcile pass.
            absorbed = await absorb_auto_classified_counterparty_payment(session)
            assert absorbed == 0
            assert operation.cashflow_transaction_id == anchor_id
            assert operation.classification_status == "needs_review"
            assert await _bank_cashflow_state(session, ids) == before_rows
            await session.commit()

    _run(classify())
    assert _snapshot(async_session_factory, ids) == before


@pytest.mark.parametrize("placement", ["anchor", "non_anchor"])
@pytest.mark.parametrize("with_reserves", [False, True])
def test_safe_topup_guard_keeps_deposit_cashflow_balance_and_existing_reserves(
    async_session_factory: async_sessionmaker[AsyncSession], placement: str, with_reserves: bool
):
    ids = _banked_deposit_flow(async_session_factory, placement=placement)
    before = _snapshot(async_session_factory, ids)

    async def classify():
        async with async_session_factory() as session:
            operation = await session.get(BankOperation, uuid.UUID(ids["operation_id"]))
            safe_wallet = await session.scalar(select(Wallet).where(Wallet.code == "cash_safe"))
            if safe_wallet is None:
                safe_wallet = Wallet(
                    code="cash_safe", name="Сейф", type="cash_safe", opening_balance=Decimal("0")
                )
                session.add(safe_wallet)
                await session.flush()
            reservation = SafeAllocation(
                wallet_id=safe_wallet.id,
                article_id=uuid.UUID(ids["plain_article_id"]),
                source_operation_id=operation.id,
                amount=Decimal("2000"),
                amount_paid=Decimal("0"),
                status="reserved",
            )
            session.add(reservation)
            await session.flush()
            reservation_id = reservation.id
            before_rows = await _bank_cashflow_state(session, ids)
            anchor_id = operation.cashflow_transaction_id
            before_balance = await wallet_balance_as_of(session, safe_wallet)
            with pytest.raises(ValueError, match="Депозиты"):
                if with_reserves:
                    await book_safe_topup_reserves(
                        session,
                        operation,
                        reserves=[(uuid.UUID(ids["plain_article_id"]), Decimal("2000"), None)],
                    )
                else:
                    await book_safe_topup(session, operation)
            assert operation.cashflow_transaction_id == anchor_id
            assert operation.classification_status == "classified"
            assert await _bank_cashflow_state(session, ids) == before_rows
            assert await wallet_balance_as_of(session, safe_wallet) == before_balance
            remaining = (
                await session.scalars(
                    select(SafeAllocation).where(SafeAllocation.source_operation_id == operation.id)
                )
            ).all()
            assert [(row.id, row.amount, row.status) for row in remaining] == [
                (reservation_id, Decimal("2000"), "reserved")
            ]
            await session.commit()

    _run(classify())
    assert _snapshot(async_session_factory, ids) == before


@pytest.mark.parametrize("placement", ["anchor", "non_anchor"])
@pytest.mark.parametrize("action", ["exclude", "mark_internal_transfer"])
def test_bank_deposit_mutation_endpoint_returns_clear_conflict(
    client: TestClient,
    async_session_factory: async_sessionmaker[AsyncSession],
    placement: str,
    action: str,
):
    ids = _banked_deposit_flow(async_session_factory, placement=placement)
    before = _snapshot(async_session_factory, ids)

    async def operation_state():
        async with async_session_factory() as session:
            operation = await session.get(BankOperation, uuid.UUID(ids["operation_id"]))
            return (
                operation.classification_status,
                operation.cashflow_transaction_id,
                await _bank_cashflow_state(session, ids),
            )

    bank_before = _run(operation_state())
    response = client.post(
        f"/api/v1/dds/operations/{ids['operation_id']}/classify",
        json={"action": action},
        headers=HEADERS,
    )
    assert response.status_code == 409, response.text
    assert "Депозиты" in response.json()["detail"]
    assert _run(operation_state()) == bank_before
    assert _snapshot(async_session_factory, ids) == before
