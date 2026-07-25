"""Разовые выплаты сотрудникам (леджер ``EmployeePayout``) — общий контур.

Единый сервис создания выплат: используется и «Включить в выплату» в ведомости (гашение
долга ЗП собственника, режим оклада ``on_demand``), и плавающей кнопкой «Создать выплату
сотруднику». Наличная/сейфовая выплата проводится сразу прямой out-проводкой ДДС; банковская
(T-Bank/ИП) — отдельным контуром с черновиком и транзитом на Сейф (Трек B, добавляется позже).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import (
    Account,
    BankOperation,
    CashflowTransaction,
    DdsArticle,
    Employee,
    EmployeePayout,
    SafeAllocation,
    Wallet,
)
from app.services.bank_payment_status import classify_payment_status
from app.services.banking import BankClient, TbankClient
from app.services.banking.classifier import (
    SAFE_WALLET_CODE,
    TRANSFER_IN_ARTICLE_CODE,
    TRANSFER_OUT_ARTICLE_CODE,
)
from app.services.banking.exceptions import BankFetchError
from app.services.banking.tbank import build_payment_draft_api_payload
from app.services.payroll_calculator import decimal
from app.services.payroll_payouts import _bank_payout_requisites, _payer_account
from app.services.payroll_runner import PayrollConflictError, PayrollNotFoundError

_CENTS = Decimal("0.01")

# Денежный факт выплаты (out-проводка) помечается этим source_kind, source_id = payout.id.
EMPLOYEE_PAYOUT_SOURCE_KIND = "employee_payout"
# Статья ДДС по умолчанию для выплаты ЗП собственника.
DEFAULT_PAYOUT_ARTICLE_CODE = "zarplata_administrativnogo_personala"
EMPLOYEE_PAYOUT_KIND_OWNER_SALARY = "owner_salary"
# owner_salary — гашение долга ЗП собственника (on_demand); salary — разовая ЗП; other — прочее.
ALLOWED_PAYOUT_KINDS = (EMPLOYEE_PAYOUT_KIND_OWNER_SALARY, "salary", "other")

# Наличные/подотчётные счета: баланс из журнала → прямая проводка выплаты допустима.
# Банковские счета (type bank/bank_account) — только через черновик+транзит на Сейф (Трек B),
# иначе прямая проводка исказит баланс банка, который ведётся от выписки.
CASH_PAYOUT_WALLET_TYPES = (
    "store_cash",
    "cash_safe",
    "cash",
    "cash_register",
    "reserve",
)
# Банковские счета — выплата через черновик Т-Банк + транзит на Сейф (Track B2).
BANK_PAYOUT_WALLET_TYPES = ("bank", "bank_account")
# Транзит банк→Сейф под банковскую выплату; банк-нога prebooked (см. classifier).
EMPLOYEE_PAYOUT_BANK_TO_SAFE_SOURCE_KIND = "employee_payout_bank_to_safe"


async def create_cash_employee_payout(
    session: AsyncSession,
    *,
    employee_id: uuid.UUID,
    amount,
    wallet_id: uuid.UUID,
    payout_date: date,
    kind: str = EMPLOYEE_PAYOUT_KIND_OWNER_SALARY,
    article_id: uuid.UUID | None = None,
    article_code: str = DEFAULT_PAYOUT_ARTICLE_CODE,
    note: str | None = None,
    created_by_user_id: uuid.UUID | None = None,
) -> EmployeePayout:
    """Разовая выплата сотруднику наличными/с подотчётного счёта (прямая out-проводка).

    Деньги уходят сразу (``status='paid'``): создаётся ``EmployeePayout`` и out-
    ``CashflowTransaction`` на выбранном наличном/сейфовом кошельке со статьёй ДДС,
    связанные через ``cashflow_transaction_id``. Статья: явный ``article_id`` (выбор в
    диалоге), иначе разрешается по ``article_code`` (по умолчанию «Зарплата собственника»).
    Банковские счета отклоняются (нужен контур черновик+Сейф из Трека B).

    Возвращает созданный ``EmployeePayout``. Не коммитит — коммит на вызывающем.
    """
    amount = decimal(amount).quantize(_CENTS)
    if amount <= 0:
        raise PayrollConflictError("Сумма выплаты должна быть больше нуля")
    if kind not in ALLOWED_PAYOUT_KINDS:
        raise PayrollConflictError(f"Недопустимый тип выплаты: {kind}")

    employee = await session.get(Employee, employee_id)
    if employee is None:
        raise PayrollNotFoundError("Сотрудник не найден")

    wallet = await session.get(Wallet, wallet_id)
    if wallet is None:
        raise PayrollNotFoundError("Счёт не найден")
    if wallet.status != "active":
        raise PayrollConflictError("Счёт неактивен")
    if wallet.type not in CASH_PAYOUT_WALLET_TYPES:
        raise PayrollConflictError(
            "С банковского счёта выплата проводится через черновик и Сейф — "
            "используйте окно «Новый платёж» из плавающей кнопки"
        )

    if article_id is not None:
        resolved_article_id = await session.scalar(
            select(DdsArticle.id).where(DdsArticle.id == article_id)
        )
        if resolved_article_id is None:
            raise PayrollNotFoundError("Статья ДДС не найдена")
    else:
        resolved_article_id = await session.scalar(
            select(DdsArticle.id).where(DdsArticle.code == article_code)
        )
    article_id = resolved_article_id

    payout = EmployeePayout(
        id=uuid.uuid4(),
        employee_id=employee_id,
        kind=kind,
        amount=amount,
        payout_date=payout_date,
        wallet_id=wallet.id,
        article_id=article_id,
        status="paid",
        note=(note.strip() if isinstance(note, str) and note.strip() else None),
        created_by_user_id=created_by_user_id,
    )
    session.add(payout)
    await session.flush()

    transaction = CashflowTransaction(
        wallet_id=wallet.id,
        direction="out",
        amount=amount,
        operation_date=payout_date,
        article_id=article_id,
        source_kind=EMPLOYEE_PAYOUT_SOURCE_KIND,
        source_id=payout.id,
        payment_purpose=f"Выплата сотруднику — {employee.full_name}",
        quality_status="final",
    )
    session.add(transaction)
    await session.flush()

    payout.cashflow_transaction_id = transaction.id
    await session.flush()
    return payout


def _employee_payout_document_id(payout_id: uuid.UUID) -> str:
    return f"teplo-emppayout-{payout_id}"


async def create_bank_employee_payout(
    session: AsyncSession,
    *,
    employee_id: uuid.UUID,
    amount,
    wallet_id: uuid.UUID,
    payout_date: date,
    kind: str = EMPLOYEE_PAYOUT_KIND_OWNER_SALARY,
    article_id: uuid.UUID | None = None,
    article_code: str = DEFAULT_PAYOUT_ARTICLE_CODE,
    note: str | None = None,
    created_by_user_id: uuid.UUID | None = None,
    bank_client: BankClient | None = None,
) -> EmployeePayout:
    """Банковская выплата сотруднику: черновик Т-Банк по реквизитам ИП (``status='pending'``).

    Деньги мгновенно НЕ уходят: заводится платёжный черновик в Т-Банке (реквизиты получателя —
    общие ``payroll.bank_payout_requisites``, плательщик — расчётный счёт ИП). Подтверждение и
    транзит банк→Сейф с резервом выполняются при привязке к реальной операции выписки
    (``confirm_employee_payout_by_operation``). При сетевой/банк-ошибке черновик сохраняется со
    статусом ``failed`` (выплату не валим — можно повторить/отменить).

    Возвращает созданный ``EmployeePayout``. Не коммитит — коммит на вызывающем.
    """
    amount = decimal(amount).quantize(_CENTS)
    if amount <= 0:
        raise PayrollConflictError("Сумма выплаты должна быть больше нуля")
    if kind not in ALLOWED_PAYOUT_KINDS:
        raise PayrollConflictError(f"Недопустимый тип выплаты: {kind}")

    employee = await session.get(Employee, employee_id)
    if employee is None:
        raise PayrollNotFoundError("Сотрудник не найден")

    wallet = await session.get(Wallet, wallet_id)
    if wallet is None:
        raise PayrollNotFoundError("Счёт не найден")
    if wallet.status != "active":
        raise PayrollConflictError("Счёт неактивен")
    if wallet.type not in BANK_PAYOUT_WALLET_TYPES:
        raise PayrollConflictError(
            "Для наличной/сейфовой выплаты используйте прямую выплату — банковский контур "
            "только для банковских счетов"
        )

    if article_id is not None:
        resolved_article_id = await session.scalar(
            select(DdsArticle.id).where(DdsArticle.id == article_id)
        )
        if resolved_article_id is None:
            raise PayrollNotFoundError("Статья ДДС не найдена")
    else:
        resolved_article_id = await session.scalar(
            select(DdsArticle.id).where(DdsArticle.code == article_code)
        )

    payout = EmployeePayout(
        id=uuid.uuid4(),
        employee_id=employee_id,
        kind=kind,
        amount=amount,
        payout_date=payout_date,
        wallet_id=wallet.id,
        article_id=resolved_article_id,
        status="pending",
        note=(note.strip() if isinstance(note, str) and note.strip() else None),
        created_by_user_id=created_by_user_id,
    )
    session.add(payout)
    await session.flush()

    # Черновик платежа умеет создавать только Т-Банк. Для прочих банков (Сбербанк) черновик
    # не заводим — выплата остаётся ``pending`` и подтверждается привязкой к операции выписки.
    account = await session.get(Account, wallet.account_id) if wallet.account_id else None
    if account is None or account.bank_code != "tbank":
        return payout

    settings = get_settings()
    payer_account = _payer_account(settings)
    requisites = await _bank_payout_requisites(session)
    document_id = _employee_payout_document_id(payout.id)
    purpose = f"Выплата сотруднику — {employee.full_name}"
    try:
        api_payload = build_payment_draft_api_payload(
            document_id=document_id,
            amount=amount,
            purpose=purpose,
            requisites=requisites,
            payer_account=payer_account,
        )
    except ValueError as exc:
        raise PayrollConflictError(str(exc)) from exc

    stored_payload = {"accountNumber": payer_account, "request": api_payload}
    client = bank_client or TbankClient(session)
    payout.document_id = document_id[:64]
    payout.payload = stored_payload
    payout.synced_at = datetime.now(UTC)
    try:
        result = await client.create_payment_draft(
            document_id=document_id,
            amount=amount,
            purpose=purpose,
            requisites=dict(requisites),
            payer_account=payer_account,
        )
    except BankFetchError as exc:
        payout.status = "failed"
        payout.last_error = str(exc)[:500]
        await session.flush()
        return payout

    payout.provider_ref = result.provider_ref
    payout.last_error = None
    await session.flush()
    return payout


async def _book_employee_payout_transit_and_reserve(
    session: AsyncSession,
    *,
    payout: EmployeePayout,
    operation_date: date | None = None,
) -> bool:
    """Транзит банк→Сейф на сумму выплаты + резерв Сейфа со статьёй выплаты.

    Зеркало ``_book_advance_transit_and_reserve``: банк-нога out (статья «перевод между
    счетами») — prebooked-цель для приходящей операции выписки (баланс банка от выписки);
    Сейф-нога in двигает баланс Сейфа; затем ``SafeAllocation(reserved)`` под выплату,
    гасится по «Выплачено». Идемпотентно по ``safe_allocation_id`` и по source. Возвращает
    ``True``, если транзит+резерв заведены (или уже были); ``False`` — если кошельки не заведены.
    """
    if payout.safe_allocation_id is not None:
        return True
    existing = await session.scalar(
        select(CashflowTransaction.id).where(
            CashflowTransaction.source_kind == EMPLOYEE_PAYOUT_BANK_TO_SAFE_SOURCE_KIND,
            CashflowTransaction.source_id == payout.id,
        )
    )
    if existing is not None:
        return True

    amount = decimal(payout.amount).quantize(_CENTS)
    settings = get_settings()
    payer_account = _payer_account(settings)
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
        select(DdsArticle.id).where(DdsArticle.code == TRANSFER_OUT_ARTICLE_CODE)
    )
    transfer_in_article = await session.scalar(
        select(DdsArticle.id).where(DdsArticle.code == TRANSFER_IN_ARTICLE_CODE)
    )
    operation_date = operation_date or payout.payout_date
    employee_name = await session.scalar(
        select(Employee.full_name).where(Employee.id == payout.employee_id)
    )
    transit_purpose = f"Перевод на Сейф под выплату — {employee_name or 'сотрудник'}"
    session.add(
        CashflowTransaction(
            wallet_id=bank_wallet.id,
            direction="out",
            amount=amount,
            operation_date=operation_date,
            article_id=transfer_out_article,
            source_kind=EMPLOYEE_PAYOUT_BANK_TO_SAFE_SOURCE_KIND,
            source_id=payout.id,
            payment_purpose=transit_purpose,
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
            source_kind=EMPLOYEE_PAYOUT_BANK_TO_SAFE_SOURCE_KIND,
            source_id=payout.id,
            payment_purpose=transit_purpose,
            quality_status="final",
        )
    )
    await session.flush()

    allocation = SafeAllocation(
        wallet_id=safe_wallet.id,
        amount=amount,
        amount_paid=Decimal("0"),
        article_id=payout.article_id,
        counterparty_id=None,
        purpose=f"Выплата сотруднику — {employee_name or 'сотрудник'}",
        status="reserved",
        created_by_user_id=payout.created_by_user_id,
    )
    session.add(allocation)
    await session.flush()
    payout.safe_allocation_id = allocation.id
    return True


async def confirm_employee_payout_by_operation(
    session: AsyncSession,
    *,
    payout_id: uuid.UUID,
    bank_operation_id: uuid.UUID,
) -> EmployeePayout:
    """Подтвердить банковскую выплату привязкой к реальной операции выписки.

    Привязывает исходящую операцию банка к выплате (``bank_operation_id``), заводит транзит
    банк→Сейф + резерв на дату операции и переводит выплату в ``paid``. Идемпотентно: повторная
    привязка той же операции не двоит транзит (защита в ``_book…transit``). Не коммитит.
    """
    payout = await session.get(EmployeePayout, payout_id)
    if payout is None:
        raise PayrollNotFoundError("Выплата не найдена")
    if payout.status == "paid" and payout.bank_operation_id is not None:
        return payout
    if payout.status not in ("pending", "failed"):
        raise PayrollConflictError("Выплату нельзя подтвердить в текущем статусе")

    operation = await session.get(BankOperation, bank_operation_id)
    if operation is None:
        raise PayrollNotFoundError("Банковская операция не найдена")
    if operation.direction != "out":
        raise PayrollConflictError("Привязать можно только исходящую операцию")

    booked = await _book_employee_payout_transit_and_reserve(
        session, payout=payout, operation_date=operation.operation_date
    )
    if not booked:
        raise PayrollConflictError(
            "Не заведены кошельки банк/Сейф — транзит под выплату создать нельзя"
        )

    payout.bank_operation_id = operation.id
    payout.status = "paid"
    payout.synced_at = datetime.now(UTC)
    await session.flush()
    return payout


async def apply_employee_payout_status(
    session: AsyncSession,
    *,
    payout: EmployeePayout,
    raw_status: str | None,
    operation_date: date | None = None,
    commit: bool = True,
) -> str:
    """Продвинуть банковскую выплату сотруднику по статусу платёжного документа.

    Зеркало ``apply_payroll_draft_status``: вебхук/поллинг доводят выплату до ``paid``, заводя
    транзит р/с→Сейф и резерв Сейфа (см. ``_book_employee_payout_transit_and_reserve``) — дальше
    деньги выдаются по «Выплатить» с резерва. Ручное подтверждение операцией выписки
    (``confirm_employee_payout_by_operation``) остаётся запасным путём для Сбера и пропущенных
    вебхуков; оба идемпотентны по ``safe_allocation_id``/``source_kind``.

    Кошельки банк/Сейф не заведены → выплата ОСТАЁТСЯ ``pending`` с ``last_error``: молчаливый
    переход в ``paid`` без резерва спрятал бы деньги (обязательство перед сотрудником исчезло
    бы из витрины). Следующий опрос повторит попытку.
    """
    outcome = classify_payment_status(raw_status)
    payout = await session.get(EmployeePayout, payout.id, with_for_update=True)
    if payout is None:
        return "pending"

    if outcome == "paid" and payout.status in ("pending", "failed"):
        booked = await _book_employee_payout_transit_and_reserve(
            session, payout=payout, operation_date=operation_date
        )
        if booked:
            payout.status = "paid"
            payout.last_error = None
        else:
            payout.last_error = (
                "Не заведены кошельки банк/Сейф — транзит под выплату создать нельзя"
            )
        payout.synced_at = datetime.now(UTC)
    elif outcome in ("failed", "deleted") and payout.status == "pending":
        payout.status = "failed"
        payout.last_error = (
            "Черновик удалён в банке"
            if outcome == "deleted"
            else f"Платёж отклонён банком: {raw_status}"
        )[:500]
        payout.synced_at = datetime.now(UTC)

    if commit:
        await session.commit()
    return payout.status
