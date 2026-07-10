from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models import (
    Account,
    BankOperation,
    CashflowTransaction,
    ClassificationRule,
    Counterparty,
    CounterpartyPayableProfile,
    DdsArticle,
    EmployeePayout,
    OwnAccountsRegistry,
    ReconciliationCase,
    SupplierPrepayment,
    Wallet,
)
from app.services.banking.base import clean_digits

# Статья ДДС «Авансы поставщикам»: строка сплита с ней рождает дебиторку
# (supplier_prepayment) на выбранного контрагента, а не просто расход.
PREPAYMENT_ARTICLE_CODE = "advance_to_supplier"
# Статья «Оплата поставщикам»: строка разбора с указанной накладной гасит её (банковская
# аллокация на операцию), а не просто заводит расход. Несколько строк → несколько накладных.
SUPPLIER_PAYMENT_ARTICLE_CODE = "payment_to_supplier"
# Зарплатные статьи-получатели: строка разбора с указанным сотрудником заводит EmployeePayout
# («выплачено»), который расчёт ЗП вычитает из «к выдаче». Дубль new_payment.EMPLOYEE_PAYOUT_
# ARTICLE_CODES (равенство закреплено tests/counterparties/test_dds_employee_payout_attribution.py),
# чтобы не тянуть payroll на импорт классификатора банка.
EMPLOYEE_PAYOUT_ARTICLE_CODES = (
    "zarplata_administrativnogo_personala",
    "zarplata_proizvodstvennogo_personala",
)

# A manually booked DDS entry (e.g. a supplier paid straight from a bank wallet via
# ``pay_invoice_from_wallet``) records the cash fact before the bank feed does. When the
# statement later imports the same operation we must LINK it to that entry instead of
# booking a second expense — otherwise the ДДС double-counts the payment. We match it
# conservatively (same wallet + direction + exact amount, a small date drift, same payee
# INN when the statement carries one) and only against these manual source kinds.
PREBOOKED_DATE_WINDOW_DAYS = 3
PREBOOKABLE_SOURCE_KINDS = (
    "counterparty_payment",
    "kassa_cheque",
    "payroll_payout",
    "payroll_bank_to_safe",
    "salary_advance_bank_to_safe",
    "manual_bank_to_safe",
    # Предоплата поставщику через банк: при статусе «исполнен» заводится prebooked-проводка
    # 'supplier_prepayment', которую забирает приходящая операция выписки (без двойного расхода).
    "supplier_prepayment",
    # Банковская выплата сотруднику → транзит банк→Сейф: банк-нога prebooked, её забирает
    # исходящая операция выписки (баланс банка от выписки, проводка его не двигает).
    "employee_payout_bank_to_safe",
    # Оплата via-safe (неофициальный поставщик ИЛИ «просто трата» окна «Новый платёж»):
    # при статусе «исполнен» _settle_draft_via_safe заводит транзит р/с→Сейф, банк-нога
    # prebooked — её забирает исходящая операция выписки (перевод на карту ИП). Без этого
    # кода операция-перевод уходила в «требует разбора».
    "supplier_bank_to_safe",
)

# «Пополнение Сейфа»: перевод р/с → личная карта (Сейф) учитывается как ВНУТРЕННИЙ
# перевод транзитом, не расход. У карты физлица нет выписки, поэтому пару проводок
# заводим вручную (как admin-ЗП book_bank_to_safe_transfer): банк-нога out служит
# привязкой операции и аналитикой «перевод между счетами» (баланс банка идёт от
# выписки, она его не двигает), Сейф-нога in двигает баланс Сейфа.
SAFE_TOPUP_SOURCE_KIND = "manual_bank_to_safe"
SAFE_WALLET_CODE = "cash_safe"
TRANSFER_OUT_ARTICLE_CODE = "vybytie_perevod_mezhdu_schetami"
TRANSFER_IN_ARTICLE_CODE = "postuplenie_perevod_mezhdu_schetami"

# Чек Кассы, заведённый ВРУЧНУЮ до прихода выписки (банк ещё не передал card-операцию —
# выходные / задержка webhook): полная сумма «висит» одной проводкой со статусом
# ``awaiting_bank`` и в баланс банка не влияет (баланс = выписка). Такую проводку НЕ трогает
# общий prebooked-механизм — у card-операции ИНН процессинговый (шум ТБанка), и развести
# по статьям её надо в момент матча. Сводит отдельный чек-реконсилер
# ``match_pending_cheque_operations`` (kassa/cheque.py).
AWAITING_BANK_QUALITY = "awaiting_bank"


@dataclass(frozen=True)
class ClassificationResult:
    classified: int = 0
    internal_transfer: int = 0
    needs_review: int = 0
    excluded: int = 0


async def run_classification_rules(
    session: AsyncSession, operations: list[BankOperation]
) -> ClassificationResult:
    rules = (
        await session.scalars(
            select(ClassificationRule)
            .where(ClassificationRule.is_active.is_(True))
            .order_by(ClassificationRule.priority, ClassificationRule.name)
        )
    ).all()
    counts = {"classified": 0, "internal_transfer": 0, "needs_review": 0, "excluded": 0}
    # Transactions claimed by an op earlier in this same run aren't flushed yet, so the
    # "already linked" subquery can't see them — track them in-memory to keep matching 1:1.
    claimed_transaction_ids: set[UUID] = set()

    for operation in operations:
        if operation.cashflow_transaction_id is None:
            prebooked = await _find_prebooked_payment(
                session, operation, claimed=claimed_transaction_ids
            )
            if prebooked is not None:
                operation.cashflow_transaction_id = prebooked.id
                operation.classification_status = "classified"
                claimed_transaction_ids.add(prebooked.id)
                counts["classified"] += 1
                continue
        matched = False
        for rule in rules:
            if not await _rule_matches(session, rule, operation):
                continue
            matched = True
            await apply_operation_action(
                session,
                operation,
                action=rule.action,
                article_id=rule.article_id,
                counterparty_id=rule.counterparty_id,
                quality_status="auto",
            )
            if rule.action == "set_article":
                counts["classified"] += 1
            elif rule.action == "mark_internal_transfer":
                counts["internal_transfer"] += 1
            elif rule.action == "exclude":
                counts["excluded"] += 1
            break
        if not matched:
            operation.classification_status = "needs_review"
            await create_or_update_reconciliation_case(
                session,
                kind="unclassified_operation",
                provider=operation.provider,
                bank_operation_id=operation.id,
                payload=_operation_review_payload(operation),
            )
            counts["needs_review"] += 1

    await session.flush()
    from app.services.banking.transfer_matching import find_and_link_transfer_pairs

    await find_and_link_transfer_pairs(session)
    return ClassificationResult(**counts)


async def reconcile_needs_review_prebooked(session: AsyncSession) -> int:
    """Подхватить «требующие проверки» операции, под которые prebooked-проводка появилась ПОЗЖЕ.

    Гонка вебхук↔поллинг: операция выписки прилетает realtime-вебхуком и уходит в needs_review
    раньше, чем поллинг статусов создаёт prebooked-проводку (оплата поставщику / транзит ЗП /
    выдача аванса). На ингесте склейки не было (проводки ещё нет), и сама она потом не
    подхватывается → дубль в журнале. Этот проход после поллинга сводит их обратно: для каждой
    непривязанной needs_review-операции ищем подходящую prebooked-проводку тем же
    ``_find_prebooked_payment`` (тот же кошелёк/сумма/направление/дата/ИНН, FIFO, без чужого ИНН),
    привязываем и закрываем кейс. Возвращает число склеек.
    """
    operations = (
        await session.scalars(
            select(BankOperation).where(
                BankOperation.classification_status == "needs_review",
                BankOperation.cashflow_transaction_id.is_(None),
            )
        )
    ).all()
    claimed: set[UUID] = set()
    linked = 0
    for operation in operations:
        prebooked = await _find_prebooked_payment(session, operation, claimed=claimed)
        if prebooked is None:
            continue
        operation.cashflow_transaction_id = prebooked.id
        operation.classification_status = "classified"
        claimed.add(prebooked.id)
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
                resolution_payload={"action": "reconcile_prebooked"},
            )
        linked += 1
    if linked:
        await session.flush()
    return linked


async def _cashflow_row_has_dependents(session: AsyncSession, transaction_id: UUID) -> bool:
    """Есть ли у проводки зависимые записи (аллокации накладных / предоплаты / выплаты).

    Такую проводку нельзя удалять как «пустую» авто-строку — за ней стоит гашение накладной,
    дебиторка или выплата. Авто-строка правила ``set_article`` их не создаёт, но проверяем на
    всякий случай, чтобы поглотитель не снёс ручную/содержательную разметку.
    """
    from app.models import EmployeePayout, InvoicePaymentAllocation

    for model in (InvoicePaymentAllocation, SupplierPrepayment, EmployeePayout):
        exists = await session.scalar(
            select(model.id).where(model.cashflow_transaction_id == transaction_id).limit(1)
        )
        if exists is not None:
            return True
    return False


async def absorb_auto_classified_counterparty_payment(session: AsyncSession) -> int:
    """Свести авто-классифицированную операцию выписки с prebooked-проводкой оплаты поставщику.

    Гонка «правило↔пометка оплаты»: операция выписки приходит и на ингесте СРАЗУ
    классифицируется правилом (у поставщика есть правило классификации) в собственную
    ``bank_operation``-проводку — ДО того, как черновик помечается ``paid`` и создаёт
    prebooked-проводку ``counterparty_payment``. Тогда один платёж описан ДВУМЯ строками, а
    ``reconcile_needs_review_prebooked`` их не сводит (та операция уже ``classified``, а не
    ``needs_review``). Здесь для каждой prebooked-проводки оплаты поставщику, ещё не привязанной
    к операции выписки, ищем авто-строку того же платежа ДЕТЕРМИНИРОВАННО — по documentNumber
    черновика (== documentNumber операции; тот же ключ, что мы шлём банку) вместе с суммой,
    кошельком и направлением ``out``. Найдя: перепривязываем операцию к prebooked-проводке и
    удаляем авто-строку. Трогаем только ``quality_status='auto'`` строки без зависимостей
    (аллокаций / предоплат / выплат) — ручную разметку не задеваем. Возвращает число склеек.
    Идемпотентно; запускать ПОСЛЕ ``reconcile_needs_review_prebooked``.
    """
    from app.models import CounterpartyPaymentDraft
    from app.services.banking.tbank import _document_number

    auto_txn = aliased(CashflowTransaction)
    prebooked_rows = (
        await session.scalars(
            select(CashflowTransaction).where(
                CashflowTransaction.source_kind == "counterparty_payment",
                CashflowTransaction.direction == "out",
                ~(
                    select(BankOperation.id)
                    .where(BankOperation.cashflow_transaction_id == CashflowTransaction.id)
                    .exists()
                ),
            )
        )
    ).all()
    absorbed = 0
    for prebooked in prebooked_rows:
        if prebooked.source_id is None:
            continue
        draft = await session.get(CounterpartyPaymentDraft, prebooked.source_id)
        if draft is None or not draft.document_id:
            continue
        docnum = _document_number(draft.document_id)
        operation = (
            await session.scalars(
                select(BankOperation)
                .join(auto_txn, auto_txn.id == BankOperation.cashflow_transaction_id)
                .where(
                    BankOperation.direction == "out",
                    BankOperation.amount == prebooked.amount,
                    BankOperation.document_number == docnum,
                    auto_txn.source_kind == "bank_operation",
                    auto_txn.quality_status == "auto",
                    auto_txn.wallet_id == prebooked.wallet_id,
                )
            )
        ).first()
        if operation is None:
            continue
        auto_row = await session.get(CashflowTransaction, operation.cashflow_transaction_id)
        if auto_row is None or await _cashflow_row_has_dependents(session, auto_row.id):
            continue
        # Перепривязываем операцию к prebooked-проводке, затем удаляем осиротевшую авто-строку —
        # но только если её больше не держит ни одна операция (иначе оставим как есть).
        operation.cashflow_transaction_id = prebooked.id
        operation.classification_status = "classified"
        await session.flush()
        still_linked = await session.scalar(
            select(func.count())
            .select_from(BankOperation)
            .where(BankOperation.cashflow_transaction_id == auto_row.id)
        )
        if still_linked:
            continue
        await session.delete(auto_row)
        await session.flush()
        absorbed += 1
    return absorbed


async def apply_operation_action(
    session: AsyncSession,
    operation: BankOperation,
    *,
    action: str,
    article_id: UUID | None = None,
    counterparty_id: UUID | None = None,
    quality_status: str = "auto",
) -> None:
    if action == "set_article":
        if article_id is None:
            operation.classification_status = "needs_review"
            await create_or_update_reconciliation_case(
                session,
                kind="unclassified_operation",
                provider=operation.provider,
                bank_operation_id=operation.id,
                payload={**_operation_review_payload(operation), "reason": "rule_without_article"},
            )
            return
        wallet = await _wallet_for_operation(session, operation)
        if wallet is None:
            operation.classification_status = "needs_review"
            await create_or_update_reconciliation_case(
                session,
                kind="unclassified_operation",
                provider=operation.provider,
                bank_operation_id=operation.id,
                payload={**_operation_review_payload(operation), "reason": "wallet_not_found"},
            )
            return
        transaction = None
        if operation.cashflow_transaction_id is not None:
            transaction = await session.get(CashflowTransaction, operation.cashflow_transaction_id)
        if transaction is None:
            # Manual classification can reach this directly (bypassing the reconcile pass
            # in ``run_classification_rules``) — link a pre-booked manual payment here too
            # so a duplicate expense is never created. Keep its article/source/allocation.
            prebooked = await _find_prebooked_payment(session, operation, claimed=set())
            if prebooked is not None:
                operation.cashflow_transaction_id = prebooked.id
                operation.classification_status = "classified"
                return
        if transaction is None:
            transaction = CashflowTransaction(
                wallet_id=wallet.id,
                direction=operation.direction,
                amount=operation.amount,
                operation_date=operation.operation_date,
                article_id=article_id,
                counterparty_id=counterparty_id,
                source_kind="bank_operation",
                source_id=operation.id,
                payment_purpose=operation.payment_purpose,
                quality_status=quality_status,
            )
            session.add(transaction)
            await session.flush()
            operation.cashflow_transaction_id = transaction.id
        else:
            transaction.wallet_id = wallet.id
            transaction.direction = operation.direction
            transaction.amount = operation.amount
            transaction.operation_date = operation.operation_date
            transaction.article_id = article_id
            transaction.counterparty_id = counterparty_id
            transaction.payment_purpose = operation.payment_purpose
            transaction.quality_status = quality_status
        operation.classification_status = "classified"
        return

    if action == "mark_internal_transfer":
        operation.classification_status = "internal_transfer"
        operation.cashflow_transaction_id = None
        return

    if action == "exclude":
        operation.classification_status = "excluded"
        operation.cashflow_transaction_id = None
        return

    operation.classification_status = "needs_review"
    await create_or_update_reconciliation_case(
        session,
        kind="unclassified_operation",
        provider=operation.provider,
        bank_operation_id=operation.id,
        payload={**_operation_review_payload(operation), "reason": f"unknown_action:{action}"},
    )


async def create_or_update_reconciliation_case(
    session: AsyncSession,
    *,
    kind: str,
    provider: str | None = None,
    bank_operation_id: UUID | None = None,
    payload: dict[str, Any] | None = None,
) -> ReconciliationCase:
    query = select(ReconciliationCase).where(
        ReconciliationCase.kind == kind,
        ReconciliationCase.status == "pending",
    )
    if bank_operation_id is not None:
        query = query.where(ReconciliationCase.bank_operation_id == bank_operation_id)
    elif provider is not None:
        query = query.where(ReconciliationCase.provider == provider)
    existing = await session.scalar(query)
    if existing is not None:
        existing.payload = payload or existing.payload or {}
        return existing
    case = ReconciliationCase(
        kind=kind,
        status="pending",
        provider=provider,
        bank_operation_id=bank_operation_id,
        payload=payload or {},
    )
    session.add(case)
    await session.flush()
    return case


async def close_reconciliation_case(
    session: AsyncSession,
    case: ReconciliationCase,
    *,
    status: str,
    resolution_payload: dict[str, Any] | None = None,
) -> None:
    case.status = status
    case.resolved_at = datetime.now(UTC)
    case.resolution_payload = resolution_payload or {}


async def _rule_matches(
    session: AsyncSession, rule: ClassificationRule, operation: BankOperation
) -> bool:
    if rule.provider and rule.provider != operation.provider:
        return False
    if rule.direction and rule.direction != operation.direction:
        return False
    expected_inn = clean_digits(rule.counterparty_inn_match)
    if expected_inn and expected_inn != clean_digits(operation.counterparty_inn_raw):
        return False
    if rule.counterparty_name_pattern and not _contains(
        operation.counterparty_name_raw, rule.counterparty_name_pattern
    ):
        return False
    if rule.purpose_pattern and not _contains(operation.payment_purpose, rule.purpose_pattern):
        return False
    amount = Decimal(operation.amount)
    if rule.amount_min is not None and amount < Decimal(rule.amount_min):
        return False
    if rule.amount_max is not None and amount > Decimal(rule.amount_max):
        return False
    if rule.action == "mark_internal_transfer":
        return await _counterparty_is_own_account(session, operation)
    return True


async def _counterparty_is_own_account(session: AsyncSession, operation: BankOperation) -> bool:
    # Internal transfers are matched by ACCOUNT NUMBER only, never by INN. An IP and the
    # owner's personal card share one INN (e.g. Шокина 890307589201), so an INN match would
    # wrongly flag a payroll/withdrawal to the personal card (40817…) as an internal transfer.
    # Only our own settlement accounts (40802…, present in the registry) count as internal.
    account_number = clean_digits(operation.counterparty_account_raw)
    if not account_number:
        return False
    query = select(OwnAccountsRegistry).where(
        OwnAccountsRegistry.is_active.is_(True),
        OwnAccountsRegistry.account_number == account_number,
    )
    return await session.scalar(query) is not None


async def _wallet_for_operation(session: AsyncSession, operation: BankOperation) -> Wallet | None:
    if operation.account_id is not None:
        wallet = await session.scalar(
            select(Wallet).where(
                Wallet.account_id == operation.account_id,
                Wallet.status == "active",
            )
        )
        if wallet is not None:
            return wallet
    return await session.scalar(
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


async def _find_prebooked_payment(
    session: AsyncSession, operation: BankOperation, *, claimed: set[UUID]
) -> CashflowTransaction | None:
    """A manually pre-booked DDS entry that this bank operation settles, or ``None``.

    Linking the bank operation to it — instead of booking a fresh ``bank_operation``
    expense — is what prevents a double expense in the ДДС. Conservative on purpose:
    same wallet (so cash/card entries on other wallets are never touched), same
    direction, exact amount, a small date drift, not yet linked to any bank operation,
    and the same payee INN whenever the statement carries one.
    """
    wallet = await _wallet_for_operation(session, operation)
    if wallet is None:
        return None
    low = operation.operation_date - timedelta(days=PREBOOKED_DATE_WINDOW_DAYS)
    high = operation.operation_date + timedelta(days=PREBOOKED_DATE_WINDOW_DAYS)
    already_linked = (
        select(BankOperation.id)
        .where(BankOperation.cashflow_transaction_id == CashflowTransaction.id)
        .exists()
    )
    query = (
        select(CashflowTransaction)
        .where(
            CashflowTransaction.wallet_id == wallet.id,
            CashflowTransaction.direction == operation.direction,
            CashflowTransaction.amount == operation.amount,
            CashflowTransaction.source_kind.in_(PREBOOKABLE_SOURCE_KINDS),
            # Пендинг-чек (ручной ввод до выписки) сводит отдельный чек-реконсилер,
            # а не общий prebooked — иначе он привяжет операцию без разнесения по статьям.
            CashflowTransaction.quality_status != AWAITING_BANK_QUALITY,
            CashflowTransaction.operation_date >= low,
            CashflowTransaction.operation_date <= high,
            ~already_linked,
        )
        .order_by(CashflowTransaction.created_at)
    )
    if claimed:
        query = query.where(CashflowTransaction.id.not_in(claimed))
    candidates = (await session.scalars(query)).all()
    if not candidates:
        return None

    op_inn = clean_digits(operation.counterparty_inn_raw)
    if not op_inn:
        # Statement has no INN to disambiguate — fall back to the oldest candidate (FIFO).
        return candidates[0]
    fallback: CashflowTransaction | None = None
    for candidate in candidates:
        candidate_inn = await _transaction_counterparty_inn(session, candidate)
        if candidate_inn == op_inn:
            return candidate
        if await _transaction_counterparty_shares_brand_group(session, candidate, op_inn):
            return candidate
        if candidate_inn is None and fallback is None:
            fallback = candidate
    # A candidate with a *different* known INN is matched only through an explicit shared
    # supplier brand group (e.g. Амай: ООО ТОРА + ИП Скачкова); otherwise only same-INN or
    # unknown-INN candidates are safe.
    return fallback


async def _transaction_counterparty_inn(
    session: AsyncSession, transaction: CashflowTransaction
) -> str | None:
    if transaction.counterparty_id is None:
        return None
    inn = await session.scalar(
        select(Counterparty.inn).where(Counterparty.id == transaction.counterparty_id)
    )
    return clean_digits(inn) or None


async def _transaction_counterparty_shares_brand_group(
    session: AsyncSession, transaction: CashflowTransaction, operation_inn: str
) -> bool:
    """prebooked-проводка и операция выписки — один поставщик под разными юрлицами (общая
    ``CounterpartyPayableProfile.brand_group``). Кейс Амай: проводка на ООО «ТОРА», а деньги
    ушли на ИП Скачкова — один бренд-поставщик, поэтому матч допустим несмотря на разные ИНН."""
    if transaction.counterparty_id is None or not operation_inn:
        return False
    candidate_group = await session.scalar(
        select(CounterpartyPayableProfile.brand_group).where(
            CounterpartyPayableProfile.counterparty_id == transaction.counterparty_id
        )
    )
    candidate_group_norm = (candidate_group or "").strip().casefold()
    if not candidate_group_norm:
        return False

    rows = await session.execute(
        select(Counterparty.inn, CounterpartyPayableProfile.brand_group).join(
            CounterpartyPayableProfile,
            CounterpartyPayableProfile.counterparty_id == Counterparty.id,
        )
    )
    for inn, group in rows:
        if (
            (group or "").strip().casefold() == candidate_group_norm
            and clean_digits(inn) == operation_inn
        ):
            return True
    return False


def _contains(value: str | None, pattern: str) -> bool:
    return pattern.casefold() in (value or "").casefold()


def _operation_review_payload(operation: BankOperation) -> dict[str, Any]:
    return {
        "provider": operation.provider,
        "provider_operation_id": operation.provider_operation_id,
        "operation_date": operation.operation_date.isoformat(),
        "direction": operation.direction,
        "amount": str(operation.amount),
        "counterparty_name_raw": operation.counterparty_name_raw,
        "counterparty_inn_raw": operation.counterparty_inn_raw,
        "counterparty_account_raw": operation.counterparty_account_raw,
        "payment_purpose": operation.payment_purpose,
    }


async def _clear_operation_cashflow(session: AsyncSession, operation: BankOperation) -> None:
    """Drop cashflow rows previously booked from this bank operation (re-split).

    Only rows whose ``source`` is this very operation are removed — a pre-booked
    manual payment (different ``source_kind``) linked to the op is left untouched.
    """
    existing = await session.scalars(
        select(CashflowTransaction).where(
            CashflowTransaction.source_kind == "bank_operation",
            CashflowTransaction.source_id == operation.id,
        )
    )
    for transaction in existing.all():
        await session.delete(transaction)
    operation.cashflow_transaction_id = None
    await session.flush()


async def resolve_or_create_operation_counterparty(
    session: AsyncSession, *, name: str, inn: str | None, account: str | None
) -> UUID:
    """Найти контрагента по ИНН (если есть) или создать нового из распознанных данных банк-
    операции (имя/ИНН/счёт). Новый — ``status='requires_setup'`` + роль supplier + профиль с
    реквизитами (расчётный счёт из выписки), чтобы будущие платежи матчились по ИНН и оператору
    осталось только донастроить карточку."""
    from app.models import Counterparty, CounterpartyPayableProfile, CounterpartyRole

    clean_name = (name or "").strip()
    if not clean_name:
        raise ValueError("В операции не распознано имя контрагента")
    clean_inn = (inn or "").strip() or None
    if clean_inn:
        existing = await session.scalar(select(Counterparty).where(Counterparty.inn == clean_inn))
        if existing is not None:
            return existing.id
    cp_type = "individual" if clean_inn and len(clean_inn) == 12 else "legal_entity"
    counterparty = Counterparty(
        name=clean_name, inn=clean_inn, type=cp_type, status="requires_setup"
    )
    session.add(counterparty)
    await session.flush()
    session.add(CounterpartyRole(counterparty_id=counterparty.id, role="supplier"))
    requisites: dict[str, str] = {}
    if clean_inn:
        requisites["inn"] = clean_inn
    clean_account = (account or "").strip() or None
    if clean_account:
        requisites["bankAcnt"] = clean_account
    session.add(
        CounterpartyPayableProfile(
            counterparty_id=counterparty.id, internal_name=clean_name, requisites=requisites
        )
    )
    await session.flush()
    return counterparty.id


async def apply_operation_split(
    session: AsyncSession,
    operation: BankOperation,
    *,
    splits: list[tuple[UUID, Decimal, str | None, UUID | None, UUID | None]],
    counterparty_id: UUID | None = None,
    quality_status: str = "owner_review",
    actor_user_id: UUID | None = None,
    allow_card: bool = False,
) -> list[UUID]:
    """Spread one bank operation across one or more DDS articles.

    Splits are ``(article_id, amount, comment, invoice_id, employee_id)``.

    Balance is taken from the statement (see ``_wallet_movement_deltas``), so this
    only fills DDS analytics — the split amounts must add up to the operation amount
    but never move the wallet balance. Re-splitting first drops any cashflow already
    booked from this operation, then books one cashflow row per article.

    Строка со статьёй «Оплата поставщикам» с указанным ``invoice_id`` дополнительно гасит эту
    накладную на сумму строки (банковская аллокация на операцию). Поддержаны несколько накладных
    на одну операцию (мультисплит) и частичная оплата (сумма строки ≤ остатка накладной).

    Строка с зарплатной статьёй (``EMPLOYEE_PAYOUT_ARTICLE_CODES``) и указанным ``employee_id``
    заводит ``EmployeePayout`` («выплачено»), привязанный к порождённой проводке — расчёт ЗП
    вычтет сумму из «к выдаче». ``employee_id`` на не-зарплатной статье — ошибка.

    ``allow_card=True`` — оператор явно подтвердил ручную привязку карт-оплаты к накладной
    (местный закуп оплачен картой): guard ``assert_bank_matchable`` пропустит карт-шум. Проверка
    делается ДО любых записей вместе с остальными валидациями, поэтому при отказе разбор не
    применяется даже частично (validate-first: единственный commit — в вызывающем роуте).
    """
    # Гашение накладной живёт в counterparty-сервисах; верхнеуровневый импорт замкнул бы цикл.
    from app.models import InvoicePaymentAllocation, SupplierInvoice
    from app.services.counterparty_bank_match import (
        CounterpartyMatchError,
        _apply_bank_allocation,
        assert_bank_matchable,
    )
    from app.services.counterparty_matching import _invoice_remaining, _recompute_status

    if not splits:
        raise ValueError("Нужна хотя бы одна статья")
    # Совместимость: строка разбора — (article, amount, comment, invoice_id[, employee_id]).
    # Старые вызовы без сотрудника (4-кортеж) дополняем employee_id=None.
    splits = [tuple(item) + (None,) * (5 - len(item)) for item in splits]
    total = sum((amount for _article, amount, *_rest in splits), Decimal("0"))
    if total != Decimal(operation.amount):
        raise ValueError(
            f"Сумма по статьям ({total}) не равна сумме операции ({operation.amount})"
        )
    wallet = await _wallet_for_operation(session, operation)
    if wallet is None:
        raise ValueError("Не найден кошелёк для операции")

    # Строка со статьёй «Авансы поставщикам» создаёт дебиторку на контрагента — без него
    # непонятно, чья это предоплата, поэтому контрагент обязателен.
    advance_article_id = await session.scalar(
        select(DdsArticle.id).where(DdsArticle.code == PREPAYMENT_ARTICLE_CODE)
    )
    uses_advance = advance_article_id is not None and any(
        article_id == advance_article_id for article_id, *_rest in splits
    )
    if uses_advance and counterparty_id is None:
        raise ValueError("Для статьи «Авансы поставщикам» укажите контрагента")

    # Зарплатные статьи: строка с сотрудником заведёт EmployeePayout. employee_id вне зарплатной
    # статьи запрещаем ДО записей (страховка бэка поверх фронтового гейтинга по статье).
    salary_article_ids = set(
        (
            await session.scalars(
                select(DdsArticle.id).where(DdsArticle.code.in_(EMPLOYEE_PAYOUT_ARTICLE_CODES))
            )
        ).all()
    )
    for article_id, _amount, _comment, _invoice, employee_id in splits:
        if employee_id is not None and article_id not in salary_article_ids:
            raise ValueError("Сотрудника можно указать только для зарплатной статьи")

    # Привязки накладных: валидируем статью и пригодность ДО любых записей.
    supplier_payment_article_id = await session.scalar(
        select(DdsArticle.id).where(DdsArticle.code == SUPPLIER_PAYMENT_ARTICLE_CODE)
    )
    invoice_by_id: dict[UUID, SupplierInvoice] = {}
    for article_id, _amount, _comment, invoice_id, _employee in splits:
        if invoice_id is None:
            continue
        if supplier_payment_article_id is None or article_id != supplier_payment_article_id:
            raise ValueError("Накладную можно привязать только к строке «Оплата поставщикам»")
        invoice = await session.get(SupplierInvoice, invoice_id)
        if invoice is None:
            raise ValueError("Накладная не найдена")
        try:
            assert_bank_matchable(invoice, operation, allow_card=allow_card)
        except CounterpartyMatchError as error:
            raise ValueError(str(error)) from error
        invoice_by_id[invoice_id] = invoice

    # Re-split: снять прежние гашения накладных ЭТОЙ операцией (cashflow чистит
    # _clear_operation_cashflow, аллокации — здесь), иначе повторный разбор задвоит гашение.
    prior_allocations = (
        await session.scalars(
            select(InvoicePaymentAllocation).where(
                InvoicePaymentAllocation.bank_operation_id == operation.id,
                InvoicePaymentAllocation.source_kind == "bank",
            )
        )
    ).all()
    reaffected_invoice_ids = {alloc.invoice_id for alloc in prior_allocations}
    for alloc in prior_allocations:
        await session.delete(alloc)
    await session.flush()

    # Остаток проверяем ПОСЛЕ снятия прежних аллокаций (иначе повторный разбор той же накладной
    # увидел бы её занятой собственной прежней аллокацией).
    for invoice_id, invoice in invoice_by_id.items():
        remaining = await _invoice_remaining(session, invoice)
        amount = next(amt for _a, amt, _c, inv, _e in splits if inv == invoice_id)
        if amount > remaining:
            raise ValueError(
                f"Сумма ({amount}) больше остатка накладной "
                f"{invoice.number or invoice.id} ({remaining})"
            )

    # Re-split: снять EmployeePayout, заведённые прежним разбором ЭТОЙ операции (их проводки
    # сейчас удалит _clear_operation_cashflow, иначе повторный разбор задвоит «выплачено»).
    # Уже зачтённую в проведённой ведомости выплату (offset_amount>0) не трогаем — блокируем.
    prior_op_cashflow = select(CashflowTransaction.id).where(
        CashflowTransaction.source_kind == "bank_operation",
        CashflowTransaction.source_id == operation.id,
    )
    prior_payouts = (
        await session.scalars(
            select(EmployeePayout).where(
                EmployeePayout.cashflow_transaction_id.in_(prior_op_cashflow)
            )
        )
    ).all()
    for payout in prior_payouts:
        if Decimal(payout.offset_amount) > 0:
            raise ValueError(
                "Операция уже зачтена в проведённой ведомости сотрудника — "
                "сначала откатите ведомость (дефинализация)"
            )
    for payout in prior_payouts:
        await session.delete(payout)
    if prior_payouts:
        await session.flush()

    await _clear_operation_cashflow(session, operation)
    created: list[UUID] = []
    for article_id, amount, comment, invoice_id, employee_id in splits:
        transaction = CashflowTransaction(
            wallet_id=wallet.id,
            direction=operation.direction,
            amount=amount,
            operation_date=operation.operation_date,
            article_id=article_id,
            counterparty_id=counterparty_id,
            source_kind="bank_operation",
            source_id=operation.id,
            payment_purpose=operation.payment_purpose,
            comment=comment,
            quality_status=quality_status,
        )
        session.add(transaction)
        await session.flush()
        created.append(transaction.id)
        # Аванс поставщику → дебиторка, привязанная к этой проводке.
        if advance_article_id is not None and article_id == advance_article_id:
            session.add(
                SupplierPrepayment(
                    counterparty_id=counterparty_id,
                    kind="goods",
                    wallet_id=wallet.id,
                    amount=amount,
                    amount_settled=Decimal("0.00"),
                    status="open",
                    cashflow_transaction_id=transaction.id,
                    article_id=article_id,
                )
            )
            await session.flush()
        # Оплата поставщику с привязкой → гасим накладную на сумму строки.
        if invoice_id is not None:
            await _apply_bank_allocation(
                session,
                invoice=invoice_by_id[invoice_id],
                operation=operation,
                amount=amount,
                actor_user_id=actor_user_id,
            )
        # Зарплатная статья + сотрудник → леджер «выплачено» (EmployeePayout), привязанный к этой
        # проводке. Второй проводки НЕ создаём: денежный факт несёт строка выписки (баланс из
        # выписки). Режим оклада определяет kind: on_demand собственник → 'owner_salary' (гасится
        # compute_on_demand_debt), обычный сотрудник → 'salary' (гасится apply_employee_payout_offsets).
        if employee_id is not None and article_id in salary_article_ids:
            from app.services.payroll_admin import resolve_employee_payout_kind

            payout_kind = await resolve_employee_payout_kind(
                session, employee_id, as_of=operation.operation_date
            )
            session.add(
                EmployeePayout(
                    employee_id=employee_id,
                    kind=payout_kind,
                    amount=amount,
                    payout_date=operation.operation_date,
                    wallet_id=wallet.id,
                    article_id=article_id,
                    cashflow_transaction_id=transaction.id,
                    bank_operation_id=operation.id,
                    status="paid",
                    created_by_user_id=actor_user_id,
                )
            )
            await session.flush()

    # Пересчёт статуса всех затронутых накладных (новые гашения + откатанные прежние).
    for invoice_id in {*invoice_by_id, *reaffected_invoice_ids}:
        invoice = invoice_by_id.get(invoice_id) or await session.get(SupplierInvoice, invoice_id)
        if invoice is not None:
            await _recompute_status(session, invoice)

    operation.cashflow_transaction_id = created[0]
    operation.classification_status = "classified"
    return created


async def book_safe_topup(session: AsyncSession, operation: BankOperation) -> list[UUID]:
    """Провести исходящий перевод р/с → карта «Сейф» как пополнение Сейфа (транзит).

    Перевод на личную карту владельца — это не расход, а перемещение денег на
    подотчётный счёт, откуда потом раздаются оплаты. Заводим ДВЕ проводки с общим
    источником (``manual_bank_to_safe`` + operation.id), как admin-ЗП
    ``book_bank_to_safe_transfer``:

    * банк-нога (out, «Выбытие — перевод между счетами») на счёте операции — служит
      привязкой операции и аналитикой перевода; баланс банка идёт от выписки, эта
      проводка его не двигает;
    * Сейф-нога (in, «Поступление — перевод между счетами») — двигает баланс Сейфа.

    Перезапуск идемпотентен: прежние ноги этой операции (обычный сплит или прошлый
    topup) удаляются перед перепроведением.
    """
    if operation.direction != "out":
        raise ValueError(
            "Пополнение Сейфа доступно только для исходящей операции (перевод с расчётного счёта)"
        )
    bank_wallet = await _wallet_for_operation(session, operation)
    if bank_wallet is None:
        raise ValueError("Не найден банковский кошелёк операции")
    safe_wallet = await session.scalar(
        select(Wallet).where(Wallet.code == SAFE_WALLET_CODE, Wallet.status == "active")
    )
    if safe_wallet is None:
        raise ValueError("Не найден кошелёк «Сейф»")
    out_article = await session.scalar(
        select(DdsArticle.id).where(DdsArticle.code == TRANSFER_OUT_ARTICLE_CODE)
    )
    in_article = await session.scalar(
        select(DdsArticle.id).where(DdsArticle.code == TRANSFER_IN_ARTICLE_CODE)
    )
    if out_article is None or in_article is None:
        raise ValueError("Не найдены статьи перевода между счетами")

    # Снести прежние проводки, заведённые из этой операции (re-classify) — и обычный
    # сплит (``bank_operation``), и прошлый topup (``manual_bank_to_safe``).
    prior = await session.scalars(
        select(CashflowTransaction).where(
            CashflowTransaction.source_id == operation.id,
            CashflowTransaction.source_kind.in_(("bank_operation", SAFE_TOPUP_SOURCE_KIND)),
        )
    )
    for transaction in prior.all():
        await session.delete(transaction)
    operation.cashflow_transaction_id = None
    await session.flush()

    bank_leg = CashflowTransaction(
        wallet_id=bank_wallet.id,
        direction="out",
        amount=operation.amount,
        operation_date=operation.operation_date,
        article_id=out_article,
        source_kind=SAFE_TOPUP_SOURCE_KIND,
        source_id=operation.id,
        payment_purpose=operation.payment_purpose,
        quality_status="final",
    )
    session.add(bank_leg)
    safe_leg = CashflowTransaction(
        wallet_id=safe_wallet.id,
        direction="in",
        amount=operation.amount,
        operation_date=operation.operation_date,
        article_id=in_article,
        source_kind=SAFE_TOPUP_SOURCE_KIND,
        source_id=operation.id,
        payment_purpose=operation.payment_purpose,
        quality_status="final",
    )
    session.add(safe_leg)
    await session.flush()
    operation.cashflow_transaction_id = bank_leg.id
    operation.classification_status = "classified"
    return [bank_leg.id, safe_leg.id]
