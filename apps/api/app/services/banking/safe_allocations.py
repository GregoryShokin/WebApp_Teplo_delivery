"""Резервы (аллокации) подотчётного счёта «Сейф» — фаза 2.

Резерв (``SafeAllocation``) — намерение потратить часть остатка Сейфа: деньги ещё
на карте, поэтому резерв НЕ создаёт cashflow и НЕ двигает баланс, лишь уменьшает
«свободно» = баланс − Σ(непогашенных резервов). Расход признаётся при оплате:
каждая оплата (в т.ч. частичная) — отдельная out-``CashflowTransaction`` на Сейфе
(``source_kind='safe_payout'``, ``source_id`` = резерв), а ``amount_paid`` копит их
сумму. Инвариант: баланс = свободно + Σ(резерв.amount − резерв.amount_paid).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    BankOperation,
    CashflowTransaction,
    DdsArticle,
    EmployeePayout,
    SafeAllocation,
    SalaryAdvanceBankDraft,
    Wallet,
)
from app.services.banking.classifier import (
    SAFE_WALLET_CODE,
    TRANSFER_IN_ARTICLE_CODE,
    TRANSFER_OUT_ARTICLE_CODE,
    book_safe_topup,
)

SAFE_PAYOUT_SOURCE_KIND = "safe_payout"
SAFE_CASH_WITHDRAWAL_SOURCE_KIND = "safe_cash_withdrawal"
SAFE_RECONCILE_ADJUST_SOURCE_KIND = "safe_reconcile_adjust"
# Двухногое перемещение при передаче целёвки Сейф → касса (source_id = резерв).
ALLOCATION_TO_KASSA_SOURCE_KIND = "safe_allocation_to_kassa"
# Обратное перемещение целёвки касса → Сейф (source_id = резерв).
ALLOCATION_TO_SAFE_SOURCE_KIND = "kassa_allocation_to_safe"
# Внутренний перевод между наличными счетами (Сейф↔Касса); source_id связывает пару ног.
INTERNAL_TRANSFER_SOURCE_KIND = "internal_transfer_manual"
# Единая статья ДДС «Внутренний перевод» (movement_type=internal): направление ноги
# (in/out) само различает поступление/списание — двух отдельных статей не нужно.
INTERNAL_TRANSFER_ARTICLE_CODE = "internal_transfer"
# Выдача целёвки наличными из кассы, вкладка «К выдаче» (source_id = резерв).
KASSA_TARGET_PAYOUT_SOURCE_KIND = "kassa_target_payout"
ACTIVE_RESERVE_STATUSES = ("reserved", "partially_paid")
# Куда снимаются наличные с карты «Сейф» (параллельный наличный счёт).
CASH_WITHDRAWAL_WALLET_CODE = "tk_chernikova"
# Корректировка дрейфа при сверке: недостача → расход, излишек → поступление.
DRIFT_SHORTAGE_ARTICLE_CODE = "prochie_rashody"
DRIFT_SURPLUS_ARTICLE_CODE = "prochie_postupleniya"


async def _article_id(session: AsyncSession, code: str) -> UUID:
    article_id = await session.scalar(select(DdsArticle.id).where(DdsArticle.code == code))
    if article_id is None:
        raise ValueError(f"Не найдена статья ДДС «{code}»")
    return article_id


async def safe_reserved_total(session: AsyncSession, wallet_id: UUID) -> Decimal:
    """Σ непогашенных остатков активных резервов кошелька (для «зарезервировано»)."""
    outstanding = func.sum(SafeAllocation.amount - SafeAllocation.amount_paid)
    total = await session.scalar(
        select(func.coalesce(outstanding, 0)).where(
            SafeAllocation.wallet_id == wallet_id,
            SafeAllocation.status.in_(ACTIVE_RESERVE_STATUSES),
        )
    )
    return Decimal(total or 0)


async def safe_active_allocations_count(session: AsyncSession, wallet_id: UUID) -> int:
    """Число активных резервов кошелька — для бейджа «N целевых» на плитке Сейфа."""
    count = await session.scalar(
        select(func.count())
        .select_from(SafeAllocation)
        .where(
            SafeAllocation.wallet_id == wallet_id,
            SafeAllocation.status.in_(ACTIVE_RESERVE_STATUSES),
        )
    )
    return int(count or 0)


async def create_allocation(
    session: AsyncSession,
    *,
    wallet_id: UUID,
    amount: Decimal,
    free_amount: Decimal | None,
    article_id: UUID | None = None,
    counterparty_id: UUID | None = None,
    employee_id: UUID | None = None,
    purpose: str | None = None,
    source_draft_id: UUID | None = None,
    source_draft_line_id: UUID | None = None,
    source_operation_id: UUID | None = None,
    source_run_id: UUID | None = None,
    location: str = "safe",
    location_id: UUID | None = None,
    lease_id: UUID | None = None,
    asset_id: UUID | None = None,
    created_by_user_id: UUID | None = None,
) -> SafeAllocation:
    """Создать резерв. Запрет перерезервирования: ``amount`` ≤ свободно (``free_amount``).

    ``free_amount=None`` — без проверки: авто-резерв под оплаченный банковский черновик
    (``source_draft_id``), где перевод р/с→Сейф уже пополнил баланс ровно на эту сумму.
    ``employee_id`` — зарплатная целёвка «через Сейф»: при оплате резерва заведётся EmployeePayout.
    ``source_run_id`` — резерв-контейнер выплаты ЗП, привязанный к ведомости (при
    ``employee_id=None`` это ПУЛ, cashflow сам не книжит — см. ``payroll_reserves``).
    ``location`` — на каком счёте резерв заводится сразу (``safe``/``kassa``): наличный пул ЗП
    заводится сразу на кассе, без перемещения.
    """
    if amount <= 0:
        raise ValueError("Сумма резерва должна быть больше нуля")
    if free_amount is not None and amount > free_amount:
        raise ValueError(
            f"Недостаточно свободных средств на Сейфе: свободно {free_amount}, запрошено {amount}"
        )
    allocation = SafeAllocation(
        wallet_id=wallet_id,
        amount=amount,
        amount_paid=Decimal("0"),
        article_id=article_id,
        counterparty_id=counterparty_id,
        employee_id=employee_id,
        purpose=purpose,
        source_draft_id=source_draft_id,
        source_draft_line_id=source_draft_line_id,
        source_operation_id=source_operation_id,
        source_run_id=source_run_id,
        status="reserved",
        location=location,
        location_id=location_id,
        lease_id=lease_id,
        asset_id=asset_id,
        created_by_user_id=created_by_user_id,
    )
    session.add(allocation)
    await session.flush()
    return allocation


async def book_safe_topup_reserves(
    session: AsyncSession,
    operation: BankOperation,
    *,
    reserves: list[tuple[UUID, Decimal, UUID | None]],
    counterparty_id: UUID | None = None,
    created_by_user_id: UUID | None = None,
) -> list[SafeAllocation]:
    """Разбор операции «через Сейф»: транзит р/с→Сейф + целёвки-резервы по разметке.

    Топ-ап (``book_safe_topup``) пополняет Сейф на всю сумму операции; на каждую размеченную
    строку (``reserves``: article_id, amount, employee_id) создаётся резерв ``SafeAllocation``
    со статьёй/получателем. Фактический расход — позже, по «Выплачено» (``pay_allocation``):
    out-проводка ``safe_payout``, а у зарплатного резерва (``employee_id``) ещё и
    ``EmployeePayout`` под сотрудника — тогда расчёт ЗП учтёт «уже выплачено».

    Идемпотентно: прежние НЕоплаченные резервы этой операции снимаем; если по любому уже была
    оплата — блокируем (деньги с Сейфа уже выданы)."""
    prior = (
        await session.scalars(
            select(SafeAllocation).where(SafeAllocation.source_operation_id == operation.id)
        )
    ).all()
    for allocation in prior:
        if Decimal(allocation.amount_paid) > 0 or allocation.status in ("paid", "partially_paid"):
            raise ValueError(
                "По этой операции уже была выплата с Сейфа — повторный разбор невозможен"
            )
    for allocation in prior:
        await session.delete(allocation)
    if prior:
        await session.flush()

    await book_safe_topup(session, operation)  # транзит р/с→Сейф (идемпотентно чистит свои ноги)
    safe_wallet = await session.scalar(
        select(Wallet).where(Wallet.code == SAFE_WALLET_CODE, Wallet.status == "active")
    )
    if safe_wallet is None:
        raise ValueError("Не найден кошелёк «Сейф»")
    created: list[SafeAllocation] = []
    for article_id, amount, employee_id in reserves:
        created.append(
            await create_allocation(
                session,
                wallet_id=safe_wallet.id,
                amount=amount,
                free_amount=None,  # транзит только что пополнил Сейф на всю сумму операции
                article_id=article_id,
                counterparty_id=counterparty_id,
                employee_id=employee_id,
                purpose=operation.payment_purpose,
                source_operation_id=operation.id,
                created_by_user_id=created_by_user_id,
            )
        )
    return created


async def pay_allocation(
    session: AsyncSession,
    allocation: SafeAllocation,
    *,
    amount: Decimal,
    operation_date: date,
    source_kind: str = SAFE_PAYOUT_SOURCE_KIND,
    created_by_user_id: UUID | None = None,
) -> UUID:
    """Провести оплату резерва (полную или частичную): out-проводка + обновить статус.

    Кошелёк ноги — текущий ``allocation.wallet_id``: у резерва на Сейфе это Сейф,
    у переданного в кассу — касса (``source_kind='kassa_target_payout'``).
    """
    if allocation.status == "cancelled":
        raise ValueError("Резерв отменён — оплата невозможна")
    if allocation.status == "paid":
        raise ValueError("Резерв уже полностью оплачен")
    if amount <= 0:
        raise ValueError("Сумма оплаты должна быть больше нуля")
    outstanding = Decimal(allocation.amount) - Decimal(allocation.amount_paid)
    if amount > outstanding:
        raise ValueError(f"Сумма оплаты ({amount}) больше остатка резерва ({outstanding})")

    leg = CashflowTransaction(
        wallet_id=allocation.wallet_id,
        direction="out",
        amount=amount,
        operation_date=operation_date,
        article_id=allocation.article_id,
        counterparty_id=allocation.counterparty_id,
        location_id=allocation.location_id,
        lease_id=allocation.lease_id,
        source_kind=source_kind,
        source_id=allocation.id,
        payment_purpose=allocation.purpose,
        quality_status="final",
        created_by_user_id=created_by_user_id,
    )
    session.add(leg)
    allocation.amount_paid = Decimal(allocation.amount_paid) + amount
    allocation.status = (
        "paid" if allocation.amount_paid >= Decimal(allocation.amount) else "partially_paid"
    )
    await session.flush()

    # КОНЕЦ ПУТИ ОБЪЕКТА: строка «Нового платежа» → резерв → эта проводка. Только здесь
    # появляется то, к чему привязку вообще можно прицепить, — сама проводка ДДС.
    #
    # Частичная оплата резерва даёт НЕСКОЛЬКО проводок, и каждая получает свою связь на свою
    # сумму: объект должен собрать все деньги, которыми за него заплатили, а не только первый
    # транш. Идемпотентность держит уникальный индекс по тройке «объект, проводка, вид связи».
    if allocation.asset_id is not None:
        # Локальный импорт — против кольца: asset_analytics тянет fixed_assets, а тот банк.
        # DdsArticle НЕ импортируем повторно: локальный импорт затенил бы модульный на всю
        # функцию, и обращение к нему ВЫШЕ по коду упало бы UnboundLocalError.
        from app.services.asset_analytics import link_transaction_to_asset, resolve_asset_context

        article = (
            await session.get(DdsArticle, allocation.article_id)
            if allocation.article_id is not None
            else None
        )
        context = await resolve_asset_context(
            session, article=article, asset_id=allocation.asset_id, amount=amount
        )
        await link_transaction_to_asset(
            session, context=context, transaction_id=leg.id, amount=amount
        )

    # Аренда: наличная оплата гасит ДЕЙСТВУЮЩИЕ обязательства этого договора, остаток становится
    # дебиторкой (аренда вперёд). Банковский платёж проходит обе половины канона сам (правило 1),
    # а наличный контур правило 1 не зовёт — см. settle_lease_invoice_from_cash.
    if allocation.lease_id is not None:
        from app.services.lease_accruals import settle_lease_invoice_from_cash

        await settle_lease_invoice_from_cash(
            session,
            lease_id=allocation.lease_id,
            transaction_id=leg.id,
            amount=amount,
            wallet_id=allocation.wallet_id,
            created_by_user_id=created_by_user_id,
        )

    # Коммуналка: наличная выдача арендодателю гасит долг его потока (вода/газ/свет) и, если
    # заплатили вперёд, оставляет остаток дебиторкой. Без этой ветки основной канал расчётов с
    # арендодателем — наличные из Сейфа — кредиторку не трогал вовсе.
    utility_handled = False
    if allocation.lease_id is None and allocation.counterparty_id is not None:
        from app.services.utility_charges import settle_utility_invoices_from_cash

        utility_handled = await settle_utility_invoices_from_cash(
            session,
            counterparty_id=allocation.counterparty_id,
            article_id=allocation.article_id,
            location_id=allocation.location_id,
            transaction_id=leg.id,
            amount=amount,
            wallet_id=allocation.wallet_id,
            created_by_user_id=created_by_user_id,
        )

    # СТРАХОВКА: наличная выплата УСЛУГОВОМУ контрагенту обязана оставить след в ДЗ/КЗ, даже
    # когда ни один адресный механизм её не подхватил. Между ветками выше есть щель, и деньги
    # в неё проваливаются молча: 08.07.2026 из Сейфа ушло 9 879 ₽ «За воду» Станиславу
    # Юрьевичу, но лицевой счёт «Вода» завели 03.08 — позже платежа. Коммунальная ветка не
    # нашла потока, вышла ни с чем, и деньги остались вне расчётов вовсе. Дальше пришедшая
    # платёжка закрылась чужим авансом — АРЕНДОЙ за август, — и 31.08 закрывающий по аренде
    # выставил бы к оплате 9 654,25 ₽ за уже оплаченный месяц.
    #
    # Гейт — ``service_billing_mode``: расход такого контрагента закрывается признанием, а не
    # платежом, значит деньги вперёд это дебиторка по построению. У поставщика ТОВАРА режим
    # пуст, его платёж закрывает накладную, и правилу 1 здесь делать нечего.
    if (
        allocation.lease_id is None
        and allocation.counterparty_id is not None
        and not utility_handled
    ):
        from app.models import CounterpartyPayableProfile
        from app.services.supplier_prepayments import sync_manual_payment_receivable

        billing_mode = await session.scalar(
            select(CounterpartyPayableProfile.service_billing_mode).where(
                CounterpartyPayableProfile.counterparty_id == allocation.counterparty_id
            )
        )
        if billing_mode is not None:
            # ``money_is_free=True`` осознанно: адресные механизмы выше уже отработали и
            # деньгами не распорядились — считать свободу заново значило бы упереться в гейт
            # ``safe_payout``, который и создал эту щель.
            await sync_manual_payment_receivable(session, leg, money_is_free=True)

    # Резерв предоплаты поставщику (статья «Авансы поставщикам» + контрагент):
    # выплата резерва — момент возникновения дебиторки, заводим SupplierPrepayment.
    # Код статьи продублирован из supplier_prepayments (циклический импорт через kassa).
    # Арендный резерв сюда не заходит: его деньгами уже распорядился блок выше — и гашением
    # действующих обязательств, и остатком в дебиторку. Иначе на одну выплату две предоплаты.
    if (
        allocation.lease_id is None
        and allocation.article_id is not None
        and allocation.counterparty_id is not None
    ):
        article = await session.get(DdsArticle, allocation.article_id)
        if article is not None and article.code == "advance_to_supplier":
            from app.models import SupplierPrepayment

            session.add(
                SupplierPrepayment(
                    counterparty_id=allocation.counterparty_id,
                    kind="goods",
                    wallet_id=allocation.wallet_id,
                    amount=amount,
                    amount_settled=Decimal("0.00"),
                    status="open",
                    cashflow_transaction_id=leg.id,
                    article_id=allocation.article_id,
                    note=allocation.purpose,
                    created_by_user_id=created_by_user_id,
                )
            )
            await session.flush()

    # Зарплатная целёвка «через Сейф»: фактическая выплата сотруднику — здесь (не при разборе).
    # Заводим EmployeePayout(«выплачено») на эту (в т.ч. частичную) оплату → расчёт ЗП учтёт.
    if allocation.employee_id is not None:
        from app.services.payroll_admin import resolve_employee_payout_kind

        payout_kind = await resolve_employee_payout_kind(
            session, allocation.employee_id, as_of=operation_date
        )
        session.add(
            EmployeePayout(
                employee_id=allocation.employee_id,
                kind=payout_kind,
                amount=amount,
                payout_date=operation_date,
                wallet_id=allocation.wallet_id,
                article_id=allocation.article_id,
                cashflow_transaction_id=leg.id,
                safe_allocation_id=allocation.id,
                status="paid",
                created_by_user_id=created_by_user_id,
            )
        )
        await session.flush()
    # Резерв под банковский закуп у неофициала (source_draft_id): выплата наличных поставщику —
    # момент гашения его накладных («Деньги в Сейфе» → «Оплачено»).
    await _settle_draft_invoices(
        session,
        allocation=allocation,
        amount=amount,
        leg_id=leg.id,
        created_by_user_id=created_by_user_id,
    )
    return leg.id


async def _settle_draft_invoices(
    session: AsyncSession,
    *,
    allocation: SafeAllocation,
    amount: Decimal,
    leg_id: UUID,
    created_by_user_id: UUID | None,
) -> None:
    """Гашение накладных при выплате резерва «через Сейф» (неофициальный поставщик).

    Накладные банковского via-safe черновика НЕ гасятся при исполнении платежа (деньги лишь
    дошли до Сейфа — UI показывает «Деньги в Сейфе»); оплаченными они становятся здесь, когда
    наличные фактически выданы поставщику. Сумму выплаты разносим по накладным черновика
    (``draft_id = source_draft_id``) FIFO по остатку. У резервов без черновика-закупа
    (целёвки, транши «Нового платежа») накладных нет — no-op.

    Фильтра ``activation_status`` здесь СОЗНАТЕЛЬНО нет — в отличие от арендного гашения
    (``lease_accruals.settle_lease_invoice_from_cash``, где будущий документ надо пропустить, а
    деньги оставить дебиторкой). Инвариант держится выше: не вступивший в силу закрывающий
    документ не пускается в банковский черновик (``create_payment_draft_for_invoices``), а
    пере-разбор письма документ в черновике не передатирует. Повторить здесь арендное решение
    НЕЛЬЗЯ: draft_id у оплаченного черновика не снимается, и заведённая предоплата никогда не
    будет зачтена (``auto_settle_invoice_from_open_prepayments`` отсекает документы с draft_id),
    то есть на одни деньги повисли бы и дебиторка, и кредиторка.
    """
    if allocation.source_draft_id is None or amount <= 0:
        return
    # Локальные импорты — низкоуровневый Сейф-модуль не тянет контрагентский контур на загрузке.
    from app.models import InvoicePaymentAllocation, SupplierInvoice
    from app.services.counterparty_matching import _invoice_remaining, _recompute_status

    invoices = (
        await session.scalars(
            select(SupplierInvoice)
            .where(SupplierInvoice.draft_id == allocation.source_draft_id)
            .order_by(SupplierInvoice.invoice_date.nulls_last(), SupplierInvoice.created_at)
        )
    ).all()
    left = amount
    for invoice in invoices:
        if left <= 0:
            break
        remaining = await _invoice_remaining(session, invoice)
        if remaining <= 0:
            continue
        part = min(left, remaining)
        session.add(
            InvoicePaymentAllocation(
                invoice_id=invoice.id,
                source_kind="cash",
                cashflow_transaction_id=leg_id,
                amount=part,
                created_by_user_id=created_by_user_id,
            )
        )
        await session.flush()
        await _recompute_status(session, invoice)
        left -= part


async def cancel_allocation(session: AsyncSession, allocation: SafeAllocation) -> None:
    """Отменить резерв. Оплаченные ноги остаются, неоплаченный остаток освобождается.

    Для целёвки, переданной в кассу, отмена работает так же: резерв снимается,
    деньги остаются в кассе свободными наличными, обратных проводок нет
    (возврат на Сейф — руками обычным переводом, вне этого контура).
    """
    if allocation.status == "paid":
        raise ValueError("Нельзя отменить полностью оплаченный резерв")
    if allocation.status == "cancelled":
        return
    allocation.status = "cancelled"
    await session.flush()


async def kassa_targets_total(session: AsyncSession, kassa_wallet_id: UUID) -> Decimal:
    """Σ непогашенных остатков целёвок, переданных в кассу («целевые в кассе»)."""
    outstanding = func.sum(SafeAllocation.amount - SafeAllocation.amount_paid)
    total = await session.scalar(
        select(func.coalesce(outstanding, 0)).where(
            SafeAllocation.wallet_id == kassa_wallet_id,
            SafeAllocation.location == "kassa",
            SafeAllocation.status.in_(ACTIVE_RESERVE_STATUSES),
        )
    )
    return Decimal(total or 0)


async def kassa_targets_count(session: AsyncSession, kassa_wallet_id: UUID) -> int:
    """Число активных целёвок в кассе — для бейджа «К выдаче» и плитки кассы."""
    count = await session.scalar(
        select(func.count())
        .select_from(SafeAllocation)
        .where(
            SafeAllocation.wallet_id == kassa_wallet_id,
            SafeAllocation.location == "kassa",
            SafeAllocation.status.in_(ACTIVE_RESERVE_STATUSES),
        )
    )
    return int(count or 0)


async def allocation_advance_draft_id(
    session: AsyncSession, allocation_id: UUID
) -> UUID | None:
    """Черновик банк-выдачи аванса, привязанный к резерву (таким передача запрещена)."""
    return await session.scalar(
        select(SalaryAdvanceBankDraft.id).where(
            SalaryAdvanceBankDraft.safe_allocation_id == allocation_id
        )
    )


async def transfer_allocation_to_kassa(
    session: AsyncSession,
    allocation: SafeAllocation,
    *,
    operation_date: date,
) -> list[UUID]:
    """Передать целёвку в кассу: перемещение ВСЕГО остатка Сейф → касса + смена локации.

    Двухногий перевод (по механике снятия наличных) на непогашенный остаток резерва —
    частичная передача запрещена by design (частичной бывает только выдача из кассы).
    Резерв меняет ``wallet_id`` на кассу и ``location`` на ``kassa``: он перестаёт
    считаться в резервах Сейфа (инвариант баланса Сейфа сходится, свободно не меняется)
    и появляется в «целевых в кассе». Вызывающий обязан держать row-lock резерва
    (повторная передача отсекается проверкой локации).
    """
    if allocation.status not in ACTIVE_RESERVE_STATUSES:
        raise ValueError("Передать в кассу можно только активный резерв")
    if allocation.location != "safe":
        raise ValueError("Резерв уже передан в кассу")
    outstanding = Decimal(allocation.amount) - Decimal(allocation.amount_paid)
    if outstanding <= 0:
        raise ValueError("У резерва нет непогашенного остатка")
    # Страховка идемпотентности поверх row-lock: одна целёвка — одно перемещение.
    existing = await session.scalar(
        select(CashflowTransaction.id).where(
            CashflowTransaction.source_kind == ALLOCATION_TO_KASSA_SOURCE_KIND,
            CashflowTransaction.source_id == allocation.id,
        )
    )
    if existing is not None:
        raise ValueError("Резерв уже передан в кассу")
    dest_wallet = await session.scalar(
        select(Wallet).where(
            Wallet.code == CASH_WITHDRAWAL_WALLET_CODE, Wallet.status == "active"
        )
    )
    if dest_wallet is None:
        raise ValueError("Не найдена наличная касса для передачи")
    out_article = await _article_id(session, TRANSFER_OUT_ARTICLE_CODE)
    in_article = await _article_id(session, TRANSFER_IN_ARTICLE_CODE)
    label = allocation.purpose or "целевой резерв"
    purpose = f"Передача целёвки в кассу: {label}"
    out_leg = CashflowTransaction(
        wallet_id=allocation.wallet_id,
        direction="out",
        amount=outstanding,
        operation_date=operation_date,
        article_id=out_article,
        source_kind=ALLOCATION_TO_KASSA_SOURCE_KIND,
        source_id=allocation.id,
        payment_purpose=purpose,
        quality_status="final",
    )
    in_leg = CashflowTransaction(
        wallet_id=dest_wallet.id,
        direction="in",
        amount=outstanding,
        operation_date=operation_date,
        article_id=in_article,
        source_kind=ALLOCATION_TO_KASSA_SOURCE_KIND,
        source_id=allocation.id,
        payment_purpose=purpose,
        quality_status="final",
    )
    session.add(out_leg)
    session.add(in_leg)
    allocation.wallet_id = dest_wallet.id
    allocation.location = "kassa"
    await session.flush()
    return [out_leg.id, in_leg.id]


async def move_allocation_location(
    session: AsyncSession,
    allocation: SafeAllocation,
    *,
    to_location: str,
    operation_date: date,
) -> list[UUID]:
    """Переместить целёвку между Сейфом и Кассой (обе стороны), ВЕСЬ остаток.

    Двухногий перевод непогашенного остатка между счетами + смена ``wallet_id``/
    ``location``. Инвариант «свободно не меняется» держится: in-нога пополняет
    счёт-получатель ровно на остаток резерва, поэтому свободный остаток каждого счёта
    не меняется. Идемпотентность — по несовпадению ``location`` + row-lock резерва
    (вызывающий держит ``with_for_update``): повторное перемещение в ту же сторону
    отсекается проверкой локации, поэтому туда-обратно работает без «вечного» гейта.
    Частичное перемещение запрещено (как и передача в кассу).
    """
    if to_location not in ("safe", "kassa"):
        raise ValueError("Неизвестное направление перемещения")
    if allocation.status not in ACTIVE_RESERVE_STATUSES:
        raise ValueError("Перемещать можно только активный резерв")
    if allocation.location == to_location:
        raise ValueError("Резерв уже на этом счёте")
    outstanding = Decimal(allocation.amount) - Decimal(allocation.amount_paid)
    if outstanding <= 0:
        raise ValueError("У резерва нет непогашенного остатка")
    dest_code = SAFE_WALLET_CODE if to_location == "safe" else CASH_WITHDRAWAL_WALLET_CODE
    dest_wallet = await session.scalar(
        select(Wallet).where(Wallet.code == dest_code, Wallet.status == "active")
    )
    if dest_wallet is None:
        raise ValueError("Не найден счёт-получатель")
    source_kind = (
        ALLOCATION_TO_KASSA_SOURCE_KIND
        if to_location == "kassa"
        else ALLOCATION_TO_SAFE_SOURCE_KIND
    )
    # Единая статья «Внутренний перевод»: направление ноги различает списание/поступление.
    article = await _article_id(session, INTERNAL_TRANSFER_ARTICLE_CODE)
    label = allocation.purpose or "целевой резерв"
    dest_name = "Сейф" if to_location == "safe" else "касса"
    purpose = f"Внутренний перевод (резерв): {label} → {dest_name}"
    out_leg = CashflowTransaction(
        wallet_id=allocation.wallet_id,
        direction="out",
        amount=outstanding,
        operation_date=operation_date,
        article_id=article,
        source_kind=source_kind,
        source_id=allocation.id,
        payment_purpose=purpose,
        quality_status="final",
    )
    in_leg = CashflowTransaction(
        wallet_id=dest_wallet.id,
        direction="in",
        amount=outstanding,
        operation_date=operation_date,
        article_id=article,
        source_kind=source_kind,
        source_id=allocation.id,
        payment_purpose=purpose,
        quality_status="final",
    )
    session.add(out_leg)
    session.add(in_leg)
    allocation.wallet_id = dest_wallet.id
    allocation.location = to_location
    await session.flush()
    return [out_leg.id, in_leg.id]


async def book_internal_transfer(
    session: AsyncSession,
    *,
    source_wallet: Wallet,
    dest_wallet: Wallet,
    amount: Decimal,
    purpose: str | None,
    operation_date: date,
) -> UUID:
    """Внутренний перевод денег между наличными счетами (Сейф↔Касса): двухногая проводка.

    out на счёте-источнике + in на счёте-получателе (статьи «перевод между счетами», в
    P&L не попадают). Обе ноги связаны общим ``source_id`` (id перевода). Не резервирует
    и не трогает выписку банка — только наличный контур. Возвращает id перевода.
    """
    if source_wallet.id == dest_wallet.id:
        raise ValueError("Счёт-источник и счёт-получатель совпадают")
    if amount <= 0:
        raise ValueError("Сумма перевода должна быть больше нуля")
    transfer_id = uuid4()
    # Единая статья «Внутренний перевод»: направление ноги различает списание/поступление.
    article = await _article_id(session, INTERNAL_TRANSFER_ARTICLE_CODE)
    note = f" · {purpose.strip()}" if purpose and purpose.strip() else ""
    out_leg = CashflowTransaction(
        wallet_id=source_wallet.id,
        direction="out",
        amount=amount,
        operation_date=operation_date,
        article_id=article,
        source_kind=INTERNAL_TRANSFER_SOURCE_KIND,
        source_id=transfer_id,
        payment_purpose=f"Внутренний перевод → {dest_wallet.name}{note}",
        quality_status="final",
    )
    in_leg = CashflowTransaction(
        wallet_id=dest_wallet.id,
        direction="in",
        amount=amount,
        operation_date=operation_date,
        article_id=article,
        source_kind=INTERNAL_TRANSFER_SOURCE_KIND,
        source_id=transfer_id,
        payment_purpose=f"Внутренний перевод ← {source_wallet.name}{note}",
        quality_status="final",
    )
    session.add(out_leg)
    session.add(in_leg)
    await session.flush()
    return transfer_id


async def book_safe_cash_withdrawal(
    session: AsyncSession,
    *,
    safe_wallet: Wallet,
    amount: Decimal,
    operation_date: date,
) -> list[UUID]:
    """Снятие наличных с карты «Сейф» → касса Черникова (двухногий перевод между счетами).

    Не расход, а перемещение: out с Сейфа + in в наличную кассу. Деньги остаются в
    учётном контуре до фактической траты и не искажают сверку остатка карты.
    """
    if amount <= 0:
        raise ValueError("Сумма снятия должна быть больше нуля")
    dest_wallet = await session.scalar(
        select(Wallet).where(
            Wallet.code == CASH_WITHDRAWAL_WALLET_CODE, Wallet.status == "active"
        )
    )
    if dest_wallet is None:
        raise ValueError("Не найдена наличная касса для снятия")
    out_article = await _article_id(session, TRANSFER_OUT_ARTICLE_CODE)
    in_article = await _article_id(session, TRANSFER_IN_ARTICLE_CODE)
    source_id = uuid4()
    purpose = "Снятие наличных с Сейфа в кассу"
    out_leg = CashflowTransaction(
        wallet_id=safe_wallet.id,
        direction="out",
        amount=amount,
        operation_date=operation_date,
        article_id=out_article,
        source_kind=SAFE_CASH_WITHDRAWAL_SOURCE_KIND,
        source_id=source_id,
        payment_purpose=purpose,
        quality_status="final",
    )
    in_leg = CashflowTransaction(
        wallet_id=dest_wallet.id,
        direction="in",
        amount=amount,
        operation_date=operation_date,
        article_id=in_article,
        source_kind=SAFE_CASH_WITHDRAWAL_SOURCE_KIND,
        source_id=source_id,
        payment_purpose=purpose,
        quality_status="final",
    )
    session.add(out_leg)
    session.add(in_leg)
    await session.flush()
    return [out_leg.id, in_leg.id]


async def book_safe_drift_adjustment(
    session: AsyncSession,
    *,
    safe_wallet: Wallet,
    delta: Decimal,
    operation_date: date,
) -> UUID:
    """Выровнять учётный баланс Сейфа на реальный при сверке (``delta`` = реальный − учётный).

    ``delta`` > 0 — излишек (нераспознанное поступление) → in «Прочие поступления»;
    ``delta`` < 0 — недостача (трата мимо учёта) → out «Прочие расходы».
    """
    if delta == 0:
        raise ValueError("Нет расхождения для корректировки")
    if delta > 0:
        direction = "in"
        article = await _article_id(session, DRIFT_SURPLUS_ARTICLE_CODE)
    else:
        direction = "out"
        article = await _article_id(session, DRIFT_SHORTAGE_ARTICLE_CODE)
    leg = CashflowTransaction(
        wallet_id=safe_wallet.id,
        direction=direction,
        amount=abs(delta),
        operation_date=operation_date,
        article_id=article,
        source_kind=SAFE_RECONCILE_ADJUST_SOURCE_KIND,
        source_id=uuid4(),
        payment_purpose="Корректировка остатка Сейфа по сверке с картой",
        quality_status="final",
    )
    session.add(leg)
    await session.flush()
    return leg.id
