"""An owner-card statement payment settles its own document, without a second cash fact."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

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
    CounterpartyPaymentDraft,
    DdsArticle,
    DepositBankDraft,
    Employee,
    EmployeePayout,
    PayrollBankDraft,
    PayrollPeriod,
    PayrollRun,
    ReconciliationCase,
    SafeAllocation,
    SalaryAdvance,
    SalaryAdvanceBankDraft,
    SupplierPrepayment,
    Wallet,
)
from app.services.bank_payment_status import apply_payment_status
from app.services.banking.classifier import (
    OperationAlreadyBooked,
    OperationSplitLine,
    _find_prebooked_payment,
    absorb_auto_classified_counterparty_payment,
    apply_operation_action,
    apply_operation_split,
    book_safe_topup,
    reconcile_needs_review_prebooked,
    run_classification_rules,
)
from app.services.banking.payment_purpose import (
    OWNER_CARD_PAYMENT_PURPOSE,
    owner_card_payment_purpose,
    payment_match_marker,
)
from app.services.banking.prebooked_identity import (
    OWNER_CARD_WAITING_REASON,
    PREPAYMENT_WAITING_REASON,
    cashflow_requires_payment_marker,
)
from app.services.banking.safe_allocations import book_safe_topup_reserves
from app.services.banking.tbank import _document_number
from app.services.wallet_balance_as_of import wallet_balance_as_of

DAY = date(2026, 10, 6)
AMOUNT = Decimal("1000.00")


@dataclass
class Environment:
    account: Account
    bank: Wallet
    safe: Wallet
    article: DdsArticle
    employee: Employee


@dataclass
class Source:
    kind: str
    id: uuid.UUID
    document_id: str
    record: Any


async def _environment(session: AsyncSession, provider: str = "tbank") -> Environment:
    suffix = uuid.uuid4().hex[:10]
    account = Account(
        bank_code=provider,
        account_number=f"4080281{uuid.uuid4().int % 10**12:012d}",
        legal_entity="ИП проверки кодов",
        status="active",
    )
    article = DdsArticle(
        code=f"owner_match_{suffix}",
        name="Проверка сохранения исходной статьи",
        movement_type="internal",
        activity_type="operating",
    )
    employee = Employee(
        full_name=f"Получатель проверки {suffix}", iiko_id=f"owner-match-{suffix}", status="active"
    )
    session.add_all([account, article, employee])
    await session.flush()
    bank = Wallet(
        code=f"owner_match_bank_{suffix}",
        name="Банк проверки",
        type="bank",
        account_id=account.id,
        opening_balance=Decimal("50000"),
    )
    safe = Wallet(
        code=f"owner_match_safe_{suffix}",
        name="Сейф проверки",
        type="cash_safe",
        opening_balance=Decimal("7000"),
    )
    session.add_all([bank, safe])
    await session.flush()
    return Environment(account, bank, safe, article, employee)


def _request(env: Environment, document_id: str, amount: Decimal = AMOUNT) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "accountNumber": env.account.account_number,
        "amount": str(amount),
        "paymentPurpose": owner_card_payment_purpose(document_id),
    }
    if env.account.bank_code == "tbank":
        payload["documentNumber"] = _document_number(document_id)
    return payload


async def _source(
    session: AsyncSession, env: Environment, kind: str, *, status: str = "paid"
) -> Source:
    source_id = uuid.uuid4()
    if kind == "supplier_bank_to_safe":
        document_id = f"teplo-cp-{source_id}"
        record = CounterpartyPaymentDraft(
            id=source_id,
            document_id=document_id,
            amount=AMOUNT,
            status=status,
            pays_via_safe=True,
            bank_provider=env.account.bank_code,
            payload=_request(env, document_id),
        )
    elif kind == "payroll_bank_to_safe":
        period = await session.scalar(
            select(PayrollPeriod).where(
                PayrollPeriod.period_type == "week", PayrollPeriod.start_date == DAY
            )
        )
        if period is None:
            period = PayrollPeriod(
                period_type="week",
                start_date=DAY,
                end_date=DAY + timedelta(days=6),
                payroll_date=DAY + timedelta(days=7),
                status="open",
            )
            session.add(period)
            await session.flush()
        run = PayrollRun(id=source_id, period_id=period.id, status="finalized", summary={})
        session.add(run)
        await session.flush()
        document_id = f"teplo-payroll-{source_id}"
        record = PayrollBankDraft(
            run_id=source_id,
            document_id=document_id,
            amount=AMOUNT,
            status=status,
            bank_provider=env.account.bank_code,
            payload=_request(env, document_id),
        )
    elif kind == "salary_advance_bank_to_safe":
        advance = SalaryAdvance(
            id=source_id,
            employee_id=env.employee.id,
            role="production",
            kind="advance",
            amount=AMOUNT,
            per_installment_amount=AMOUNT,
            installments_count=1,
            recovered_amount=0,
            status="awaiting_payout",
            issued_on=DAY,
            wallet_id=env.bank.id,
        )
        session.add(advance)
        await session.flush()
        document_id = f"teplo-advance-{source_id}"
        record = SalaryAdvanceBankDraft(
            advance_id=source_id,
            document_id=document_id,
            amount=AMOUNT,
            status=status,
            bank_provider=env.account.bank_code,
            payload={"request": _request(env, document_id)},
        )
    elif kind == "employee_payout_bank_to_safe":
        document_id = f"teplo-emppayout-{source_id}"
        record = EmployeePayout(
            id=source_id,
            employee_id=env.employee.id,
            kind="salary",
            amount=AMOUNT,
            payout_date=DAY,
            wallet_id=env.bank.id,
            status="paid" if status == "paid" else "pending",
            document_id=document_id,
            payload={"request": _request(env, document_id)},
        )
    elif kind == "production_deposit_payout_draft":
        document_id = f"teplo-deposit-{source_id}"
        record = DepositBankDraft(
            id=source_id,
            employee_id=env.employee.id,
            recipient_kind="production",
            document_id=document_id,
            amount=AMOUNT,
            status="disbursed" if status == "paid" else status,
            bank_provider=env.account.bank_code,
            payload={
                "internal_purpose": "Выдача депозита сотруднику",
                "request": _request(env, document_id),
            },
        )
    else:
        raise AssertionError(kind)
    session.add(record)
    await session.flush()
    return Source(kind, source_id, document_id, record)


async def _book_transit(
    session: AsyncSession,
    env: Environment,
    source: Source,
    *,
    index: int = 0,
    amount: Decimal = AMOUNT,
) -> CashflowTransaction:
    outflow = CashflowTransaction(
        wallet_id=env.bank.id,
        direction="out",
        amount=amount,
        operation_date=DAY,
        article_id=env.article.id,
        source_kind=source.kind,
        source_id=source.id,
        payment_purpose=f"Внутреннее назначение {source.kind} получателю",
        quality_status="final",
        created_at=datetime(2026, 10, 6, 9, index, tzinfo=UTC),
    )
    inflow = CashflowTransaction(
        wallet_id=env.safe.id,
        direction="in",
        amount=amount,
        operation_date=DAY,
        article_id=env.article.id,
        source_kind=source.kind,
        source_id=source.id,
        payment_purpose=f"Сейф: {source.kind}",
        quality_status="final",
    )
    session.add_all([outflow, inflow])
    await session.flush()
    return outflow


async def _operation(
    session: AsyncSession,
    env: Environment,
    document_id: str,
    *,
    amount: Decimal = AMOUNT,
    purpose: str | None = None,
) -> BankOperation:
    operation = BankOperation(
        provider=env.account.bank_code,
        provider_operation_id=f"owner-match-{uuid.uuid4()}",
        account_id=env.account.id,
        operation_date=DAY,
        direction="out",
        amount=amount,
        document_number=_document_number(document_id)
        if env.account.bank_code == "tbank"
        else "777001",
        payment_purpose=purpose or owner_card_payment_purpose(document_id),
        raw_payload={},
        classification_status="needs_review",
    )
    session.add(operation)
    await session.flush()
    return operation


async def _money_snapshot(session: AsyncSession, env: Environment) -> dict[str, Any]:
    rows = (
        await session.scalars(
            select(CashflowTransaction)
            .where(CashflowTransaction.wallet_id.in_([env.bank.id, env.safe.id]))
            .order_by(CashflowTransaction.id)
        )
    ).all()
    bank = await wallet_balance_as_of(session, env.bank)
    safe = await wallet_balance_as_of(session, env.safe)
    return {
        "cashflows": [
            (
                row.id,
                row.wallet_id,
                row.amount,
                row.direction,
                row.article_id,
                row.source_kind,
                row.source_id,
                row.payment_purpose,
                row.quality_status,
            )
            for row in rows
        ],
        "bank_balance": bank,
        "safe_balance": safe,
        "employee_payout_count": await session.scalar(
            select(func.count())
            .select_from(EmployeePayout)
            .where(EmployeePayout.employee_id == env.employee.id)
        ),
    }


async def _assert_waiting(session: AsyncSession, operation: BankOperation) -> ReconciliationCase:
    assert operation.cashflow_transaction_id is None
    assert operation.classification_status == "needs_review"
    case = await session.scalar(
        select(ReconciliationCase).where(
            ReconciliationCase.bank_operation_id == operation.id,
            ReconciliationCase.status == "pending",
        )
    )
    assert case is not None
    assert case.payload["reason"] == OWNER_CARD_WAITING_REASON
    return case


@pytest.mark.parametrize("provider", ["tbank", "sber"])
async def test_equal_amounts_across_sources_match_document_in_reverse_order_without_money_changes(
    async_session_factory: async_sessionmaker[AsyncSession], provider: str
):
    async with async_session_factory() as session:
        env = await _environment(session, provider)
        sources = [
            await _source(session, env, kind)
            for kind in (
                "supplier_bank_to_safe",
                "payroll_bank_to_safe",
                "salary_advance_bank_to_safe",
                "employee_payout_bank_to_safe",
                "production_deposit_payout_draft",
            )
        ]
        cashflows = [
            await _book_transit(session, env, source, index=index)
            for index, source in enumerate(sources)
        ]
        operations = []
        for source in reversed(sources):
            # Statement normalization may change case and spacing inside the technical tag.
            purpose = (
                owner_card_payment_purpose(source.document_id)
                .lower()
                .replace("[tpl-", "[ tpl - ")
                .replace("]", " ]")
            )
            operations.append(await _operation(session, env, source.document_id, purpose=purpose))
        before = await _money_snapshot(session, env)
        result = await run_classification_rules(session, operations)
        await session.flush()
        assert result.classified == len(sources)
        assert result.needs_review == 0
        assert [op.cashflow_transaction_id for op in operations] == [
            row.id for row in reversed(cashflows)
        ]
        assert await _money_snapshot(session, env) == before


@pytest.mark.parametrize("marker_kind", ["unknown", "foreign", "multiple", "ambiguous"])
async def test_tagged_operation_never_claims_a_same_amount_fifo_candidate(
    async_session_factory: async_sessionmaker[AsyncSession], marker_kind: str
):
    async with async_session_factory() as session:
        env = await _environment(session)
        source = await _source(session, env, "supplier_bank_to_safe")
        await _book_transit(session, env, source)
        document_id = (
            f"teplo-cp-{uuid.uuid4()}"
            if marker_kind in {"unknown", "foreign"}
            else source.document_id
        )
        if marker_kind == "foreign":
            other = await _environment(session)
            foreign = await _source(session, other, "supplier_bank_to_safe")
            await _book_transit(session, other, foreign)
            document_id = foreign.document_id
        purpose = owner_card_payment_purpose(document_id)
        if marker_kind == "multiple":
            purpose += " " + payment_match_marker(f"teplo-cp-{uuid.uuid4()}")
        if marker_kind == "ambiguous":
            await _book_transit(session, env, source, index=1)
        operation = await _operation(session, env, document_id, purpose=purpose)
        session.add(
            ClassificationRule(
                name=f"Would create a guessed expense {uuid.uuid4()}",
                priority=-1000,
                provider="tbank",
                purpose_pattern="Вывод собственных средств",
                action="set_article",
                article_id=env.article.id,
            )
        )
        await session.flush()
        before = await _money_snapshot(session, env)
        assert await _find_prebooked_payment(session, operation, claimed=set()) is None
        result = await run_classification_rules(session, [operation])
        assert result.needs_review == 1
        await _assert_waiting(session, operation)
        assert await _money_snapshot(session, env) == before


@pytest.mark.parametrize("entry_point", ["rules", "manual"])
async def test_payment_before_source_is_held_then_late_reconcile_links_once(
    async_session_factory: async_sessionmaker[AsyncSession], entry_point: str
):
    async with async_session_factory() as session:
        env = await _environment(session)
        source = await _source(session, env, "supplier_bank_to_safe", status="created")
        operation = await _operation(session, env, source.document_id)
        session.add(
            ClassificationRule(
                name=f"Would classify early {uuid.uuid4()}",
                priority=-1000,
                provider="tbank",
                purpose_pattern=OWNER_CARD_PAYMENT_PURPOSE,
                action="set_article",
                article_id=env.article.id,
            )
        )
        await session.flush()
        before = await _money_snapshot(session, env)
        await run_classification_rules(session, [operation])
        if entry_point == "manual":
            with pytest.raises(OperationAlreadyBooked, match="банковскому черновику"):
                await apply_operation_action(
                    session,
                    operation,
                    action="set_article",
                    article_id=env.article.id,
                    quality_status="owner_review",
                )
        case = await _assert_waiting(session, operation)
        assert await _money_snapshot(session, env) == before
        source.record.status = "paid"
        cashflow = await _book_transit(session, env, source)
        after_payment = await _money_snapshot(session, env)
        assert await reconcile_needs_review_prebooked(session) == 1
        assert operation.cashflow_transaction_id == cashflow.id
        assert case.status == "resolved"
        assert await _money_snapshot(session, env) == after_payment
        assert await reconcile_needs_review_prebooked(session) == 0
        assert await _money_snapshot(session, env) == after_payment


@pytest.mark.parametrize("provider", ["tbank", "sber"])
async def test_document_number_is_tbank_extra_check_not_sber_generated_identity(
    async_session_factory: async_sessionmaker[AsyncSession], provider: str
):
    async with async_session_factory() as session:
        env = await _environment(session, provider)
        source = await _source(session, env, "supplier_bank_to_safe")
        cashflow = await _book_transit(session, env, source)
        operation = await _operation(session, env, source.document_id)
        operation.document_number = "000987654321"
        matched = await _find_prebooked_payment(session, operation, claimed=set())
        assert (matched.id if matched is not None else None) == (
            cashflow.id if provider == "sber" else None
        )
        if provider == "tbank":
            operation.document_number = "000" + str(_document_number(source.document_id))
            assert (
                await _find_prebooked_payment(session, operation, claimed=set())
            ).id == cashflow.id


async def test_payroll_topup_actual_request_marker_does_not_claim_old_aggregate(
    async_session_factory: async_sessionmaker[AsyncSession],
):
    async with async_session_factory() as session:
        env = await _environment(session)
        source = await _source(session, env, "payroll_bank_to_safe")
        topup_document = source.document_id + "-topup-1"
        source.record.amount = Decimal("1500")
        source.record.payload = {
            "last_action": "topup",
            "payload": _request(env, topup_document, Decimal("500")),
        }
        await _book_transit(session, env, source, amount=Decimal("1500"))
        base_operation = await _operation(session, env, source.document_id, amount=Decimal("1500"))
        delta_operation = await _operation(session, env, topup_document, amount=Decimal("500"))
        before = await _money_snapshot(session, env)
        assert await _find_prebooked_payment(session, base_operation, claimed=set()) is None
        assert await _find_prebooked_payment(session, delta_operation, claimed=set()) is None
        await run_classification_rules(session, [base_operation, delta_operation])
        await _assert_waiting(session, base_operation)
        await _assert_waiting(session, delta_operation)
        assert await _money_snapshot(session, env) == before
        # If the source carries the delta as its own fact, use the actual nested request.
        delta_fact = await _book_transit(session, env, source, amount=Decimal("500"), index=1)
        assert (
            await _find_prebooked_payment(session, delta_operation, claimed=set())
        ).id == delta_fact.id


@pytest.mark.parametrize(
    "legacy_kind", ["production_deposit_payout_draft", "courier_deposit_return_draft"]
)
async def test_legacy_direct_deposit_transit_has_document_identity(
    async_session_factory: async_sessionmaker[AsyncSession], legacy_kind: str
):
    async with async_session_factory() as session:
        env = await _environment(session)
        source_id = uuid.uuid4() if legacy_kind == "production_deposit_payout_draft" else None
        document_id = (
            f"teplo-deposit-{source_id}" if source_id is not None else "teplo-courier-deposit-4242"
        )
        cashflow = CashflowTransaction(
            wallet_id=env.bank.id,
            direction="out",
            amount=AMOUNT,
            operation_date=DAY,
            article_id=env.article.id,
            source_kind=legacy_kind,
            source_id=source_id,
            payment_purpose="Возврат депозита курьеру (операция #4242) через Сейф",
            quality_status="final",
        )
        session.add(cashflow)
        await session.flush()
        operation = await _operation(session, env, document_id)
        assert (await _find_prebooked_payment(session, operation, claimed=set())).id == cashflow.id


async def test_untagged_legacy_statement_keeps_fifo_compatibility(
    async_session_factory: async_sessionmaker[AsyncSession],
):
    async with async_session_factory() as session:
        env = await _environment(session)
        first = await _source(session, env, "supplier_bank_to_safe")
        second = await _source(session, env, "employee_payout_bank_to_safe")
        first.record.payload = {
            **first.record.payload,
            "paymentPurpose": "Старое назначение без кода",
        }
        second.record.payload = {
            "request": {
                **second.record.payload["request"],
                "paymentPurpose": "Зарплата по старому назначению без кода",
            }
        }
        oldest = await _book_transit(session, env, first, index=0)
        await _book_transit(session, env, second, index=1)
        operation = await _operation(
            session, env, second.document_id, purpose="Заработная плата за старый период"
        )
        assert (await _find_prebooked_payment(session, operation, claimed=set())).id == oldest.id


async def _use_system_safe(session: AsyncSession, env: Environment) -> None:
    safe = await session.scalar(select(Wallet).where(Wallet.code == "cash_safe"))
    if safe is None:
        safe = Wallet(
            code="cash_safe", name="Сейф", type="cash_safe", opening_balance=Decimal("7000")
        )
        session.add(safe)
        await session.flush()
    env.safe = safe


async def _assert_single_paid_transit(
    session: AsyncSession, env: Environment, source: Source, operation: BankOperation
) -> None:
    before_safe = await wallet_balance_as_of(session, env.safe)
    before_bank = await wallet_balance_as_of(session, env.bank)
    assert (
        await apply_payment_status(
            session, draft=source.record, raw_status="paid", operation_date=DAY, commit=False
        )
        == "paid"
    )
    rows = (
        await session.scalars(
            select(CashflowTransaction).where(
                CashflowTransaction.source_kind == source.kind,
                CashflowTransaction.source_id == source.id,
            )
        )
    ).all()
    assert {(row.wallet_id, row.direction, row.amount) for row in rows} == {
        (env.bank.id, "out", AMOUNT),
        (env.safe.id, "in", AMOUNT),
    }
    assert len(rows) == 2
    assert await wallet_balance_as_of(session, env.safe) == before_safe + AMOUNT
    assert await wallet_balance_as_of(session, env.bank) == before_bank
    after_payment = await _money_snapshot(session, env)
    assert await reconcile_needs_review_prebooked(session) == 1
    assert operation.cashflow_transaction_id == next(
        row.id for row in rows if row.direction == "out"
    )
    assert await reconcile_needs_review_prebooked(session) == 0
    assert await _money_snapshot(session, env) == after_payment


@pytest.mark.parametrize("action", ["split", "topup", "reserves", "exclude", "transfer"])
async def test_waiting_service_refuses_changes_then_matches_paid_once(
    async_session_factory: async_sessionmaker[AsyncSession], action: str
):
    async with async_session_factory() as session:
        env = await _environment(session)
        await _use_system_safe(session, env)
        source = await _source(session, env, "supplier_bank_to_safe", status="created")
        source.record.topup_only = True
        source.record.target_purpose = "Служебное пополнение"
        operation = await _operation(session, env, source.document_id)
        await run_classification_rules(session, [operation])
        reservation = SafeAllocation(
            wallet_id=env.safe.id,
            article_id=env.article.id,
            source_operation_id=operation.id,
            amount=AMOUNT,
            amount_paid=0,
            status="reserved",
        )
        session.add(reservation)
        await session.flush()
        before = await _money_snapshot(session, env)
        with pytest.raises(OperationAlreadyBooked, match="банковскому черновику"):
            if action == "split":
                await apply_operation_split(
                    session, operation, splits=[OperationSplitLine(env.article.id, AMOUNT)]
                )
            elif action == "topup":
                await book_safe_topup(session, operation)
            elif action == "reserves":
                await book_safe_topup_reserves(
                    session, operation, reserves=[(env.article.id, AMOUNT, None)]
                )
            else:
                await apply_operation_action(
                    session,
                    operation,
                    action="exclude" if action == "exclude" else "mark_internal_transfer",
                    quality_status="owner_review",
                )
        await session.flush()
        assert await _money_snapshot(session, env) == before
        assert (await session.get(SafeAllocation, reservation.id)).status == "reserved"
        assert (
            await session.scalar(
                select(func.count())
                .select_from(SafeAllocation)
                .where(SafeAllocation.source_operation_id == operation.id)
            )
            == 1
        )
        await _assert_waiting(session, operation)
        await _assert_single_paid_transit(session, env, source, operation)
        assert (await session.get(SafeAllocation, reservation.id)).status == "reserved"


@pytest.mark.parametrize(
    "action",
    [
        "split",
        "mark_safe_topup",
        "employee_advance",
        "exclude",
        "mark_internal_transfer",
        "owner_review",
    ],
)
async def test_waiting_source_http409_keeps_case_open_and_late_paid_has_one_pair(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession], action: str
):
    async with async_session_factory() as session:
        env = await _environment(session)
        await _use_system_safe(session, env)
        source = await _source(session, env, "supplier_bank_to_safe", status="created")
        source.record.topup_only = True
        operation = await _operation(session, env, source.document_id)
        await run_classification_rules(session, [operation])
        case = await _assert_waiting(session, operation)
        await session.commit()
        before = await _money_snapshot(session, env)
        if action == "owner_review":
            response = client.post(
                f"/api/v1/dds/owner-review/{case.id}/classify",
                headers={"X-User-Role": "admin"},
                json={
                    "action": "set_article",
                    "article_id": str(env.article.id),
                    "remember_as_rule": True,
                },
            )
        else:
            response = client.post(
                f"/api/v1/dds/operations/{operation.id}/classify",
                headers={"X-User-Role": "finance_manager"},
                json={
                    "action": action,
                    "splits": [{"article_id": str(env.article.id), "amount": str(AMOUNT)}],
                    "new_counterparty_name": "Guard must prevent this new recipient",
                },
            )
        assert response.status_code == 409, response.text
        assert "банковскому черновику" in response.json()["detail"]
        await session.refresh(case)
        await session.refresh(operation)
        assert case.status == "pending"
        assert operation.classification_status == "needs_review"
        assert await _money_snapshot(session, env) == before
        assert (
            await session.scalar(
                select(func.count())
                .select_from(Counterparty)
                .where(Counterparty.name == "Guard must prevent this new recipient")
            )
            == 0
        )
        await _assert_single_paid_transit(session, env, source, operation)


async def _prepayment_draft(
    session: AsyncSession, env: Environment, counterparty: Counterparty | None = None
) -> CounterpartyPaymentDraft:
    if counterparty is None:
        counterparty = Counterparty(
            name=f"Получатель предоплаты {uuid.uuid4()}",
            type="legal_entity",
            inn=f"79{uuid.uuid4().int % 10**8:08d}",
        )
        session.add(counterparty)
        await session.flush()
    draft_id = uuid.uuid4()
    document_id = f"teplo-cp-{draft_id}"
    request = _request(env, document_id)
    request["paymentPurpose"] = "Предоплата поставщику. Без НДС. " + payment_match_marker(
        document_id
    )
    draft = CounterpartyPaymentDraft(
        id=draft_id,
        counterparty_id=counterparty.id,
        document_id=document_id,
        amount=AMOUNT,
        status="created",
        creates_prepayment=True,
        prepayment_article_id=env.article.id,
        bank_provider=env.account.bank_code,
        payload=request,
    )
    session.add(draft)
    await session.flush()
    return draft


@pytest.mark.parametrize("order", ["paid_first", "statement_first"])
async def test_direct_prepayment_exact_link_matches_same_amount_reverse_order_without_second_debt(
    async_session_factory: async_sessionmaker[AsyncSession], order: str
):
    async with async_session_factory() as session:
        env = await _environment(session)
        first = await _prepayment_draft(session, env)
        counterparty = await session.get(Counterparty, first.counterparty_id)
        second = await _prepayment_draft(session, env, counterparty)
        operations = [
            await _operation(
                session, env, draft.document_id, purpose=draft.payload["paymentPurpose"]
            )
            for draft in (second, first)
        ]
        session.add(
            ClassificationRule(
                name=f"Would create duplicate prepayment {uuid.uuid4()}",
                priority=-1000,
                provider="tbank",
                purpose_pattern="Предоплата поставщику",
                action="set_article",
                article_id=env.article.id,
                counterparty_id=counterparty.id,
            )
        )
        await session.flush()
        if order == "statement_first":
            assert (await run_classification_rules(session, operations)).needs_review == 2
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(CashflowTransaction)
                    .where(CashflowTransaction.wallet_id == env.bank.id)
                )
                == 0
            )
        for draft in (first, second):
            await apply_payment_status(
                session, draft=draft, raw_status="paid", operation_date=DAY, commit=False
            )
        before_match = await _money_snapshot(session, env)
        if order == "paid_first":
            assert (await run_classification_rules(session, operations)).classified == 2
        else:
            assert await reconcile_needs_review_prebooked(session) == 2
        prepayments = (
            await session.scalars(
                select(SupplierPrepayment).where(
                    SupplierPrepayment.counterparty_id == counterparty.id
                )
            )
        ).all()
        assert len(prepayments) == 2
        by_id = {str(item.id): item for item in prepayments}
        assert [operation.cashflow_transaction_id for operation in operations] == [
            by_id[draft.payload["dds_prepayment_id"]].cashflow_transaction_id
            for draft in (second, first)
        ]
        assert await _money_snapshot(session, env) == before_match
        assert all(item.amount == AMOUNT and item.amount_settled == 0 for item in prepayments)


@pytest.mark.parametrize("bad_link", ["missing_legacy", "foreign_draft", "ambiguous"])
async def test_prepayment_without_one_exact_source_link_waits_without_fifo_or_second_debt(
    async_session_factory: async_sessionmaker[AsyncSession], bad_link: str
):
    async with async_session_factory() as session:
        env = await _environment(session)
        draft = await _prepayment_draft(session, env)
        await apply_payment_status(
            session, draft=draft, raw_status="paid", operation_date=DAY, commit=False
        )
        document_draft = draft
        if bad_link == "missing_legacy":
            draft.payload = {
                key: value for key, value in draft.payload.items() if key != "dds_prepayment_id"
            }
        else:
            document_draft = await _prepayment_draft(
                session, env, await session.get(Counterparty, draft.counterparty_id)
            )
            if bad_link == "ambiguous":
                document_draft.status = "paid"
                document_draft.payload = {
                    **document_draft.payload,
                    "dds_prepayment_id": draft.payload["dds_prepayment_id"],
                }
                document_draft = draft
        await session.flush()
        operation = await _operation(
            session,
            env,
            document_draft.document_id,
            purpose=document_draft.payload["paymentPurpose"],
        )
        before = await _money_snapshot(session, env)
        debt_count = await session.scalar(
            select(func.count())
            .select_from(SupplierPrepayment)
            .where(SupplierPrepayment.counterparty_id == draft.counterparty_id)
        )
        assert await _find_prebooked_payment(session, operation, claimed=set()) is None
        assert (await run_classification_rules(session, [operation])).needs_review == 1
        case = await session.scalar(
            select(ReconciliationCase).where(
                ReconciliationCase.bank_operation_id == operation.id,
                ReconciliationCase.status == "pending",
            )
        )
        assert case.payload["reason"] == PREPAYMENT_WAITING_REASON
        assert await _money_snapshot(session, env) == before
        assert (
            await session.scalar(
                select(func.count())
                .select_from(SupplierPrepayment)
                .where(SupplierPrepayment.counterparty_id == draft.counterparty_id)
            )
            == debt_count
        )


@pytest.mark.parametrize("marker_state", ["conflicting", "missing"])
async def test_absorb_does_not_claim_other_marker_on_document_number_collision(
    async_session_factory: async_sessionmaker[AsyncSession], marker_state: str
):
    async with async_session_factory() as session:
        env = await _environment(session)
        source_ids = [
            uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaa123456"),
            uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbb123456"),
        ]
        drafts = []
        for source_id in source_ids:
            document_id = f"teplo-cp-{source_id}"
            drafts.append(
                CounterpartyPaymentDraft(
                    id=source_id,
                    document_id=document_id,
                    amount=AMOUNT,
                    status="paid",
                    payload=_request(env, document_id),
                    bank_provider="tbank",
                )
            )
        assert _document_number(drafts[0].document_id) == _document_number(drafts[1].document_id)
        session.add_all(drafts)
        await session.flush()
        prebooked = CashflowTransaction(
            wallet_id=env.bank.id,
            direction="out",
            amount=AMOUNT,
            operation_date=DAY,
            article_id=env.article.id,
            source_kind="counterparty_payment",
            source_id=drafts[0].id,
            payment_purpose="Первый исходный платёж",
            quality_status="final",
        )
        session.add(prebooked)
        operation = await _operation(
            session,
            env,
            drafts[1].document_id,
            purpose="Чужая операция без кода" if marker_state == "missing" else None,
        )
        auto = CashflowTransaction(
            wallet_id=env.bank.id,
            direction="out",
            amount=AMOUNT,
            operation_date=DAY,
            article_id=env.article.id,
            source_kind="bank_operation",
            source_id=operation.id,
            payment_purpose=operation.payment_purpose,
            quality_status="auto",
        )
        session.add(auto)
        await session.flush()
        operation.cashflow_transaction_id = auto.id
        operation.classification_status = "classified"
        await session.flush()
        before = await _money_snapshot(session, env)
        assert await absorb_auto_classified_counterparty_payment(session) == 0
        assert operation.cashflow_transaction_id == auto.id
        assert await _money_snapshot(session, env) == before
        correct = CashflowTransaction(
            wallet_id=env.bank.id,
            direction="out",
            amount=AMOUNT,
            operation_date=DAY,
            article_id=env.article.id,
            source_kind="counterparty_payment",
            source_id=drafts[1].id,
            payment_purpose="Второй исходный платёж",
            quality_status="final",
        )
        session.add(correct)
        await session.flush()
        if marker_state == "missing":
            # Even the right source's tagged draft cannot settle an untagged statement by FIFO.
            assert await absorb_auto_classified_counterparty_payment(session) == 0
            assert operation.cashflow_transaction_id == auto.id
            operation.payment_purpose = owner_card_payment_purpose(drafts[1].document_id)
            await session.flush()
        assert await absorb_auto_classified_counterparty_payment(session) == 1
        assert operation.cashflow_transaction_id == correct.id
        assert await session.get(CashflowTransaction, auto.id) is None
        assert (
            await session.get(CashflowTransaction, prebooked.id)
        ).payment_purpose == "Первый исходный платёж"


async def test_rules_and_manual_cannot_rewrite_an_already_matched_source(
    async_session_factory: async_sessionmaker[AsyncSession],
):
    async with async_session_factory() as session:
        env = await _environment(session)
        source = await _source(session, env, "supplier_bank_to_safe")
        cashflow = await _book_transit(session, env, source)
        operation = await _operation(session, env, source.document_id)
        assert (await run_classification_rules(session, [operation])).classified == 1
        original = await _money_snapshot(session, env)
        different_article = DdsArticle(
            code=f"owner-match-different-{uuid.uuid4().hex[:8]}",
            name="Другая статья",
            movement_type="outflow",
            activity_type="operating",
        )
        session.add(different_article)
        await session.flush()
        session.add(
            ClassificationRule(
                name=f"Would overwrite source {uuid.uuid4()}",
                priority=-1000,
                purpose_pattern=OWNER_CARD_PAYMENT_PURPOSE,
                action="set_article",
                article_id=different_article.id,
            )
        )
        await session.flush()
        assert (await run_classification_rules(session, [operation])).classified == 1
        assert await _money_snapshot(session, env) == original
        with pytest.raises(OperationAlreadyBooked):
            await apply_operation_action(
                session,
                operation,
                action="set_article",
                article_id=different_article.id,
                quality_status="owner_review",
            )
        assert operation.cashflow_transaction_id == cashflow.id
        assert await _money_snapshot(session, env) == original


async def test_owner_review_links_exact_existing_source_and_closes_case_without_new_cashflows(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
):
    async with async_session_factory() as session:
        env = await _environment(session)
        source = await _source(session, env, "supplier_bank_to_safe")
        cashflow = await _book_transit(session, env, source)
        operation = await _operation(session, env, source.document_id)
        different_article = DdsArticle(
            code=f"owner-success-different-{uuid.uuid4().hex[:8]}",
            name="Запрошенная другая статья",
            movement_type="outflow",
            activity_type="operating",
        )
        case = ReconciliationCase(
            kind="unclassified_operation",
            status="pending",
            provider=operation.provider,
            bank_operation_id=operation.id,
            payload={"reason": OWNER_CARD_WAITING_REASON},
        )
        session.add_all([different_article, case])
        await session.commit()
        assert operation.cashflow_transaction_id is None
        original_purpose = cashflow.payment_purpose
        before = await _money_snapshot(session, env)

        response = client.post(
            f"/api/v1/dds/owner-review/{case.id}/classify",
            headers={"X-User-Role": "admin"},
            json={"action": "set_article", "article_id": str(different_article.id)},
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "resolved"
        assert response.json()["classification_status"] == "classified"
        await session.refresh(operation)
        await session.refresh(case)
        await session.refresh(cashflow)
        assert operation.cashflow_transaction_id == cashflow.id
        assert operation.classification_status == "classified"
        assert case.status == "resolved"
        assert cashflow.article_id == env.article.id
        assert cashflow.payment_purpose == original_purpose
        assert (cashflow.source_kind, cashflow.source_id) == (source.kind, source.id)
        assert await _money_snapshot(session, env) == before


@pytest.mark.parametrize(
    "kind",
    [
        "supplier_bank_to_safe",
        "payroll_bank_to_safe",
        "salary_advance_bank_to_safe",
        "employee_payout_bank_to_safe",
        "production_deposit_payout_draft",
        "supplier_prepayment",
    ],
)
async def test_untagged_statement_cannot_claim_actually_tagged_source(
    async_session_factory: async_sessionmaker[AsyncSession], kind: str
):
    async with async_session_factory() as session:
        env = await _environment(session)
        if kind == "supplier_prepayment":
            draft = await _prepayment_draft(session, env)
            await apply_payment_status(
                session, draft=draft, raw_status="paid", operation_date=DAY, commit=False
            )
            prepayment = await session.get(
                SupplierPrepayment, uuid.UUID(draft.payload["dds_prepayment_id"])
            )
            cashflow = await session.get(CashflowTransaction, prepayment.cashflow_transaction_id)
            document_id = draft.document_id
        else:
            source = await _source(session, env, kind)
            cashflow = await _book_transit(session, env, source)
            document_id = source.document_id
        operation = await _operation(
            session, env, document_id, purpose="Другая операция без технической метки"
        )
        before = await _money_snapshot(session, env)
        assert await cashflow_requires_payment_marker(session, cashflow)
        assert await _find_prebooked_payment(session, operation, claimed=set()) is None
        assert operation.cashflow_transaction_id is None
        assert await _money_snapshot(session, env) == before


@pytest.mark.parametrize("with_legacy", [False, True])
async def test_untagged_fifo_skips_marked_source_and_only_preserves_actual_legacy_requests(
    async_session_factory: async_sessionmaker[AsyncSession], with_legacy: bool
):
    async with async_session_factory() as session:
        env = await _environment(session)
        marked = await _source(session, env, "supplier_bank_to_safe")
        marked_fact = await _book_transit(session, env, marked, index=0)
        legacy_fact = None
        if with_legacy:
            legacy = await _source(session, env, "employee_payout_bank_to_safe")
            legacy.record.payload = {
                "request": {
                    **legacy.record.payload["request"],
                    "paymentPurpose": "Старая зарплата без кода",
                }
            }
            legacy_fact = await _book_transit(session, env, legacy, index=1)
            assert not await cashflow_requires_payment_marker(session, legacy_fact)
        operation = await _operation(
            session, env, marked.document_id, purpose="Немаркированная банковская операция"
        )
        found = await _find_prebooked_payment(session, operation, claimed=set())
        assert (found.id if found is not None else None) == (
            legacy_fact.id if legacy_fact is not None else None
        )
        assert found is None or found.id != marked_fact.id


async def test_common_owner_purpose_without_tag_waits_before_fifo_rules_and_manual_topup(
    async_session_factory: async_sessionmaker[AsyncSession],
):
    async with async_session_factory() as session:
        env = await _environment(session)
        marked = await _source(session, env, "supplier_bank_to_safe")
        await _book_transit(session, env, marked, index=0)
        legacy = await _source(session, env, "employee_payout_bank_to_safe")
        legacy.record.payload = {
            "request": {
                **legacy.record.payload["request"],
                "paymentPurpose": "Старая зарплата без кода",
            }
        }
        await _book_transit(session, env, legacy, index=1)
        operation = await _operation(
            session, env, marked.document_id, purpose=OWNER_CARD_PAYMENT_PURPOSE
        )
        session.add(
            ClassificationRule(
                name=f"Would guess expense without owner tag {uuid.uuid4()}",
                priority=-1000,
                purpose_pattern=OWNER_CARD_PAYMENT_PURPOSE,
                action="set_article",
                article_id=env.article.id,
            )
        )
        await session.flush()
        before = await _money_snapshot(session, env)
        assert await _find_prebooked_payment(session, operation, claimed=set()) is None
        assert (await run_classification_rules(session, [operation])).needs_review == 1
        await _assert_waiting(session, operation)
        with pytest.raises(OperationAlreadyBooked, match="банковскому черновику"):
            await apply_operation_action(
                session,
                operation,
                action="set_article",
                article_id=env.article.id,
                quality_status="owner_review",
            )
        with pytest.raises(OperationAlreadyBooked, match="банковскому черновику"):
            await book_safe_topup(session, operation)
        assert await _money_snapshot(session, env) == before


async def test_topup_aggregate_retains_marker_requirement_when_request_amount_is_a_delta(
    async_session_factory: async_sessionmaker[AsyncSession],
):
    async with async_session_factory() as session:
        env = await _environment(session)
        source = await _source(session, env, "payroll_bank_to_safe")
        source.record.amount = Decimal("1500")
        source.record.payload = {
            "last_action": "topup",
            "payload": _request(env, source.document_id + "-topup-1", Decimal("500")),
        }
        aggregate = await _book_transit(session, env, source, amount=Decimal("1500"))
        operation = await _operation(
            session,
            env,
            source.document_id,
            amount=Decimal("1500"),
            purpose="Зарплата без технического кода",
        )
        before = await _money_snapshot(session, env)
        assert await cashflow_requires_payment_marker(session, aggregate)
        assert await _find_prebooked_payment(session, operation, claimed=set()) is None
        assert await _money_snapshot(session, env) == before
