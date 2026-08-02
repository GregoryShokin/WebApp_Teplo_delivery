"""Кассовый журнал и «Выплата из кассы» (наличные с ТК Черникова).

Журнал — зеркало ДДС строго по одному кошельку ``tk_chernikova``: все движения
(системные — выручка смен, оплаты накладных, поступления из Сейфа — и ручные),
других счетов администратор не видит.

Выплата маршрутизируется по статье (только статьи с флагом ``kassa_enabled``):

* статьи аванса/займа сотруднику → существующий авансовый леджер
  (``payroll_advance_service.issue_advance``, наличный путь книжит ДДС сам) —
  голую проводку по этим статьям создавать нельзя, иначе не будет удержания
  из зарплаты;
* статья предоплаты поставщику → ``supplier_prepayments`` (возникает дебиторка);
* прочие разрешённые статьи (аренда, хозрасходы…) → обычная расходная
  ДДС-проводка (``source_kind='kassa_payout'``) с автором в
  ``created_by_user_id``.

Правка/удаление — только СВОЯ запись и только в день создания (позднее появится
отдельное право на поздние коррекции). Записи системных контуров (выручка,
накладные, переводы, депозиты курьеров) из кассы не правятся никогда.

Исключение — посменная выдача внештатнику (``freelancer_shift_payout``): править её
нельзя (сумма учётная, её считает движок), но ошибочную операцию можно ОТМЕНИТЬ целиком
под отдельным правом ``kassa.freelancer_shift.void`` — см.
``freelancer.shift_settlement.void_freelancer_shift_payout``. В журнале такая строка
несёт флаг ``voidable``.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    CashflowTransaction,
    Counterparty,
    CounterpartyPayableProfile,
    DdsArticle,
    Employee,
    FreelancerShiftSettlement,
    PayrollLine,
    PayrollPayment,
    PayrollPeriod,
    SafeAllocation,
    SalaryAdvance,
    SupplierPrepayment,
    Wallet,
)
from app.services import clock
from app.services.banking.cashflow_classify import EXCLUDED_QUALITY
from app.services.banking.safe_allocations import (
    ACTIVE_RESERVE_STATUSES,
    KASSA_TARGET_PAYOUT_SOURCE_KIND,
    kassa_targets_count,
    kassa_targets_total,
    pay_allocation,
)
from app.services.freelancer.shift_settlement import FREELANCER_SHIFT_PAYOUT_SOURCE_KIND
from app.services.location_analytics import (
    LocationAnalyticsError,
    resolve_location_context,
)
from app.services.wallets import CashWalletError, resolve_cash_wallet

MOSCOW_TZ = ZoneInfo("Europe/Moscow")

# Наличный счёт администраторов — единственный кошелёк модуля «Касса».
KASSA_WALLET_CODE = "tk_chernikova"

# Источник голой расходной проводки «Выплата из кассы».
KASSA_PAYOUT_SOURCE_KIND = "kassa_payout"

# Источник приходной проводки «Внесение в кассу» по пресету (см. services/kassa/payins.py).
KASSA_PAYIN_SOURCE_KIND = "kassa_payin"

# Статьи-маршруты (коды продублированы из профильных сервисов, чтобы не тянуть
# их тяжёлые модули на импорт; равенство закреплено тестами test_kassa_payouts).
EMPLOYEE_ADVANCE_ARTICLE_CODE = "employee_advance"  # payroll_advance_service
EMPLOYEE_LOAN_ARTICLE_CODE = "vydacha_zaymov_sotrudnikam"  # payroll_advance_service
SUPPLIER_PREPAYMENT_ARTICLE_CODE = "advance_to_supplier"  # supplier_prepayments

# Статьи, которым флаг «доступна в кассе» включать нельзя: у них собственные
# контуры выдачи/движения, голая выплата из кассы задвоила бы их. Коды
# продублированы из профильных сервисов (закреплено тестами).
PROTECTED_ARTICLE_CODES = frozenset(
    {
        # Движковые переводы между счетами (services/wallets.py).
        "postuplenie_perevod_mezhdu_schetami",
        "vybytie_perevod_mezhdu_schetami",
        # Депозиты курьеров: RETURN/TOP_UP из этой же кассы (couriers/deposit_service.py).
        "vozvrat_depozita_kurera",
        "kurerskaya_sluzhba_2",
        # Депозиты производственного персонала (deposit_service.py).
        "vydacha_depozita_sotrudniku",
        # Выплата зарплаты — контур ведомостей (payroll_payouts / payroll_payout_allocation).
        "zarplata_proizvodstvennogo_personala",
        "zarplata_administrativnogo_personala",
        # Оплата накладных поставщикам — контур накладных (counterparty/warehouse_payments).
        "payment_to_supplier",
        "supplier_staff_payment",
    }
)

KassaFlow = Literal["expense", "employee_advance", "employee_loan", "supplier_prepayment"]

_CENTS = Decimal("0.01")


class KassaPayoutError(Exception):
    """Выплата/правка из кассы невозможна (валидация, права на запись, состояние)."""


class KassaPayoutResult:
    """Результат создания/правки выплаты: тип маршрута и созданные сущности."""

    def __init__(
        self,
        *,
        kind: KassaFlow,
        transaction_id: uuid.UUID | None = None,
        advance: SalaryAdvance | None = None,
        prepayment: SupplierPrepayment | None = None,
    ) -> None:
        self.kind = kind
        self.transaction_id = transaction_id
        self.advance = advance
        self.prepayment = prepayment

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "transaction_id": self.transaction_id,
            "advance_id": self.advance.id if self.advance is not None else None,
            "prepayment_id": self.prepayment.id if self.prepayment is not None else None,
        }


def kassa_today() -> date:
    """Календарная дата кассы (МСК): правило «править можно в день создания».

    Через ``clock``, а не напрямую от системных часов: так дату можно закрепить в тестах
    одной подменой — см. докстринг ``app.services.clock``.
    """
    return clock.moscow_today()


def _money(value: object) -> Decimal:
    return Decimal(str(value or 0)).quantize(_CENTS, rounding=ROUND_HALF_UP)


def article_kassa_flow(article: DdsArticle) -> KassaFlow:
    """Маршрут выплаты по статье: аванс/заём → леджер, предоплата → дебиторка, прочее → проводка."""
    if article.code == EMPLOYEE_ADVANCE_ARTICLE_CODE:
        return "employee_advance"
    if article.code == EMPLOYEE_LOAN_ARTICLE_CODE:
        return "employee_loan"
    if article.code == SUPPLIER_PREPAYMENT_ARTICLE_CODE:
        return "supplier_prepayment"
    return "expense"


def ensure_article_kassa_eligible(article: DdsArticle) -> None:
    """Можно ли статье включить флаг «доступна в кассе» (422 в API, если нет).

    Касса — только расход; статьи с собственными контурами выдачи (переводы,
    возврат депозита курьера, ЗП, оплата накладных…) включать нельзя — выдача
    задвоится.
    """
    if article.movement_type != "outflow":
        raise KassaPayoutError(
            "В кассе доступны только расходные статьи (списание) — "
            "приходы кассы идут своими контурами"
        )
    if article.code in PROTECTED_ARTICLE_CODES:
        raise KassaPayoutError(
            "У этой статьи собственный контур выдачи — включить её в кассу нельзя, "
            "выплата задвоится"
        )


async def get_kassa_wallet(session: AsyncSession) -> Wallet:
    try:
        return await resolve_cash_wallet(session, KASSA_WALLET_CODE)
    except CashWalletError as exc:
        raise KassaPayoutError("Счёт «Торговая касса Черникова» не найден или неактивен") from exc


async def kassa_balance(session: AsyncSession, wallet: Wallet) -> Decimal:
    """Учётный остаток кассы: вступительный остаток + движения ДДС после него.

    Наличный кошелёк без выписки — баланс от проводок, ТОЧНО как плитка ДДС
    «Кошельки» (``_wallet_movement_deltas``): мягко-исключённые проводки
    (``quality_status='excluded'``, ручной разбор) в баланс НЕ входят. Фильтр
    обязан совпадать с ``_wallet_movement_deltas`` — иначе плитка/окно/журнал
    кассы разъезжаются (был баг: исключённая корректировка кассы двоила остаток).
    """
    after_opening = or_(
        Wallet.opening_balance_date.is_(None),
        CashflowTransaction.operation_date > Wallet.opening_balance_date,
    )
    rows = await session.execute(
        select(
            CashflowTransaction.direction,
            func.coalesce(func.sum(CashflowTransaction.amount), 0),
        )
        .join(Wallet, Wallet.id == CashflowTransaction.wallet_id)
        .where(
            CashflowTransaction.wallet_id == wallet.id,
            CashflowTransaction.quality_status != EXCLUDED_QUALITY,
            after_opening,
        )
        .group_by(CashflowTransaction.direction)
    )
    delta = Decimal("0")
    for direction, total in rows:
        amount = Decimal(total)
        delta += amount if direction == "in" else -amount
    return _money(Decimal(str(wallet.opening_balance)) + delta)


async def list_payout_articles(session: AsyncSession) -> list[dict[str, Any]]:
    """Статьи, разрешённые для «Выплаты из кассы» (флаг владельца + только расход)."""
    articles = (
        await session.scalars(
            select(DdsArticle)
            .where(
                DdsArticle.kassa_enabled.is_(True),
                DdsArticle.is_active.is_(True),
                DdsArticle.movement_type == "outflow",
            )
            .order_by(DdsArticle.name)
        )
    ).all()
    return [
        {
            "id": article.id,
            "code": article.code,
            "name": article.name,
            "flow": article_kassa_flow(article),
        }
        for article in articles
        if article.code not in PROTECTED_ARTICLE_CODES
    ]


async def list_payout_employees(session: AsyncSession) -> list[Employee]:
    """Активные сотрудники для сотрудниковых статей (аванс/заём)."""
    result = await session.scalars(
        select(Employee).where(Employee.status == "active").order_by(Employee.full_name)
    )
    return list(result.all())


async def _ensure_kassa_supplier(session: AsyncSession, counterparty_id: uuid.UUID) -> None:
    """Предоплата — только поставщикам «Активен в Кассе» (тот же флаг, что в накладных)."""
    enabled = await session.scalar(
        select(CounterpartyPayableProfile.kassa_enabled).where(
            CounterpartyPayableProfile.counterparty_id == counterparty_id
        )
    )
    if not enabled:
        raise KassaPayoutError(
            "Поставщик не активен в Кассе — флаг включает владелец в реестре контрагентов"
        )


async def _validated_payout_article(session: AsyncSession, article_id: uuid.UUID) -> DdsArticle:
    article = await session.get(DdsArticle, article_id)
    if article is None:
        raise KassaPayoutError("Статья не найдена")
    if not article.is_active:
        raise KassaPayoutError("Статья деактивирована")
    if not article.kassa_enabled:
        raise KassaPayoutError("Статья не разрешена для выплат из кассы")
    ensure_article_kassa_eligible(article)
    return article


async def create_payout(
    session: AsyncSession,
    *,
    article_id: uuid.UUID,
    amount: Decimal,
    comment: str | None = None,
    employee_id: uuid.UUID | None = None,
    counterparty_id: uuid.UUID | None = None,
    location_id: uuid.UUID | None = None,
    lease_id: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
) -> KassaPayoutResult:
    """Выдать наличные из кассы по разрешённой статье (дата — всегда сегодня).

    Сумма больше учётного остатка НЕ блокируется (предупреждение на фронте):
    остаток учётный, в ящике денег может быть больше.
    """
    article = await _validated_payout_article(session, article_id)
    amount = _money(amount)
    if amount <= 0:
        raise KassaPayoutError("Сумма должна быть больше нуля")
    wallet = await get_kassa_wallet(session)
    today = kassa_today()
    try:
        location_context = await resolve_location_context(
            session,
            article=article,
            location_id=location_id,
            lease_id=lease_id,
            counterparty_id=counterparty_id,
            on_date=today,
        )
    except LocationAnalyticsError as exc:
        raise KassaPayoutError(str(exc)) from exc
    if location_context.counterparty_id is not None:
        counterparty_id = location_context.counterparty_id
    flow = article_kassa_flow(article)

    if flow in ("employee_advance", "employee_loan"):
        if employee_id is None:
            raise KassaPayoutError("Для аванса/займа выберите сотрудника")
        # Выдача через авансовый леджер: проводку ДДС с ТК книжит сам issue_advance,
        # удержание попадёт в ближайшую ведомость. Заём — потому что владелец явно
        # включил статью займов в кассу; потолок займа проверяется внутри.
        from app.services.payroll_advance_service import issue_advance
        from app.services.payroll_runner import PayrollConflictError, PayrollNotFoundError

        try:
            advance = await issue_advance(
                session,
                employee_id=employee_id,
                amount=amount,
                allow_loan=flow == "employee_loan",
                requested_kind="loan" if flow == "employee_loan" else "advance",
                issued_on=today,
                payout_method="cash",
                wallet_id=wallet.id,
                comment=comment,
                actor_user_id=actor_user_id,
            )
        except (PayrollConflictError, PayrollNotFoundError) as exc:
            raise KassaPayoutError(str(exc)) from exc
        transaction_id = await session.scalar(
            select(CashflowTransaction.id).where(
                CashflowTransaction.source_kind == "salary_advance",
                CashflowTransaction.source_id == advance.id,
            )
        )
        return KassaPayoutResult(kind=flow, transaction_id=transaction_id, advance=advance)

    if flow == "supplier_prepayment":
        if counterparty_id is None:
            raise KassaPayoutError("Для предоплаты выберите поставщика")
        await _ensure_kassa_supplier(session, counterparty_id)
        from app.services.counterparty_payments import CounterpartyPaymentError
        from app.services.supplier_prepayments import create_supplier_prepayment

        try:
            prepayment = await create_supplier_prepayment(
                session,
                counterparty_id=counterparty_id,
                wallet_id=wallet.id,
                amount=amount,
                operation_date=today,
                article_id=article.id,
                note=comment,
                actor_user_id=actor_user_id,
            )
        except CounterpartyPaymentError as exc:
            raise KassaPayoutError(str(exc)) from exc
        return KassaPayoutResult(
            kind=flow,
            transaction_id=prepayment.cashflow_transaction_id,
            prepayment=prepayment,
        )

    transaction = CashflowTransaction(
        wallet_id=wallet.id,
        direction="out",
        amount=amount,
        operation_date=today,
        article_id=article.id,
        counterparty_id=counterparty_id,
        location_id=location_context.location_id,
        lease_id=location_context.lease_id,
        source_kind=KASSA_PAYOUT_SOURCE_KIND,
        payment_purpose=comment,
        quality_status="final",
        created_by_user_id=actor_user_id,
    )
    session.add(transaction)
    await session.commit()
    await session.refresh(transaction)
    return KassaPayoutResult(kind="expense", transaction_id=transaction.id)


async def _kassa_transaction_or_error(
    session: AsyncSession, wallet: Wallet, transaction_id: uuid.UUID
) -> CashflowTransaction:
    transaction = await session.get(CashflowTransaction, transaction_id)
    if transaction is None or transaction.wallet_id != wallet.id:
        raise KassaPayoutError("Запись кассового журнала не найдена")
    return transaction


async def _editable_target(
    session: AsyncSession,
    transaction: CashflowTransaction,
    *,
    actor_user_id: uuid.UUID | None,
) -> tuple[str, SalaryAdvance | SupplierPrepayment | None]:
    """Проверить, что запись — своя, сегодняшняя и кассовая; вернуть её источник.

    Возвращает ('manual'|'advance'|'prepayment', сущность-источник или None).
    """
    today = kassa_today()
    if transaction.source_kind == KASSA_PAYOUT_SOURCE_KIND:
        author_id = transaction.created_by_user_id
        target: SalaryAdvance | SupplierPrepayment | None = None
        kind = "manual"
    elif transaction.source_kind == "salary_advance":
        advance = (
            await session.get(SalaryAdvance, transaction.source_id)
            if transaction.source_id
            else None
        )
        if advance is None:
            raise KassaPayoutError("Выдача по этой записи не найдена — правка недоступна")
        author_id = advance.created_by_user_id
        target = advance
        kind = "advance"
    elif transaction.source_kind == "supplier_prepayment":
        prepayment = (
            await session.get(SupplierPrepayment, transaction.source_id)
            if transaction.source_id
            else None
        )
        if prepayment is None:
            raise KassaPayoutError("Предоплата по этой записи не найдена — правка недоступна")
        author_id = prepayment.created_by_user_id
        target = prepayment
        kind = "prepayment"
    else:
        raise KassaPayoutError(
            "Эта запись создана системным контуром (выручка, накладные, переводы) — "
            "из кассы она не редактируется"
        )

    if actor_user_id is None or author_id != actor_user_id:
        raise KassaPayoutError("Можно править только свои кассовые записи")
    if transaction.operation_date != today:
        raise KassaPayoutError(
            "Правка доступна только в день создания записи — за прошлые даты обратитесь к владельцу"
        )
    if isinstance(target, SalaryAdvance) and (
        target.status != "issued" or _money(target.recovered_amount) > 0
    ):
        raise KassaPayoutError("По выдаче уже есть удержания или она отменена")
    if isinstance(target, SupplierPrepayment) and (
        _money(target.amount_settled) > 0 or target.status != "open"
    ):
        raise KassaPayoutError("Предоплата уже начала гаситься накладными — правка недоступна")
    return kind, target


async def _remove_payout_target(
    session: AsyncSession,
    transaction: CashflowTransaction,
    kind: str,
    target: SalaryAdvance | SupplierPrepayment | None,
) -> None:
    """Снять запись вместе с источником (проверки уже пройдены в _editable_target)."""
    if kind == "manual":
        await session.delete(transaction)
        await session.commit()
        return
    if kind == "advance":
        from app.services.payroll_advance_service import cancel_advance
        from app.services.payroll_runner import PayrollConflictError, PayrollNotFoundError

        assert isinstance(target, SalaryAdvance)  # noqa: S101 — гарантировано _editable_target
        try:
            await cancel_advance(session, target.id)
        except (PayrollConflictError, PayrollNotFoundError) as exc:
            raise KassaPayoutError(str(exc)) from exc
        return
    from app.services.counterparty_payments import CounterpartyPaymentError
    from app.services.supplier_prepayments import cancel_supplier_prepayment

    assert isinstance(target, SupplierPrepayment)  # noqa: S101 — гарантировано _editable_target
    try:
        await cancel_supplier_prepayment(session, target.id)
    except CounterpartyPaymentError as exc:
        raise KassaPayoutError(str(exc)) from exc


async def update_payout(
    session: AsyncSession,
    *,
    transaction_id: uuid.UUID,
    article_id: uuid.UUID,
    amount: Decimal,
    comment: str | None = None,
    employee_id: uuid.UUID | None = None,
    counterparty_id: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
) -> KassaPayoutResult:
    """Исправить свою сегодняшнюю кассовую запись.

    Голая проводка правится на месте; аванс/предоплата — отмена + новая выдача
    (существующие механики), поэтому у записи появится новый id.
    """
    wallet = await get_kassa_wallet(session)
    transaction = await _kassa_transaction_or_error(session, wallet, transaction_id)
    kind, target = await _editable_target(session, transaction, actor_user_id=actor_user_id)

    new_article = await _validated_payout_article(session, article_id)
    new_flow = article_kassa_flow(new_article)
    new_amount = _money(amount)
    if new_amount <= 0:
        raise KassaPayoutError("Сумма должна быть больше нуля")

    if kind == "manual" and new_flow == "expense":
        transaction.article_id = new_article.id
        transaction.amount = new_amount
        transaction.payment_purpose = comment
        await session.commit()
        return KassaPayoutResult(kind="expense", transaction_id=transaction.id)

    # Смена суммы/параметров аванса и предоплаты (и смена типа выплаты) — всегда
    # «отмена + новая выдача»: леджеры не поддерживают правку на месте.
    await _remove_payout_target(session, transaction, kind, target)
    return await create_payout(
        session,
        article_id=new_article.id,
        amount=new_amount,
        comment=comment,
        employee_id=employee_id,
        counterparty_id=counterparty_id,
        actor_user_id=actor_user_id,
    )


async def delete_payout(
    session: AsyncSession,
    *,
    transaction_id: uuid.UUID,
    actor_user_id: uuid.UUID | None = None,
) -> None:
    """Удалить свою сегодняшнюю кассовую запись (аванс — отмена, предоплата — снятие)."""
    wallet = await get_kassa_wallet(session)
    transaction = await _kassa_transaction_or_error(session, wallet, transaction_id)
    kind, target = await _editable_target(session, transaction, actor_user_id=actor_user_id)
    await _remove_payout_target(session, transaction, kind, target)


# --- Вкладка «К выдаче»: целёвки в кассе и разрешения на авансы/займы -----------


async def _kassa_pending_advances(session: AsyncSession) -> list[dict[str, Any]]:
    """Ожидающие разрешения на авансы/займы (pending-выдачи со страницы авансов)."""
    from app.services.payroll_advance_service import list_kassa_pending_advances

    return [
        {
            "id": advance.id,
            "employee_id": advance.employee_id,
            "employee_name": employee_name,
            "kind": advance.kind,
            "amount": float(_money(advance.amount)),
            "comment": advance.comment,
            "created_by_label": advance.created_by_label,
            "created_at": advance.created_at,
        }
        for advance, employee_name in await list_kassa_pending_advances(session)
    ]


async def _kassa_pending_freelancers(session: AsyncSession) -> list[dict[str, Any]]:
    """Внештатники с непогашенным: одна строка на человека (Σ неоплаченных смен)."""
    from app.services.freelancer.shift_settlement import list_unpaid_freelancers

    return await list_unpaid_freelancers(session)


async def _payroll_employees_by_run(
    session: AsyncSession,
    run_ids: set[uuid.UUID],
) -> dict[uuid.UUID, list[dict[str, Any]]]:
    """Состав зарплатных резервов для кассира без доступа к payroll API."""
    if not run_ids:
        return {}
    accrued_rows = (
        await session.execute(
            select(
                PayrollLine.run_id,
                PayrollLine.employee_id,
                Employee.full_name,
                func.sum(PayrollLine.total_payable),
                func.coalesce(func.sum(PayrollLine.deposit_payout_scheduled), 0),
            )
            .join(Employee, Employee.id == PayrollLine.employee_id)
            .where(PayrollLine.run_id.in_(run_ids))
            .group_by(PayrollLine.run_id, PayrollLine.employee_id, Employee.full_name)
        )
    ).all()
    paid_rows = (
        await session.execute(
            select(PayrollPayment.run_id, PayrollPayment.employee_id, PayrollPayment.amount).where(
                PayrollPayment.run_id.in_(run_ids)
            )
        )
    ).all()
    paid_by_employee = {
        (run_id, employee_id): _money(amount) for run_id, employee_id, amount in paid_rows
    }
    result: dict[uuid.UUID, list[dict[str, Any]]] = {run_id: [] for run_id in run_ids}
    for run_id, employee_id, employee_name, accrued, deposit_scheduled in accrued_rows:
        accrued_q = _money(accrued or 0)
        paid_q = min(accrued_q, paid_by_employee.get((run_id, employee_id), Decimal("0")))
        remaining = _money(max(Decimal("0"), accrued_q - paid_q))
        result[run_id].append(
            {
                "employee_id": employee_id,
                "employee_name": employee_name,
                "accrued": float(accrued_q),
                "paid": float(paid_q),
                "remaining": float(remaining),
                "payment_status": (
                    "paid" if remaining == 0 else "partially_paid" if paid_q > 0 else "pending"
                ),
                # Депозитная выдача имеет отдельный полный контур и из кассового пула не идёт.
                "payable": _money(deposit_scheduled or 0) == 0,
            }
        )
    for employees in result.values():
        employees.sort(key=lambda employee: employee["employee_name"].casefold())
    return result


async def kassa_pending_payload(
    session: AsyncSession,
    *,
    include_payroll_targets: bool = True,
) -> dict[str, Any]:
    """Состав вкладки «К выдаче»: целёвки в кассе + ожидающие разрешения + итоги.

    Пул-резервы ведомостей входят в целевые деньги кассы и раскрываются до сотрудников.
    Их выдача идёт отдельным зарплатным endpoint, а не обычной кнопкой целёвки «Выдано».
    ``include_payroll_targets=False`` оставлен для узких внутренних витрин.
    """
    wallet = await get_kassa_wallet(session)
    target_filters = [
        SafeAllocation.wallet_id == wallet.id,
        SafeAllocation.location == "kassa",
        SafeAllocation.status.in_(ACTIVE_RESERVE_STATUSES),
    ]
    if not include_payroll_targets:
        target_filters.append(SafeAllocation.source_run_id.is_(None))
    rows = (
        await session.execute(
            select(SafeAllocation, DdsArticle.name, Counterparty.name)
            .outerjoin(DdsArticle, DdsArticle.id == SafeAllocation.article_id)
            .outerjoin(Counterparty, Counterparty.id == SafeAllocation.counterparty_id)
            .where(*target_filters)
            .order_by(SafeAllocation.created_at.desc())
        )
    ).all()
    payroll_employees = await _payroll_employees_by_run(
        session,
        {
            allocation.source_run_id
            for allocation, _article_name, _counterparty_name in rows
            if allocation.source_run_id is not None
        },
    )
    targets = [
        {
            "id": allocation.id,
            "article_id": allocation.article_id,
            "article_name": article_name,
            "counterparty_id": allocation.counterparty_id,
            "counterparty_name": counterparty_name,
            "purpose": allocation.purpose,
            "amount": float(_money(allocation.amount)),
            "amount_paid": float(_money(allocation.amount_paid)),
            "outstanding": float(
                _money(Decimal(str(allocation.amount)) - Decimal(str(allocation.amount_paid)))
            ),
            # Происхождение: авто-целёвка закупа из оплаченного банковского черновика.
            "from_bank_payout": allocation.source_draft_id is not None,
            "is_payroll": allocation.source_run_id is not None,
            "run_id": allocation.source_run_id,
            "payroll_employees": payroll_employees.get(allocation.source_run_id, []),
            "created_at": allocation.created_at,
        }
        for allocation, article_name, counterparty_name in rows
    ]
    permissions = await _kassa_pending_advances(session)
    freelancers = await _kassa_pending_freelancers(session)
    targets_total = sum(Decimal(str(target["outstanding"])) for target in targets)
    return {
        "wallet_name": wallet.name,
        "balance": float(await kassa_balance(session, wallet)),
        "targets": targets,
        "permissions": permissions,
        "freelancers": freelancers,
        "targets_total": float(_money(targets_total)),
        # В счётчик вкладки идут только те внештатники, кому реально есть что выдать:
        # человек с одной ИДУЩЕЙ сменой в списке виден (серой строкой), но выдавать
        # ему нечего — бейдж «К выдаче» на него звать не должен.
        "pending_count": len(targets)
        + len(permissions)
        + sum(1 for freelancer in freelancers if freelancer.get("unpaid_total", 0)),
    }


async def pay_kassa_target(
    session: AsyncSession,
    *,
    allocation_id: uuid.UUID,
    amount: Decimal,
    actor_user_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """«Выдано»: расход целёвки наличными из кассы (полный или частичный).

    Семантика оплат — как у ``pay_allocation`` на Сейфе (``amount_paid``/статусы),
    только кошелёк — касса и свой ``source_kind``; статья и контрагент — целёвки.
    Сумма больше учётного остатка кассы не блокируется (предупреждение на фронте).
    """
    # Локальные импорты: deposit_bank_draft локально импортирует этот модуль (наличный резерв),
    # deposit_iiko тянет iiko-клиента — top-import замкнул бы цикл / раздул загрузку.
    from app.services.deposit_bank_draft import (
        allocation_deposit_draft,
        sync_deposit_after_allocation_change,
    )
    from app.services.deposit_iiko_payout_production import (
        post_production_deposit_payout_to_iiko,
    )

    wallet = await get_kassa_wallet(session)
    allocation = await session.get(SafeAllocation, allocation_id, with_for_update=True)
    if allocation is None:
        raise KassaPayoutError("Целёвка не найдена")
    if allocation.source_run_id is not None:
        # Пул-резерв выплаты ЗП: выдача только через окно ведомости (иначе pay_allocation
        # книжит лишний cashflow мимо PayrollPayment).
        raise KassaPayoutError("Резерв выплаты ЗП — выдача через окно ведомости, не из «К выдаче»")
    if allocation.location != "kassa" or allocation.wallet_id != wallet.id:
        raise KassaPayoutError("Целёвка не передана в кассу — оплата идёт с Сейфа")
    # Депозит-резерв (обязательство перед сотрудником) выдаётся ТОЛЬКО целиком: частичная
    # выдача разъехала бы депозит-леджер (списываем всю сумму) с ДДС (частичный расход).
    deposit_draft = await allocation_deposit_draft(session, allocation.id)
    if deposit_draft is not None:
        outstanding = _money(Decimal(str(allocation.amount)) - Decimal(str(allocation.amount_paid)))
        if _money(amount) < outstanding:
            raise KassaPayoutError(
                "Депозит-резерв выдаётся только целиком (частичная выдача запрещена)"
            )
    try:
        transaction_id = await pay_allocation(
            session,
            allocation,
            amount=_money(amount),
            operation_date=kassa_today(),
            source_kind=KASSA_TARGET_PAYOUT_SOURCE_KIND,
            created_by_user_id=actor_user_id,
        )
    except ValueError as exc:
        raise KassaPayoutError(str(exc)) from exc
    # Депозит из кассы: фактическая выдача = списание депозита + черновик → disbursed.
    disbursement = await sync_deposit_after_allocation_change(session, allocation_id=allocation.id)
    await session.commit()
    # iiko-изъятие из «Главной кассы» при выдаче депозита из кассы — ПОСЛЕ commit (БД источник
    # истины). disbursement заполнен ровно один раз (черновик paid→disbursed), поэтому изъятие
    # не задваивается; ключ-комментарий — allocation.id.
    if disbursement is not None:
        await post_production_deposit_payout_to_iiko(
            session, amount=disbursement.amount, payout_date=kassa_today(),
            source_id=str(allocation.id),
        )
    return transaction_id


async def kassa_journal(
    session: AsyncSession,
    *,
    date_from: date,
    date_to: date,
    direction: str | None = None,
    search: str | None = None,
    actor_user_id: uuid.UUID | None = None,
    limit: int = 500,
) -> dict[str, Any]:
    """Журнал кассы: остаток сейчас, приход/расход за период, движения за период."""
    wallet = await get_kassa_wallet(session)
    balance = await kassa_balance(session, wallet)

    # Мягко-исключённые проводки (quality_status='excluded', ручной разбор) НЕ входят
    # ни в остаток, ни в итоги/список журнала — как в плитке ДДС. Иначе исключённая
    # запись висит в журнале и двоит расход периода, хотя из баланса уже выкинута.
    period_filter = (
        CashflowTransaction.wallet_id == wallet.id,
        CashflowTransaction.quality_status != EXCLUDED_QUALITY,
        CashflowTransaction.operation_date >= date_from,
        CashflowTransaction.operation_date <= date_to,
    )
    totals = await session.execute(
        select(
            CashflowTransaction.direction,
            func.coalesce(func.sum(CashflowTransaction.amount), 0),
        )
        .where(*period_filter)
        .group_by(CashflowTransaction.direction)
    )
    period_in = Decimal("0")
    period_out = Decimal("0")
    for row_direction, total in totals:
        if row_direction == "in":
            period_in += Decimal(total)
        else:
            period_out += Decimal(total)

    stmt = (
        select(CashflowTransaction, DdsArticle)
        .join(DdsArticle, DdsArticle.id == CashflowTransaction.article_id, isouter=True)
        .where(*period_filter)
        .order_by(
            CashflowTransaction.operation_date.desc(),
            CashflowTransaction.created_at.desc(),
        )
        .limit(limit)
    )
    if direction in ("in", "out"):
        stmt = stmt.where(CashflowTransaction.direction == direction)
    if search and search.strip():
        pattern = f"%{search.strip()}%"
        stmt = stmt.where(
            or_(
                CashflowTransaction.payment_purpose.ilike(pattern),
                CashflowTransaction.comment.ilike(pattern),
                DdsArticle.name.ilike(pattern),
            )
        )
    rows = (await session.execute(stmt)).all()

    transactions = [row[0] for row in rows]
    advances = await _load_sources(
        session, SalaryAdvance, transactions, source_kind="salary_advance"
    )
    prepayments = await _load_sources(
        session, SupplierPrepayment, transactions, source_kind="supplier_prepayment"
    )
    employee_names = await _employee_names(session, advances.values())
    counterparty_names = await _counterparty_names(session, transactions)
    freelancer_payouts = await _freelancer_shift_payouts(session, transactions)

    today = kassa_today()
    items: list[dict[str, Any]] = []
    for transaction, article in rows:
        flow: str | None = None
        editable = False
        voidable = False
        label: str | None = None
        employee_id: uuid.UUID | None = None
        if transaction.source_kind == FREELANCER_SHIFT_PAYOUT_SOURCE_KIND:
            # Посменная выдача внештатнику. Правке она не подлежит (сумма учётная, её
            # считает движок), но ошибочную операцию можно отменить целиком — отдельным
            # правом ``kassa.freelancer_shift.void``. Приход-сторно уже отменённой выдачи
            # приходит сюда же и, разумеется, не отменяется повторно.
            payout = freelancer_payouts.get(transaction.source_id)
            flow = "freelancer_shift"
            if payout is not None:
                label = payout["employee_name"]
                employee_id = payout["employee_id"]
                voidable = transaction.direction == "out" and payout["voidable"]
        elif transaction.source_kind == KASSA_PAYOUT_SOURCE_KIND:
            flow = "expense"
            editable = (
                actor_user_id is not None
                and transaction.created_by_user_id == actor_user_id
                and transaction.operation_date == today
            )
        elif transaction.source_kind == KASSA_PAYIN_SOURCE_KIND:
            # Внесение по пресету — приход; правится/удаляется как своя сегодняшняя запись.
            flow = "payin"
            editable = (
                actor_user_id is not None
                and transaction.created_by_user_id == actor_user_id
                and transaction.operation_date == today
            )
        elif transaction.source_kind == "salary_advance":
            advance = advances.get(transaction.source_id)
            if advance is not None:
                flow = "employee_loan" if advance.kind == "loan" else "employee_advance"
                label = employee_names.get(advance.employee_id)
                employee_id = advance.employee_id
                editable = (
                    actor_user_id is not None
                    and advance.created_by_user_id == actor_user_id
                    and transaction.operation_date == today
                    and advance.status == "issued"
                    and _money(advance.recovered_amount) <= 0
                )
        elif transaction.source_kind == "supplier_prepayment":
            prepayment = prepayments.get(transaction.source_id)
            if prepayment is not None:
                flow = "supplier_prepayment"
                editable = (
                    actor_user_id is not None
                    and prepayment.created_by_user_id == actor_user_id
                    and transaction.operation_date == today
                    and prepayment.status == "open"
                    and _money(prepayment.amount_settled) <= 0
                )
        if label is None and transaction.counterparty_id is not None:
            label = counterparty_names.get(transaction.counterparty_id)
        items.append(
            {
                "id": transaction.id,
                "operation_date": transaction.operation_date,
                "direction": transaction.direction,
                "amount": float(_money(transaction.amount)),
                "article_id": transaction.article_id,
                "article_name": article.name if article is not None else None,
                "purpose": transaction.payment_purpose,
                "comment": transaction.comment,
                "source_kind": transaction.source_kind,
                "counterparty_label": label,
                "employee_id": employee_id,
                "counterparty_id": transaction.counterparty_id,
                "kassa_flow": flow,
                "editable": editable,
                # Ошибочную выдачу внештатнику отменяют целиком (деньги + зачёт в ведомости),
                # а не правят — поэтому отдельный флаг, а не расширение `editable`.
                "voidable": voidable,
                "source_id": transaction.source_id,
                "created_at": transaction.created_at,
            }
        )

    # Шапка «в кассе N ₽ · из них целевые M ₽» + бейдж вкладки «К выдаче».
    targets_total = await kassa_targets_total(session, wallet.id)
    pending_advances = await _kassa_pending_advances(session)
    targets_count = await kassa_targets_count(session, wallet.id)
    return {
        "wallet_name": wallet.name,
        "balance": float(balance),
        "period_in": float(_money(period_in)),
        "period_out": float(_money(period_out)),
        "targets_total": float(_money(targets_total)),
        "pending_count": targets_count + len(pending_advances),
        "items": items,
    }


async def _load_sources(
    session: AsyncSession,
    model: type,
    transactions: list[CashflowTransaction],
    *,
    source_kind: str,
) -> dict[uuid.UUID, Any]:
    ids = {
        transaction.source_id
        for transaction in transactions
        if transaction.source_kind == source_kind and transaction.source_id is not None
    }
    if not ids:
        return {}
    rows = (await session.scalars(select(model).where(model.id.in_(ids)))).all()
    return {row.id: row for row in rows}


async def _freelancer_shift_payouts(
    session: AsyncSession, transactions: list[CashflowTransaction]
) -> dict[uuid.UUID, dict[str, Any]]:
    """Подписи и признак отменяемости для строк посменной выдачи внештатнику.

    Ключ — ``source_id`` проводки, то есть явка (``attendance_entry_id``). Отменить можно
    только ДЕЙСТВУЮЩУЮ выдачу (``paid_cash``) неф��нализированного периода: после финализации
    ведомость уже вычла выданное, и вернуть смену «к выдаче» некуда.
    """
    entry_ids = {
        transaction.source_id
        for transaction in transactions
        if transaction.source_kind == FREELANCER_SHIFT_PAYOUT_SOURCE_KIND
        and transaction.source_id is not None
    }
    if not entry_ids:
        return {}
    rows = (
        await session.execute(
            select(
                FreelancerShiftSettlement.attendance_entry_id,
                FreelancerShiftSettlement.employee_id,
                Employee.full_name,
                PayrollPeriod.status,
            )
            .join(Employee, Employee.id == FreelancerShiftSettlement.employee_id)
            .join(PayrollPeriod, PayrollPeriod.id == FreelancerShiftSettlement.period_id)
            .where(
                FreelancerShiftSettlement.attendance_entry_id.in_(entry_ids),
                FreelancerShiftSettlement.status == "paid_cash",
            )
        )
    ).all()
    return {
        entry_id: {
            "employee_id": employee_id,
            "employee_name": full_name,
            "voidable": period_status != "finalized",
        }
        for entry_id, employee_id, full_name, period_status in rows
    }


async def _employee_names(
    session: AsyncSession, advances: list[SalaryAdvance] | Any
) -> dict[uuid.UUID, str]:
    employee_ids = {advance.employee_id for advance in advances}
    if not employee_ids:
        return {}
    rows = await session.execute(
        select(Employee.id, Employee.full_name).where(Employee.id.in_(employee_ids))
    )
    return dict(rows.all())


async def _counterparty_names(
    session: AsyncSession, transactions: list[CashflowTransaction]
) -> dict[uuid.UUID, str]:
    counterparty_ids = {
        transaction.counterparty_id
        for transaction in transactions
        if transaction.counterparty_id is not None
    }
    if not counterparty_ids:
        return {}
    rows = await session.execute(
        select(Counterparty.id, Counterparty.name).where(Counterparty.id.in_(counterparty_ids))
    )
    return dict(rows.all())
