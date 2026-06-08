from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.models import (
    AppSetting,
    PayrollBankDraft,
    PayrollLine,
    PayrollPayment,
    PayrollPeriod,
    PayrollRun,
    PayrollRunEvent,
)
from app.services.banking import BankClient, TbankClient
from app.services.banking.exceptions import BankFetchError
from app.services.banking.tbank import build_payment_draft_api_payload
from app.services.payroll_runner import PayrollConflictError, PayrollNotFoundError, money_text

PAYOUT_REQUISITES_KEY = "payroll.bank_payout_requisites"
DEFAULT_PAYMENT_PURPOSE_TEMPLATE = "Выплата заработной платы за период {start}–{end}"
MOCK_PAYER_ACCOUNT = "00000000000000000000"
PAYROLL_BANK_DRAFT_STATUSES = frozenset({"created", "updated", "paid", "failed"})


async def set_payout_split(
    session: AsyncSession,
    run_id: uuid.UUID,
    employee_id: uuid.UUID,
    *,
    amount_cash: Decimal,
    actor_user_id: uuid.UUID | None,
) -> PayrollPayment:
    await _get_payout_run(session, run_id)
    total = await _employee_payable_amount(session, run_id, employee_id)
    cash = _money(amount_cash)
    if cash < 0 or cash > total:
        raise PayrollConflictError("Наличная часть должна быть от 0 до суммы к выплате")

    payment = await session.scalar(
        select(PayrollPayment).where(
            PayrollPayment.run_id == run_id,
            PayrollPayment.employee_id == employee_id,
        )
    )
    if payment is not None and payment.status == "paid":
        raise PayrollConflictError("Выплата уже отмечена, сначала откатите")

    account = total - cash
    if payment is None:
        payment = PayrollPayment(
            id=uuid.uuid4(),
            run_id=run_id,
            employee_id=employee_id,
            amount=total,
            amount_cash=cash,
            amount_account=account,
            status="planned",
        )
        session.add(payment)
    else:
        payment.amount = total
        payment.amount_cash = cash
        payment.amount_account = account
        payment.status = "planned"
        payment.draft_document_id = None
        payment.draft_status = None
        payment.draft_synced_at = None

    await session.commit()
    await session.refresh(payment)
    return payment


async def create_or_update_run_draft(
    session: AsyncSession,
    run_id: uuid.UUID,
    *,
    actor_user_id: uuid.UUID | None,
    bank_client: BankClient | None = None,
) -> PayrollBankDraft:
    run = await _get_payout_run(session, run_id)
    period = await _get_run_period(session, run)
    requisites = await _bank_payout_requisites(session)
    settings = get_settings()
    payer_account = _payer_account(settings)
    total_account = await _total_account_amount(session, run_id, sync_payments=True)
    if total_account <= 0:
        raise PayrollConflictError("РС-часть ведомости равна нулю")

    document_id = run_payout_document_id(run_id)
    purpose = _payment_purpose(requisites, run_id=run_id, period=period)
    try:
        payload = build_payment_draft_api_payload(
            document_id=document_id,
            amount=total_account,
            purpose=purpose,
            requisites=requisites,
            payer_account=payer_account,
        )
    except ValueError as exc:
        raise PayrollConflictError(str(exc)) from exc

    existing = await _get_bank_draft(session, run_id)
    client = bank_client or TbankClient(session)
    try:
        result = await client.create_payment_draft(
            document_id=document_id,
            amount=total_account,
            purpose=purpose,
            requisites=dict(requisites),
            payer_account=payer_account,
        )
    except BankFetchError as exc:
        draft = await _upsert_bank_draft(
            session,
            existing=existing,
            run_id=run_id,
            document_id=document_id,
            amount=total_account,
            status="failed",
            provider_ref=None,
            payload=payload,
            last_error=str(exc),
        )
        _add_payout_event(
            session,
            run=run,
            action="bank_draft_failed",
            actor_user_id=actor_user_id,
            payload=_draft_event_payload(draft) | {"error": str(exc)},
        )
        await session.commit()
        raise

    draft = await _upsert_bank_draft(
        session,
        existing=existing,
        run_id=run_id,
        document_id=document_id,
        amount=total_account,
        status="updated" if existing is not None else _safe_draft_status(result.status, "created"),
        provider_ref=result.provider_ref,
        payload=payload,
        last_error=None,
    )
    await _mark_run_account_payments_as_draft_created(session, run_id)
    _add_payout_event(
        session,
        run=run,
        action="bank_draft_updated" if existing is not None else "bank_draft_created",
        actor_user_id=actor_user_id,
        payload=_draft_event_payload(draft),
    )
    await session.commit()
    await session.refresh(draft)
    return draft


async def get_run_bank_draft(
    session: AsyncSession,
    run_id: uuid.UUID,
) -> PayrollBankDraft | None:
    await _get_payout_run(session, run_id)
    return await _get_bank_draft(session, run_id)


async def get_run_payout_delta(session: AsyncSession, run_id: uuid.UUID) -> dict[str, Any]:
    await _get_payout_run(session, run_id)
    draft = await _get_bank_draft(session, run_id)
    previous_amount = _money(draft.amount) if draft is not None else Decimal("0.00")
    new_amount = await _total_account_amount(session, run_id, sync_payments=False)
    delta = new_amount - previous_amount
    return {
        "run_id": run_id,
        "document_id": draft.document_id if draft is not None else None,
        "previous_amount": previous_amount,
        "new_amount": new_amount,
        "delta": delta,
        "classification": _delta_classification(delta),
    }


async def apply_run_payout_delta(
    session: AsyncSession,
    run_id: uuid.UUID,
    *,
    actor_user_id: uuid.UUID | None,
    bank_client: BankClient | None = None,
) -> int:
    run = await _get_payout_run(session, run_id)
    draft = await _get_bank_draft(session, run_id)
    if draft is None:
        raise PayrollConflictError("Сначала создайте банковский черновик ведомости")

    new_amount = await _total_account_amount(session, run_id, sync_payments=True)
    previous_amount = _money(draft.amount)
    delta = new_amount - previous_amount
    if delta == 0:
        await session.commit()
        return 0

    if delta > 0:
        await _apply_topup_delta(
            session,
            run=run,
            draft=draft,
            previous_amount=previous_amount,
            new_amount=new_amount,
            delta=delta,
            actor_user_id=actor_user_id,
            bank_client=bank_client,
        )
    else:
        overpaid = -delta
        draft.amount = new_amount
        draft.status = "updated"
        draft.last_error = None
        draft.synced_at = datetime.now(UTC)
        _add_payout_event(
            session,
            run=run,
            action="payout_overpaid",
            actor_user_id=actor_user_id,
            payload={
                "document_id": draft.document_id,
                "previous_amount": money_text(previous_amount),
                "new_amount": money_text(new_amount),
                "overpaid_amount": money_text(overpaid),
                "note": "Излишек остаётся на бизнес-карте владельца",
            },
        )

    await session.commit()
    return 1


async def create_or_update_drafts(
    session: AsyncSession,
    run_id: uuid.UUID,
    *,
    actor_user_id: uuid.UUID | None,
    bank_client: BankClient | None = None,
) -> int:
    await create_or_update_run_draft(
        session,
        run_id,
        actor_user_id=actor_user_id,
        bank_client=bank_client,
    )
    return 1


async def get_payout_deltas(session: AsyncSession, run_id: uuid.UUID) -> list[dict[str, Any]]:
    return [await get_run_payout_delta(session, run_id)]


async def apply_payout_deltas(
    session: AsyncSession,
    run_id: uuid.UUID,
    *,
    actor_user_id: uuid.UUID | None,
    bank_client: BankClient | None = None,
) -> int:
    return await apply_run_payout_delta(
        session,
        run_id,
        actor_user_id=actor_user_id,
        bank_client=bank_client,
    )


def run_payout_document_id(run_id: uuid.UUID) -> str:
    return f"teplo-payroll-{run_id}"


def payout_document_id(run_id: uuid.UUID, employee_id: uuid.UUID | None = None) -> str:
    return run_payout_document_id(run_id)


async def next_topup_document_id(
    session: AsyncSession,
    run_id: uuid.UUID,
    employee_id: uuid.UUID | None = None,
) -> str:
    events = (
        await session.scalars(
            select(PayrollRunEvent).where(
                PayrollRunEvent.run_id == run_id,
                PayrollRunEvent.action == "bank_draft_topup",
            )
        )
    ).all()
    return f"{run_payout_document_id(run_id)}-topup-{len(events) + 1}"


async def _apply_topup_delta(
    session: AsyncSession,
    *,
    run: PayrollRun,
    draft: PayrollBankDraft,
    previous_amount: Decimal,
    new_amount: Decimal,
    delta: Decimal,
    actor_user_id: uuid.UUID | None,
    bank_client: BankClient | None,
) -> None:
    period = await _get_run_period(session, run)
    requisites = await _bank_payout_requisites(session)
    settings = get_settings()
    payer_account = _payer_account(settings)
    document_id = await next_topup_document_id(session, run.id)
    purpose = _payment_purpose(requisites, run_id=run.id, period=period)
    try:
        payload = build_payment_draft_api_payload(
            document_id=document_id,
            amount=delta,
            purpose=purpose,
            requisites=requisites,
            payer_account=payer_account,
        )
    except ValueError as exc:
        raise PayrollConflictError(str(exc)) from exc

    client = bank_client or TbankClient(session)
    try:
        result = await client.create_payment_draft(
            document_id=document_id,
            amount=delta,
            purpose=purpose,
            requisites=dict(requisites),
            payer_account=payer_account,
        )
    except BankFetchError as exc:
        draft.status = "failed"
        draft.last_error = str(exc)
        draft.payload = {"last_action": "topup", "payload": payload}
        draft.synced_at = datetime.now(UTC)
        _add_payout_event(
            session,
            run=run,
            action="bank_draft_failed",
            actor_user_id=actor_user_id,
            payload={
                "document_id": document_id,
                "amount": money_text(delta),
                "error": str(exc),
            },
        )
        await session.commit()
        raise

    draft.amount = new_amount
    draft.status = "updated"
    draft.provider_ref = result.provider_ref
    draft.payload = {"last_action": "topup", "payload": payload}
    draft.last_error = None
    draft.synced_at = datetime.now(UTC)
    _add_payout_event(
        session,
        run=run,
        action="bank_draft_topup",
        actor_user_id=actor_user_id,
        payload={
            "document_id": document_id,
            "previous_amount": money_text(previous_amount),
            "new_amount": money_text(new_amount),
            "delta": money_text(delta),
            "draft_status": _safe_draft_status(result.status, "created"),
            "provider_ref": result.provider_ref,
        },
    )


async def _get_payout_run(session: AsyncSession, run_id: uuid.UUID) -> PayrollRun:
    run = await session.get(PayrollRun, run_id)
    if run is None:
        raise PayrollNotFoundError("Payroll run not found")
    if run.is_imported_legacy:
        raise PayrollConflictError("Импортированная ведомость — выплаты не отмечаются")
    if run.status != "finalized":
        raise PayrollConflictError("Сначала финализируйте ведомость")
    return run


async def _get_run_period(session: AsyncSession, run: PayrollRun) -> PayrollPeriod:
    period = await session.get(PayrollPeriod, run.period_id)
    if period is None:
        raise PayrollNotFoundError("Payroll period not found")
    return period


async def _employee_payable_amount(
    session: AsyncSession,
    run_id: uuid.UUID,
    employee_id: uuid.UUID,
) -> Decimal:
    amount = await session.scalar(
        select(func.sum(PayrollLine.total_payable)).where(
            PayrollLine.run_id == run_id,
            PayrollLine.employee_id == employee_id,
        )
    )
    if amount is None:
        raise PayrollConflictError("У сотрудника нет начислений в этой ведомости")
    return _money(amount)


async def _bank_payout_requisites(session: AsyncSession) -> Mapping[str, Any]:
    setting = await session.scalar(
        select(AppSetting).where(AppSetting.key == PAYOUT_REQUISITES_KEY)
    )
    if setting is None or not isinstance(setting.value, Mapping):
        raise PayrollConflictError("Не настроены реквизиты payroll.bank_payout_requisites")
    return setting.value


def _payer_account(settings: Settings) -> str:
    if settings.tbank_api_account_number:
        return settings.tbank_api_account_number
    if settings.teplo_bank_client_mode == "mock":
        return MOCK_PAYER_ACCOUNT
    raise PayrollConflictError("Не настроен T-Bank расчётный счёт плательщика")


async def _get_bank_draft(
    session: AsyncSession,
    run_id: uuid.UUID,
) -> PayrollBankDraft | None:
    return await session.scalar(select(PayrollBankDraft).where(PayrollBankDraft.run_id == run_id))


async def _upsert_bank_draft(
    session: AsyncSession,
    *,
    existing: PayrollBankDraft | None,
    run_id: uuid.UUID,
    document_id: str,
    amount: Decimal,
    status: str,
    provider_ref: str | None,
    payload: dict[str, Any],
    last_error: str | None,
) -> PayrollBankDraft:
    draft = existing or await _get_bank_draft(session, run_id)
    if draft is None:
        draft = PayrollBankDraft(id=uuid.uuid4(), run_id=run_id)
        session.add(draft)

    draft.document_id = document_id[:64]
    draft.amount = _money(amount)
    draft.status = _safe_draft_status(status, "updated")
    draft.provider_ref = provider_ref
    draft.payload = payload
    draft.last_error = last_error
    draft.synced_at = datetime.now(UTC)
    await session.flush()
    return draft


async def _total_account_amount(
    session: AsyncSession,
    run_id: uuid.UUID,
    *,
    sync_payments: bool,
) -> Decimal:
    rows = await _payment_rows_with_current_totals(session, run_id)
    total = Decimal("0.00")
    for payment, new_amount in rows:
        amount_cash = min(_money(payment.amount_cash), new_amount)
        amount_account = new_amount - amount_cash
        total += amount_account
        if sync_payments:
            payment.amount = new_amount
            payment.amount_cash = amount_cash
            payment.amount_account = amount_account
            if payment.status != "paid" and amount_account == 0:
                payment.status = "planned"
                payment.draft_document_id = None
                payment.draft_status = None
                payment.draft_synced_at = None
    if sync_payments:
        await session.flush()
    return _money(total)


async def _payment_rows_with_current_totals(
    session: AsyncSession,
    run_id: uuid.UUID,
) -> list[tuple[PayrollPayment, Decimal]]:
    result = await session.execute(
        select(
            PayrollPayment,
            func.coalesce(func.sum(PayrollLine.total_payable), 0),
        )
        .outerjoin(
            PayrollLine,
            (PayrollLine.run_id == PayrollPayment.run_id)
            & (PayrollLine.employee_id == PayrollPayment.employee_id),
        )
        .where(PayrollPayment.run_id == run_id)
        .group_by(PayrollPayment.id)
        .order_by(PayrollPayment.employee_id)
    )
    return [(payment, _money(total)) for payment, total in result.all()]


async def _mark_run_account_payments_as_draft_created(
    session: AsyncSession,
    run_id: uuid.UUID,
) -> None:
    payments = (
        await session.scalars(
            select(PayrollPayment).where(
                PayrollPayment.run_id == run_id,
                PayrollPayment.amount_account > 0,
                PayrollPayment.status != "paid",
            )
        )
    ).all()
    for payment in payments:
        payment.status = "draft_created"
        payment.draft_document_id = None
        payment.draft_status = None
        payment.draft_synced_at = None


def _payment_purpose(
    requisites: Mapping[str, Any],
    *,
    run_id: uuid.UUID,
    period: PayrollPeriod,
) -> str:
    template = str(
        requisites.get("paymentPurpose")
        or requisites.get("paymentPurposeTemplate")
        or DEFAULT_PAYMENT_PURPOSE_TEMPLATE
    )
    try:
        return template.format(
            start=period.start_date.isoformat(),
            end=period.end_date.isoformat(),
            payroll_date=period.payroll_date.isoformat(),
            run_id=run_id,
        )
    except (KeyError, ValueError) as exc:
        raise PayrollConflictError("Некорректный шаблон назначения платежа") from exc


def _draft_event_payload(draft: PayrollBankDraft) -> dict[str, Any]:
    return {
        "run_id": str(draft.run_id),
        "document_id": draft.document_id,
        "amount_account": money_text(draft.amount),
        "draft_status": draft.status,
        "provider_ref": draft.provider_ref,
    }


def _safe_draft_status(value: str | None, fallback: str) -> str:
    if value in PAYROLL_BANK_DRAFT_STATUSES:
        return str(value)
    return fallback


def _delta_classification(delta: Decimal) -> str:
    if delta > 0:
        return "topup"
    if delta < 0:
        return "overpay"
    return "unchanged"


def _money(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"))


def _add_payout_event(
    session: AsyncSession,
    *,
    run: PayrollRun,
    action: str,
    actor_user_id: uuid.UUID | None,
    payload: dict[str, Any],
) -> None:
    session.add(
        PayrollRunEvent(
            run_id=run.id,
            period_id=run.period_id,
            action=action,
            actor_user_id=actor_user_id,
            payload=payload,
        )
    )
