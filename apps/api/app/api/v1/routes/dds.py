from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy import false, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    CurrentActor,
    ensure_any_permission,
    ensure_permission,
    get_current_actor,
    require_permission,
)
from app.auth.permissions import permission_is_granted
from app.db.session import get_session
from app.models import (
    Account,
    BankOperation,
    CashflowTransaction,
    ClassificationRule,
    Counterparty,
    CounterpartyAlias,
    CounterpartyPaymentDraft,
    DdsArticle,
    DdsArticleAlias,
    ReconciliationCase,
    SafeAllocation,
    SourceCredential,
    SupplierInvoice,
    Wallet,
    WalletBalanceSnapshot,
)
from app.scheduler import run_bank_sync_job
from app.schemas.dds import (
    AllocationMoveRequest,
    BankOperationListRead,
    BankSyncQueuedRead,
    BankSyncRequest,
    CashflowClassifyRead,
    CashflowClassifyRequest,
    CashflowTransactionListRead,
    ClassificationRuleCreate,
    ClassificationRulePatch,
    ClassificationRuleRead,
    CredentialCreate,
    CredentialRead,
    DdsAliasCreate,
    DdsAliasRead,
    DdsArticleCreate,
    DdsArticlePatch,
    DdsArticleRead,
    DdsCounterpartyCreate,
    DdsCounterpartyPatch,
    DdsCounterpartyRead,
    DdsWalletRead,
    InternalTransferCreate,
    InternalTransferRead,
    JournalListRead,
    NewPaymentContextRead,
    NewPaymentExpenseCashCreate,
    NewPaymentExpenseCashRead,
    NewPaymentExpenseDraftCreate,
    NewPaymentExpenseDraftRead,
    NewPaymentIncomeCreate,
    NewPaymentIncomeRead,
    NewPaymentTransferCreate,
    NewPaymentTransferRead,
    OperationClassifyRead,
    OperationClassifyRequest,
    OwnerReviewActionRead,
    OwnerReviewClassifyRequest,
    OwnerReviewListRead,
    PayoutAttributionEmployeeRead,
    SafeAllocationCreate,
    SafeAllocationPayRequest,
    SafeAllocationRead,
    SafeCashWithdrawRequest,
    SafeReconcileRead,
    SafeReconcileRequest,
    TransactionClassifyRequest,
)
from app.schemas.kassa import KassaPendingRead
from app.services.advance_iiko_payout import post_advance_payout_to_iiko
from app.services.banking import BankCredentialsError, BankFetchError
from app.services.banking.cashflow_classify import (
    EXCLUDED_QUALITY,
    apply_cashflow_exclude,
    apply_cashflow_split,
)
from app.services.banking.classifier import (
    AWAITING_BANK_QUALITY,
    EMPLOYEE_PAYOUT_ARTICLE_CODES,
    SAFE_WALLET_CODE,
    apply_operation_action,
    apply_operation_split,
    book_safe_topup,
    close_reconciliation_case,
    resolve_or_create_operation_counterparty,
)
from app.services.banking.credentials import set_credential
from app.services.banking.safe_allocations import (
    CASH_WITHDRAWAL_WALLET_CODE,
    allocation_advance_draft_id,
    book_internal_transfer,
    book_safe_cash_withdrawal,
    book_safe_drift_adjustment,
    book_safe_topup_reserves,
    cancel_allocation,
    create_allocation,
    kassa_targets_count,
    kassa_targets_total,
    move_allocation_location,
    pay_allocation,
    safe_active_allocations_count,
    safe_reserved_total,
    transfer_allocation_to_kassa,
)
from app.services.banking.transfer_matching import find_and_link_transfer_pairs
from app.services.counterparty_bank_match import _is_card_noise
from app.services.counterparty_payments import (
    CounterpartyPaymentError,
    ExpenseLineInput,
    create_bank_safe_topup_draft,
    create_expense_payment_draft,
)
from app.services.kassa.payouts import (
    KassaPayoutError,
    ensure_article_kassa_eligible,
    kassa_pending_payload,
)
from app.services.new_payment import (
    NEW_PAYMENT_PERMISSION_CODES,
    build_new_payment_context,
    ensure_income_article_allowed,
    ensure_reservable_article_allowed,
    list_payout_attribution_employees,
)
from app.services.payroll_advance_service import (
    book_operation_advance,
    list_kassa_pending_advances,
    sync_advance_after_allocation_change,
)
from app.services.payroll_runner import PayrollConflictError, PayrollNotFoundError
from app.services.supplier_prepayments import (
    SUPPLIER_REFUND_ARTICLE_CODE,
    refund_counterparty_prepayments,
)
from app.services.warehouse_invoices import invoice_permission_kind

# Статьи аванса/займа сотрудника: разбор операции на них заводит SalaryAdvance (деньги ушли
# банком). Дубль kassa/payouts.py (payroll_advance_service) — во избежание тяжёлого импорта.
EMPLOYEE_ADVANCE_ARTICLE_CODE = "employee_advance"
EMPLOYEE_LOAN_ARTICLE_CODE = "vydacha_zaymov_sotrudnikam"
EMPLOYEE_ADVANCE_ARTICLE_CODES = (EMPLOYEE_ADVANCE_ARTICLE_CODE, EMPLOYEE_LOAN_ARTICLE_CODE)

router = APIRouter()
MOSCOW_TZ = ZoneInfo("Europe/Moscow")
DDS_READ_ACCESS = (Depends(require_permission("finance.cashflow.read")),)
DDS_EDIT_ACCESS = (Depends(require_permission("finance.cashflow.edit")),)
DDS_RULES_MANAGE_ACCESS = (Depends(require_permission("finance.classification_rules.manage")),)
DDS_CLASSIFY_ACCESS = (Depends(require_permission("finance.cashflow.classify")),)
DDS_SAFE_ALLOCATE_ACCESS = (Depends(require_permission("finance.safe.allocate")),)
DDS_SAFE_CONFIRM_PAID_ACCESS = (Depends(require_permission("finance.safe.confirm_paid")),)
DDS_INTEGRATIONS_MANAGE_ACCESS = (
    Depends(require_permission("finance.cashflow.integrations.manage")),
)
DDS_WALLETS_READ_ACCESS = (Depends(require_permission("finance.wallets.read")),)
DDS_COUNTERPARTIES_READ_ACCESS = (Depends(require_permission("finance.counterparties.read")),)
DDS_COUNTERPARTIES_EDIT_ACCESS = (Depends(require_permission("finance.counterparties.edit")),)
DDS_OWNER_REVIEW_PREPARE_ACCESS = (Depends(require_permission("finance.owner_review.prepare")),)


@router.get("/bank-operations", response_model=BankOperationListRead, dependencies=DDS_READ_ACCESS)
async def list_bank_operations(
    session: Annotated[AsyncSession, Depends(get_session)],
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
    provider: str | None = None,
    classification_status: str | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, object]:
    conditions = []
    if date_from is not None:
        conditions.append(BankOperation.operation_date >= date_from)
    if date_to is not None:
        conditions.append(BankOperation.operation_date <= date_to)
    if provider is not None:
        conditions.append(BankOperation.provider == provider)
    if classification_status is not None:
        conditions.append(BankOperation.classification_status == classification_status)

    total = int(
        await session.scalar(select(func.count()).select_from(BankOperation).where(*conditions))
        or 0
    )
    rows = await session.scalars(
        select(BankOperation)
        .where(*conditions)
        .order_by(BankOperation.operation_date.desc(), BankOperation.imported_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return {"items": [_bank_operation_payload(row) for row in rows.all()], "total": total}


@router.get("/cashflow", response_model=CashflowTransactionListRead, dependencies=DDS_READ_ACCESS)
async def list_cashflow(
    session: Annotated[AsyncSession, Depends(get_session)],
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
    wallet_id: UUID | None = None,
    article_id: UUID | None = None,
    direction: Literal["in", "out"] | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, object]:
    conditions = []
    if date_from is not None:
        conditions.append(CashflowTransaction.operation_date >= date_from)
    if date_to is not None:
        conditions.append(CashflowTransaction.operation_date <= date_to)
    if wallet_id is not None:
        conditions.append(CashflowTransaction.wallet_id == wallet_id)
    if article_id is not None:
        conditions.append(CashflowTransaction.article_id == article_id)
    if direction is not None:
        conditions.append(CashflowTransaction.direction == direction)

    total = int(
        await session.scalar(
            select(func.count()).select_from(CashflowTransaction).where(*conditions)
        )
        or 0
    )
    rows = await session.scalars(
        select(CashflowTransaction)
        .where(*conditions)
        .order_by(CashflowTransaction.operation_date.desc(), CashflowTransaction.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return {"items": [_cashflow_payload(row) for row in rows.all()], "total": total}


@router.get("/journal", response_model=JournalListRead, dependencies=DDS_READ_ACCESS)
async def list_journal(
    session: Annotated[AsyncSession, Depends(get_session)],
    status: Literal["all", "marked", "unmarked", "transfers"] = "all",
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
    direction: Literal["in", "out"] | None = None,
    wallet_id: UUID | None = None,
    article_id: UUID | None = None,
    counterparty_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, object]:
    """Unified DDS journal: classified cashflow movements + bank operations awaiting review.

    ``status`` is the quick filter: ``marked`` (classified movements), ``unmarked``
    (operations needing review), or ``all`` (both, newest first). Date window keeps the
    merged set small enough to sort/paginate in memory.
    """
    cf_conditions = []
    op_conditions = [BankOperation.classification_status == "needs_review"]
    # Внутренние переводы (счёт→счёт) — это движение денег, поэтому показываем их в
    # журнале отдельным статусом «Внутренний перевод» (не доход/не расход, проводки-
    # cashflow у них нет). Баланс их и так учитывает (≠ excluded).
    transfer_conditions = [BankOperation.classification_status == "internal_transfer"]
    if date_from is not None:
        cf_conditions.append(CashflowTransaction.operation_date >= date_from)
        op_conditions.append(BankOperation.operation_date >= date_from)
        transfer_conditions.append(BankOperation.operation_date >= date_from)
    if date_to is not None:
        cf_conditions.append(CashflowTransaction.operation_date <= date_to)
        op_conditions.append(BankOperation.operation_date <= date_to)
        transfer_conditions.append(BankOperation.operation_date <= date_to)
    if direction is not None:
        cf_conditions.append(CashflowTransaction.direction == direction)
        op_conditions.append(BankOperation.direction == direction)
        transfer_conditions.append(BankOperation.direction == direction)
    if wallet_id is not None:
        cf_conditions.append(CashflowTransaction.wallet_id == wallet_id)
        wallet = await session.get(Wallet, wallet_id)
        if wallet is not None and wallet.account_id is not None:
            op_conditions.append(BankOperation.account_id == wallet.account_id)
            transfer_conditions.append(BankOperation.account_id == wallet.account_id)
        else:
            op_conditions.append(false())
            transfer_conditions.append(false())
    if article_id is not None:
        cf_conditions.append(CashflowTransaction.article_id == article_id)
        op_conditions.append(false())
        transfer_conditions.append(false())
    if counterparty_id is not None:
        cf_conditions.append(CashflowTransaction.counterparty_id == counterparty_id)
        counterparty = await session.get(Counterparty, counterparty_id)
        if counterparty is None:
            op_conditions.append(false())
            transfer_conditions.append(false())
        else:
            raw_counterparty_conditions = [
                func.lower(BankOperation.counterparty_name_raw) == counterparty.name.strip().lower()
            ]
            if counterparty.inn:
                raw_counterparty_conditions.append(
                    BankOperation.counterparty_inn_raw == counterparty.inn
                )
            raw_counterparty_match = or_(*raw_counterparty_conditions)
            op_conditions.append(raw_counterparty_match)
            transfer_conditions.append(raw_counterparty_match)

    marked_total = int(
        await session.scalar(
            select(func.count()).select_from(CashflowTransaction).where(*cf_conditions)
        )
        or 0
    )
    unmarked_total = int(
        await session.scalar(
            select(func.count()).select_from(BankOperation).where(*op_conditions)
        )
        or 0
    )
    transfer_total = int(
        await session.scalar(
            select(func.count()).select_from(BankOperation).where(*transfer_conditions)
        )
        or 0
    )

    rows: list[dict[str, object]] = []
    wallet_id_by_account_id: dict[UUID, UUID] = {}
    if status in ("all", "unmarked", "transfers"):
        wallet_id_by_account_id = {
            account_id: wallet_id
            for account_id, wallet_id in (
                await session.execute(
                    select(Wallet.account_id, Wallet.id).where(Wallet.account_id.is_not(None))
                )
            ).all()
            if account_id is not None
        }
    if status in ("all", "marked"):
        cashflow_list = (
            await session.scalars(select(CashflowTransaction).where(*cf_conditions))
        ).all()
        # Cashflow classified out of a bank operation inherits that operation's
        # exact ``posted_at`` so it sorts by real banking time, not by the moment
        # it happened to be classified. One lookup avoids an N+1.
        bank_source_ids = {
            cf.source_id
            for cf in cashflow_list
            if cf.source_kind == "bank_operation" and cf.source_id is not None
        }
        posted_at_by_op: dict[UUID, datetime | None] = {}
        if bank_source_ids:
            posted_at_by_op = dict(
                (
                    await session.execute(
                        select(BankOperation.id, BankOperation.posted_at).where(
                            BankOperation.id.in_(bank_source_ids)
                        )
                    )
                ).all()
            )
        for cf in cashflow_list:
            time_source = cf.created_at
            if cf.source_kind == "bank_operation" and cf.source_id is not None:
                time_source = posted_at_by_op.get(cf.source_id) or cf.created_at
            # Пендинг-чек (ручной ввод, банк ещё не подтвердил) — отдельный статус журнала.
            # Проводка без статьи (article_id пуст) — требует ручной разметки: фронт делает такую
            # строку кликабельной для назначения статьи (для ручных проводок без bank-операции).
            if cf.quality_status == AWAITING_BANK_QUALITY:
                cf_status = "awaiting_confirmation"
            elif cf.quality_status == EXCLUDED_QUALITY:
                cf_status = "excluded"
            elif cf.article_id is None:
                cf_status = "needs_review"
            else:
                cf_status = "classified"
            rows.append(
                {
                    "kind": "cashflow",
                    "id": cf.id,
                    "bank_operation_id": cf.source_id
                    if cf.source_kind == "bank_operation"
                    else None,
                    "status": cf_status,
                    "operation_date": cf.operation_date,
                    "occurred_at": _journal_occurred_at(cf.operation_date, time_source),
                    "direction": cf.direction,
                    "amount": _money(cf.amount),
                    "article_id": cf.article_id,
                    "counterparty_id": cf.counterparty_id,
                    "wallet_id": cf.wallet_id,
                    "provider": None,
                    "payment_purpose": cf.payment_purpose,
                    "counterparty_name_raw": None,
                    "counterparty_inn_raw": None,
                }
            )
    if status in ("all", "unmarked"):
        operation_rows = await session.scalars(select(BankOperation).where(*op_conditions))
        for op in operation_rows.all():
            rows.append(
                {
                    "kind": "operation",
                    "id": op.id,
                    "bank_operation_id": op.id,
                    "status": "needs_review",
                    "operation_date": op.operation_date,
                    "occurred_at": _journal_occurred_at(op.operation_date, op.posted_at),
                    "direction": op.direction,
                    "amount": _money(op.amount),
                    "article_id": None,
                    "counterparty_id": None,
                    "wallet_id": wallet_id_by_account_id.get(op.account_id)
                    if op.account_id is not None
                    else None,
                    "provider": op.provider,
                    "payment_purpose": op.payment_purpose,
                    "counterparty_name_raw": op.counterparty_name_raw,
                    "counterparty_inn_raw": op.counterparty_inn_raw,
                    # Карт-операция (получатель в банке — эквайер): фронт показывает мягкое
                    # предупреждение при ручной привязке к накладной вместо жёсткой ошибки.
                    "is_card": _is_card_noise(op),
                }
            )

    if status in ("all", "transfers"):
        transfer_rows = await session.scalars(select(BankOperation).where(*transfer_conditions))
        for op in transfer_rows.all():
            rows.append(
                {
                    "kind": "operation",
                    "id": op.id,
                    "bank_operation_id": op.id,
                    "status": "internal_transfer",
                    "operation_date": op.operation_date,
                    "occurred_at": _journal_occurred_at(op.operation_date, op.posted_at),
                    "direction": op.direction,
                    "amount": _money(op.amount),
                    "article_id": None,
                    "counterparty_id": None,
                    "wallet_id": wallet_id_by_account_id.get(op.account_id)
                    if op.account_id is not None
                    else None,
                    "provider": op.provider,
                    "payment_purpose": op.payment_purpose,
                    "counterparty_name_raw": op.counterparty_name_raw,
                    "counterparty_inn_raw": op.counterparty_inn_raw,
                    # Карт-операция (получатель в банке — эквайер): фронт показывает мягкое
                    # предупреждение при ручной привязке к накладной вместо жёсткой ошибки.
                    "is_card": _is_card_noise(op),
                }
            )

    rows.sort(key=lambda row: (row["occurred_at"], row["amount"]), reverse=True)
    total = len(rows)
    return {
        "items": rows[offset : offset + limit],
        "total": total,
        "marked_total": marked_total,
        "unmarked_total": unmarked_total,
        "transfer_total": transfer_total,
    }


@router.get("/wallets", response_model=list[DdsWalletRead], dependencies=DDS_WALLETS_READ_ACCESS)
async def list_wallets(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[dict[str, object]]:
    result = await session.scalars(select(Wallet).order_by(Wallet.code))
    wallets = result.all()
    deltas = await _wallet_movement_deltas(session)
    bank_by_account = dict(
        (await session.execute(select(Account.id, Account.bank_code))).all()
    )
    payloads: list[dict[str, object]] = []
    for wallet in wallets:
        is_safe = wallet.code == SAFE_WALLET_CODE
        is_kassa = wallet.code == CASH_WITHDRAWAL_WALLET_CODE
        reserved = Decimal("0")
        active_count = 0
        pending_payout_count: int | None = None
        if is_safe:
            reserved = await safe_reserved_total(session, wallet.id)
            active_count = await safe_active_allocations_count(session, wallet.id)
        elif is_kassa:
            # Торговая касса: «целевые» = переданные целёвки, «к выдаче» = они же +
            # ожидающие разрешения на авансы/займы (для подписи на «Деньгах сегодня»).
            reserved = await kassa_targets_total(session, wallet.id)
            active_count = await kassa_targets_count(session, wallet.id)
            pending_payout_count = active_count + len(
                await list_kassa_pending_advances(session)
            )
        payloads.append(
            _wallet_payload(
                wallet,
                deltas.get(wallet.id, Decimal("0")),
                bank_by_account.get(wallet.account_id),
                reserved,
                active_count,
                pending_payout_count,
            )
        )
    return payloads


async def _wallet_movement_deltas(session: AsyncSession) -> dict[UUID, Decimal]:
    """Net cash movement per wallet — driven by the FACT of money moving.

    Balance is bank reality, not DDS classification:

    * Bank wallets follow their STATEMENT. Every imported bank operation moves the
      balance by its direction/amount the moment it lands, whether or not it has
      been classified into a DDS article yet — classification only decides which
      article a movement is filed under, it never gates the balance. Only
      operations explicitly marked ``excluded`` are left out. Internal transfers
      need no special handling: each leg is a real operation on its own account
      (out of one bank, in to another) that moves its wallet on its own, so the
      balance never depends on the transfer pair being matched. The bank's own
      reported balance (T-Bank ``otb``) is never used — it is inflated by the
      overdraft limit.
    * Non-bank wallets (cash safe, store cash, funds) have no statement, so their
      balance comes from the manually booked cashflow entries instead.

    Only movements dated AFTER ``opening_balance_date`` are counted: the opening
    snapshot already incorporates everything up to and including that date, so
    summing earlier movements would double-count them. Wallets with no opening
    date count all movements.
    """
    bank_types = ("bank", "bank_account")
    deltas: dict[UUID, Decimal] = defaultdict(lambda: Decimal("0"))

    # Bank wallets: balance IS the statement — every non-excluded operation counts,
    # classified or not, so "needs review" never hides money from the balance.
    after_opening_bank = or_(
        Wallet.opening_balance_date.is_(None),
        BankOperation.operation_date > Wallet.opening_balance_date,
    )
    bank_rows = await session.execute(
        select(
            Wallet.id,
            BankOperation.direction,
            func.coalesce(func.sum(BankOperation.amount), 0),
        )
        .join(Account, Account.id == Wallet.account_id)
        .join(BankOperation, BankOperation.account_id == Account.id)
        .where(
            Wallet.type.in_(bank_types),
            BankOperation.classification_status != "excluded",
            after_opening_bank,
        )
        .group_by(Wallet.id, BankOperation.direction)
    )
    for wallet_id, direction, total in bank_rows:
        amount = Decimal(total)
        deltas[wallet_id] += amount if direction == "in" else -amount

    # Non-bank wallets: no statement — balance comes from manual cashflow entries.
    # Мягко исключённые проводки (ручной разбор) из баланса выпадают — как excluded у банка.
    after_opening_cash = or_(
        Wallet.opening_balance_date.is_(None),
        CashflowTransaction.operation_date > Wallet.opening_balance_date,
    )
    cash_rows = await session.execute(
        select(
            CashflowTransaction.wallet_id,
            CashflowTransaction.direction,
            func.coalesce(func.sum(CashflowTransaction.amount), 0),
        )
        .join(Wallet, Wallet.id == CashflowTransaction.wallet_id)
        .where(
            Wallet.type.not_in(bank_types),
            CashflowTransaction.quality_status != EXCLUDED_QUALITY,
            after_opening_cash,
        )
        .group_by(CashflowTransaction.wallet_id, CashflowTransaction.direction)
    )
    for wallet_id, direction, total in cash_rows:
        if wallet_id is None:
            continue
        amount = Decimal(total)
        deltas[wallet_id] += amount if direction == "in" else -amount

    return deltas


@router.get("/articles", response_model=list[DdsArticleRead], dependencies=DDS_READ_ACCESS)
async def list_articles(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[dict[str, object]]:
    result = await session.scalars(select(DdsArticle).order_by(DdsArticle.code))
    return await _article_payloads(session, result.all())


@router.get("/new-payment/context", response_model=NewPaymentContextRead)
async def get_new_payment_context(
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, object]:
    """Контекст окна «Новый платёж»: статьи (по правам пользователя), счета, сотрудники.

    Свой справочник вместо ``/dds/articles``+``/dds/wallets``: окно доступно и без
    прав чтения ДДС (например, менеджеру с правом оплаты накладных), а фильтрация
    статей по маршрутам/правам делается на бэке — фронт только рендерит.
    """
    ensure_any_permission(actor, NEW_PAYMENT_PERMISSION_CODES)
    return await build_new_payment_context(session, permissions=actor.permissions)


@router.get(
    "/payout-employees",
    response_model=list[PayoutAttributionEmployeeRead],
    dependencies=DDS_CLASSIFY_ACCESS,
)
async def list_payout_attribution_employees_endpoint(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[dict[str, object]]:
    """Сотрудники для привязки выплаты при разборе операции журнала (активные + увольняемые).

    Обходит запрет ``/staff`` кассиру: тот же гейт, что и классификация операций
    (``finance.cashflow.classify``) — кто разбирает журнал, тот и выбирает получателя.
    """
    return await list_payout_attribution_employees(session)


@router.post(
    "/new-payment/expense-draft",
    response_model=NewPaymentExpenseDraftRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=DDS_SAFE_ALLOCATE_ACCESS,
)
async def post_new_payment_expense_draft(
    payload: NewPaymentExpenseDraftCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> CounterpartyPaymentDraft:
    """«Просто трата» без получателя: черновик на карту ИП с целевой статьёй/назначением.

    Право — как у ручного резерва Сейфа (``finance.safe.allocate``): итог оплаты
    черновика — та же целёвка, только пополненная транзитом с р/с. Подтверждение
    всегда в банке; проводки и целёвку создаёт вебхук-контур (paid-переход).
    """
    lines = [
        ExpenseLineInput(
            article_id=line.article_id,
            amount=line.amount,
            purpose=line.purpose,
            counterparty_id=line.counterparty_id,
        )
        for line in payload.normalized_lines()
    ]
    try:
        return await create_expense_payment_draft(
            session,
            lines=lines,
            channel=payload.channel,
            actor_user_id=actor.user_id,
        )
    except CounterpartyPaymentError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except BankCredentialsError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    except BankFetchError as exc:
        # Черновик уже сохранён со status='failed' — отдаём причину, а не голый 500.
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


@router.post(
    "/new-payment/expense-cash",
    response_model=NewPaymentExpenseCashRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=DDS_SAFE_ALLOCATE_ACCESS,
)
async def post_new_payment_expense_cash(
    payload: NewPaymentExpenseCashCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, object]:
    """Свободный вывод НАЛИЧНЫМИ: сразу резерв(ы) на Сейфе/в Кассе без банковского
    черновика. Деньги уже на счёте — по одному резерву на строку (статья+сумма+
    назначение), как у оплаченного транша. Дальше «Выплатить»/«Списать» из окна платежей.

    Право — как у ручного резерва Сейфа (``finance.safe.allocate``). Для Сейфа проверяем
    свободный остаток; касса толерантна к перерезерву (как её выдача), поэтому без гейта.

    ``pay_now=true`` («Создать платёж») — каждый резерв тут же оплачивается целиком
    (out-проводка, деньги реально ушли со счёта); требует дополнительно права
    ``finance.safe.confirm_paid`` — ровно как ``pay_full`` ручного резерва Сейфа.
    """
    if payload.pay_now:
        ensure_permission(actor, "finance.safe.confirm_paid")
    wallet = await session.get(Wallet, payload.wallet_id)
    is_cash_wallet = wallet is not None and wallet.type in ("cash_safe", "store_cash")
    if wallet is None or wallet.status != "active" or not is_cash_wallet:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Наличный счёт (Сейф/Касса) не найден"
        )
    location = "safe" if wallet.type == "cash_safe" else "kassa"

    prepared: list[tuple[DdsArticle, Decimal, str, UUID | None]] = []
    total = Decimal("0")
    for line in payload.lines:
        article = await session.get(DdsArticle, line.article_id)
        if article is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="Статья ДДС не найдена"
            )
        try:
            # Расходные статьи + «Авансы поставщикам» с контрагентом (резерв предоплаты:
            # дебиторка возникнет при выплате резерва — pay_allocation).
            ensure_reservable_article_allowed(
                article, has_counterparty=line.counterparty_id is not None
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        if line.counterparty_id is not None:
            cp = await session.get(Counterparty, line.counterparty_id)
            if cp is None or cp.status == "archived":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT, detail="Контрагент не найден"
                )
        amount = Decimal(line.amount)
        line_purpose = " ".join((line.purpose or "").split()) or article.name
        prepared.append((article, amount, line_purpose, line.counterparty_id))
        total += amount

    # Сейф: суммарный резерв не должен превышать свободный остаток.
    if location == "safe":
        free = await _safe_free_amount(session, wallet)
        if total > free:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Недостаточно свободных средств на Сейфе: "
                    f"свободно {free}, запрошено {total}"
                ),
            )

    today_msk = datetime.now(MOSCOW_TZ).date()
    for article, amount, line_purpose, cp_id in prepared:
        allocation = await create_allocation(
            session,
            wallet_id=wallet.id,
            amount=amount,
            free_amount=None,  # суммарный лимит уже проверен выше
            article_id=article.id,
            counterparty_id=cp_id,
            purpose=line_purpose,
            created_by_user_id=actor.user_id,
        )
        if location == "kassa":
            allocation.location = "kassa"
        if payload.pay_now:
            # Немедленная оплата резерва целиком; source_kind — как у штатной выдачи
            # соответствующего счёта (журнал Кассы различает свои проводки по нему).
            try:
                await pay_allocation(
                    session,
                    allocation,
                    amount=amount,
                    operation_date=today_msk,
                    source_kind=(
                        "kassa_target_payout" if location == "kassa" else "safe_payout"
                    ),
                    created_by_user_id=actor.user_id,
                )
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT, detail=str(exc)
                ) from exc
    await session.commit()
    return {
        "created": len(prepared),
        "total": float(total),
        "location": location,
        "paid": payload.pay_now,
    }


@router.post(
    "/new-payment/income-cash",
    response_model=NewPaymentIncomeRead,
    status_code=status.HTTP_201_CREATED,
    # Приход — реальное движение денег сразу: право подтверждения оплат (как pay_now).
    dependencies=DDS_SAFE_CONFIRM_PAID_ACCESS,
)
async def post_new_payment_income_cash(
    payload: NewPaymentIncomeCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, object]:
    """Наличное ПОСТУПЛЕНИЕ из окна «Новый платёж»: одна in-проводка на строку
    (статья+сумма+назначение) на Сейф или в Кассу. Приход — факт, не намерение:
    без резервов и черновиков. Банковские приходы вручную запрещены by design —
    баланс банка ведётся от выписки, их приносит вебхук/поллинг.
    """
    wallet = await session.get(Wallet, payload.wallet_id)
    is_cash_wallet = wallet is not None and wallet.type in ("cash_safe", "store_cash")
    if wallet is None or wallet.status != "active" or not is_cash_wallet:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Наличный счёт (Сейф/Касса) не найден"
        )
    location = "safe" if wallet.type == "cash_safe" else "kassa"

    operation_date = datetime.now(MOSCOW_TZ).date()
    # Проводка датой не позже опорного остатка кошелька в баланс не попадёт
    # (double-count-защита) — не даём провести приход «в никуда».
    if wallet.opening_balance_date is not None and operation_date <= wallet.opening_balance_date:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Дата поступления не позже опорного остатка кошелька",
        )

    total = Decimal("0")
    created = 0
    for line in payload.lines:
        article = await session.get(DdsArticle, line.article_id)
        if article is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="Статья ДДС не найдена"
            )
        try:
            ensure_income_article_allowed(article)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        if line.counterparty_id is not None:
            cp = await session.get(Counterparty, line.counterparty_id)
            if cp is None or cp.status == "archived":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT, detail="Контрагент не найден"
                )
        amount = Decimal(line.amount)
        line_purpose = " ".join((line.purpose or "").split()) or article.name
        # «Возврат переплаты от поставщиков» гасит открытые предоплаты контрагента
        # (FIFO): иначе дебиторка задваивается — деньги вернулись, а долг «висит».
        if article.code == SUPPLIER_REFUND_ARTICLE_CODE:
            if line.counterparty_id is None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Возврат от поставщика требует контрагента",
                )
            settled = await refund_counterparty_prepayments(
                session, counterparty_id=line.counterparty_id, amount=amount
            )
            if settled > 0:
                line_purpose = f"{line_purpose} (зачтено в предоплаты: {settled})"
        session.add(
            CashflowTransaction(
                wallet_id=wallet.id,
                direction="in",
                amount=amount,
                operation_date=operation_date,
                article_id=article.id,
                counterparty_id=line.counterparty_id,
                source_kind="new_payment_income",
                payment_purpose=line_purpose,
                quality_status="final",
                created_by_user_id=actor.user_id,
            )
        )
        total += amount
        created += 1
    await session.commit()
    return {"created": created, "total": float(total), "location": location}


@router.post(
    "/internal-transfer",
    response_model=InternalTransferRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=DDS_SAFE_CONFIRM_PAID_ACCESS,
)
async def post_internal_transfer(
    payload: InternalTransferCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, object]:
    """Внутренний перевод между наличными счетами (Сейф↔Касса).

    Обычный (``plain``) — просто перемещение суммы. Целевой (``targeted``) — перемещение
    Σ строк + целевой резерв на счёте-получателе по каждой строке (появляется в «Платежах»,
    корзины «На Сейфе»/«В кассе»). Получатель — только наличный счёт; на банковские нельзя.
    Сейф-источник ограничен свободным остатком; касса-источник — без гейта (как её выдача).
    """
    source = await session.get(Wallet, payload.source_wallet_id)
    dest = await session.get(Wallet, payload.dest_wallet_id)
    for wallet, label in ((source, "источник"), (dest, "получатель")):
        if wallet is None or wallet.status != "active" or wallet.type not in (
            "cash_safe",
            "store_cash",
        ):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Наличный счёт ({label}) не найден — перевод только между Сейфом и Кассой",
            )
    if source.id == dest.id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Счёт-источник и получатель совпадают"
        )
    dest_location = "safe" if dest.type == "cash_safe" else "kassa"

    prepared: list[tuple[DdsArticle, Decimal, str, UUID | None]] = []
    if payload.mode == "targeted":
        total = Decimal("0")
        for line in payload.lines or []:
            article = await session.get(DdsArticle, line.article_id)
            if article is None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT, detail="Статья ДДС не найдена"
                )
            try:
                ensure_reservable_article_allowed(
                    article, has_counterparty=line.counterparty_id is not None
                )
            except ValueError as exc:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
            amount = Decimal(line.amount)
            line_purpose = " ".join((line.purpose or "").split()) or article.name
            prepared.append((article, amount, line_purpose, line.counterparty_id))
            total += amount
    else:
        total = Decimal(payload.amount)  # валидатор гарантирует непустую сумму в plain

    # Сейф-источник: перевести можно только в пределах свободного остатка.
    if source.type == "cash_safe":
        free = await _safe_free_amount(session, source)
        if total > free:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Недостаточно свободных средств на Сейфе: "
                    f"свободно {free}, запрошено {total}"
                ),
            )

    try:
        transfer_id = await book_internal_transfer(
            session,
            source_wallet=source,
            dest_wallet=dest,
            amount=total,
            purpose=payload.purpose,
            operation_date=datetime.now(MOSCOW_TZ).date(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    reserves = 0
    for article, amount, line_purpose, cp_id in prepared:
        allocation = await create_allocation(
            session,
            wallet_id=dest.id,
            amount=amount,
            free_amount=None,  # деньги только что переведены на счёт-получатель
            article_id=article.id,
            counterparty_id=cp_id,
            purpose=line_purpose,
            created_by_user_id=actor.user_id,
        )
        if dest_location == "kassa":
            allocation.location = "kassa"
        reserves += 1
    await session.commit()
    return {"transfer_id": transfer_id, "amount": float(total), "reserves": reserves}


@router.post(
    "/new-payment/internal-transfer",
    response_model=NewPaymentTransferRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=DDS_SAFE_ALLOCATE_ACCESS,
)
async def post_new_payment_internal_transfer(
    payload: NewPaymentTransferCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, object]:
    """Обычный внутренний перевод из «Нового платежа» (маршрут статьи «Внутренний перевод»).

    Источник — «Счёт списания»; получатель — только наличный (Сейф/Касса).
    Наличный источник → мгновенный двухногий перевод. Банковский источник → черновик-
    пополнение Сейфа (при оплате транзит р/с→Сейф без резерва); банк→Касса запрещён.
    """
    source = await session.get(Wallet, payload.source_wallet_id)
    dest = await session.get(Wallet, payload.dest_wallet_id)
    if source is None or source.status != "active":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Счёт списания не найден")
    # Получатель — только наличный. Внесение Сейф→банк из окна НЕ книжим: входящая
    # операция придёт выпиской неразобранной, разметка перевода создаст свою ногу
    # Сейфа — ручная нога из окна дала бы задвоение (решение владельца 12.07).
    if dest is None or dest.status != "active" or dest.type not in ("cash_safe", "store_cash"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Счёт-получатель должен быть наличным (Сейф/Касса)",
        )
    dest_location = "safe" if dest.type == "cash_safe" else "kassa"

    # Наличный источник (Сейф/Касса) — мгновенный перевод.
    if source.type in ("cash_safe", "store_cash"):
        if source.id == dest.id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="Счёт-источник и получатель совпадают"
            )
        if source.type == "cash_safe":
            free = await _safe_free_amount(session, source)
            if Decimal(payload.amount) > free:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"Недостаточно свободных средств на Сейфе: "
                        f"свободно {free}, запрошено {payload.amount}"
                    ),
                )
        try:
            await book_internal_transfer(
                session,
                source_wallet=source,
                dest_wallet=dest,
                amount=Decimal(payload.amount),
                purpose=payload.purpose,
                operation_date=datetime.now(MOSCOW_TZ).date(),
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        await session.commit()
        return {"kind": "transfer", "amount": float(payload.amount), "draft_id": None}

    # Банковский источник — только на Сейф, через черновик-пополнение (topup_only).
    if dest_location != "safe":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="С банковского счёта перевод возможен только на Сейф (в кассу — нельзя)",
        )
    account = await session.get(Account, source.account_id) if source.account_id else None
    is_sber = account is not None and account.bank_code == "sber"
    channel = "bank_draft_sber" if is_sber else "bank_draft"
    try:
        draft = await create_bank_safe_topup_draft(
            session,
            amount=Decimal(payload.amount),
            purpose=payload.purpose,
            channel=channel,
            actor_user_id=actor.user_id,
        )
    except CounterpartyPaymentError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except BankCredentialsError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    except BankFetchError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return {"kind": "draft", "amount": float(payload.amount), "draft_id": draft.id}


@router.post(
    "/articles",
    response_model=DdsArticleRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=DDS_EDIT_ACCESS,
)
async def create_article(
    payload: DdsArticleCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    article = DdsArticle(**payload.model_dump())
    _ensure_kassa_flag_allowed(article)
    session.add(article)
    await session.commit()
    await session.refresh(article)
    return (await _article_payloads(session, [article]))[0]


@router.patch(
    "/articles/{article_id}",
    response_model=DdsArticleRead,
    dependencies=DDS_EDIT_ACCESS,
)
async def patch_article(
    article_id: UUID,
    payload: DdsArticlePatch,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    article = await _article_or_404(session, article_id)
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(article, key, value)
    _ensure_kassa_flag_allowed(article)
    await session.commit()
    await session.refresh(article)
    return (await _article_payloads(session, [article]))[0]


def _ensure_kassa_flag_allowed(article: DdsArticle) -> None:
    """Флаг «доступна в кассе» — только расходным статьям без своих контуров выдачи."""
    if not article.kassa_enabled:
        return
    try:
        ensure_article_kassa_eligible(article)
    except KassaPayoutError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc


@router.delete(
    "/articles/{article_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=DDS_EDIT_ACCESS,
)
async def delete_article(
    article_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    article = await _article_or_404(session, article_id)
    article.is_active = False
    await session.commit()


@router.post(
    "/articles/{article_id}/aliases",
    response_model=DdsAliasRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=DDS_EDIT_ACCESS,
)
async def create_article_alias(
    article_id: UUID,
    payload: DdsAliasCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> DdsArticleAlias:
    await _article_or_404(session, article_id)
    alias = DdsArticleAlias(
        article_id=article_id,
        alias=payload.alias,
        source=payload.source,
    )
    session.add(alias)
    await session.commit()
    await session.refresh(alias)
    return alias


@router.delete(
    "/articles/aliases/{alias_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=DDS_EDIT_ACCESS,
)
async def delete_article_alias(
    alias_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    alias = await session.get(DdsArticleAlias, alias_id)
    if alias is None:
        raise HTTPException(status_code=404, detail="Article alias not found")
    await session.delete(alias)
    await session.commit()


@router.get(
    "/counterparties",
    response_model=list[DdsCounterpartyRead],
    dependencies=DDS_COUNTERPARTIES_READ_ACCESS,
)
async def list_counterparties(
    session: Annotated[AsyncSession, Depends(get_session)],
    search: str | None = None,
) -> list[dict[str, object]]:
    conditions = []
    if search:
        pattern = f"%{search.strip()}%"
        conditions.append(or_(Counterparty.name.ilike(pattern), Counterparty.inn.ilike(pattern)))
    result = await session.scalars(
        select(Counterparty).where(*conditions).order_by(Counterparty.name)
    )
    return await _counterparty_payloads(session, result.all())


@router.post(
    "/counterparties",
    response_model=DdsCounterpartyRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=DDS_COUNTERPARTIES_EDIT_ACCESS,
)
async def create_counterparty(
    payload: DdsCounterpartyCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    counterparty = Counterparty(**payload.model_dump())
    session.add(counterparty)
    await session.commit()
    await session.refresh(counterparty)
    return (await _counterparty_payloads(session, [counterparty]))[0]


@router.patch(
    "/counterparties/{counterparty_id}",
    response_model=DdsCounterpartyRead,
    dependencies=DDS_COUNTERPARTIES_EDIT_ACCESS,
)
async def patch_counterparty(
    counterparty_id: UUID,
    payload: DdsCounterpartyPatch,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    counterparty = await _counterparty_or_404(session, counterparty_id)
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(counterparty, key, value)
    await session.commit()
    await session.refresh(counterparty)
    return (await _counterparty_payloads(session, [counterparty]))[0]


@router.delete(
    "/counterparties/{counterparty_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=DDS_COUNTERPARTIES_EDIT_ACCESS,
)
async def delete_counterparty(
    counterparty_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    counterparty = await _counterparty_or_404(session, counterparty_id)
    counterparty.status = "inactive"
    await session.commit()


@router.post(
    "/counterparties/{counterparty_id}/aliases",
    response_model=DdsAliasRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=DDS_COUNTERPARTIES_EDIT_ACCESS,
)
async def create_counterparty_alias(
    counterparty_id: UUID,
    payload: DdsAliasCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CounterpartyAlias:
    await _counterparty_or_404(session, counterparty_id)
    alias = CounterpartyAlias(
        counterparty_id=counterparty_id,
        alias=payload.alias,
        source=payload.source,
    )
    session.add(alias)
    await session.commit()
    await session.refresh(alias)
    return alias


@router.delete(
    "/counterparties/aliases/{alias_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=DDS_COUNTERPARTIES_EDIT_ACCESS,
)
async def delete_counterparty_alias(
    alias_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    alias = await session.get(CounterpartyAlias, alias_id)
    if alias is None:
        raise HTTPException(status_code=404, detail="Counterparty alias not found")
    await session.delete(alias)
    await session.commit()


@router.get(
    "/classification-rules",
    response_model=list[ClassificationRuleRead],
    dependencies=DDS_RULES_MANAGE_ACCESS,
)
async def list_classification_rules(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[dict[str, object]]:
    result = await session.scalars(
        select(ClassificationRule).order_by(ClassificationRule.priority, ClassificationRule.name)
    )
    return [_classification_rule_payload(rule) for rule in result.all()]


@router.post(
    "/classification-rules",
    response_model=ClassificationRuleRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=DDS_RULES_MANAGE_ACCESS,
)
async def create_classification_rule(
    payload: ClassificationRuleCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    data = _classification_rule_data(payload.model_dump())
    rule = ClassificationRule(**data)
    session.add(rule)
    await session.commit()
    await session.refresh(rule)
    return _classification_rule_payload(rule)


@router.patch(
    "/classification-rules/{rule_id}",
    response_model=ClassificationRuleRead,
    dependencies=DDS_RULES_MANAGE_ACCESS,
)
async def patch_classification_rule(
    rule_id: UUID,
    payload: ClassificationRulePatch,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    rule = await _classification_rule_or_404(session, rule_id)
    for key, value in _classification_rule_data(payload.model_dump(exclude_unset=True)).items():
        setattr(rule, key, value)
    await session.commit()
    await session.refresh(rule)
    return _classification_rule_payload(rule)


@router.delete(
    "/classification-rules/{rule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=DDS_RULES_MANAGE_ACCESS,
)
async def delete_classification_rule(
    rule_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    rule = await _classification_rule_or_404(session, rule_id)
    rule.is_active = False
    await session.commit()


@router.post(
    "/classification-rules/{rule_id}/toggle",
    response_model=ClassificationRuleRead,
    dependencies=DDS_RULES_MANAGE_ACCESS,
)
async def toggle_classification_rule(
    rule_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    rule = await _classification_rule_or_404(session, rule_id)
    rule.is_active = not rule.is_active
    await session.commit()
    await session.refresh(rule)
    return _classification_rule_payload(rule)


@router.get(
    "/credentials",
    response_model=list[CredentialRead],
    dependencies=DDS_INTEGRATIONS_MANAGE_ACCESS,
)
async def list_credentials(
    session: Annotated[AsyncSession, Depends(get_session)],
    is_active: bool | None = True,
) -> list[dict[str, object]]:
    conditions = []
    if is_active is not None:
        conditions.append(SourceCredential.is_active.is_(is_active))
    result = await session.scalars(
        select(SourceCredential)
        .where(*conditions)
        .order_by(SourceCredential.provider, SourceCredential.credential_kind)
    )
    return [_credential_payload(credential) for credential in result.all()]


@router.post(
    "/credentials",
    response_model=CredentialRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=DDS_INTEGRATIONS_MANAGE_ACCESS,
)
async def create_credential(
    payload: CredentialCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    credential = await set_credential(
        session,
        provider=payload.provider,
        kind=payload.credential_kind,
        value=payload.value,
        expires_at=payload.expires_at,
        metadata_json=payload.metadata,
    )
    return _credential_payload(credential)


@router.delete(
    "/credentials/{credential_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=DDS_INTEGRATIONS_MANAGE_ACCESS,
)
async def delete_credential(
    credential_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    credential = await session.get(SourceCredential, credential_id)
    if credential is None:
        raise HTTPException(status_code=404, detail="Credential not found")
    credential.is_active = False
    credential.status = "inactive"
    await session.commit()


@router.post(
    "/bank-sync/{provider}",
    response_model=BankSyncQueuedRead,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=DDS_INTEGRATIONS_MANAGE_ACCESS,
)
async def sync_bank_operations(
    provider: Literal["sber", "tbank"],
    payload: BankSyncRequest,
    background_tasks: BackgroundTasks,
) -> dict[str, object]:
    job_id = uuid4()
    background_tasks.add_task(
        run_bank_sync_job,
        provider=provider,
        date_from=payload.date_from,
        date_to=payload.date_to,
        job_id=job_id,
    )
    return {"job_id": job_id, "status": "queued", "queued_at": datetime.now(UTC)}


@router.get(
    "/owner-review",
    response_model=OwnerReviewListRead,
    dependencies=DDS_OWNER_REVIEW_PREPARE_ACCESS,
)
async def list_owner_review_cases(
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
    kind: Literal[
        "unclassified_operation",
        "invalid_credentials",
        "unmatched_transfer",
        "unconfirmed_cheque",
        "payer_wallet_unresolved",
        "iiko_payment_unsettled",
        "card_refund_after_cheque",
        "cheque_refund_missing",
    ]
    | None = None,
) -> dict[str, object]:
    allowed_kinds = (
        "unclassified_operation",
        "invalid_credentials",
        "unmatched_transfer",
        "unconfirmed_cheque",
        "payer_wallet_unresolved",
        "iiko_payment_unsettled",
        "card_refund_after_cheque",
        "cheque_refund_missing",
    )
    conditions = [
        ReconciliationCase.status == "pending",
        ReconciliationCase.kind.in_(allowed_kinds),
    ]
    if kind is not None:
        conditions.append(ReconciliationCase.kind == kind)

    total = int(
        await session.scalar(
            select(func.count()).select_from(ReconciliationCase).where(*conditions)
        )
        or 0
    )
    cases = await session.scalars(
        select(ReconciliationCase)
        .where(*conditions)
        .order_by(ReconciliationCase.created_at, ReconciliationCase.id)
        .limit(limit)
        .offset(offset)
    )
    items = []
    for case in cases.all():
        operation = None
        if case.bank_operation_id is not None:
            operation = await session.get(BankOperation, case.bank_operation_id)
        items.append(_owner_review_payload(case, operation))
    return {"items": items, "total": total}


@router.post(
    "/owner-review/{case_id}/classify",
    response_model=OwnerReviewActionRead,
    # Owner-review действие → право owner_review.prepare (как у соседних dismiss/apply-refund/
    # list). Общий finance.cashflow.classify (есть у управляющего) сюда пускать нельзя — он для
    # обычной разметки транзакций, а не для кейсов «на утверждении собственника».
    dependencies=DDS_OWNER_REVIEW_PREPARE_ACCESS,
)
async def classify_owner_review_case(
    case_id: UUID,
    payload: OwnerReviewClassifyRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    case = await _pending_case_or_404(session, case_id)
    if case.bank_operation_id is None:
        raise HTTPException(status_code=400, detail="Review case is not linked to a bank operation")
    operation = await session.get(BankOperation, case.bank_operation_id)
    if operation is None:
        raise HTTPException(status_code=404, detail="Bank operation not found")
    if payload.action == "set_article" and payload.article_id is None:
        raise HTTPException(status_code=400, detail="article_id is required for set_article")

    await apply_operation_action(
        session,
        operation,
        action=payload.action,
        article_id=payload.article_id,
        counterparty_id=payload.counterparty_id,
        quality_status="owner_review",
    )
    if payload.action == "mark_internal_transfer":
        await find_and_link_transfer_pairs(session)

    rule_id = None
    if payload.remember_as_rule:
        rule = _rule_from_owner_review(operation, payload)
        session.add(rule)
        await session.flush()
        rule_id = rule.id

    await close_reconciliation_case(
        session,
        case,
        status="resolved",
        resolution_payload={
            "action": payload.action,
            "article_id": str(payload.article_id) if payload.article_id else None,
            "counterparty_id": str(payload.counterparty_id) if payload.counterparty_id else None,
            "rule_id": str(rule_id) if rule_id else None,
        },
    )
    await session.commit()
    return {
        "case_id": case.id,
        "status": case.status,
        "bank_operation_id": operation.id,
        "classification_status": operation.classification_status,
        "rule_id": rule_id,
    }


@router.post(
    "/owner-review/{case_id}/dismiss",
    response_model=OwnerReviewActionRead,
    dependencies=DDS_OWNER_REVIEW_PREPARE_ACCESS,
)
async def dismiss_owner_review_case(
    case_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    case = await _pending_case_or_404(session, case_id)
    await close_reconciliation_case(session, case, status="dismissed")
    await session.commit()
    return {"case_id": case.id, "status": case.status, "bank_operation_id": case.bank_operation_id}


@router.post(
    "/owner-review/{case_id}/apply-card-refund",
    response_model=OwnerReviewActionRead,
    dependencies=DDS_OWNER_REVIEW_PREPARE_ACCESS,
)
async def apply_card_refund_owner_review_case(
    case_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    invoice_id: UUID | None = None,
) -> dict[str, object]:
    """«Учесть возврат» по кейсу «возврат по проведённому чеку».

    Если возврат относится к конкретному чеку (единственный кандидат или явно выбранный
    ``invoice_id`` при неоднозначности) — привязывает возврат к чеку и гасит его ожидание.
    Иначе (сирота) — заводит входящую проводку «Возврат расходов». Чек и iiko не мутируются.
    """
    case = await _pending_case_or_404(session, case_id)
    from app.services.kassa.cheque import KassaChequeError, apply_card_refund_case

    try:
        await apply_card_refund_case(session, case, invoice_id=invoice_id)
    except KassaChequeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await session.commit()
    return {"case_id": case.id, "status": case.status, "bank_operation_id": case.bank_operation_id}


@router.post(
    "/owner-review/{case_id}/retry-iiko-payment",
    response_model=OwnerReviewActionRead,
    dependencies=DDS_OWNER_REVIEW_PREPARE_ACCESS,
)
async def retry_iiko_payment_owner_review_case(
    case_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, object]:
    """«Повторить отправку» по кейсу «оплата в iiko не проведена»: снять кап авто-ретраев и
    переотправить (синхронно для одиночного банк-черновика; иначе сверочный джоб добьёт в ближайшие
    минуты). Успех закрывает кейс. Только для авто-повторяемых причин (не мультиплатёж/аванс)."""
    case = await _pending_case_or_404(session, case_id)
    if case.kind != "iiko_payment_unsettled":
        raise HTTPException(status_code=400, detail="Действие только для кейсов оплаты в iiko")
    from app.services.counterparty_iiko_payment import (
        IIKO_UNSETTLED_RETRIABLE,
        IikoPaymentError,
        retry_iiko_payment,
    )

    payload = case.payload or {}
    reason_code = payload.get("reason_code")
    if reason_code is not None and reason_code not in IIKO_UNSETTLED_RETRIABLE:
        raise HTTPException(
            status_code=422,
            detail="Эту причину нельзя авто-повторить — подтвердите оплату вручную",
        )
    invoice_id = payload.get("invoice_id")
    if not invoice_id:
        raise HTTPException(status_code=400, detail="В кейсе не указана накладная")
    try:
        res = await retry_iiko_payment(session, UUID(str(invoice_id)), actor_user_id=actor.user_id)
    except IikoPaymentError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await session.commit()
    push_error = res.error if (res is not None and not res.ok) else None
    return {
        "case_id": case.id,
        "status": case.status,
        "bank_operation_id": case.bank_operation_id,
        "iiko_payment_push_error": push_error,
    }


@router.post(
    "/owner-review/{case_id}/confirm-iiko-manual",
    response_model=OwnerReviewActionRead,
    dependencies=DDS_OWNER_REVIEW_PREPARE_ACCESS,
)
async def confirm_iiko_manual_owner_review_case(
    case_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, object]:
    """«Оплата проведена вручную» по кейсу: человек утверждает, что платёж уже в бэк-офисе iiko —
    пишем ok-маркер (сверочный джоб больше не шлёт) и закрываем кейс. Отказ для авто-отправляемой
    (одиночный банк) — для неё «Повторить отправку» (вслепую глушить зеркало нельзя)."""
    case = await _pending_case_or_404(session, case_id)
    if case.kind != "iiko_payment_unsettled":
        raise HTTPException(status_code=400, detail="Действие только для кейсов оплаты в iiko")
    from app.services.counterparty_iiko_payment import (
        IikoPaymentError,
        mark_iiko_payment_settled_manually,
    )

    invoice_id = (case.payload or {}).get("invoice_id")
    if not invoice_id:
        raise HTTPException(status_code=400, detail="В кейсе не указана накладная")
    invoice = await session.get(SupplierInvoice, UUID(str(invoice_id)))
    if invoice is None:
        raise HTTPException(status_code=404, detail="Накладная не найдена")
    try:
        await mark_iiko_payment_settled_manually(session, invoice, actor_user_id=actor.user_id)
    except IikoPaymentError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # mark резолвит кейс через _resolve_iiko_payment_case; подстрахуем закрытие, если ещё pending.
    if case.status == "pending":
        await close_reconciliation_case(
            session, case, status="resolved",
            resolution_payload={"reason": "settled_manually_in_iiko"},
        )
    await session.commit()
    return {"case_id": case.id, "status": case.status, "bank_operation_id": case.bank_operation_id}


@router.post(
    "/operations/{operation_id}/classify",
    response_model=OperationClassifyRead,
    dependencies=DDS_CLASSIFY_ACCESS,
)
async def classify_operation(
    operation_id: UUID,
    payload: OperationClassifyRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, object]:
    """Classify a bank operation directly: multi-article split, internal transfer, or exclude."""
    operation = await session.get(BankOperation, operation_id)
    if operation is None:
        raise HTTPException(status_code=404, detail="Bank operation not found")

    created_ids: list[UUID] = []
    if payload.action == "split":
        if not payload.splits:
            raise HTTPException(status_code=400, detail="Нужна хотя бы одна статья")
        counterparty_id = payload.counterparty_id
        # Создать контрагента из распознанных данных операции, если оператор это выбрал (его нет
        # в реестре, но имя/ИНН известны из выписки) — резолв по ИНН или новая карточка.
        if payload.new_counterparty_name:
            try:
                counterparty_id = await resolve_or_create_operation_counterparty(
                    session,
                    name=payload.new_counterparty_name,
                    inn=payload.new_counterparty_inn,
                    account=operation.counterparty_account_raw,
                )
            except ValueError as error:
                raise HTTPException(status_code=400, detail=str(error)) from error
        # Ручная привязка карт-оплаты к накладной (получатель в банке — эквайер, не поставщик) —
        # привилегированное действие: требуем право оплаты накладной (как в /match/confirm), чтобы
        # разбор ДДС не обходил RBAC. Проверяем ДО мутаций (единственный commit — в конце роута).
        if payload.allow_card:
            for item in payload.splits:
                if item.invoice_id is None:
                    continue
                invoice = await session.get(SupplierInvoice, item.invoice_id)
                if invoice is None:
                    raise HTTPException(status_code=404, detail="Накладная не найдена")
                ensure_permission(
                    actor, f"invoices.{await invoice_permission_kind(session, invoice)}.pay"
                )
        try:
            created_ids = await apply_operation_split(
                session,
                operation,
                splits=[
                    (item.article_id, item.amount, item.comment, item.invoice_id, item.employee_id)
                    for item in payload.splits
                ],
                counterparty_id=counterparty_id,
                actor_user_id=actor.user_id,
                allow_card=payload.allow_card,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
    elif payload.action == "mark_safe_topup":
        # Пополнение Сейфа. Если строки размечены статьёй/получателем — это уже целёвки-резервы
        # (не спрашиваем отдельно); голое пополнение без разметки — просто транзит р/с→Сейф.
        reserves: list[tuple[UUID, Decimal, UUID | None]] = []
        for item in payload.splits:
            article = await session.get(DdsArticle, item.article_id)
            if article is None:
                raise HTTPException(status_code=400, detail="Статья не найдена")
            is_salary = article.code in EMPLOYEE_PAYOUT_ARTICLE_CODES
            if item.employee_id is not None and not is_salary:
                raise HTTPException(
                    status_code=400, detail="Сотрудника можно указать только для зарплатной статьи"
                )
            if is_salary and item.employee_id is None:
                raise HTTPException(
                    status_code=400, detail="Для зарплатной строки выберите сотрудника-получателя"
                )
            reserves.append((item.article_id, item.amount, item.employee_id))
        if reserves and sum((amount for _a, amount, _e in reserves), Decimal("0")) > Decimal(
            operation.amount
        ):
            raise HTTPException(
                status_code=400, detail="Сумма резервов больше суммы пополнения Сейфа"
            )
        try:
            if reserves:
                await book_safe_topup_reserves(
                    session,
                    operation,
                    reserves=reserves,
                    counterparty_id=payload.counterparty_id,
                )
                created_ids = []
            else:
                created_ids = await book_safe_topup(session, operation)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
    elif payload.action == "employee_advance":
        # Разбор операции как аванс/заём сотруднику: split-проводка со статьёй аванса + SalaryAdvance
        # (деньги ушли банком, второй проводки нет). Возврат маршрутизируется по роли в ведомости.
        if len(payload.splits) != 1:
            raise HTTPException(status_code=400, detail="Авансом оформляется одна строка")
        item = payload.splits[0]
        if item.employee_id is None:
            raise HTTPException(status_code=400, detail="Выберите сотрудника")
        article = await session.get(DdsArticle, item.article_id)
        if article is None or article.code not in EMPLOYEE_ADVANCE_ARTICLE_CODES:
            raise HTTPException(status_code=400, detail="Эта статья не для аванса/займа сотрудника")
        if operation.direction != "out":
            raise HTTPException(status_code=400, detail="Аванс — только для исходящей операции")
        ensure_any_permission(
            actor, ("payroll.advances.admin.issue", "payroll.advances.production.issue")
        )
        kind = payload.advance_kind or (
            "loan" if article.code == EMPLOYEE_LOAN_ARTICLE_CODE else "advance"
        )
        allow_loan = permission_is_granted("payroll.loans.issue", actor.permissions)
        try:
            created_ids = await apply_operation_split(
                session,
                operation,
                splits=[(item.article_id, item.amount, None, None, None)],
            )
            await book_operation_advance(
                session,
                operation,
                employee_id=item.employee_id,
                kind=kind,
                installment_amount=payload.advance_installment_amount,
                recovery_start_date=payload.advance_recovery_start_date,
                override_ceiling=payload.advance_override_ceiling,
                allow_loan=allow_loan,
                actor_user_id=actor.user_id,
            )
        except (ValueError, PayrollConflictError, PayrollNotFoundError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
    else:
        await apply_operation_action(
            session,
            operation,
            action=payload.action,
            counterparty_id=payload.counterparty_id,
            quality_status="owner_review",
        )
        if payload.action == "mark_internal_transfer":
            await find_and_link_transfer_pairs(session)

    rule_id = None
    # Карт-привязку к накладной (allow_card) правилом НЕ запоминаем: правило не должно
    # авто-матчить будущие карт-операции к накладным — это разовое ручное действие.
    if (
        payload.remember_as_rule
        and payload.action == "split"
        and len(payload.splits) == 1
        and not payload.allow_card
    ):
        rule = _rule_from_operation_split(
            operation, payload.splits[0].article_id, payload.counterparty_id
        )
        session.add(rule)
        await session.flush()
        rule_id = rule.id

    case = await session.scalar(
        select(ReconciliationCase).where(
            ReconciliationCase.bank_operation_id == operation.id,
            ReconciliationCase.kind == "unclassified_operation",
            ReconciliationCase.status == "pending",
        )
    )
    if case is not None:
        await close_reconciliation_case(
            session,
            case,
            status="resolved",
            resolution_payload={
                "action": payload.action,
                "splits": len(payload.splits),
                "rule_id": str(rule_id) if rule_id else None,
            },
        )
    await session.commit()
    return {
        "bank_operation_id": operation.id,
        "classification_status": operation.classification_status,
        "cashflow_transaction_ids": created_ids,
        "rule_id": rule_id,
    }


@router.patch("/transactions/{transaction_id}", dependencies=DDS_CLASSIFY_ACCESS)
async def classify_transaction(
    transaction_id: UUID,
    payload: TransactionClassifyRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    """Вручную разметить проводку ДДС (статья + контрагент).

    Для РУЧНЫХ проводок без bank-операции (напр. снятых с авто-разметки или заведённых при
    сведении касс), которые нельзя разобрать через операцию выписки. Пустой ``article_id``
    возвращает проводку в статус «требует разметки». Не влияет на баланс — только аналитика.
    """
    txn = await session.get(CashflowTransaction, transaction_id)
    if txn is None:
        raise HTTPException(status_code=404, detail="Проводка не найдена")
    if payload.article_id is not None and await session.get(DdsArticle, payload.article_id) is None:
        raise HTTPException(status_code=400, detail="Статья не найдена")
    txn.article_id = payload.article_id
    txn.counterparty_id = payload.counterparty_id
    txn.quality_status = "manual_override"
    await session.commit()
    return {
        "id": txn.id,
        "article_id": txn.article_id,
        "counterparty_id": txn.counterparty_id,
    }


@router.post(
    "/transactions/{transaction_id}/classify",
    response_model=CashflowClassifyRead,
    dependencies=DDS_CLASSIFY_ACCESS,
)
async def classify_transaction_full(
    transaction_id: UUID,
    payload: CashflowClassifyRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    """Полный разбор РУЧНОЙ проводки ДДС (без bank-операции): мультисплит по статьям (в т.ч.
    строка «перевод между счетами» со счётом-получателем) или мягкое исключение.

    Каждое действие сохраняет баланс кошелька (проводка сама двигает баланс, в отличие от
    операции выписки). Проводки, порождённые из bank-операции, разбираются через операцию
    выписки (``/operations/{id}/classify``), поэтому здесь отклоняются.
    """
    txn = await session.get(CashflowTransaction, transaction_id)
    if txn is None:
        raise HTTPException(status_code=404, detail="Проводка не найдена")
    if txn.source_kind == "bank_operation":
        raise HTTPException(
            status_code=400,
            detail="Эту проводку разбирают через операцию выписки",
        )
    try:
        if payload.action == "split":
            created_ids = await apply_cashflow_split(
                session,
                txn,
                splits=[
                    (
                        item.article_id,
                        item.amount,
                        item.comment,
                        item.transfer_wallet_id,
                        item.employee_id,
                    )
                    for item in payload.splits
                ],
                counterparty_id=payload.counterparty_id,
            )
        else:
            created_ids = await apply_cashflow_exclude(session, txn)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    await session.commit()
    return {"transaction_id": txn.id, "cashflow_transaction_ids": created_ids}


def _safe_allocation_payload(
    allocation: SafeAllocation, counterparty_name: str | None = None
) -> dict[str, object]:
    outstanding = Decimal(allocation.amount) - Decimal(allocation.amount_paid)
    return {
        "id": allocation.id,
        "wallet_id": allocation.wallet_id,
        "amount": _money(allocation.amount),
        "amount_paid": _money(allocation.amount_paid),
        "outstanding": _money(outstanding),
        "article_id": allocation.article_id,
        "counterparty_id": allocation.counterparty_id,
        "counterparty_name": counterparty_name,
        "purpose": allocation.purpose,
        # Происхождение: авто-резерв, созданный оплатой черновика выплаты на карту ИП
        # (неофициальный поставщик) — в UI помечается «из банковской выплаты».
        "source_draft_id": allocation.source_draft_id,
        "status": allocation.status,
        # Где живёт целёвка: 'safe' — на карте Сейф, 'kassa' — передана в кассу.
        "location": allocation.location,
        "created_at": allocation.created_at,
    }


async def _allocation_counterparty_name(
    session: AsyncSession, allocation: SafeAllocation
) -> str | None:
    if allocation.counterparty_id is None:
        return None
    return await session.scalar(
        select(Counterparty.name).where(Counterparty.id == allocation.counterparty_id)
    )


async def _safe_free_amount(session: AsyncSession, wallet: Wallet) -> Decimal:
    """Свободный остаток Сейфа = баланс − Σ непогашенных резервов."""
    deltas = await _wallet_movement_deltas(session)
    balance = Decimal(str(wallet.opening_balance)) + deltas.get(wallet.id, Decimal("0"))
    reserved = await safe_reserved_total(session, wallet.id)
    return balance - reserved


@router.get(
    "/wallets/{wallet_id}/allocations",
    response_model=list[SafeAllocationRead],
    dependencies=DDS_READ_ACCESS,
)
async def list_safe_allocations(
    wallet_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    status_filter: Annotated[Literal["active", "all"], Query(alias="status")] = "active",
) -> list[dict[str, object]]:
    """Резервы Сейфа: ``active`` (reserved/partially_paid) или ``all``
    (вкл. оплаченные/отменённые)."""
    conditions = [SafeAllocation.wallet_id == wallet_id]
    if status_filter == "active":
        conditions.append(SafeAllocation.status.in_(("reserved", "partially_paid")))
    rows = (
        await session.execute(
            select(SafeAllocation, Counterparty.name)
            .outerjoin(Counterparty, Counterparty.id == SafeAllocation.counterparty_id)
            .where(*conditions)
            .order_by(SafeAllocation.created_at.desc())
        )
    ).all()
    return [_safe_allocation_payload(allocation, name) for allocation, name in rows]


@router.post(
    "/wallets/{wallet_id}/allocations",
    response_model=SafeAllocationRead,
    dependencies=DDS_SAFE_ALLOCATE_ACCESS,
)
async def create_safe_allocation(
    wallet_id: UUID,
    payload: SafeAllocationCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, object]:
    """Создать резерв на Сейфе. ``pay_full`` — прямая трата свободных (создать и сразу оплатить)."""
    wallet = await session.get(Wallet, wallet_id)
    if wallet is None or wallet.code != SAFE_WALLET_CODE:
        raise HTTPException(status_code=404, detail="Кошелёк «Сейф» не найден")
    if payload.pay_full:
        ensure_permission(actor, "finance.safe.confirm_paid")
    free = await _safe_free_amount(session, wallet)
    try:
        allocation = await create_allocation(
            session,
            wallet_id=wallet.id,
            amount=payload.amount,
            free_amount=free,
            article_id=payload.article_id,
            counterparty_id=payload.counterparty_id,
            purpose=payload.purpose,
            created_by_user_id=actor.user_id,
        )
        if payload.pay_full:
            await pay_allocation(
                session,
                allocation,
                amount=payload.amount,
                operation_date=datetime.now(MOSCOW_TZ).date(),
            )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    await session.commit()
    await session.refresh(allocation)
    return _safe_allocation_payload(
        allocation, await _allocation_counterparty_name(session, allocation)
    )


@router.post(
    "/allocations/{allocation_id}/pay",
    response_model=SafeAllocationRead,
    dependencies=DDS_SAFE_CONFIRM_PAID_ACCESS,
)
async def pay_safe_allocation(
    allocation_id: UUID,
    payload: SafeAllocationPayRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    """Оплатить резерв (полностью или частично) — списание с Сейфа, признание расхода."""
    allocation = await session.get(SafeAllocation, allocation_id, with_for_update=True)
    if allocation is None:
        raise HTTPException(status_code=404, detail="Резерв не найден")
    if allocation.source_run_id is not None:
        # Пул-резерв выплаты ЗП: pay_allocation здесь книжил бы лишний cashflow мимо
        # PayrollPayment. Выдача только через окно ведомости (POST /payroll/reserves/{id}/payout).
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Резерв выплаты ЗП — выдача через окно ведомости, не отсюда",
        )
    if allocation.location != "safe":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Целёвка передана в кассу — выдача только из модуля «Касса»",
        )
    try:
        await pay_allocation(
            session,
            allocation,
            amount=payload.amount,
            operation_date=datetime.now(MOSCOW_TZ).date(),
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    # Если резерв — это банк-выдача аванса/займа, полная оплата формирует долг сотрудника.
    disbursed_advance = await sync_advance_after_allocation_change(
        session, allocation_id=allocation.id
    )
    await session.commit()
    await session.refresh(allocation)
    # Выдача состоялась через оплату резерва (не кнопкой «Выплачено») → изъятие в iiko
    # «эквайринг» ПОСЛЕ commit, как делает disburse_bank_advance. Переход в disbursed
    # однократен, поэтому задвоения с путём «Выплачено» нет; ошибка iiko не валит оплату.
    if disbursed_advance is not None:
        post_advance_payout_to_iiko(
            amount=disbursed_advance.amount,
            payout_date=datetime.now(MOSCOW_TZ).date(),
            source_id=disbursed_advance.id,
            is_loan=disbursed_advance.kind == "loan",
            source="bank",
        )
    return _safe_allocation_payload(
        allocation, await _allocation_counterparty_name(session, allocation)
    )


@router.post(
    "/allocations/{allocation_id}/cancel",
    response_model=SafeAllocationRead,
    dependencies=DDS_SAFE_ALLOCATE_ACCESS,
)
async def cancel_safe_allocation(
    allocation_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    """Отменить резерв: неоплаченный остаток освобождается, оплаченные ноги остаются."""
    allocation = await session.get(SafeAllocation, allocation_id, with_for_update=True)
    if allocation is None:
        raise HTTPException(status_code=404, detail="Резерв не найден")
    try:
        await cancel_allocation(session, allocation)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    # Отмена резерва банк-выдачи отменяет и сам аванс/заём (деньги остаются в Сейфе).
    await sync_advance_after_allocation_change(session, allocation_id=allocation.id)
    await session.commit()
    await session.refresh(allocation)
    return _safe_allocation_payload(
        allocation, await _allocation_counterparty_name(session, allocation)
    )


@router.post(
    "/allocations/{allocation_id}/transfer-to-kassa",
    response_model=SafeAllocationRead,
    dependencies=DDS_SAFE_CONFIRM_PAID_ACCESS,
)
async def transfer_safe_allocation_to_kassa(
    allocation_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    """«Передать в кассу»: целёвка переезжает вместе с деньгами (весь остаток).

    Двухногое перемещение Сейф → ТК Черникова на непогашенный остаток + смена
    локации резерва: он уходит из резервов Сейфа и появляется во вкладке
    «К выдаче» Кассы. Частичная передача запрещена; повторная — конфликт (409,
    row-lock резерва сериализует двойной клик).
    """
    allocation = await session.get(SafeAllocation, allocation_id, with_for_update=True)
    if allocation is None:
        raise HTTPException(status_code=404, detail="Резерв не найден")
    # Резерв банк-выдачи аванса/займа передавать нельзя: его путь прежний — оплата
    # с Сейфа по «Выплачено» (sync_advance_after_allocation_change активирует долг).
    if await allocation_advance_draft_id(session, allocation.id) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Этот резерв привязан к банковской выдаче аванса/займа — "
            "он выдаётся оплатой с Сейфа, передача в кассу недоступна",
        )
    try:
        await transfer_allocation_to_kassa(
            session,
            allocation,
            operation_date=datetime.now(MOSCOW_TZ).date(),
        )
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    await session.commit()
    await session.refresh(allocation)
    return _safe_allocation_payload(
        allocation, await _allocation_counterparty_name(session, allocation)
    )


@router.post(
    "/allocations/{allocation_id}/move",
    response_model=SafeAllocationRead,
    dependencies=DDS_SAFE_CONFIRM_PAID_ACCESS,
)
async def move_safe_allocation(
    allocation_id: UUID,
    payload: AllocationMoveRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    """Переместить резерв между Сейфом и Кассой в любую сторону (весь остаток).

    Двухногий перевод остатка между счетами + смена локации: Сейф→Касса резерв уходит
    в «К выдаче», Касса→Сейф — обратно в резервы Сейфа. Свободный остаток счетов не
    меняется. Частичное перемещение запрещено; повторное в ту же сторону — 409 (row-lock
    сериализует двойной клик, туда-обратно работает).
    """
    allocation = await session.get(SafeAllocation, allocation_id, with_for_update=True)
    if allocation is None:
        raise HTTPException(status_code=404, detail="Резерв не найден")
    # Резерв банк-выдачи аванса/займа перемещать нельзя: его путь — оплата с Сейфа.
    if await allocation_advance_draft_id(session, allocation.id) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Этот резерв привязан к банковской выдаче аванса/займа — перемещение недоступно",
        )
    try:
        await move_allocation_location(
            session,
            allocation,
            to_location=payload.to_location,
            operation_date=datetime.now(MOSCOW_TZ).date(),
        )
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    await session.commit()
    await session.refresh(allocation)
    return _safe_allocation_payload(
        allocation, await _allocation_counterparty_name(session, allocation)
    )


@router.get(
    "/kassa-targets", response_model=KassaPendingRead, dependencies=DDS_WALLETS_READ_ACCESS
)
async def list_dds_kassa_targets(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    """Целевые в Торговой кассе + ожидающие разрешения (read-only, «Деньги сегодня»).

    Тот же состав, что во вкладке «К выдаче» Кассы, но без кнопок выдачи — выдаёт
    только администратор в модуле «Касса». Права — менеджерские (кошельки ДДС).
    """
    try:
        return await kassa_pending_payload(session)
    except KassaPayoutError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post(
    "/wallets/{wallet_id}/withdraw-cash",
    response_model=DdsWalletRead,
    dependencies=DDS_SAFE_CONFIRM_PAID_ACCESS,
)
async def withdraw_safe_cash(
    wallet_id: UUID,
    payload: SafeCashWithdrawRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    """Снять наличные с карты «Сейф» в кассу Черникова (перевод между счетами)."""
    wallet = await session.get(Wallet, wallet_id)
    if wallet is None or wallet.code != SAFE_WALLET_CODE:
        raise HTTPException(status_code=404, detail="Кошелёк «Сейф» не найден")
    free = await _safe_free_amount(session, wallet)
    if payload.amount > free:
        raise HTTPException(
            status_code=400, detail=f"Недостаточно свободных средств: свободно {_money(free)}"
        )
    try:
        await book_safe_cash_withdrawal(
            session,
            safe_wallet=wallet,
            amount=payload.amount,
            operation_date=datetime.now(MOSCOW_TZ).date(),
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    await session.commit()
    deltas = await _wallet_movement_deltas(session)
    reserved = await safe_reserved_total(session, wallet.id)
    active_count = await safe_active_allocations_count(session, wallet.id)
    return _wallet_payload(
        wallet, deltas.get(wallet.id, Decimal("0")), None, reserved, active_count
    )


@router.post(
    "/wallets/{wallet_id}/reconcile",
    response_model=SafeReconcileRead,
    dependencies=DDS_SAFE_CONFIRM_PAID_ACCESS,
)
async def reconcile_safe(
    wallet_id: UUID,
    payload: SafeReconcileRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    """Сверить реальный остаток карты с учётным; опционально выровнять корректировкой."""
    wallet = await session.get(Wallet, wallet_id)
    if wallet is None or wallet.code != SAFE_WALLET_CODE:
        raise HTTPException(status_code=404, detail="Кошелёк «Сейф» не найден")
    deltas = await _wallet_movement_deltas(session)
    accounted = Decimal(str(wallet.opening_balance)) + deltas.get(wallet.id, Decimal("0"))
    actual = payload.actual_balance
    delta = actual - accounted
    today = datetime.now(MOSCOW_TZ).date()

    snapshot = await session.scalar(
        select(WalletBalanceSnapshot).where(
            WalletBalanceSnapshot.wallet_id == wallet.id,
            WalletBalanceSnapshot.snapshot_date == today,
            WalletBalanceSnapshot.source == "manual_statement",
        )
    )
    if snapshot is None:
        session.add(
            WalletBalanceSnapshot(
                wallet_id=wallet.id,
                snapshot_date=today,
                balance=actual,
                source="manual_statement",
            )
        )
    else:
        snapshot.balance = actual

    adjusted = False
    if payload.apply_adjustment and delta != 0:
        await book_safe_drift_adjustment(
            session, safe_wallet=wallet, delta=delta, operation_date=today
        )
        adjusted = True
    await session.commit()
    return {
        "accounted": _money(accounted),
        "actual": _money(actual),
        "delta": _money(delta),
        "adjusted": adjusted,
    }


def _wallet_payload(
    wallet: Wallet,
    movement_delta: Decimal = Decimal("0"),
    bank_code: str | None = None,
    reserved_total: Decimal = Decimal("0"),
    active_allocations: int = 0,
    pending_payout_count: int | None = None,
) -> dict[str, object]:
    opening = _money(wallet.opening_balance)
    balance_dec = Decimal(str(wallet.opening_balance)) + movement_delta
    balance = _money(balance_dec)
    # Подотчётный Сейф несёт раскладку остатка: зарезервировано (намечено под оплаты)
    # и свободно, плюс число активных резервов (бейдж «N целевых» на плитке).
    # Торговая касса Черникова — то же для переданных целёвок («целевые в кассе»),
    # плюс pending_payout_count: позиции «К выдаче» (целёвки + разрешения на авансы).
    # Для прочих кошельков понятия резерва нет → null.
    has_targets = wallet.code in (SAFE_WALLET_CODE, CASH_WITHDRAWAL_WALLET_CODE)
    return {
        "id": wallet.id,
        "code": wallet.code,
        "name": wallet.name,
        "type": wallet.type,
        "currency": wallet.currency,
        "is_internal_transfer_eligible": wallet.is_internal_transfer_eligible,
        "status": wallet.status,
        "account_id": wallet.account_id,
        "bank_code": bank_code,
        "opening_balance": opening,
        "opening_balance_date": wallet.opening_balance_date,
        "balance": balance,
        "reserved_total": _money(reserved_total) if has_targets else None,
        "free_total": _money(balance_dec - reserved_total) if has_targets else None,
        "active_allocations": active_allocations if has_targets else None,
        "pending_payout_count": pending_payout_count,
    }


def _classification_rule_payload(rule: ClassificationRule) -> dict[str, object]:
    return {
        "id": rule.id,
        "name": rule.name,
        "priority": rule.priority,
        "is_active": rule.is_active,
        "provider": rule.provider,
        "direction": rule.direction,
        "counterparty_inn_match": rule.counterparty_inn_match,
        "counterparty_name_pattern": rule.counterparty_name_pattern,
        "purpose_pattern": rule.purpose_pattern,
        "amount_min": _optional_money(rule.amount_min),
        "amount_max": _optional_money(rule.amount_max),
        "action": rule.action,
        "article_id": rule.article_id,
        "counterparty_id": rule.counterparty_id,
        "comment": rule.comment,
    }


async def _article_payloads(
    session: AsyncSession, articles: list[DdsArticle]
) -> list[dict[str, object]]:
    article_ids = [article.id for article in articles]
    aliases_by_article: dict[UUID, list[dict[str, object]]] = {
        article_id: [] for article_id in article_ids
    }
    if article_ids:
        aliases = await session.scalars(
            select(DdsArticleAlias)
            .where(DdsArticleAlias.article_id.in_(article_ids))
            .order_by(DdsArticleAlias.alias)
        )
        for alias in aliases.all():
            aliases_by_article.setdefault(alias.article_id, []).append(
                {"id": alias.id, "alias": alias.alias, "source": alias.source}
            )
    return [
        {
            "id": article.id,
            "code": article.code,
            "name": article.name,
            "movement_type": article.movement_type,
            "activity_type": article.activity_type,
            "parent_id": article.parent_id,
            "is_active": article.is_active,
            "kassa_enabled": article.kassa_enabled,
            "description": article.description,
            "aliases": aliases_by_article.get(article.id, []),
        }
        for article in articles
    ]


async def _counterparty_payloads(
    session: AsyncSession, counterparties: list[Counterparty]
) -> list[dict[str, object]]:
    counterparty_ids = [counterparty.id for counterparty in counterparties]
    aliases_by_counterparty: dict[UUID, list[dict[str, object]]] = {
        counterparty_id: [] for counterparty_id in counterparty_ids
    }
    if counterparty_ids:
        aliases = await session.scalars(
            select(CounterpartyAlias)
            .where(CounterpartyAlias.counterparty_id.in_(counterparty_ids))
            .order_by(CounterpartyAlias.alias)
        )
        for alias in aliases.all():
            aliases_by_counterparty.setdefault(alias.counterparty_id, []).append(
                {"id": alias.id, "alias": alias.alias, "source": alias.source}
            )
    return [
        {
            "id": counterparty.id,
            "name": counterparty.name,
            "inn": counterparty.inn,
            "type": counterparty.type,
            "status": counterparty.status,
            "aliases": aliases_by_counterparty.get(counterparty.id, []),
        }
        for counterparty in counterparties
    ]


def _classification_rule_data(data: dict[str, object]) -> dict[str, object]:
    for amount_key in ("amount_min", "amount_max"):
        if amount_key in data and data[amount_key] is not None:
            data[amount_key] = Decimal(str(data[amount_key]))
    return data


def _credential_payload(credential: SourceCredential) -> dict[str, object]:
    return {
        "id": credential.id,
        "provider": credential.provider,
        "credential_kind": credential.credential_kind,
        "is_active": credential.is_active,
        "expires_at": credential.expires_at,
        "metadata": credential.metadata_json,
        "created_at": credential.created_at,
        "updated_at": credential.updated_at,
    }


def _journal_occurred_at(operation_date: date, time_source: datetime | None) -> datetime:
    """Sortable/displayable timestamp for a journal row, in Moscow time.

    The DAY always comes from ``operation_date`` (the business date the row is
    filed under), so a row never drifts out of its day. The TIME-OF-DAY comes
    from the best real signal available — the bank ``posted_at`` for bank-backed
    movements, or the cashflow ``created_at`` for everything booked by hand. When
    there is no time signal at all the row sits at midnight. This is what lets the
    journal order same-day operations chronologically instead of by amount.
    """
    if time_source is None:
        return datetime.combine(operation_date, time(0, 0), tzinfo=MOSCOW_TZ)
    local = time_source.astimezone(MOSCOW_TZ)
    return datetime.combine(operation_date, local.timetz())


def _money(value: Decimal | int | None) -> str:
    return f"{Decimal(value or 0):.2f}"


def _optional_money(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return _money(value)


def _bank_operation_payload(operation: BankOperation) -> dict[str, object]:
    return {
        "id": operation.id,
        "provider": operation.provider,
        "provider_operation_id": operation.provider_operation_id,
        "account_id": operation.account_id,
        "operation_date": operation.operation_date,
        "posted_at": operation.posted_at,
        "direction": operation.direction,
        "amount": _money(operation.amount),
        "currency": operation.currency,
        "counterparty_name_raw": operation.counterparty_name_raw,
        "counterparty_inn_raw": operation.counterparty_inn_raw,
        "counterparty_account_raw": operation.counterparty_account_raw,
        "payment_purpose": operation.payment_purpose,
        "document_number": operation.document_number,
        "classification_status": operation.classification_status,
        "cashflow_transaction_id": operation.cashflow_transaction_id,
        "transfer_group_id": operation.transfer_group_id,
        "raw_payload": operation.raw_payload,
        # Карт-операция (получатель — эквайер): фронт показывает мягкое предупреждение при
        # ручной привязке к накладной вместо жёсткой ошибки guard.
        "is_card": _is_card_noise(operation),
    }


def _cashflow_payload(transaction: CashflowTransaction) -> dict[str, object]:
    return {
        "id": transaction.id,
        "wallet_id": transaction.wallet_id,
        "direction": transaction.direction,
        "amount": _money(transaction.amount),
        "operation_date": transaction.operation_date,
        "article_id": transaction.article_id,
        "counterparty_id": transaction.counterparty_id,
        "transfer_group_id": transaction.transfer_group_id,
        "source_kind": transaction.source_kind,
        "source_id": transaction.source_id,
        "payment_purpose": transaction.payment_purpose,
        "comment": transaction.comment,
        "quality_status": transaction.quality_status,
    }


def _owner_review_payload(
    case: ReconciliationCase, operation: BankOperation | None
) -> dict[str, object]:
    return {
        "id": case.id,
        "kind": case.kind,
        "status": case.status,
        "provider": case.provider,
        "bank_operation_id": case.bank_operation_id,
        "payload": case.payload,
        "created_at": case.created_at,
        "operation": _bank_operation_payload(operation) if operation else None,
    }


async def _pending_case_or_404(session: AsyncSession, case_id: UUID) -> ReconciliationCase:
    case = await session.get(ReconciliationCase, case_id)
    if case is None or case.status != "pending":
        raise HTTPException(status_code=404, detail="Pending review case not found")
    return case


async def _article_or_404(session: AsyncSession, article_id: UUID) -> DdsArticle:
    article = await session.get(DdsArticle, article_id)
    if article is None:
        raise HTTPException(status_code=404, detail="DDS article not found")
    return article


async def _counterparty_or_404(session: AsyncSession, counterparty_id: UUID) -> Counterparty:
    counterparty = await session.get(Counterparty, counterparty_id)
    if counterparty is None:
        raise HTTPException(status_code=404, detail="Counterparty not found")
    return counterparty


async def _classification_rule_or_404(session: AsyncSession, rule_id: UUID) -> ClassificationRule:
    rule = await session.get(ClassificationRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="Classification rule not found")
    return rule


def _rule_from_owner_review(
    operation: BankOperation, payload: OwnerReviewClassifyRequest
) -> ClassificationRule:
    purpose_pattern = _short_pattern(operation.payment_purpose)
    counterparty_pattern = (
        None if operation.counterparty_inn_raw else _short_pattern(operation.counterparty_name_raw)
    )
    return ClassificationRule(
        name=f"Owner review {operation.provider} {operation.provider_operation_id}",
        priority=50,
        is_active=True,
        provider=operation.provider,
        direction=operation.direction,
        counterparty_inn_match=operation.counterparty_inn_raw,
        counterparty_name_pattern=counterparty_pattern,
        purpose_pattern=purpose_pattern,
        action=payload.action,
        article_id=payload.article_id,
        counterparty_id=payload.counterparty_id,
        comment=f"Created from owner-review case for {operation.provider_operation_id}",
    )


def _rule_from_operation_split(
    operation: BankOperation, article_id: UUID, counterparty_id: UUID | None
) -> ClassificationRule:
    purpose_pattern = _short_pattern(operation.payment_purpose)
    counterparty_pattern = (
        None if operation.counterparty_inn_raw else _short_pattern(operation.counterparty_name_raw)
    )
    return ClassificationRule(
        name=f"Owner review {operation.provider} {operation.provider_operation_id}",
        priority=50,
        is_active=True,
        provider=operation.provider,
        direction=operation.direction,
        counterparty_inn_match=operation.counterparty_inn_raw,
        counterparty_name_pattern=counterparty_pattern,
        purpose_pattern=purpose_pattern,
        action="set_article",
        article_id=article_id,
        counterparty_id=counterparty_id,
        comment=f"Created from operation review for {operation.provider_operation_id}",
    )


def _short_pattern(value: str | None) -> str | None:
    text = " ".join((value or "").split())
    if not text:
        return None
    return text[:120]
