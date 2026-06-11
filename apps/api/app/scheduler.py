from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.session import AsyncSessionLocal
from app.models import (
    Account,
    BankOperation,
    OwnAccountsRegistry,
    ReconciliationCase,
    Wallet,
)
from app.services.banking.base import AccountMeta, NormalizedBankOperation, clean_digits
from app.services.banking.classifier import (
    create_or_update_reconciliation_case,
    run_classification_rules,
)
from app.services.banking.exceptions import BankCredentialsError
from app.services.banking.own_accounts import sync_own_accounts
from app.services.banking.sber import SberClient
from app.services.banking.tbank import TbankClient
from app.services.couriers.iiko_attendance_sync import sync_attendance
from app.services.couriers.iiko_olap_sync import sync_courier_olap_deliveries
from app.services.couriers.shift_matching import recalculate_matches

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
MOSCOW_TZ = ZoneInfo("Europe/Moscow")
LEGAL_ENTITY = "ИП Шокина Е.А."
IIKO_COURIER_JOB_RETRIES = 3
SUPPORTED_BANK_PROVIDERS = ("sber", "tbank")


@scheduler.scheduled_job(
    "cron",
    minute="*/15",
    hour="8-22",
    day_of_week="mon-fri",
    id="poll_banks",
    max_instances=1,
    coalesce=True,
)
async def poll_banks() -> None:
    for provider in _bank_sync_providers():
        await run_bank_sync_job(provider=provider)


@scheduler.scheduled_job(
    "interval",
    minutes=30,
    id="iiko_courier_attendance_sync",
    max_instances=1,
    coalesce=True,
)
async def iiko_courier_attendance_sync_job() -> None:
    await run_with_iiko_backoff(run_iiko_courier_attendance_sync_once)


@scheduler.scheduled_job(
    "interval",
    hours=1,
    id="iiko_courier_shift_matching",
    max_instances=1,
    coalesce=True,
)
async def iiko_courier_shift_matching_job() -> None:
    await run_iiko_courier_shift_matching_once()


@scheduler.scheduled_job(
    "interval",
    minutes=20,
    id="iiko_courier_delivery_sync",
    max_instances=1,
    coalesce=True,
)
async def iiko_courier_delivery_sync_job() -> None:
    await run_with_iiko_backoff(run_iiko_courier_delivery_sync_once)


async def run_iiko_courier_attendance_sync_once() -> dict[str, object]:
    now = datetime.now(MOSCOW_TZ)
    date_from = now.date() - timedelta(days=2)
    date_to = now.date() + timedelta(days=1)
    async with AsyncSessionLocal() as session:
        report = await sync_attendance(
            session,
            from_date=date_from,
            to_date=date_to,
            run_reason="cron",
        )
    logger.info("iiko courier attendance sync completed: %s", report.as_dict())
    return report.as_dict()


async def run_iiko_courier_shift_matching_once() -> dict[str, object]:
    now = datetime.now(MOSCOW_TZ)
    date_from = now.date() - timedelta(days=2)
    date_to = now.date() + timedelta(days=1)
    async with AsyncSessionLocal() as session:
        report = await recalculate_matches(session, date_from, date_to)
        await session.commit()
    logger.info("iiko courier shift matching completed: %s", report.as_dict())
    return report.as_dict()


async def run_iiko_courier_delivery_sync_once() -> dict[str, object]:
    now = datetime.now(MOSCOW_TZ)
    date_from = now.date() - timedelta(days=1)
    date_to = now.date()
    async with AsyncSessionLocal() as session:
        result = await sync_courier_olap_deliveries(
            session, date_from=date_from, date_to=date_to
        )
        await session.commit()
    payload = result.as_dict()
    logger.info("iiko courier delivery sync completed: %s", payload)
    return payload


async def run_with_iiko_backoff(operation: Callable[[], Awaitable[object]]) -> None:
    for attempt in range(1, IIKO_COURIER_JOB_RETRIES + 1):
        try:
            await operation()
            return
        except Exception as exc:  # noqa: BLE001 - scheduler must log and survive iiko outages
            if attempt == IIKO_COURIER_JOB_RETRIES or not is_iiko_retryable_error(exc):
                logger.exception("iiko courier attendance sync job failed")
                return
            delay = 2 ** (attempt - 1)
            logger.warning(
                "iiko courier attendance sync retry %s/%s in %ss after: %s",
                attempt + 1,
                IIKO_COURIER_JOB_RETRIES,
                delay,
                exc,
            )
            await asyncio.sleep(delay)


def is_iiko_retryable_error(exc: BaseException) -> bool:
    status = getattr(exc, "status", None)
    if status in {401, 403, 429}:
        return True
    message = str(exc).casefold()
    return any(
        marker in message
        for marker in (
            "auth",
            "unauthorized",
            "forbidden",
            "rate",
            "too many",
            "invalid key",
            "не авториз",
            "неверный ключ",
            "лимит",
        )
    )


async def run_bank_sync_job(
    *,
    provider: str,
    date_from: date | None = None,
    date_to: date | None = None,
    job_id: uuid.UUID | None = None,
) -> dict[str, object]:
    job_id = job_id or uuid.uuid4()
    async with AsyncSessionLocal() as session:
        if date_from is None or date_to is None:
            auto_from, auto_to = await _default_sync_period(session, provider)
            date_from = date_from or auto_from
            date_to = date_to or auto_to
        try:
            result = await sync_bank_provider(
                session,
                provider=provider,
                date_from=date_from,
                date_to=date_to,
            )
            await session.commit()
            return {"job_id": job_id, **result}
        except BankCredentialsError as exc:
            await session.rollback()
            await _create_invalid_credentials_case(session, provider, str(exc))
            await session.commit()
            logger.warning("Bank credentials error for %s: %s", provider, exc)
            return {"job_id": job_id, "provider": provider, "status": "invalid_credentials"}


async def sync_bank_provider(
    session: AsyncSession, *, provider: str, date_from: date, date_to: date
) -> dict[str, object]:
    client = _client_for_provider(provider, session)
    metadata = await client.fetch_account_metadata()
    await _upsert_accounts_from_metadata(session, provider, metadata)
    operations = await client.fetch_statement(date_from=date_from, date_to=date_to)

    inserted = 0
    updated = 0
    for operation in operations:
        account = await _account_for_operation(session, provider, operation)
        existing = await session.scalar(
            select(BankOperation).where(
                BankOperation.provider == provider,
                BankOperation.provider_operation_id == operation.provider_operation_id,
            )
        )
        if existing is None:
            session.add(_bank_operation_from_normalized(operation, account.id if account else None))
            inserted += 1
        else:
            _update_bank_operation(existing, operation, account.id if account else None)
            updated += 1

    await session.flush()
    own_accounts_added = await sync_own_accounts(session, provider=provider)
    pending_operations = (
        await session.scalars(
            select(BankOperation).where(
                BankOperation.provider == provider,
                BankOperation.classification_status == "pending",
            )
        )
    ).all()
    classification = await run_classification_rules(session, pending_operations)
    return {
        "provider": provider,
        "status": "completed",
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "fetched": len(operations),
        "inserted": inserted,
        "updated": updated,
        "own_accounts_added": own_accounts_added,
        "classification": classification.__dict__,
    }


def _client_for_provider(provider: str, session: AsyncSession) -> SberClient | TbankClient:
    if provider == "sber":
        return SberClient(session)
    if provider == "tbank":
        return TbankClient(session)
    raise ValueError(f"Unsupported bank provider: {provider}")


def _bank_sync_providers() -> tuple[str, ...]:
    providers: list[str] = []
    for raw_provider in get_settings().bank_sync_providers.split(","):
        provider = raw_provider.strip().casefold()
        if not provider:
            continue
        if provider not in SUPPORTED_BANK_PROVIDERS:
            logger.warning("Ignoring unsupported bank sync provider: %s", provider)
            continue
        if provider not in providers:
            providers.append(provider)
    return tuple(providers) or ("tbank",)


async def _default_sync_period(session: AsyncSession, provider: str) -> tuple[date, date]:
    latest = await session.scalar(
        select(func.max(BankOperation.operation_date)).where(BankOperation.provider == provider)
    )
    today = datetime.now(MOSCOW_TZ).date()
    if latest is None:
        return today - timedelta(days=30), today
    return latest - timedelta(days=1), today


async def _upsert_accounts_from_metadata(
    session: AsyncSession, provider: str, metadata: list[AccountMeta]
) -> None:
    for row in metadata:
        account = await _account_for_number(session, provider, row.account_number)
        if account is None:
            account = await _claim_seed_account(session, provider)
        if account is None:
            account = Account(
                bank_code=provider,
                account_number=row.account_number,
                bic=row.bic,
                legal_entity=LEGAL_ENTITY,
                currency=row.currency,
                status="active",
            )
            session.add(account)
            await session.flush()
        else:
            account.account_number = account.account_number or row.account_number
            account.bic = row.bic or account.bic
            account.currency = row.currency or account.currency
            account.status = "active"
        await _upsert_own_account_registry(session, provider, account, row)
    await session.flush()


async def _account_for_operation(
    session: AsyncSession, provider: str, operation: NormalizedBankOperation
) -> Account | None:
    number = clean_digits(operation.account_number)
    if not number:
        return None
    account = await _account_for_number(session, provider, number)
    if account is not None:
        return account
    account = await _claim_seed_account(session, provider)
    if account is None:
        account = Account(
            bank_code=provider,
            account_number=number,
            legal_entity=LEGAL_ENTITY,
            currency=operation.currency,
            status="active",
        )
        session.add(account)
    else:
        account.account_number = number
        account.currency = operation.currency or account.currency
    await session.flush()
    return account


async def _account_for_number(
    session: AsyncSession, provider: str, account_number: str
) -> Account | None:
    number = clean_digits(account_number)
    if not number:
        return None
    return await session.scalar(
        select(Account).where(
            Account.bank_code == provider,
            Account.account_number == number,
        )
    )


async def _claim_seed_account(session: AsyncSession, provider: str) -> Account | None:
    return await session.scalar(
        select(Account)
        .join(Wallet, Wallet.account_id == Account.id)
        .where(
            Account.bank_code == provider,
            Account.account_number.is_(None),
            Wallet.type == "bank",
            Wallet.status == "active",
            Wallet.is_internal_transfer_eligible.is_(True),
        )
        .order_by(Wallet.code)
    )


async def _upsert_own_account_registry(
    session: AsyncSession, provider: str, account: Account, metadata: AccountMeta
) -> None:
    if not account.account_number:
        return
    row = await session.scalar(
        select(OwnAccountsRegistry).where(
            OwnAccountsRegistry.bank_code == provider,
            OwnAccountsRegistry.account_number == account.account_number,
        )
    )
    if row is None:
        session.add(
            OwnAccountsRegistry(
                bank_code=provider,
                account_number=account.account_number,
                bic=metadata.bic or account.bic,
                legal_entity_inn=metadata.legal_entity_inn,
                account_id=account.id,
                is_active=True,
                metadata_json={"source": "bank_metadata"},
            )
        )
    else:
        row.bic = metadata.bic or account.bic or row.bic
        row.legal_entity_inn = metadata.legal_entity_inn or row.legal_entity_inn
        row.account_id = account.id
        row.is_active = True


def _bank_operation_from_normalized(
    operation: NormalizedBankOperation, account_id: uuid.UUID | None
) -> BankOperation:
    return BankOperation(
        provider=operation.provider,
        provider_operation_id=operation.provider_operation_id,
        account_id=account_id,
        operation_date=operation.operation_date,
        posted_at=operation.posted_at,
        direction=operation.direction,
        amount=operation.amount,
        currency=operation.currency,
        counterparty_name_raw=operation.counterparty_name_raw,
        counterparty_inn_raw=operation.counterparty_inn_raw,
        counterparty_account_raw=operation.counterparty_account_raw,
        payment_purpose=operation.payment_purpose,
        document_number=operation.document_number,
        raw_payload=operation.raw_payload,
        classification_status="pending",
    )


def _update_bank_operation(
    row: BankOperation, operation: NormalizedBankOperation, account_id: uuid.UUID | None
) -> None:
    row.account_id = account_id
    row.operation_date = operation.operation_date
    row.posted_at = operation.posted_at
    row.direction = operation.direction
    row.amount = operation.amount
    row.currency = operation.currency
    row.counterparty_name_raw = operation.counterparty_name_raw
    row.counterparty_inn_raw = operation.counterparty_inn_raw
    row.counterparty_account_raw = operation.counterparty_account_raw
    row.payment_purpose = operation.payment_purpose
    row.document_number = operation.document_number
    row.raw_payload = operation.raw_payload


async def _create_invalid_credentials_case(
    session: AsyncSession, provider: str, error: str
) -> ReconciliationCase:
    return await create_or_update_reconciliation_case(
        session,
        kind="invalid_credentials",
        provider=provider,
        payload={"provider": provider, "error": error},
    )
