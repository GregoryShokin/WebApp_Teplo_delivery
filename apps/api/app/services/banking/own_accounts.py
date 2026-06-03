from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Account, BankOperation, ClassificationRule, OwnAccountsRegistry


async def sync_own_accounts(session: AsyncSession, *, provider: str) -> int:
    accounts = await session.scalars(
        select(Account)
        .outerjoin(BankOperation, BankOperation.account_id == Account.id)
        .where(
            Account.bank_code == provider,
            Account.account_number.is_not(None),
            Account.status == "active",
        )
        .distinct()
    )
    added = 0
    for account in accounts.all():
        if not account.account_number:
            continue
        existing = await session.scalar(
            select(OwnAccountsRegistry).where(
                OwnAccountsRegistry.bank_code == provider,
                OwnAccountsRegistry.account_number == account.account_number,
            )
        )
        if existing is None:
            session.add(
                OwnAccountsRegistry(
                    bank_code=provider,
                    account_number=account.account_number,
                    bic=account.bic,
                    account_id=account.id,
                    is_active=True,
                    metadata_json={"source": "bank_import"},
                )
            )
            added += 1
        else:
            existing.bic = account.bic or existing.bic
            existing.account_id = account.id
            existing.is_active = True
            existing.metadata_json = existing.metadata_json or {"source": "bank_import"}
    await session.flush()
    await _activate_internal_transfer_rule_if_ready(session)
    return added


async def _activate_internal_transfer_rule_if_ready(session: AsyncSession) -> None:
    sber_count = await _active_own_account_count(session, "sber")
    tbank_count = await _active_own_account_count(session, "tbank")
    if sber_count < 1 or tbank_count < 1:
        return
    rule = await session.scalar(
        select(ClassificationRule).where(ClassificationRule.name == "Internal transfer placeholder")
    )
    if rule is not None:
        rule.is_active = True


async def _active_own_account_count(session: AsyncSession, provider: str) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(OwnAccountsRegistry)
            .where(
                OwnAccountsRegistry.bank_code == provider,
                OwnAccountsRegistry.is_active.is_(True),
            )
        )
        or 0
    )
