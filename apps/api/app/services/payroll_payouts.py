from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.models import (
    Account,
    AppSetting,
    BankOperation,
    CashflowTransaction,
    DdsArticle,
    PayrollBankDraft,
    PayrollLine,
    PayrollPayment,
    PayrollPeriod,
    PayrollRun,
    PayrollRunEvent,
    Wallet,
)
from app.services.bank_payment_status import classify_payment_status
from app.services.banking import BankClient
from app.services.banking.exceptions import BankFetchError
from app.services.banking.payout import payer_account_for, payout_client_for
from app.services.banking.tbank import build_payment_draft_api_payload
from app.services.payroll_payout_allocation import (
    DDS_ARTICLE_ADMIN_PAYROLL,
    DDS_ARTICLE_PRODUCTION_PAYROLL,
    PayoutBucket,
    allocate_cash_cascade,
    build_payout_buckets,
)
from app.services.payroll_runner import PayrollConflictError, PayrollNotFoundError, money_text
from app.services.wallets import (  # реэкспорт для обратной совместимости
    CASH_WALLET_TYPES,
    DDS_ARTICLE_TRANSFER_IN_CODE,
    DDS_ARTICLE_TRANSFER_OUT_CODE,
    SAFE_WALLET_CODE,
    CashWalletError,
    resolve_cash_wallet,
)

PAYOUT_REQUISITES_KEY = "payroll.bank_payout_requisites"
DEFAULT_PAYMENT_PURPOSE_TEMPLATE = "Выплата заработной платы за период {start}–{end}"
MOCK_PAYER_ACCOUNT = "00000000000000000000"
PAYROLL_BANK_DRAFT_STATUSES = frozenset({"created", "updated", "paid", "failed"})
# Pre-booking the payroll draft as a DDS expense lets the imported bank operation that
# settles it inherit this article automatically (production payroll → IP/owner card).
PAYROLL_PAYOUT_SOURCE_KIND = "payroll_payout"
PAYROLL_PAYOUT_ARTICLE_CODE = "zarplata_proizvodstvennogo_personala"
# Общие константы/резолвер кошельков вынесены в app.services.wallets (разрыв цикл-импорта
# deposit↔payroll). Реэкспортируем для обратной совместимости со внешними импортами.
# CASH_WALLET_TYPES / SAFE_WALLET_CODE / DDS_ARTICLE_TRANSFER_{IN,OUT}_CODE ← wallets.
# Внутренний перевод банк→Сейф под выплату (шаг 2 — при оплате черновика в банке):
# безналичная часть переводится с расчётного счёта в Сейф, откуда раздаётся по «Выплатить».
BANK_TO_SAFE_SOURCE_KIND = "payroll_bank_to_safe"
# Отложенная выдача депозита (этап 5): в «Выплатить» — отдельная корзина по своей статье ДДС
# (не смешивать с зарплатными статьями). «На руки» = ФОТ + выдача депозита; банк-черновик и
# наличный сплит считаются от этой суммы. iiko-изъятие «Выдача депозита» — для наличной части
# с ТК Черникова (= iiko Главная касса). Когда выдач нет (deposit_payout_scheduled=0) — инертно.
DDS_ARTICLE_DEPOSIT_PAYOUT = "vydacha_depozita_sotrudniku"
DEPOSIT_PAYOUT_TK_WALLET_CODE = "tk_chernikova"


@dataclass(frozen=True, slots=True)
class PayoutExpenseResult:
    """Итог проводок «Выплатить»: создан ли расход и наличная выдача депозита с ТК Черникова
    (для iiko-изъятия «Выдача депозита» после commit)."""

    booked: bool
    deposit_iiko_amount: Decimal


async def set_run_payout_cash(
    session: AsyncSession,
    run_id: uuid.UUID,
    *,
    amount_cash: Decimal,
    cash_wallet_code: str | None = None,
    actor_user_id: uuid.UUID | None,
) -> PayrollRun:
    """Set the run-level cash portion of the payroll and the cash wallet it is paid from.

    The remainder (total payable minus cash) is what goes to the IP account as a single
    bank draft. A non-zero cash part requires a cash wallet (Сейф / Торговая касса
    Черникова) — that wallet carries the cash-side DDS transactions of the payout.
    """
    run = await _get_payout_run(session, run_id)
    total = await _run_payout_grand_total(session, run_id)
    cash = _money(amount_cash)
    if cash < 0 or cash > total:
        raise PayrollConflictError("Наличная сумма должна быть от 0 до суммы к выплате")

    wallet: Wallet | None = None
    if cash > 0 and cash_wallet_code:
        wallet = await _resolve_cash_wallet(session, cash_wallet_code)
    elif cash > 0 and _uses_safe_payout(run):
        # Сейф-модель разносит наличную часть в ДДС (списание с наличного кошелька при
        # «Выплатить»), поэтому наличный счёт обязателен (Сейф / Торговая касса Черникова).
        raise PayrollConflictError(
            "Укажите наличный счёт (Сейф или Торговая касса Черникова)"
        )

    run.payout_cash_total = cash
    run.payout_cash_wallet_id = wallet.id if wallet is not None else None
    _add_payout_event(
        session,
        run=run,
        action="payout_cash_set",
        actor_user_id=actor_user_id,
        payload={
            "total_payable": money_text(total),
            "cash_total": money_text(cash),
            "account_total": money_text(total - cash),
            "cash_wallet_code": wallet.code if wallet is not None else None,
            "cash_wallet_name": wallet.name if wallet is not None else None,
        },
    )
    await session.commit()
    await session.refresh(run)
    return run


def _is_admin_run(run: PayrollRun) -> bool:
    """Административная (полумесячная) ведомость — помечена ``summary.kind == "admin"``."""
    return isinstance(run.summary, dict) and run.summary.get("kind") == "admin"


def _uses_safe_payout(run: PayrollRun) -> bool:
    """Все ведомости платятся через Сейф: банк→Сейф транзит + раздача с Сейфа (безнал)
    и с наличного кошелька (нал).

    Изначально через Сейф шла только админская ведомость; производственная унифицирована
    2026-06-21 — её прежний прямой prebook расхода ЗП на р/с заменён той же трёхшаговой
    Сейф-моделью (банковский direct-контур для неё фактически не использовался, грязных
    данных нет). Единый гейт вместо разрозненных ``_is_admin_run`` в payout-функциях.
    Для отката производственной на прежний прямой расход — вернуть ``_is_admin_run(run)``
    (тип ведомости различается там, где нужно, отдельно через ``_is_admin_run``).
    """
    return True


async def _resolve_cash_wallet(session: AsyncSession, code: str) -> Wallet:
    """Найти активный наличный кошелёк по коду (Сейф / Торговая касса Черникова).

    Делегирует в общий ``wallets.resolve_cash_wallet``; ошибку приводит к
    ``PayrollConflictError`` для единообразной обработки в payroll-роутах.
    """
    try:
        return await resolve_cash_wallet(session, code)
    except CashWalletError as error:
        raise PayrollConflictError(str(error)) from error


async def book_bank_to_safe_transfer(
    session: AsyncSession,
    run: PayrollRun,
    *,
    operation_date: date | None = None,
    provider: str = "tbank",
) -> bool:
    """Шаг 2: при оплате черновика завести внутренний перевод банк→Сейф на безналичную часть.

    ``provider`` (``tbank`` / ``sber``) задаёт банк-плательщика: банк-нога перевода садится на
    кошелёк расчётного счёта этого банка (для Сбера — Сбер-счёт).

    Две проводки с общим source (``payroll_bank_to_safe`` + run.id):
    - банк-нога (out, статья «Выбытие — Перевод между счетами») на расчётном счёте — служит
      prebooked-целью: исходящая операция из выписки сматчится с ней и унаследует статью
      перевода (баланс банка идёт от выписки, эта проводка его не двигает);
    - Сейф-нога (in, статья «Поступление — Перевод между счетами») — двигает баланс Сейфа,
      откуда деньги раздаются по «Выплатить».

    Идемпотентно: повторный вызов (дубль webhook / повторный polling) — no-op. Возвращает
    True, если перевод создан.

    Только для Сейф-модели (админская ведомость; производственная — после унификации).
    Вызывается из ``apply_payroll_draft_status`` безусловно, поэтому гейт здесь: для
    производственного (direct) прогона прямой prebook расхода ЗП уже создан в
    ``_upsert_payout_cashflow`` — второй транзит банк→Сейф дал бы две prebook-цели на одну
    банк-операцию (неверная статья + завышение баланса Сейфа).
    """
    if not _uses_safe_payout(run):
        return False
    existing = await session.scalar(
        select(CashflowTransaction.id).where(
            CashflowTransaction.source_kind == BANK_TO_SAFE_SOURCE_KIND,
            CashflowTransaction.source_id == run.id,
        )
    )
    if existing is not None:
        return False
    amount = await _run_account_amount(session, run)
    if amount <= 0:
        return False

    settings = get_settings()
    payer_account = payer_account_for(settings, provider)
    bank_wallet = await session.scalar(
        select(Wallet)
        .join(Account, Account.id == Wallet.account_id)
        .where(Account.account_number == payer_account, Wallet.status == "active")
    )
    safe_wallet = await session.scalar(
        select(Wallet).where(Wallet.code == SAFE_WALLET_CODE, Wallet.status == "active")
    )
    if bank_wallet is None or safe_wallet is None:
        return False

    transfer_out_article = await session.scalar(
        select(DdsArticle.id).where(DdsArticle.code == DDS_ARTICLE_TRANSFER_OUT_CODE)
    )
    transfer_in_article = await session.scalar(
        select(DdsArticle.id).where(DdsArticle.code == DDS_ARTICLE_TRANSFER_IN_CODE)
    )
    # Дата перевода = дата реальной банк-операции (приходит из вебхука «операция по счёту»),
    # а не момент обнаружения. Иначе при отложенном/сверочном поллинге перевод встаёт задним
    # числом «сегодня» и в журнале выглядит позже выплат с Сейфа. Фолбэк (поллинг без операции
    # под рукой) — текущая дата.
    operation_date = operation_date or datetime.now(UTC).date()
    purpose = "Перевод на Сейф под выплату ЗП"
    session.add(
        CashflowTransaction(
            wallet_id=bank_wallet.id,
            direction="out",
            amount=amount,
            operation_date=operation_date,
            article_id=transfer_out_article,
            source_kind=BANK_TO_SAFE_SOURCE_KIND,
            source_id=run.id,
            payment_purpose=purpose,
            quality_status="final",
        )
    )
    session.add(
        CashflowTransaction(
            wallet_id=safe_wallet.id,
            direction="in",
            amount=amount,
            operation_date=operation_date,
            article_id=transfer_in_article,
            source_kind=BANK_TO_SAFE_SOURCE_KIND,
            source_id=run.id,
            payment_purpose=purpose,
            quality_status="final",
        )
    )
    await session.flush()
    return True


async def apply_payroll_draft_status(
    session: AsyncSession,
    *,
    draft: PayrollBankDraft,
    raw_status: str | None,
    operation_date: date | None = None,
    commit: bool = True,
) -> str:
    """Продвинуть payroll-черновик по статусу банковского платежа (polling/webhook).

    При ``paid`` заводит внутренний перевод банк→Сейф (см. ``book_bank_to_safe_transfer``) —
    деньги приходят в Сейф, откуда раздаются по «Выплатить». ``operation_date`` — дата реальной
    банк-операции (из вебхука «операция по счёту»), которой датируется перевод; без неё (поллинг)
    берётся текущая дата. Идемпотентно: блокировка строки сериализует гонку webhook↔polling и
    дубль-доставку; переход только из created/updated.
    """
    outcome = classify_payment_status(raw_status)
    draft = await session.get(PayrollBankDraft, draft.id, with_for_update=True)
    if draft is None:
        return "created"

    if outcome == "paid" and draft.status in ("created", "updated"):
        run = await session.get(PayrollRun, draft.run_id)
        if run is not None:
            await book_bank_to_safe_transfer(
                session, run, operation_date=operation_date, provider=draft.bank_provider
            )
        draft.status = "paid"
        draft.synced_at = datetime.now(UTC)
    elif outcome == "failed" and draft.status in ("created", "updated"):
        draft.status = "failed"
        draft.last_error = f"Платёж отклонён банком: {raw_status}"[:500]
        draft.synced_at = datetime.now(UTC)

    if commit:
        await session.commit()
    return draft.status


def _distribute_amount(amount: Decimal, lines: list[PayrollLine]) -> list[tuple[str, Decimal]]:
    """Разнести сумму по строкам сотрудника пропорционально ``total_payable``.

    Остаток от округления отдаётся последней строке. Для однострочного (обычного) сотрудника —
    вся сумма на его единственную роль. Нужно для частичной выплаты двуролевого сотрудника.
    """
    if not lines:
        return []
    if len(lines) == 1:
        return [(lines[0].role, amount)]
    total = sum((_money(line.total_payable) for line in lines), Decimal("0"))
    if total <= 0:
        return [(lines[0].role, amount)]
    result: list[tuple[str, Decimal]] = []
    allocated = Decimal("0")
    last = len(lines) - 1
    for idx, line in enumerate(lines):
        if idx == last:
            share = amount - allocated
        else:
            share = (_money(line.total_payable) / total * amount).quantize(Decimal("0.01"))
            allocated += share
        if share > 0:
            result.append((line.role, share))
    return result


async def book_payout_expense_for_employees(
    session: AsyncSession,
    run: PayrollRun,
    employee_ids: list[uuid.UUID],
    *,
    amount_by_employee: dict[uuid.UUID, Decimal] | None = None,
) -> PayoutExpenseResult:
    """Шаг 3: расход ЗП + выдача депозита по статьям для выплаченных сотрудников («Выплатить»).

    Для Сейф-модели (админ + производственная после унификации). Суммы по строкам
    указанных сотрудников разносятся по статьям ДДС: должность сама маппится на статью
    (повар/кассир → «Зарплата производственного персонала», вспом. → «Содержание торг.
    точек», админ → «Зарплата административного персонала»). Запланированная выдача депозита
    (``deposit_payout_scheduled``) идёт ОТДЕЛЬНОЙ корзиной по статье «Выдача депозита» (не
    под зарплатной статьёй). Безналичная часть списывается с Сейфа (куда пришёл перевод),
    наличная — с наличного кошелька прогона (ТК Черникова / Сейф). Наличный бюджет
    каскадируется на уровне прогона: уже списанная наличными часть вычитается, поэтому
    инкрементальные «Выплатить» не задваивают наличное распределение.

    Возвращает ``PayoutExpenseResult`` с наличной частью выдачи депозита с ТК Черникова —
    для iiko-изъятия «Выдача депозита» (после commit). Когда выдач депозита нет, корзина
    не создаётся и поведение совпадает с прежним (только ЗП).

    ``amount_by_employee`` — целевая ВЫПЛАЧЕННАЯ сумма по сотруднику (бегущий итог для
    частичной выплаты). Книжится только дельта ``target − booked_amount`` (инкрементально, без
    задвоения при partial→bulk). Когда не задан — полная выплата по ``total_payable`` (прежнее
    поведение). В режиме частичной выплаты корзина «Выдача депозита» не заводится — депозит
    идёт полным путём «Выплатить».
    """
    empty = PayoutExpenseResult(booked=False, deposit_iiko_amount=Decimal("0"))
    if not _uses_safe_payout(run) or not employee_ids:
        return empty
    lines = (
        await session.scalars(
            select(PayrollLine).where(
                PayrollLine.run_id == run.id,
                PayrollLine.employee_id.in_(employee_ids),
            )
        )
    ).all()
    lines_by_employee: dict[uuid.UUID, list[PayrollLine]] = defaultdict(list)
    for line in lines:
        lines_by_employee[line.employee_id].append(line)
    payments = {
        payment.employee_id: payment
        for payment in (
            await session.scalars(
                select(PayrollPayment).where(
                    PayrollPayment.run_id == run.id,
                    PayrollPayment.employee_id.in_(employee_ids),
                )
            )
        ).all()
    }
    default_article = (
        DDS_ARTICLE_ADMIN_PAYROLL if _is_admin_run(run) else DDS_ARTICLE_PRODUCTION_PAYROLL
    )
    # Книжим только НЕ забронированную часть по каждому сотруднику (delta = target − booked).
    row_amounts: list[tuple[str, Decimal]] = []
    booked_targets: dict[uuid.UUID, Decimal] = {}
    for employee_id, employee_lines in lines_by_employee.items():
        accrued = sum((_money(line.total_payable) for line in employee_lines), Decimal("0"))
        if amount_by_employee is not None and employee_id in amount_by_employee:
            target = min(_money(amount_by_employee[employee_id]), accrued)
        else:
            target = accrued
        payment = payments.get(employee_id)
        already_booked = _money(payment.booked_amount) if payment is not None else Decimal("0")
        delta = target - already_booked
        if delta <= 0:
            continue
        row_amounts.extend(_distribute_amount(delta, employee_lines))
        booked_targets[employee_id] = target
    buckets = build_payout_buckets(row_amounts, default_article_code=default_article)
    # Выдача депозита — отдельной корзиной В КОНЦЕ (наличные гасят сначала ЗП, потом выдачу).
    # Только при полной выплате: в режиме частичной выплаты депозит не трогаем.
    if amount_by_employee is None:
        deposit_total = sum(
            (_money(getattr(line, "deposit_payout_scheduled", 0)) for line in lines), Decimal("0")
        )
        if deposit_total > 0:
            buckets = [*buckets, PayoutBucket(DDS_ARTICLE_DEPOSIT_PAYOUT, deposit_total)]
    if not buckets:
        return empty
    safe_wallet = await session.scalar(
        select(Wallet).where(Wallet.code == SAFE_WALLET_CODE, Wallet.status == "active")
    )
    if safe_wallet is None:
        return empty

    # Наличный бюджет прогона за вычетом уже списанного наличными (cash-проводки = не на Сейфе).
    cash_wallet: Wallet | None = None
    if run.payout_cash_wallet_id is not None:
        cash_wallet = await session.get(Wallet, run.payout_cash_wallet_id)
    already_cash = Decimal("0")
    if cash_wallet is not None and cash_wallet.id != safe_wallet.id:
        already_cash = _money(
            await session.scalar(
                select(func.coalesce(func.sum(CashflowTransaction.amount), 0)).where(
                    CashflowTransaction.source_kind == PAYROLL_PAYOUT_SOURCE_KIND,
                    CashflowTransaction.source_id == run.id,
                    CashflowTransaction.direction == "out",
                    CashflowTransaction.wallet_id == cash_wallet.id,
                )
            )
        )
    rows_total = sum((bucket.total for bucket in buckets), Decimal("0"))
    cash_budget = max(Decimal("0"), _money(run.payout_cash_total) - already_cash)
    cash_for_rows = min(cash_budget, rows_total)
    allocations = allocate_cash_cascade(buckets, cash_for_rows)

    codes = {bucket.article_code for bucket in buckets}
    article_rows = (
        await session.execute(
            select(DdsArticle.code, DdsArticle.id).where(DdsArticle.code.in_(codes))
        )
    ).all()
    article_ids = {code: article_id for code, article_id in article_rows}
    operation_date = datetime.now(UTC).date()
    # Если наличный кошелёк не задан, наличную часть тоже списываем с Сейфа (фолбэк).
    cash_target = cash_wallet.id if cash_wallet is not None else safe_wallet.id
    deposit_iiko_amount = Decimal("0")
    for alloc in allocations:
        article_id = article_ids.get(alloc.article_code)
        is_deposit = alloc.article_code == DDS_ARTICLE_DEPOSIT_PAYOUT
        purpose = "Выдача депозита (из Сейфа)" if is_deposit else "Выплата ЗП (из Сейфа)"
        if alloc.bank > 0:
            session.add(
                CashflowTransaction(
                    wallet_id=safe_wallet.id,
                    direction="out",
                    amount=alloc.bank,
                    operation_date=operation_date,
                    article_id=article_id,
                    source_kind=PAYROLL_PAYOUT_SOURCE_KIND,
                    source_id=run.id,
                    payment_purpose=purpose,
                    quality_status="final",
                )
            )
        if alloc.cash > 0:
            session.add(
                CashflowTransaction(
                    wallet_id=cash_target,
                    direction="out",
                    amount=alloc.cash,
                    operation_date=operation_date,
                    article_id=article_id,
                    source_kind=PAYROLL_PAYOUT_SOURCE_KIND,
                    source_id=run.id,
                    payment_purpose=purpose,
                    quality_status="final",
                )
            )
            # iiko-изъятие «Выдача депозита» — только наличная часть выдачи с ТК Черникова.
            if is_deposit and cash_wallet is not None and (
                cash_wallet.code == DEPOSIT_PAYOUT_TK_WALLET_CODE
            ):
                deposit_iiko_amount += alloc.cash
    await session.flush()
    # Отмечаем забронированную сумму по каждому проведённому сотруднику (защита от задвоения).
    for employee_id, target in booked_targets.items():
        payment = payments.get(employee_id)
        if payment is not None:
            payment.booked_amount = target
    return PayoutExpenseResult(
        booked=True, deposit_iiko_amount=_money(deposit_iiko_amount)
    )


async def create_or_update_run_draft(
    session: AsyncSession,
    run_id: uuid.UUID,
    *,
    actor_user_id: uuid.UUID | None,
    bank_client: BankClient | None = None,
    provider: str = "tbank",
) -> PayrollBankDraft | None:
    run = await _get_payout_run(session, run_id)
    period = await _get_run_period(session, run)
    requisites = await _bank_payout_requisites(session)
    settings = get_settings()
    payer_account = payer_account_for(settings, provider)
    total_account = await _run_account_amount(session, run)
    if total_account <= 0:
        if _uses_safe_payout(run):
            # Вся выплата наличными: банковского черновика нет. Проводки ДДС в Сейф-модели
            # создаются по «Выплатить» (расход с наличного кошелька), не на черновике.
            return await _get_bank_draft(session, run_id)
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
    client = bank_client or payout_client_for(provider, session)
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
            bank_provider=provider,
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
        bank_provider=provider,
    )
    await _upsert_payout_cashflow(session, run, total_account, datetime.now(UTC).date())
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
    run = await _get_payout_run(session, run_id)
    draft = await _get_bank_draft(session, run_id)
    previous_amount = _money(draft.amount) if draft is not None else Decimal("0.00")
    new_amount = await _run_account_amount(session, run)
    delta = new_amount - previous_amount
    return {
        "run_id": run_id,
        "document_id": draft.document_id if draft is not None else None,
        "previous_amount": previous_amount,
        "new_amount": new_amount,
        "delta": delta,
        "classification": _delta_classification(delta),
    }


async def get_run_payout_allocation(session: AsyncSession, run_id: uuid.UUID) -> dict[str, Any]:
    """Превью разнесения выплаты по статьям ДДС при текущем сплите наличные/банк.

    Каскад наличных тот же, что при создании проводок (``_upsert_admin_payout_cashflows``):
    наличные гасят вспомогательную корзину раньше администрации, банк добивает остаток.
    """
    run = await _get_payout_run(session, run_id)
    payable = await _run_total_payable(session, run_id)
    deposit_total = await _run_deposit_payout_total(session, run_id)
    grand_total = _money(payable + deposit_total)
    cash = min(_money(run.payout_cash_total), grand_total)
    lines = (
        await session.scalars(select(PayrollLine).where(PayrollLine.run_id == run_id))
    ).all()
    default_article = (
        DDS_ARTICLE_ADMIN_PAYROLL if _is_admin_run(run) else DDS_ARTICLE_PRODUCTION_PAYROLL
    )
    buckets = build_payout_buckets(
        [(line.role, _money(line.total_payable)) for line in lines],
        default_article_code=default_article,
    )
    # Выдача депозита — отдельной корзиной в конце (как в book_payout_expense_for_employees).
    if deposit_total > 0:
        buckets = [*buckets, PayoutBucket(DDS_ARTICLE_DEPOSIT_PAYOUT, deposit_total)]
    allocations = allocate_cash_cascade(buckets, cash)

    names: dict[str, str] = {}
    codes = [alloc.article_code for alloc in allocations]
    if codes:
        rows = (
            await session.execute(
                select(DdsArticle.code, DdsArticle.name).where(DdsArticle.code.in_(codes))
            )
        ).all()
        names = {code: name for code, name in rows}

    return {
        "run_id": run_id,
        "total_payable": payable,
        "deposit_payout_total": deposit_total,
        "grand_total": grand_total,
        "cash_total": cash,
        "bank_total": _money(grand_total - cash),
        "cash_wallet_id": run.payout_cash_wallet_id,
        "buckets": [
            {
                "article_code": alloc.article_code,
                "article_name": names.get(alloc.article_code, alloc.article_code),
                "total": alloc.total,
                "cash": alloc.cash,
                "bank": alloc.bank,
            }
            for alloc in allocations
        ],
    }


async def list_cash_wallets(session: AsyncSession) -> list[Wallet]:
    """Активные наличные кошельки для выбора при сплите (Сейф, Торговая касса Черникова)."""
    return list(
        (
            await session.scalars(
                select(Wallet)
                .where(Wallet.type.in_(CASH_WALLET_TYPES), Wallet.status == "active")
                .order_by(Wallet.name)
            )
        ).all()
    )


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

    new_amount = await _run_account_amount(session, run)
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

    await _upsert_payout_cashflow(session, run, new_amount, datetime.now(UTC).date())
    await session.commit()
    return 1


async def create_or_update_drafts(
    session: AsyncSession,
    run_id: uuid.UUID,
    *,
    actor_user_id: uuid.UUID | None,
    bank_client: BankClient | None = None,
    provider: str = "tbank",
) -> int:
    await create_or_update_run_draft(
        session,
        run_id,
        actor_user_id=actor_user_id,
        bank_client=bank_client,
        provider=provider,
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
    payer_account = payer_account_for(settings, draft.bank_provider)
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

    client = bank_client or payout_client_for(draft.bank_provider, session)
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


async def _run_total_payable(session: AsyncSession, run_id: uuid.UUID) -> Decimal:
    amount = await session.scalar(
        select(func.coalesce(func.sum(PayrollLine.total_payable), 0)).where(
            PayrollLine.run_id == run_id
        )
    )
    return _money(amount)


async def _run_deposit_payout_total(session: AsyncSession, run_id: uuid.UUID) -> Decimal:
    """Сумма запланированной выдачи депозита по ведомости (столбец «Выдача депозита»).

    0 для всех обычных ведомостей (deposit_payout_scheduled заполняется только при
    включённой отложенной выдаче) — тогда выплатной поток ниже работает как раньше.
    """
    amount = await session.scalar(
        select(func.coalesce(func.sum(PayrollLine.deposit_payout_scheduled), 0)).where(
            PayrollLine.run_id == run_id
        )
    )
    return _money(amount)


async def _run_payout_grand_total(session: AsyncSession, run_id: uuid.UUID) -> Decimal:
    """«На руки» = ФОТ (total_payable) + выдача депозита. База банк-черновика и наличного сплита."""
    payable = await _run_total_payable(session, run_id)
    deposit = await _run_deposit_payout_total(session, run_id)
    return _money(payable + deposit)


async def _run_account_amount(session: AsyncSession, run: PayrollRun) -> Decimal:
    """РС-часть ведомости = «на руки» (ФОТ + выдача депозита) − наличные (run-level), ≥ 0.

    Наличная сумма ограничивается суммой «на руки»: если после пересчёта она упала ниже
    ранее заданной наличной суммы, на счёт ИП ничего не уходит.
    """
    total = await _run_payout_grand_total(session, run.id)
    cash = min(_money(run.payout_cash_total), total)
    return _money(total - cash)


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


async def _upsert_payout_cashflow(
    session: AsyncSession,
    run: PayrollRun,
    amount: Decimal,
    operation_date: date,
) -> None:
    """Pre-book the payroll payout as DDS expense(s) so the imported bank statement settles it.

    Mirrors the supplier-invoice prebooked flow: when the bank statement later imports the
    matching outgoing payment, ``classifier._find_prebooked_payment`` links it to this row,
    so the operation inherits the payroll article without manual review. Balance is derived
    from the statement (see ``dds._wallet_movement_deltas``), so these rows only carry the
    article — they never move the wallet balance. No-op if the payer wallet isn't provisioned.

    Производственная ведомость — одна проводка «Зарплата производственного персонала».
    Административная ведомость разносится на несколько проводок по статьям ДДС (банковская
    часть на счёте ИП + наличная часть на выбранном наличном кошельке).
    """
    settings = get_settings()
    payer_account = _payer_account(settings)
    wallet = await session.scalar(
        select(Wallet)
        .join(Account, Account.id == Wallet.account_id)
        .where(Account.account_number == payer_account, Wallet.status == "active")
    )
    if wallet is None:
        return
    period = await _get_run_period(session, run)
    requisites = await _bank_payout_requisites(session)
    purpose = _payment_purpose(requisites, run_id=run.id, period=period)

    if _uses_safe_payout(run):
        # Сейф-модель (админ + производственная): проводки ДДС создаются по «Выплатить»
        # (расход из Сейфа/кассы) и при подтверждении платежа (перевод банк→Сейф), а НЕ на
        # черновике — поэтому прямой prebook расхода ЗП здесь не нужен. Ветка ниже остаётся
        # для отката производственной на прежний direct-контур (вернуть _uses_safe_payout).
        return

    article_id = await session.scalar(
        select(DdsArticle.id).where(DdsArticle.code == PAYROLL_PAYOUT_ARTICLE_CODE)
    )
    existing = await session.scalar(
        select(CashflowTransaction).where(
            CashflowTransaction.source_kind == PAYROLL_PAYOUT_SOURCE_KIND,
            CashflowTransaction.source_id == run.id,
        )
    )
    if existing is not None:
        settled = await session.scalar(
            select(BankOperation.id).where(BankOperation.cashflow_transaction_id == existing.id)
        )
        if settled is not None:
            return
        existing.wallet_id = wallet.id
        existing.amount = _money(amount)
        existing.operation_date = operation_date
        existing.article_id = article_id
        existing.payment_purpose = purpose
    else:
        session.add(
            CashflowTransaction(
                wallet_id=wallet.id,
                direction="out",
                amount=_money(amount),
                operation_date=operation_date,
                article_id=article_id,
                source_kind=PAYROLL_PAYOUT_SOURCE_KIND,
                source_id=run.id,
                payment_purpose=purpose,
                quality_status="final",
            )
        )
    await session.flush()


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
    bank_provider: str = "tbank",
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
    draft.bank_provider = bank_provider
    draft.synced_at = datetime.now(UTC)
    await session.flush()
    return draft


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
