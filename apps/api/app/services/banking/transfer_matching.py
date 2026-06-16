from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Account, BankOperation, OwnAccountsRegistry, TransferGroup, Wallet
from app.services.banking.base import clean_digits


async def find_and_link_transfer_pairs(session: AsyncSession) -> int:
    operations = (
        await session.scalars(
            select(BankOperation)
            .where(
                BankOperation.classification_status == "internal_transfer",
                BankOperation.transfer_group_id.is_(None),
            )
            .order_by(BankOperation.operation_date, BankOperation.provider)
        )
    ).all()
    if not operations:
        return 0

    account_numbers = {
        operation.id: await _operation_account_number(session, operation)
        for operation in operations
    }
    linked: set[UUID] = set()
    linked_count = 0
    inflows = [operation for operation in operations if operation.direction == "in"]
    for outflow in [operation for operation in operations if operation.direction == "out"]:
        if outflow.id in linked:
            continue
        match = None
        for inflow in inflows:
            if inflow.id in linked:
                continue
            if await _operations_match(session, outflow, inflow, account_numbers):
                match = inflow
                break
        if match is None:
            continue
        group = TransferGroup(
            amount=outflow.amount,
            from_wallet_id=await _wallet_id_for_operation(session, outflow),
            to_wallet_id=await _wallet_id_for_operation(session, match),
            initiated_at=outflow.operation_date,
            completed_at=match.operation_date,
            status="matched",
        )
        session.add(group)
        await session.flush()
        outflow.transfer_group_id = group.id
        match.transfer_group_id = group.id
        linked.add(outflow.id)
        linked.add(match.id)
        linked_count += 1

    await session.flush()
    await _create_unmatched_transfer_cases(session)
    return linked_count


async def _operations_match(
    session: AsyncSession,
    outflow: BankOperation,
    inflow: BankOperation,
    account_numbers: dict[UUID, str],
) -> bool:
    if Decimal(outflow.amount) != Decimal(inflow.amount):
        return False
    if abs((outflow.operation_date - inflow.operation_date).days) > 2:
        return False

    out_account = account_numbers.get(outflow.id, "")
    inflow_account = account_numbers.get(inflow.id, "")
    # A transfer always moves money between two DIFFERENT accounts.
    if out_account and inflow_account and out_account == inflow_account:
        return False

    out_counterparty_account = clean_digits(outflow.counterparty_account_raw)
    # Strong signal: the outflow names the inflow's own account as the counterparty.
    # Works for both cross-bank and intra-bank (same provider) transfers, e.g. T-Bank
    # расчётный счёт -> Бизнес-копилка / Гарант фонд / Накопительный фонд.
    if out_counterparty_account and out_counterparty_account == inflow_account:
        return True

    # INN fallback only ACROSS different banks: within one bank all of our accounts
    # share the same legal-entity INN, so an INN match there is not specific enough
    # and would pair unrelated operations.
    if outflow.provider != inflow.provider:
        out_counterparty_inn = clean_digits(outflow.counterparty_inn_raw)
        inflow_own_inn = await _own_inn_for_account(session, inflow.provider, inflow_account)
        return bool(
            out_counterparty_inn and inflow_own_inn and out_counterparty_inn == inflow_own_inn
        )
    return False


async def _operation_account_number(session: AsyncSession, operation: BankOperation) -> str:
    if operation.account_id is None:
        return clean_digits(operation.raw_payload.get("accountNumber"))
    account = await session.get(Account, operation.account_id)
    if account is None:
        return clean_digits(operation.raw_payload.get("accountNumber"))
    return clean_digits(account.account_number)


async def _own_inn_for_account(session: AsyncSession, provider: str, account_number: str) -> str:
    if not account_number:
        return ""
    row = await session.scalar(
        select(OwnAccountsRegistry).where(
            OwnAccountsRegistry.bank_code == provider,
            OwnAccountsRegistry.account_number == account_number,
            OwnAccountsRegistry.is_active.is_(True),
        )
    )
    return clean_digits(row.legal_entity_inn) if row else ""


async def _wallet_id_for_operation(session: AsyncSession, operation: BankOperation) -> UUID | None:
    if operation.account_id is not None:
        wallet = await session.scalar(
            select(Wallet).where(
                Wallet.account_id == operation.account_id,
                Wallet.status == "active",
            )
        )
        if wallet is not None:
            return wallet.id
    wallet = await session.scalar(
        select(Wallet)
        .join(Account, Account.id == Wallet.account_id)
        .where(
            Account.bank_code == operation.provider,
            Wallet.type == "bank",
            Wallet.status == "active",
            Wallet.is_internal_transfer_eligible.is_(True),
        )
        .order_by(Wallet.code)
    )
    return wallet.id if wallet else None


async def _create_unmatched_transfer_cases(session: AsyncSession) -> None:
    from app.services.banking.classifier import create_or_update_reconciliation_case

    operations = (
        await session.scalars(
            select(BankOperation).where(
                BankOperation.classification_status == "internal_transfer",
                BankOperation.transfer_group_id.is_(None),
            )
        )
    ).all()
    for operation in operations:
        await create_or_update_reconciliation_case(
            session,
            kind="unmatched_transfer",
            provider=operation.provider,
            bank_operation_id=operation.id,
            payload={
                "provider": operation.provider,
                "provider_operation_id": operation.provider_operation_id,
                "operation_date": operation.operation_date.isoformat(),
                "amount": str(operation.amount),
            },
        )
