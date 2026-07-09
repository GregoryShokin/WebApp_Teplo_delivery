"""Выдача и ведение авансов/займов сотрудников (write-путь).

Выдача классифицируется по доступному (earned-to-date из
`payroll_advance_availability`): сумма ≤ доступного → аванс (право A, гасится в
ближайшей ведомости разом); сумма > доступного → заём (право B, гасится в
рассрочку N равных долей). `allow_loan` отражает наличие права B у инициатора —
без него выдача сверх заработанного отклоняется.

MVP-упрощение: одна выдача = одна запись `SalaryAdvance` целиком (без расщепления
суммы на аванс-часть + заём-часть). Если сумма превышает доступное, ВСЯ сумма
оформляется займом с рассрочкой.

Запись `SalaryAdvance` — источник истины и факт выплаты (нал/перевод) для ДДС.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import (
    Account,
    AppSetting,
    BankOperation,
    CashflowTransaction,
    DdsArticle,
    Employee,
    PayrollLine,
    PayrollPayment,
    PayrollPeriod,
    PayrollRun,
    PayrollRunEvent,
    SafeAllocation,
    SalaryAdvance,
    SalaryAdvanceBankDraft,
    SalaryAdvanceRecovery,
    SalaryAdvanceRecoveryOverride,
    Wallet,
)
from app.services.advance_iiko_payout import post_advance_payout_to_iiko
from app.services.bank_payment_status import classify_payment_status
from app.services.banking import BankClient, TbankClient
from app.services.banking.payout import payer_account_for, payout_client_for
from app.services.banking.classifier import (
    SAFE_WALLET_CODE,
    TRANSFER_IN_ARTICLE_CODE,
    TRANSFER_OUT_ARTICLE_CODE,
)
from app.services.banking.exceptions import BankFetchError
from app.services.banking.safe_allocations import cancel_allocation, pay_allocation
from app.services.banking.tbank import build_payment_draft_api_payload
from app.services.employee_effective_events import get_position_on_date
from app.services.payroll_admin import _upsert_setting
from app.services.payroll_advance_availability import available_to_advance
from app.services.payroll_calculator import decimal, money
from app.services.payroll_payouts import _bank_payout_requisites, _payer_account
from app.services.payroll_runner import PayrollConflictError, PayrollNotFoundError
from app.services.position_registry import admin_payroll_positions

_CENTS = Decimal("0.01")
PAYOUT_METHODS = ("business_card", "cash", "transfer", "other", "payroll")

# Проводка ДДС при выдаче аванса/займа: статья по типу, источник по выбранному кошельку.
ADVANCE_PAYOUT_SOURCE_KIND = "salary_advance"
ADVANCE_ARTICLE_CODE = "employee_advance"  # аванс → «Авансы сотрудникам»
LOAN_ARTICLE_CODE = "vydacha_zaymov_sotrudnikam"  # заём → «Выдача займов сотрудникам»
# Наличные счета: выдача = прямой расход с кошелька (Фаза 1). Банк-кошелёк (черновик+
# транзит на Сейф, как у ЗП) — Фаза 2: см. блок банк-выдачи ниже.
# ТК Черникова = iiko «Главная касса»: наличная выдача с неё параллельно изымается в iiko.
ADVANCE_TK_WALLET_CODE = "tk_chernikova"
ADVANCE_CASH_WALLET_CODES = (ADVANCE_TK_WALLET_CODE, "cash_safe")
# Банк-выдача (Фаза 2): транзит безналичной части на Сейф под выдачу аванса/займа.
# Банк-нога out служит prebooked-целью для приходящей операции выписки (как у ЗП),
# поэтому source_kind входит в classifier.PREBOOKABLE_SOURCE_KINDS.
ADVANCE_BANK_TO_SAFE_SOURCE_KIND = "salary_advance_bank_to_safe"
ADVANCE_BANK_DRAFT_STATUSES = frozenset(
    {"created", "updated", "paid", "disbursed", "failed", "cancelled"}
)


def _article_code_for(advance: SalaryAdvance) -> str:
    return LOAN_ARTICLE_CODE if advance.kind == "loan" else ADVANCE_ARTICLE_CODE


def _kind_label(advance: SalaryAdvance) -> str:
    return "займа" if advance.kind == "loan" else "аванса"


async def book_advance_payout_cashflow(
    session: AsyncSession, *, advance: SalaryAdvance, wallet: Wallet | None
) -> None:
    """Провести выдачу аванса/займа в ДДС — расход с наличного счёта.

    Статья по ``advance.kind`` (аванс → «Авансы сотрудникам», заём → «Выдача займов
    сотрудникам»). Идемпотентно по ``source_id = advance.id``. Только для наличных счетов
    (ТК Черникова / Сейф); банк-выдача проводится отдельно (черновик + транзит, Фаза 2).
    Устойчиво: если кошелёк/статья не заведены — проводка пропускается, выдачу не валим.
    """
    if wallet is None or wallet.code not in ADVANCE_CASH_WALLET_CODES:
        return
    existing = await session.scalar(
        select(CashflowTransaction.id).where(
            CashflowTransaction.source_kind == ADVANCE_PAYOUT_SOURCE_KIND,
            CashflowTransaction.source_id == advance.id,
        )
    )
    if existing is not None:
        return
    code = LOAN_ARTICLE_CODE if advance.kind == "loan" else ADVANCE_ARTICLE_CODE
    article_id = await session.scalar(select(DdsArticle.id).where(DdsArticle.code == code))
    if article_id is None:
        return
    label = "займа" if advance.kind == "loan" else "аванса"
    session.add(
        CashflowTransaction(
            wallet_id=wallet.id,
            direction="out",
            amount=advance.amount,
            operation_date=advance.issued_on,
            article_id=article_id,
            source_kind=ADVANCE_PAYOUT_SOURCE_KIND,
            source_id=advance.id,
            payment_purpose=f"Выдача {label} сотруднику (операция {advance.id})",
            quality_status="final",
        )
    )
    await session.flush()

LOAN_MAX_KEY = "payroll.loan_max_amount"
# Заглушка-дефолт: владелец задаёт реальный потолок в «Настройках».
LOAN_MAX_DEFAULT = Decimal("100000")


async def get_loan_max(session: AsyncSession) -> Decimal:
    """Потолок займа (₽) — настраиваемый в «Настройках», иначе дефолт."""
    setting = await session.scalar(select(AppSetting).where(AppSetting.key == LOAN_MAX_KEY))
    if setting is None or setting.value is None:
        return LOAN_MAX_DEFAULT
    try:
        value = decimal(setting.value)
    except (TypeError, ValueError, ArithmeticError):
        return LOAN_MAX_DEFAULT
    return value if value > 0 else LOAN_MAX_DEFAULT


async def set_loan_max(session: AsyncSession, amount: Decimal) -> Decimal:
    amount_dec = decimal(amount)
    if amount_dec <= 0:
        raise PayrollConflictError("Потолок займа должен быть больше нуля")
    await _upsert_setting(
        session,
        key=LOAN_MAX_KEY,
        value=float(amount_dec),
        value_type="decimal",
        display_name="Потолок займа, ₽",
        unit="₽",
    )
    return amount_dec


async def _outstanding_loan_principal(
    session: AsyncSession, employee_id: uuid.UUID
) -> Decimal:
    """Сумма непогашенного тела действующих займов сотрудника (для потолка)."""
    rows = (
        await session.scalars(
            select(SalaryAdvance).where(
                SalaryAdvance.employee_id == employee_id,
                SalaryAdvance.kind == "loan",
                # Банк-займы «ожидает выплаты» ещё не выданы, но деньги уже зарезервированы —
                # учитываем в потолке, чтобы не выдать сверх лимита, пока заём в полёте.
                SalaryAdvance.status.in_(("issued", "awaiting_payout")),
            )
        )
    ).all()
    return sum(
        (decimal(row.amount) - decimal(row.recovered_amount) for row in rows),
        Decimal("0"),
    )


async def _date_in_finalized_period(
    session: AsyncSession, on_date: date, *, admin: bool
) -> bool:
    """Покрыта ли дата финализированной ведомостью соответствующего пайплайна."""
    period_type = "half_month" if admin else "week"
    locked = await session.scalar(
        select(PayrollPeriod.id).where(
            PayrollPeriod.period_type == period_type,
            PayrollPeriod.status == "finalized",
            PayrollPeriod.start_date <= on_date,
            PayrollPeriod.end_date >= on_date,
        )
    )
    return locked is not None


async def _earliest_unfinalized_period(
    session: AsyncSession, *, admin: bool
) -> PayrollPeriod | None:
    """Самая ранняя выплата нужного пайплайна, куда ещё можно добавить выдачу/удержание."""
    period_type = "half_month" if admin else "week"
    return await session.scalar(
        select(PayrollPeriod)
        .where(
            PayrollPeriod.period_type == period_type,
            PayrollPeriod.status != "finalized",
        )
        .order_by(PayrollPeriod.payroll_date, PayrollPeriod.start_date)
        .limit(1)
    )


def _per_installment(amount: Decimal, count: int) -> Decimal:
    """Размер равной доли (остаток копеек добирается на возврате, до остатка баланса)."""
    count = max(int(count), 1)
    return (amount / Decimal(count)).quantize(_CENTS, rounding=ROUND_HALF_UP)


def _loan_installments(
    amount: Decimal,
    *,
    installment_amount: Decimal | None,
    installments_count: int,
) -> tuple[int, Decimal]:
    """Рассрочка займа: по сумме доли (приоритет) или по числу периодов.

    При заданной сумме доли число периодов выводится округлением вверх; последний
    период добирает остаток на возврате (`due = min(доля, остаток)`).
    """
    if installment_amount is not None:
        per = decimal(installment_amount).quantize(_CENTS)
        if per <= 0:
            raise PayrollConflictError("Сумма доли удержания должна быть больше нуля")
        per = min(per, amount)
        count = int((amount / per).to_integral_value(rounding=ROUND_CEILING))
        return max(count, 1), per
    count = max(int(installments_count), 1)
    return count, _per_installment(amount, count)


async def _bank_wallet_provider(session: AsyncSession, wallet: Wallet) -> str:
    """Банк-провайдер (tbank/sber) по банковскому кошельку выдачи — сравнением его расчётного
    счёта с настроенным Сбер-счётом плательщика; иначе Т-Банк.

    Сравниваем с «сырым» ``sber_api_account_number`` (не с mock-заглушкой ``payer_account_for``):
    в mock-режиме Сбер и Т-Банк делят один заглушечный счёт, поэтому только явно настроенный
    Сбер-счёт (прод) должен переключать провайдера на Сбер."""
    from app.models import Account

    sber_account = getattr(get_settings(), "sber_api_account_number", None)
    if not sber_account:
        return "tbank"
    account_number = await session.scalar(
        select(Account.account_number).where(Account.id == wallet.account_id)
    )
    if account_number and str(account_number) == str(sber_account):
        return "sber"
    return "tbank"


async def issue_advance(
    session: AsyncSession,
    *,
    employee_id: uuid.UUID,
    amount: Decimal,
    allow_loan: bool,
    requested_kind: str | None = None,
    override_ceiling: bool = False,
    issued_on: date | None = None,
    payout_method: str | None = None,
    wallet_id: uuid.UUID | None = None,
    installments_count: int = 1,
    installment_amount: Decimal | None = None,
    recovery_start_date: date | None = None,
    comment: str | None = None,
    actor_user_id: uuid.UUID | None = None,
    actor_label: str | None = None,
    bank_client: BankClient | None = None,
    kassa_pending: bool = False,
) -> SalaryAdvance:
    """Выдать аванс или заём.

    `requested_kind`:
      - `None` — авто-классификация по заработанному (сумма ≤ доступного → аванс,
        иначе заём) — обратная совместимость;
      - `'advance'` — принудительно аванс (отклоняется, если сумма > заработанного);
      - `'loan'` — принудительно заём, даже в пределах заработанного (рассрочка).

    Для займа рассрочка задаётся либо суммой доли (`installment_amount`, приоритет),
    либо числом периодов (`installments_count`); `recovery_start_date` откладывает
    начало удержания (применяется только к займу).

    `kassa_pending=True` — не выдача, а РАЗРЕШЕНИЕ на выдачу через кассу (страница
    авансов при счёте «ТК Черникова»): аванс создаётся в ``awaiting_payout`` без
    проводки ДДС, без iiko и без удержаний — их проведёт администратор кассы по
    «Выплачено» (``disburse_kassa_advance``), датой подтверждения. Самовыдача
    админом из формы «Выплата из кассы» этот флаг не передаёт и остаётся
    одношаговой.
    """
    if requested_kind not in (None, "advance", "loan"):
        raise PayrollConflictError("Неизвестный тип выдачи")
    employee = await session.get(Employee, employee_id)
    if employee is None:
        raise PayrollNotFoundError("Сотрудник не найден")
    requested_issued_on = issued_on or datetime.now(UTC).date()
    amount = decimal(amount).quantize(_CENTS)
    if amount <= 0:
        raise PayrollConflictError("Сумма должна быть больше нуля")
    if payout_method is not None and payout_method not in PAYOUT_METHODS:
        raise PayrollConflictError("Неизвестный способ выплаты")
    if payout_method == "payroll" and wallet_id is not None:
        raise PayrollConflictError("Для выдачи через ведомость отдельный счёт не выбирается")

    role = await get_position_on_date(session, employee_id, requested_issued_on)
    role = role or employee.position or ""
    # Без даты — привязываем к ближайшей нефинализированной выплате нужного пайплайна;
    # заработанное считаем на конец её периода (к выплате период отработан целиком).
    target_period: PayrollPeriod | None = None
    if issued_on is None:
        target_period = await _earliest_unfinalized_period(
            session, admin=role in admin_payroll_positions()
        )
        if target_period is not None:
            issued_on = target_period.payroll_date
            role = await get_position_on_date(session, employee_id, target_period.end_date)
            role = role or employee.position or ""
        else:
            issued_on = requested_issued_on
    else:
        issued_on = requested_issued_on

    if target_period is None:
        availability_as_of = issued_on
    elif target_period.period_type == "week":
        availability_as_of = target_period.payroll_date
    else:
        availability_as_of = target_period.end_date
    availability = await available_to_advance(session, employee, availability_as_of)
    finalization_date = target_period.end_date if target_period is not None else issued_on
    if await _date_in_finalized_period(
        session, finalization_date, admin=role in admin_payroll_positions()
    ):
        raise PayrollConflictError(
            "Период этой даты уже финализирован — выдача в закрытую ведомость невозможна"
        )

    over_earned = amount > availability.available
    if requested_kind == "advance" and over_earned:
        detail = availability.note or f"доступно к авансу {availability.available} ₽"
        raise PayrollConflictError(f"Сумма превышает заработанное ({detail}). Оформите заём.")
    # Явный заём — даже в пределах заработанного; иначе авто по заработанному.
    is_loan = requested_kind == "loan" or (requested_kind is None and over_earned)

    if not is_loan:
        kind = "advance"
        installments = 1
        per_installment = amount
        recovery_start: date | None = None
    else:
        if not allow_loan:
            raise PayrollConflictError("Выдача займа требует права на займы.")
        if not override_ceiling:
            ceiling = await get_loan_max(session)
            outstanding = await _outstanding_loan_principal(session, employee_id)
            if outstanding + amount > ceiling:
                raise PayrollConflictError(
                    f"Заём превышает потолок {ceiling} ₽ "
                    f"(непогашено по займам {outstanding} ₽). Требуется подтверждение превышения."
                )
        kind = "loan"
        installments, per_installment = _loan_installments(
            amount,
            installment_amount=installment_amount,
            installments_count=installments_count,
        )
        recovery_start = recovery_start_date

    wallet = await session.get(Wallet, wallet_id) if wallet_id is not None else None
    # Банк-счёт: деньги уходят не мгновенно → выдача ждёт исполнения платежа и фактической
    # выдачи сотруднику. Долг (удержание из ведомости) формируется только по «Выплачено».
    is_bank_payout = wallet is not None and wallet.type == "bank"
    if kassa_pending:
        # Разрешение на выдачу через кассу возможно только на ТК Черникова: деньги —
        # наличные кассы, перевод с Сейфа не требуется.
        if wallet is None or wallet.code != ADVANCE_TK_WALLET_CODE:
            raise PayrollConflictError(
                "Разрешение на выдачу через кассу доступно только для счёта "
                "«Торговая касса Черникова»"
            )
        if payout_method is None:
            payout_method = "cash"

    advance = SalaryAdvance(
        employee_id=employee_id,
        role=role,
        kind=kind,
        amount=amount,
        per_installment_amount=per_installment,
        installments_count=installments,
        recovered_amount=Decimal("0"),
        status="awaiting_payout" if is_bank_payout or kassa_pending else "issued",
        issued_on=issued_on,
        recovery_start_date=recovery_start,
        payout_method=payout_method,
        wallet_id=wallet_id,
        comment=comment,
        created_by_user_id=actor_user_id,
        created_by_label=actor_label,
    )
    session.add(advance)
    await session.flush()
    # Выдача = отток денег → проводка ДДС. Наличный счёт (ТК Черникова/Сейф) — прямой расход;
    # банк-счёт — черновик платежа + транзит на Сейф при исполнении (как у ЗП), Фаза 2;
    # kassa-разрешение — ничего: обязательство не начато, проводку/удержания/iiko проведёт
    # администратор кассы по «Выплачено».
    if kassa_pending:
        pass
    elif payout_method == "payroll":
        # Выдача через ведомость: сумма добавляется к ближайшей выплате, отдельного оттока
        # денег нет (ДДС проведётся зарплатной статьёй при выплате ведомости).
        await _apply_payroll_advance_to_existing_run(session, advance=advance)
    elif is_bank_payout:
        await create_advance_bank_draft(
            session, advance=advance, wallet=wallet, bank_client=bank_client
        )
    elif wallet is not None:
        await book_advance_payout_cashflow(session, advance=advance, wallet=wallet)
    await session.commit()
    await session.refresh(advance)
    # Наличная выдача с ТК Черникова (= iiko «Главная касса») → параллельное изъятие в iiko.
    # После commit: БД — источник истины, ошибка iiko не откатывает выдачу (логируется внутри).
    # Сейф и банк-выдача здесь iiko не трогают (банк изымается при «Выплачено», см. disburse);
    # kassa-разрешение — тоже нет (изъятие при исполнении, см. disburse_kassa_advance).
    if (
        not is_bank_payout
        and not kassa_pending
        and wallet is not None
        and wallet.code == ADVANCE_TK_WALLET_CODE
    ):
        post_advance_payout_to_iiko(
            amount=advance.amount,
            payout_date=advance.issued_on,
            source_id=advance.id,
            is_loan=advance.kind == "loan",
            source="cash",
        )
    return advance


async def book_operation_advance(
    session: AsyncSession,
    operation: BankOperation,
    *,
    employee_id: uuid.UUID,
    kind: str,
    installment_amount: Decimal | None = None,
    installments_count: int = 1,
    recovery_start_date: date | None = None,
    override_ceiling: bool = False,
    allow_loan: bool = False,
    comment: str | None = None,
    actor_user_id: uuid.UUID | None = None,
) -> SalaryAdvance:
    """Завести аванс/заём из разбора банковской операции (деньги уже ушли банком).

    В отличие от ``issue_advance``, НЕ бронирует выдачу и НЕ создаёт вторую проводку — деньги
    несёт split-проводка операции (её заводит ``apply_operation_split`` в роуте). Аванс
    фиксируется на всю сумму операции (излишек над заработанным сам переносится на следующую
    выплату существующим ``apply_advance_recoveries``, тип на заём не форсим). Возврат
    маршрутизируется по роли (админ/производство). Идемпотентно по ``source_operation_id``:
    прежний НЕпогашенный аванс операции снимаем; если по нему уже гасили — блокируем.
    """
    if kind not in ("advance", "loan"):
        raise PayrollConflictError("Неизвестный тип выдачи")
    employee = await session.get(Employee, employee_id)
    if employee is None:
        raise PayrollNotFoundError("Сотрудник не найден")

    prior = (
        await session.scalars(
            select(SalaryAdvance).where(SalaryAdvance.source_operation_id == operation.id)
        )
    ).all()
    for advance in prior:
        if decimal(advance.recovered_amount) > 0 or advance.status != "issued":
            raise PayrollConflictError(
                "Аванс по этой операции уже гасится в ведомости — сначала откатите её"
            )
    for advance in prior:
        await session.delete(advance)
    if prior:
        await session.flush()

    # Должность на дату операции (не employee.position — ленивое column_property роняет async).
    role = await get_position_on_date(session, employee_id, operation.operation_date) or ""
    amount = decimal(operation.amount).quantize(_CENTS)
    if amount <= 0:
        raise PayrollConflictError("Сумма должна быть больше нуля")

    if kind == "loan":
        if not allow_loan:
            raise PayrollConflictError("Выдача займа требует права на займы.")
        if not override_ceiling:
            ceiling = await get_loan_max(session)
            outstanding = await _outstanding_loan_principal(session, employee_id)
            if outstanding + amount > ceiling:
                raise PayrollConflictError(
                    f"Заём превышает потолок {ceiling} ₽ "
                    f"(непогашено по займам {outstanding} ₽). Требуется подтверждение превышения."
                )
        installments, per_installment = _loan_installments(
            amount, installment_amount=installment_amount, installments_count=installments_count
        )
        recovery_start = recovery_start_date
    else:
        installments, per_installment, recovery_start = 1, amount, None

    advance = SalaryAdvance(
        employee_id=employee_id,
        role=role,
        kind=kind,
        amount=amount,
        per_installment_amount=per_installment,
        installments_count=installments,
        issued_on=operation.operation_date,
        recovery_start_date=recovery_start,
        status="issued",
        source_operation_id=operation.id,
        comment=comment,
        created_by_user_id=actor_user_id,
    )
    session.add(advance)
    await session.flush()
    return advance


async def _apply_payroll_advance_to_existing_run(
    session: AsyncSession, *, advance: SalaryAdvance
) -> None:
    """Если ведомость дня выплаты уже рассчитана, сразу добавляем выдачу в её строку."""
    admin = advance.role in admin_payroll_positions()
    period_type = "half_month" if admin else "week"
    period = await session.scalar(
        select(PayrollPeriod)
        .where(
            PayrollPeriod.period_type == period_type,
            PayrollPeriod.status != "finalized",
            PayrollPeriod.payroll_date == advance.issued_on,
        )
        .order_by(PayrollPeriod.start_date)
        .limit(1)
    )
    if period is None:
        return
    run = await session.scalar(
        select(PayrollRun)
        .where(PayrollRun.period_id == period.id, PayrollRun.status != "finalized")
        .order_by(PayrollRun.started_at.desc())
        .limit(1)
    )
    if run is None:
        return
    line = await session.scalar(
        select(PayrollLine)
        .where(PayrollLine.run_id == run.id, PayrollLine.employee_id == advance.employee_id)
        .order_by(PayrollLine.role)
        .limit(1)
    )
    if line is None:
        raise PayrollConflictError("У сотрудника нет строки в выбранной ведомости")
    paid_payment_id = await session.scalar(
        select(PayrollPayment.id).where(
            PayrollPayment.run_id == run.id,
            PayrollPayment.employee_id == advance.employee_id,
            PayrollPayment.status == "paid",
        )
    )
    if paid_payment_id is not None:
        raise PayrollConflictError(
            "Сотруднику уже отмечена выплата в этой ведомости — сначала снимите отметку выплаты"
        )
    from app.services.payroll_advance_recovery import apply_advance_issuance_to_line

    amount = apply_advance_issuance_to_line(line, advance)
    summary = dict(run.summary or {})
    summary["advance_issued_total"] = money(
        decimal(summary.get("advance_issued_total", 0)) + amount
    )
    summary["advance_issued_count"] = int(summary.get("advance_issued_count", 0) or 0) + 1
    summary["total_payable"] = money(decimal(summary.get("total_payable", 0)) + amount)
    run.summary = summary


async def _remove_payroll_advance_from_existing_run(
    session: AsyncSession, *, advance: SalaryAdvance
) -> None:
    admin = advance.role in admin_payroll_positions()
    period_type = "half_month" if admin else "week"
    period = await session.scalar(
        select(PayrollPeriod)
        .where(
            PayrollPeriod.period_type == period_type,
            PayrollPeriod.payroll_date == advance.issued_on,
        )
        .order_by(PayrollPeriod.start_date)
        .limit(1)
    )
    if period is None:
        return
    if period.status == "finalized":
        raise PayrollConflictError("Ведомость уже финализирована — отмена выдачи невозможна")
    run = await session.scalar(
        select(PayrollRun)
        .where(PayrollRun.period_id == period.id, PayrollRun.status != "finalized")
        .order_by(PayrollRun.started_at.desc())
        .limit(1)
    )
    if run is None:
        return
    line = await session.scalar(
        select(PayrollLine)
        .where(PayrollLine.run_id == run.id, PayrollLine.employee_id == advance.employee_id)
        .order_by(PayrollLine.role)
        .limit(1)
    )
    if line is None:
        return
    from app.services.payroll_advance_recovery import remove_advance_issuance_from_line

    amount = remove_advance_issuance_from_line(line, advance)
    if amount <= 0:
        return
    summary = dict(run.summary or {})
    summary["advance_issued_total"] = money(
        max(decimal(summary.get("advance_issued_total", 0)) - amount, Decimal("0"))
    )
    summary["advance_issued_count"] = max(int(summary.get("advance_issued_count", 0) or 0) - 1, 0)
    summary["total_payable"] = money(
        max(decimal(summary.get("total_payable", 0)) - amount, Decimal("0"))
    )
    run.summary = summary


async def cancel_advance(
    session: AsyncSession,
    advance_id: uuid.UUID,
) -> SalaryAdvance:
    """Отменить выдачу (до начала возврата). Нельзя, если уже что-то удержано.

    Банк-выдача «ожидает выплаты» тоже отменяется: связанный черновик помечается
    ``cancelled``, а уже созданный резерв Сейфа освобождается (деньги остаются в Сейфе
    свободными — на расчётный счёт не возвращаем, by design).
    """
    advance = await session.get(SalaryAdvance, advance_id)
    if advance is None:
        raise PayrollNotFoundError("Аванс не найден")
    if decimal(advance.recovered_amount) > 0:
        raise PayrollConflictError("По авансу уже есть удержания — отмена невозможна")
    if advance.status not in ("issued", "awaiting_payout"):
        raise PayrollConflictError("Аванс нельзя отменить в текущем статусе")
    if advance.status == "awaiting_payout":
        draft = await _get_advance_draft(session, advance.id, lock=True)
        if draft is not None:
            if draft.safe_allocation_id is not None:
                allocation = await session.get(
                    SafeAllocation, draft.safe_allocation_id, with_for_update=True
                )
                if allocation is not None and allocation.status in (
                    "reserved",
                    "partially_paid",
                ):
                    await cancel_allocation(session, allocation)
            draft.status = "cancelled"
            draft.synced_at = datetime.now(UTC)
    if advance.payout_method == "payroll":
        await _remove_payroll_advance_from_existing_run(session, advance=advance)
    # Наличная выдача книжила ДДС-расход с кассы/Сейфа — снять его, иначе после
    # отмены в журнале остаётся висящий расход и баланс счёта врёт.
    cash_tx = await session.scalar(
        select(CashflowTransaction).where(
            CashflowTransaction.source_kind == ADVANCE_PAYOUT_SOURCE_KIND,
            CashflowTransaction.source_id == advance.id,
        )
    )
    if cash_tx is not None:
        await session.delete(cash_tx)
    advance.status = "cancelled"
    await session.commit()
    await session.refresh(advance)
    return advance


async def write_off_advance(
    session: AsyncSession,
    advance_id: uuid.UUID,
    *,
    reason: str | None = None,
) -> SalaryAdvance:
    """Списать непогашенный остаток (решение владельца, напр. при увольнении).

    Помечает аванс `written_off` — сейм возврата его больше не подхватывает
    (выбирает только `issued`). Удержанная часть остаётся, остаток = списанный убыток.
    """
    advance = await session.get(SalaryAdvance, advance_id)
    if advance is None:
        raise PayrollNotFoundError("Аванс не найден")
    if advance.status != "issued":
        raise PayrollConflictError("Списать можно только непогашенный (выданный) аванс")
    advance.status = "written_off"
    if reason:
        suffix = f"Списание: {reason}"
        advance.comment = f"{advance.comment} | {suffix}" if advance.comment else suffix
    await session.commit()
    await session.refresh(advance)
    return advance


async def set_advance_recovery_deferral(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    advance_id: uuid.UUID,
    defer: bool,
    reason: str | None = None,
    actor_user_id: uuid.UUID | None = None,
) -> SalaryAdvance:
    """Отсрочить (или вернуть) удержание займа в конкретной ведомости.

    Отсрочка освобождает заём от удержания в этой выплате: `recovery_start_date`
    сдвигается за конец периода ведомости, поэтому в текущей ведомости заём не
    гасится, а догашение продолжается со следующей. «Вернуть» обнуляет дату.
    Вызывающий код обязан после этого пересчитать ведомость.
    """
    run = await session.get(PayrollRun, run_id)
    if run is None:
        raise PayrollNotFoundError("Ведомость не найдена")
    if run.is_imported_legacy:
        raise PayrollConflictError("Импортированная ведомость — отсрочка недоступна")
    if run.status == "finalized":
        raise PayrollConflictError("Ведомость финализирована — сначала верните её в работу")
    period = await session.get(PayrollPeriod, run.period_id)
    if period is None:
        raise PayrollNotFoundError("Период ведомости не найден")

    advance = await session.get(SalaryAdvance, advance_id)
    if advance is None:
        raise PayrollNotFoundError("Заём не найден")
    if advance.kind != "loan":
        raise PayrollConflictError("Отсрочка доступна только для займа")
    if advance.status != "issued":
        raise PayrollConflictError("Отсрочить можно только действующий заём")
    # Заём должен относиться к пайплайну этой ведомости (админ/производство).
    admin = period.period_type == "half_month"
    if (advance.role in admin_payroll_positions()) != admin:
        raise PayrollConflictError("Этот заём гасится в другой ведомости")

    advance.recovery_start_date = (period.end_date + timedelta(days=1)) if defer else None
    session.add(
        PayrollRunEvent(
            run_id=run.id,
            period_id=run.period_id,
            action="advance_recovery_deferred" if defer else "advance_recovery_resumed",
            actor_user_id=actor_user_id,
            payload={
                "advance_id": str(advance.id),
                "employee_id": str(advance.employee_id),
                "recovery_start_date": (
                    advance.recovery_start_date.isoformat()
                    if advance.recovery_start_date
                    else None
                ),
                "reason": reason,
            },
        )
    )
    await session.commit()
    await session.refresh(advance)
    return advance


async def set_advance_recovery_overrides(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    items: list[tuple[uuid.UUID, Decimal]],
    reason: str | None = None,
    actor_user_id: uuid.UUID | None = None,
) -> None:
    """Задать ручные суммы удержания авансов/займов в этой ведомости (окно «Удержания»).

    Для каждого аванса: ``0`` — отложить удержание в этом периоде, ``= остатку долга`` —
    закрыть досрочно, между — частично. Ключ (advance_id, period_id) переживает пересчёт.
    Вызывающий код обязан после этого пересчитать ведомость.
    """
    run = await session.get(PayrollRun, run_id)
    if run is None:
        raise PayrollNotFoundError("Ведомость не найдена")
    if run.is_imported_legacy:
        raise PayrollConflictError("Импортированная ведомость — правка удержаний недоступна")
    if run.status == "finalized":
        raise PayrollConflictError("Ведомость финализирована — сначала верните её в работу")
    period = await session.get(PayrollPeriod, run.period_id)
    if period is None:
        raise PayrollNotFoundError("Период ведомости не найден")
    admin = period.period_type == "half_month"

    applied: list[dict[str, Any]] = []
    for advance_id, raw_amount in items:
        advance = await session.get(SalaryAdvance, advance_id)
        if advance is None:
            raise PayrollNotFoundError("Аванс/заём не найден")
        if advance.status != "issued":
            raise PayrollConflictError(
                "Правка удержания доступна только для действующего аванса/займа"
            )
        # Аванс/заём должен относиться к пайплайну этой ведомости (админ/производство).
        if (advance.role in admin_payroll_positions()) != admin:
            raise PayrollConflictError("Этот аванс/заём гасится в другой ведомости")
        amount = decimal(raw_amount).quantize(_CENTS)
        outstanding = (decimal(advance.amount) - decimal(advance.recovered_amount)).quantize(_CENTS)
        if amount < 0:
            raise PayrollConflictError("Сумма удержания не может быть отрицательной")
        if amount > outstanding:
            raise PayrollConflictError(
                f"Сумма удержания больше остатка долга ({money(outstanding)} ₽)"
            )
        existing = await session.scalar(
            select(SalaryAdvanceRecoveryOverride).where(
                SalaryAdvanceRecoveryOverride.advance_id == advance.id,
                SalaryAdvanceRecoveryOverride.period_id == period.id,
            )
        )
        if existing is None:
            session.add(
                SalaryAdvanceRecoveryOverride(
                    advance_id=advance.id,
                    period_id=period.id,
                    amount=amount,
                    created_by_user_id=actor_user_id,
                )
            )
        else:
            existing.amount = amount
        applied.append({"advance_id": str(advance.id), "amount": money(amount)})

    session.add(
        PayrollRunEvent(
            run_id=run.id,
            period_id=run.period_id,
            action="advance_recovery_override",
            actor_user_id=actor_user_id,
            payload={"items": applied, "reason": reason},
        )
    )
    await session.commit()


async def get_employee_recovery_detail(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    employee_id: uuid.UUID,
) -> dict[str, Any]:
    """Детализация удержаний сотрудника в ведомости: начислено, к выплате и построчно
    авансы/займы (остаток, доля по умолчанию, текущее удержание, ручной override)."""
    from app.services.payroll_advance_recovery import (
        _outstanding_advances,
        _recovery_overrides,
    )

    run = await session.get(PayrollRun, run_id)
    if run is None:
        raise PayrollNotFoundError("Ведомость не найдена")
    period = await session.get(PayrollPeriod, run.period_id)
    if period is None:
        raise PayrollNotFoundError("Период ведомости не найден")
    employee = await session.get(Employee, employee_id)
    if employee is None:
        raise PayrollNotFoundError("Сотрудник не найден")
    admin = period.period_type == "half_month"

    lines = (
        await session.scalars(
            select(PayrollLine).where(
                PayrollLine.run_id == run.id, PayrollLine.employee_id == employee_id
            )
        )
    ).all()
    net = sum((decimal(line.total_payable) for line in lines), Decimal("0"))
    recovered_now = sum((decimal(line.advance_recovered) for line in lines), Decimal("0"))
    accrued = net + recovered_now

    advances_by_emp = await _outstanding_advances(
        session, {employee_id}, admin=admin, period_end=period.end_date
    )
    advances = advances_by_emp.get(employee_id, [])
    overrides = await _recovery_overrides(session, period.id)

    applied_rows = (
        await session.scalars(
            select(SalaryAdvanceRecovery).where(SalaryAdvanceRecovery.run_id == run.id)
        )
    ).all()
    applied_by_advance: dict[uuid.UUID, Decimal] = {}
    for row in applied_rows:
        applied_by_advance[row.advance_id] = (
            applied_by_advance.get(row.advance_id, Decimal("0")) + decimal(row.amount)
        )

    recovery_items: list[dict[str, Any]] = []
    for advance in advances:
        outstanding = (decimal(advance.amount) - decimal(advance.recovered_amount)).quantize(_CENTS)
        default_installment = min(decimal(advance.per_installment_amount), outstanding)
        override = overrides.get(advance.id)
        recovery_items.append(
            {
                "advance_id": str(advance.id),
                "kind": advance.kind,
                "issued_on": advance.issued_on,
                "amount": money(advance.amount),
                "recovered_prior": money(advance.recovered_amount),
                "outstanding": money(outstanding),
                "default_installment": money(default_installment),
                "current_recovery": money(applied_by_advance.get(advance.id, Decimal("0"))),
                "override_amount": money(override) if override is not None else None,
                "max_amount": money(outstanding),
            }
        )

    return {
        "run_id": str(run.id),
        "employee_id": str(employee_id),
        "employee_name": employee.full_name,
        "role": employee.position,
        "period_start": period.start_date,
        "period_end": period.end_date,
        "payroll_date": period.payroll_date,
        "accrued": money(accrued),
        "net": money(net),
        "total_recovered": money(recovered_now),
        "items": recovery_items,
    }


async def list_advances(
    session: AsyncSession,
    *,
    employee_id: uuid.UUID | None = None,
    statuses: tuple[str, ...] | None = None,
) -> list[SalaryAdvance]:
    """Реестр авансов/займов: по сотруднику и/или статусам, новые сверху."""
    stmt = select(SalaryAdvance)
    if employee_id is not None:
        stmt = stmt.where(SalaryAdvance.employee_id == employee_id)
    if statuses:
        stmt = stmt.where(SalaryAdvance.status.in_(statuses))
    stmt = stmt.order_by(SalaryAdvance.issued_on.desc(), SalaryAdvance.created_at.desc())
    return list((await session.scalars(stmt)).all())


# ---------------------------------------------------------------------------
# Банк-выдача (Фаза 2): черновик платежа → транзит банк→Сейф + резерв → «Выплачено».
# ---------------------------------------------------------------------------


def _advance_document_id(advance_id: uuid.UUID) -> str:
    return f"teplo-advance-{advance_id}"


def _safe_advance_draft_status(value: str | None, fallback: str) -> str:
    return value if value in ADVANCE_BANK_DRAFT_STATUSES else fallback


async def _get_advance_draft(
    session: AsyncSession, advance_id: uuid.UUID, *, lock: bool = False
) -> SalaryAdvanceBankDraft | None:
    stmt = select(SalaryAdvanceBankDraft).where(
        SalaryAdvanceBankDraft.advance_id == advance_id
    )
    if lock:
        stmt = stmt.with_for_update()
    return await session.scalar(stmt)


async def list_advance_drafts(
    session: AsyncSession, advance_ids: list[uuid.UUID]
) -> dict[uuid.UUID, SalaryAdvanceBankDraft]:
    """Черновики банк-выдачи по списку авансов (для статуса выплаты в реестре)."""
    if not advance_ids:
        return {}
    rows = (
        await session.scalars(
            select(SalaryAdvanceBankDraft).where(
                SalaryAdvanceBankDraft.advance_id.in_(advance_ids)
            )
        )
    ).all()
    return {row.advance_id: row for row in rows}


def advance_payout_status(
    advance: SalaryAdvance, draft: SalaryAdvanceBankDraft | None
) -> str:
    """Сводный статус выплаты для UI: один код на «наличную» и «банковскую» выдачу.

    ``disbursed`` — деньги отданы сотруднику (наличная выдача сразу, банковская — по
    «Выплачено»); ``sent_to_bank`` — черновик в банке; ``awaiting_payout`` — исполнен,
    деньги зарезервированы в Сейфе, ждут выдачи; ``awaiting_kassa`` — разрешение на
    выдачу через кассу, ждёт администратора; ``failed`` — отклонён банком;
    ``cancelled`` — отменено (``cancelled_by_kassa`` — отклонено администратором кассы).
    """
    if advance.status == "cancelled":
        return "cancelled_by_kassa" if advance.kassa_cancelled_at is not None else "cancelled"
    if draft is None:
        if advance.status == "awaiting_payout":
            # Кассовое разрешение: выдачи ещё не было, ждёт «Выплачено» в модуле «Касса».
            return "awaiting_kassa"
        return "disbursed"  # наличная выдача — отток уже проведён при выдаче
    if draft.status == "disbursed":
        return "disbursed"
    if draft.status == "failed":
        return "failed"
    if draft.status == "cancelled":
        return "cancelled"
    if draft.status == "paid":
        return "awaiting_payout"
    return "sent_to_bank"


async def create_advance_bank_draft(
    session: AsyncSession,
    *,
    advance: SalaryAdvance,
    wallet: Wallet,
    bank_client: BankClient | None = None,
) -> SalaryAdvanceBankDraft:
    """Завести черновик платежа в банке под банк-выдачу аванса/займа.

    Реквизиты получателя — общие с зарплатой (``payroll.bank_payout_requisites``): деньги
    уходят на счёт ИП/владельца, откуда выдаются наличными. При сетевой/банк-ошибке черновик
    сохраняется со статусом ``failed`` (выдачу не валим — её можно отменить/повторить).
    """
    settings = get_settings()
    # Банк-плательщик (tbank/sber) — по выбранному счёту выдачи (кошельку). Транзит банк→Сейф
    # и черновик садятся на счёт этого провайдера; хранится на черновике (bank_provider).
    provider = await _bank_wallet_provider(session, wallet)
    payer_account = payer_account_for(settings, provider)
    requisites = await _bank_payout_requisites(session)
    document_id = _advance_document_id(advance.id)
    purpose = f"Перевод под выдачу {_kind_label(advance)} сотруднику"
    try:
        api_payload = build_payment_draft_api_payload(
            document_id=document_id,
            amount=advance.amount,
            purpose=purpose,
            requisites=requisites,
            payer_account=payer_account,
        )
    except ValueError as exc:
        raise PayrollConflictError(str(exc)) from exc

    stored_payload = {"accountNumber": payer_account, "request": api_payload}
    client = bank_client or payout_client_for(provider, session)
    try:
        result = await client.create_payment_draft(
            document_id=document_id,
            amount=advance.amount,
            purpose=purpose,
            requisites=dict(requisites),
            payer_account=payer_account,
        )
    except BankFetchError as exc:
        draft = SalaryAdvanceBankDraft(
            advance_id=advance.id,
            document_id=document_id[:64],
            amount=advance.amount,
            status="failed",
            bank_provider=provider,
            provider_ref=None,
            payload=stored_payload,
            last_error=str(exc)[:500],
            synced_at=datetime.now(UTC),
        )
        session.add(draft)
        await session.flush()
        return draft

    draft = SalaryAdvanceBankDraft(
        advance_id=advance.id,
        document_id=document_id[:64],
        amount=advance.amount,
        status=_safe_advance_draft_status(result.status, "created"),
        bank_provider=provider,
        provider_ref=result.provider_ref,
        payload=stored_payload,
        last_error=None,
        synced_at=datetime.now(UTC),
    )
    session.add(draft)
    await session.flush()
    return draft


async def apply_advance_draft_status(
    session: AsyncSession,
    *,
    draft: SalaryAdvanceBankDraft,
    raw_status: str | None,
    operation_date: date | None = None,
    commit: bool = True,
) -> str:
    """Продвинуть банк-черновик выдачи по статусу платежа (polling/webhook).

    При ``paid`` заводит транзит банк→Сейф и резерв Сейфа под выдачу (см.
    ``_book_advance_transit_and_reserve``). ``operation_date`` — дата реальной банк-операции
    (из вебхука «операция по счёту»), которой датируется транзит; без неё (поллинг) — текущая
    дата. Идемпотентно: блокировка строки сериализует гонку, переход только из created/updated.
    Аванс остаётся ``awaiting_payout`` — долг формируется лишь при фактической выдаче.
    """
    outcome = classify_payment_status(raw_status)
    draft = await session.get(SalaryAdvanceBankDraft, draft.id, with_for_update=True)
    if draft is None:
        return "created"

    if outcome == "paid" and draft.status in ("created", "updated"):
        # Блокируем аванс: сериализует гонку с отменой (cancel_advance) и не даёт завести
        # транзит/резерв поверх уже отменённой/выданной выдачи.
        advance = await session.get(SalaryAdvance, draft.advance_id, with_for_update=True)
        # В paid переходим ТОЛЬКО если аванс ещё ждёт выплаты И транзит+резерв реально заведены.
        # Если кошельки ещё не настроены — оставляем черновик в created, чтобы следующий поллинг
        # повторил (иначе «Выплачено» осталось бы навсегда заблокированным без резерва).
        if (
            advance is not None
            and advance.status == "awaiting_payout"
            and await _book_advance_transit_and_reserve(
                session, advance=advance, draft=draft, operation_date=operation_date
            )
        ):
            draft.status = "paid"
            draft.synced_at = datetime.now(UTC)
    elif outcome == "failed" and draft.status in ("created", "updated"):
        draft.status = "failed"
        draft.last_error = f"Платёж отклонён банком: {raw_status}"[:500]
        draft.synced_at = datetime.now(UTC)

    if commit:
        await session.commit()
    return draft.status


async def _book_advance_transit_and_reserve(
    session: AsyncSession,
    *,
    advance: SalaryAdvance,
    draft: SalaryAdvanceBankDraft,
    operation_date: date | None = None,
) -> bool:
    """Транзит банк→Сейф на сумму выдачи + резерв Сейфа со статьёй аванса/займа.

    Две ноги транзита с общим source (``salary_advance_bank_to_safe`` + advance.id): банк-нога
    out (статья «перевод между счетами») — prebooked-цель для приходящей операции выписки
    (баланс банка идёт от выписки, она его не двигает); Сейф-нога in двигает баланс Сейфа.
    Затем создаётся ``SafeAllocation(reserved)`` на ту же сумму — деньги «висят» под выдачу,
    видны в карточке Сейфа и гасятся по «Выплачено». Идемпотентно: резерв создаётся один раз.

    Возвращает ``True``, если транзит+резерв заведены (или уже были); ``False`` — если кошельки
    не настроены (вызывающий не переводит черновик в ``paid``, чтобы поллинг повторил позже).
    """
    if draft.safe_allocation_id is not None:
        return True
    existing = await session.scalar(
        select(CashflowTransaction.id).where(
            CashflowTransaction.source_kind == ADVANCE_BANK_TO_SAFE_SOURCE_KIND,
            CashflowTransaction.source_id == advance.id,
        )
    )
    if existing is not None:
        return True

    amount = decimal(advance.amount).quantize(_CENTS)
    settings = get_settings()
    # Транзит садится на банк-кошелёк провайдера черновика (tbank/sber), не всегда Т-Банк.
    payer_account = payer_account_for(settings, draft.bank_provider)
    bank_wallet = await session.scalar(
        select(Wallet)
        .join(Account, Account.id == Wallet.account_id)
        .where(Account.account_number == payer_account, Wallet.status == "active")
    )
    safe_wallet = await session.scalar(
        select(Wallet).where(Wallet.code == SAFE_WALLET_CODE, Wallet.status == "active")
    )
    if bank_wallet is None or safe_wallet is None:
        # Кошельки не заведены — транзит/резерв не создаём и сигналим неуспех: черновик
        # останется в created и поллинг повторит, когда кошельки появятся (на проде они есть).
        return False

    transfer_out_article = await session.scalar(
        select(DdsArticle.id).where(DdsArticle.code == TRANSFER_OUT_ARTICLE_CODE)
    )
    transfer_in_article = await session.scalar(
        select(DdsArticle.id).where(DdsArticle.code == TRANSFER_IN_ARTICLE_CODE)
    )
    # Дата транзита = дата реальной банк-операции (из вебхука); фолбэк — текущая дата.
    operation_date = operation_date or datetime.now(UTC).date()
    transit_purpose = f"Перевод на Сейф под выдачу {_kind_label(advance)}"
    session.add(
        CashflowTransaction(
            wallet_id=bank_wallet.id,
            direction="out",
            amount=amount,
            operation_date=operation_date,
            article_id=transfer_out_article,
            source_kind=ADVANCE_BANK_TO_SAFE_SOURCE_KIND,
            source_id=advance.id,
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
            source_kind=ADVANCE_BANK_TO_SAFE_SOURCE_KIND,
            source_id=advance.id,
            payment_purpose=transit_purpose,
            quality_status="final",
        )
    )
    await session.flush()

    article_id = await session.scalar(
        select(DdsArticle.id).where(DdsArticle.code == _article_code_for(advance))
    )
    employee_name = await session.scalar(
        select(Employee.full_name).where(Employee.id == advance.employee_id)
    )
    reserve_purpose = f"Выдача {_kind_label(advance)}"
    if employee_name:
        reserve_purpose = f"{reserve_purpose} — {employee_name}"
    allocation = SafeAllocation(
        wallet_id=safe_wallet.id,
        amount=amount,
        amount_paid=Decimal("0"),
        article_id=article_id,
        counterparty_id=None,
        purpose=reserve_purpose,
        status="reserved",
        created_by_user_id=advance.created_by_user_id,
    )
    session.add(allocation)
    await session.flush()
    draft.safe_allocation_id = allocation.id
    return True


async def disburse_bank_advance(
    session: AsyncSession, *, advance_id: uuid.UUID
) -> SalaryAdvance:
    """«Выплачено»: фактическая выдача банк-аванса сотруднику.

    Оплачивает резерв Сейфа (расход с Сейфа со статьёй аванса/займа), помечает черновик
    ``disbursed`` и переводит аванс в ``issued`` — только теперь формируется долг сотрудника
    (удержание из ближайшей ведомости). Идемпотентно: повторный вызов на выданном — no-op.
    """
    advance = await session.get(SalaryAdvance, advance_id)
    if advance is None:
        raise PayrollNotFoundError("Аванс не найден")
    draft = await _get_advance_draft(session, advance_id, lock=True)
    if draft is None:
        raise PayrollConflictError("Это не банковская выдача — отдельное подтверждение не нужно")
    if draft.status == "disbursed":
        return advance
    if draft.status in ("created", "updated"):
        raise PayrollConflictError("Платёж ещё не исполнен банком — выдать пока нельзя")
    if draft.status == "failed":
        raise PayrollConflictError("Платёж отклонён банком — выдача невозможна")
    if draft.status == "cancelled":
        raise PayrollConflictError("Выдача отменена")
    if draft.safe_allocation_id is None:
        raise PayrollConflictError("Резерв Сейфа под выдачу не найден")
    allocation = await session.get(
        SafeAllocation, draft.safe_allocation_id, with_for_update=True
    )
    if allocation is None:
        raise PayrollConflictError("Резерв Сейфа под выдачу не найден")

    outstanding = decimal(allocation.amount) - decimal(allocation.amount_paid)
    if outstanding > 0:
        try:
            await pay_allocation(
                session,
                allocation,
                amount=outstanding,
                operation_date=datetime.now(UTC).date(),
            )
        except ValueError as exc:
            raise PayrollConflictError(str(exc)) from exc
    draft.status = "disbursed"
    draft.synced_at = datetime.now(UTC)
    advance.status = "issued"
    await session.commit()
    await session.refresh(advance)
    # Деньги физически выданы сотруднику (нал из Сейфа) → изъятие в iiko по типу «эквайринг»
    # (безналичный источник выдачи). После commit, не валит выдачу при ошибке. Idempotent:
    # повторный «Выплачено» вышел выше по early-return (draft уже disbursed) → изъятие однажды.
    post_advance_payout_to_iiko(
        amount=advance.amount,
        payout_date=datetime.now(UTC).date(),
        source_id=advance.id,
        is_loan=advance.kind == "loan",
        source="bank",
    )
    return advance


async def sync_advance_after_allocation_change(
    session: AsyncSession, *, allocation_id: uuid.UUID
) -> SalaryAdvance | None:
    """Свести банк-аванс со статусом его резерва Сейфа (когда резерв трогают из карточки Сейфа).

    Оплата/отмена резерва возможна и напрямую в карточке Сейфа — тогда долг сотрудника тоже
    должен сформироваться/сняться. Вызывается из роутов оплаты/отмены резерва ДО их commit;
    при полной оплате резерва аванс → ``issued`` (+черновик ``disbursed``), при отмене →
    ``cancelled`` (+черновик ``cancelled``). Частичная оплата ничего не меняет.

    Возвращает ``SalaryAdvance``, если выдача состоялась (резерв оплачен → аванс выдан) —
    вызывающий роут обязан провести изъятие в iiko ПОСЛЕ commit; иначе ``None``.
    """
    draft = await session.scalar(
        select(SalaryAdvanceBankDraft)
        .where(SalaryAdvanceBankDraft.safe_allocation_id == allocation_id)
        .with_for_update()
    )
    if draft is None:
        return None
    allocation = await session.get(SafeAllocation, allocation_id)
    advance = await session.get(SalaryAdvance, draft.advance_id, with_for_update=True)
    if allocation is None or advance is None:
        return None
    if allocation.status == "paid" and draft.status == "paid":
        draft.status = "disbursed"
        draft.synced_at = datetime.now(UTC)
        advance.status = "issued"
        return advance
    if allocation.status == "cancelled" and draft.status in (
        "created",
        "updated",
        "paid",
    ):
        draft.status = "cancelled"
        draft.synced_at = datetime.now(UTC)
        advance.status = "cancelled"
    return None


# ---------------------------------------------------------------------------
# Разрешения на выдачу через кассу (этап 3): pending-выдача → «Выплачено» админом.
# ---------------------------------------------------------------------------

# Дата кассовых операций — календарный день МСК (как в kassa.payouts.kassa_today).
_KASSA_TZ = ZoneInfo("Europe/Moscow")


async def _kassa_pending_advance_locked(
    session: AsyncSession, advance_id: uuid.UUID
) -> SalaryAdvance:
    """Взять кассовое разрешение под row-lock с проверками состояния (гонки → 409).

    Кассовое разрешение = ``awaiting_payout`` + счёт «ТК Черникова» + БЕЗ банковского
    черновика (банк-выдача живёт своим контуром «черновик → Сейф → Выплачено»).
    """
    advance = await session.get(SalaryAdvance, advance_id, with_for_update=True)
    if advance is None:
        raise PayrollNotFoundError("Выдача не найдена")
    draft = await _get_advance_draft(session, advance_id)
    if draft is not None:
        raise PayrollConflictError(
            "Это банковская выдача — она подтверждается со страницы авансов, не через кассу"
        )
    if advance.status == "issued":
        raise PayrollConflictError("Разрешение уже исполнено — деньги выданы из кассы")
    if advance.status == "cancelled":
        raise PayrollConflictError("Разрешение уже отменено")
    if advance.status != "awaiting_payout":
        raise PayrollConflictError("Выдача не в статусе ожидания кассы")
    wallet = await session.get(Wallet, advance.wallet_id) if advance.wallet_id else None
    if wallet is None or wallet.code != ADVANCE_TK_WALLET_CODE:
        raise PayrollConflictError("Это не кассовое разрешение — выдача идёт другим счётом")
    return advance


async def list_kassa_pending_advances(
    session: AsyncSession,
) -> list[tuple[SalaryAdvance, str]]:
    """Ожидающие разрешения на авансы/займы через кассу (для вкладки «К выдаче»).

    Возвращает пары (выдача, имя сотрудника), новые сверху.
    """
    rows = (
        await session.execute(
            select(SalaryAdvance, Employee.full_name)
            .join(Employee, Employee.id == SalaryAdvance.employee_id)
            .join(Wallet, Wallet.id == SalaryAdvance.wallet_id)
            .outerjoin(
                SalaryAdvanceBankDraft,
                SalaryAdvanceBankDraft.advance_id == SalaryAdvance.id,
            )
            .where(
                SalaryAdvance.status == "awaiting_payout",
                Wallet.code == ADVANCE_TK_WALLET_CODE,
                SalaryAdvanceBankDraft.id.is_(None),
            )
            .order_by(SalaryAdvance.created_at.desc())
        )
    ).all()
    return [(advance, full_name) for advance, full_name in rows]


async def disburse_kassa_advance(
    session: AsyncSession, *, advance_id: uuid.UUID
) -> SalaryAdvance:
    """«Выплачено» администратором кассы: исполнить разрешение на аванс/заём.

    Только вся сумма. Наличная проводка из ТК Черникова + активация в леджере датой
    подтверждения (``issued_on`` = сегодня МСК, удержания от неё) — до этого момента
    обязательства не существовало. Вызывающий роут обязан провести изъятие в iiko
    ПОСЛЕ commit (``post_advance_payout_to_iiko(source="cash")``, как у наличной
    выдачи — ошибка iiko выдачу не валит). Повторное исполнение → конфликт (409).
    """
    advance = await _kassa_pending_advance_locked(session, advance_id)
    wallet = await session.get(Wallet, advance.wallet_id)
    advance.issued_on = datetime.now(_KASSA_TZ).date()
    advance.status = "issued"
    await book_advance_payout_cashflow(session, advance=advance, wallet=wallet)
    await session.commit()
    await session.refresh(advance)
    return advance


async def cancel_kassa_advance(
    session: AsyncSession, *, advance_id: uuid.UUID, by_kassa_admin: bool
) -> SalaryAdvance:
    """Отменить кассовое разрешение (двусторонне): админ кассы или сам создатель.

    ``by_kassa_admin=True`` ставит отметку «отменено кассой» — создатель увидит её
    на странице авансов. Отзыв создателем после исполнения админом → конфликт (409,
    «уже исполнено»), после отмены — тоже конфликт (гонки видимы). Проводок нет:
    разрешение не двигало деньги.
    """
    advance = await _kassa_pending_advance_locked(session, advance_id)
    advance.status = "cancelled"
    if by_kassa_admin:
        advance.kassa_cancelled_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(advance)
    return advance
