"""Авто-гашение накладных по статусу банковского платежа (T-Банк).

Заменяет ручной мэчинг выписки: статус платежа (из polling ``GET /payment/{id}`` или
webhook «Статус платежа») двигает черновик ``CounterpartyPaymentDraft`` и привязанные к
нему накладные. Сопоставление — по ``provider_ref`` (идентификатор платежа у банка), без
сумм/реквизитов.

Выписка (``BankOperation``) здесь НЕ нужна: при статусе «исполнен» аллокация создаётся без
``bank_operation_id`` (CHECK ``ck_invoice_allocation_single_source`` допускает оба NULL) —
это «оплата подтверждена статусом платежа». Денежное движение ДДС формируется отдельно при
классификации выписки; двойного учёта нет, так как старый auto-match по сумме снят.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import (
    Account,
    CashflowTransaction,
    CounterpartyPaymentDraft,
    DdsArticle,
    InvoicePaymentAllocation,
    ReconciliationCase,
    SupplierPrepayment,
    Wallet,
)
from app.services.banking.base import clean_digits
from app.services.counterparty_matching import (
    _draft_invoices,
    _invoice_remaining,
    _recompute_status,
)

logger = logging.getLogger(__name__)

# Статья ДДС для оплаты поставщику по умолчанию (та же, что у ручной оплаты с кошелька).
_SUPPLIER_ARTICLE_CODE = "payment_to_supplier"


async def _resolve_payer_bank_wallet(
    session: AsyncSession, draft: CounterpartyPaymentDraft
) -> Wallet | None:
    """Банк-кошелёк, с которого ушёл платёж (по номеру счёта плательщика из тела черновика
    или из настроек). Нужен, чтобы завести prebooked-проводку оплаты, которую заберёт
    приходящая операция выписки (prebooked-claim) — иначе операция уходит в needs_review."""
    account_number = None
    if isinstance(draft.payload, dict):
        account_number = draft.payload.get("accountNumber")
    account_number = clean_digits(account_number or get_settings().tbank_api_account_number or "")
    if not account_number:
        return None
    return await session.scalar(
        select(Wallet)
        .join(Account, Account.id == Wallet.account_id)
        .where(
            Account.account_number == account_number,
            Wallet.type == "bank",
            Wallet.status == "active",
        )
    )

# Нормализованные строки статусов банка → исход. Точные значения T-Банка уточняются при
# подключении (банк присылает схему); неизвестный статус оставляет платёж «в банке».
PAID_STATUSES = frozenset(
    {
        "executed",
        "paid",
        "completed",
        "complete",
        "success",
        "successful",
        "done",
        "исполнен",
        "исполнено",
        "оплачен",
        "оплачено",
        "проведен",
        "проведён",
        "проведено",
    }
)
FAILED_STATUSES = frozenset(
    {
        "declined",
        "rejected",
        "canceled",
        "cancelled",
        "failed",
        "error",
        "отклонен",
        "отклонён",
        "отклонено",
        "отменен",
        "отменён",
        "отменено",
        "ошибка",
    }
)
# Удаление платёжного черновика в банке (T-Банк отдаёт статус ``DELETED`` по
# ``POST /payment/status``): черновик отозван/не подписан, деньги НЕ ушли. Накладные
# возвращаем в «неоплачено». Отделено от ``failed`` (отклонён банком) ради ясности статуса.
DELETED_STATUSES = frozenset(
    {
        "deleted",
        "removed",
        "удален",
        "удалён",
        "удалена",
        "удалено",
    }
)


def classify_payment_status(raw_status: str | None) -> str:
    """Свести строку статуса банка к ``paid`` / ``failed`` / ``deleted`` / ``pending``."""
    value = (raw_status or "").strip().lower()
    if value in PAID_STATUSES:
        return "paid"
    if value in DELETED_STATUSES:
        return "deleted"
    if value in FAILED_STATUSES:
        return "failed"
    return "pending"


async def apply_payment_status(
    session: AsyncSession,
    *,
    draft: CounterpartyPaymentDraft,
    raw_status: str | None,
    operation_date: date | None = None,
    actor_user_id: uuid.UUID | None = None,
    commit: bool = True,
) -> str:
    """Продвинуть черновик и его накладные по статусу платежа. Идемпотентно: повторный
    «исполнен» по оплаченному черновику — no-op. Возвращает итоговый статус черновика.

    ``operation_date`` — дата фактической банковской операции (есть при гашении из вебхука
    «операция по счёту»); ею датируем prebooked-проводку ДДС, чтобы она совпала с приходящей
    операцией выписки (точный prebooked-claim). При поллинге даты нет → дата обработки."""
    outcome = classify_payment_status(raw_status)

    # Сериализуем обработку этого черновика блокировкой строки: закрывает гонку webhook↔polling
    # и дубль-доставку webhook (иначе обе транзакции прошли бы in-memory guard и создали по
    # аллокации → двойное гашение). Вторая транзакция дождётся коммита первой и увидит paid/failed.
    draft = await session.get(CounterpartyPaymentDraft, draft.id, with_for_update=True)
    if draft is None:
        return "created"

    # Переход разрешён только из активных статусов — это и идемпотентность (paid→paid no-op), и
    # защита от реанимации: out-of-order «исполнен» после «отклонён» не воскрешает failed-черновик
    # с уже отвязанными накладными.
    if outcome == "paid" and draft.status in ("created", "updated"):
        # Гасим остаток каждой накладной. Если знаем банк-кошелёк плательщика — заводим
        # prebooked-проводку «Оплата поставщикам» (её заберёт приходящая операция выписки через
        # prebooked-claim → операция не уходит в needs_review, ДДС-аналитика наполняется,
        # накладная↔операция связаны). Иначе fallback: статус-аллокация без bank_operation_id.
        from app.services.counterparty_payments import _apply_wallet_payment

        bank_wallet = await _resolve_payer_bank_wallet(session, draft)
        supplier_article_id = await session.scalar(
            select(DdsArticle.id).where(DdsArticle.code == _SUPPLIER_ARTICLE_CODE)
        )
        draft_invoices = await _draft_invoices(session, draft.id)
        op_date = operation_date or datetime.now(UTC).date()
        if bank_wallet is None and (draft_invoices or draft.creates_prepayment):
            # Не нашли банк-кошелёк плательщика (счёт из тела черновика/настроек не привязан к
            # активному bank-Wallet). Платёж всё равно фиксируем — деньги УЖЕ ушли из банка,
            # блокировать нельзя; но prebooked-проводку ДДС завести не на чем. Громкий лог +
            # durable-маркер в payload + видимый кейс owner-review, чтобы пропавший prebook был
            # виден; сам расход доберётся общей классификацией приходящей операции выписки.
            logger.warning(
                "apply_payment_status: банк-кошелёк плательщика не найден "
                "(draft=%s cp=%s amount=%s) — фиксируем оплату без prebooked-проводки ДДС",
                draft.id,
                draft.counterparty_id,
                draft.amount,
            )
            draft.payload = {
                **(draft.payload or {}),
                "dds_prebook_skipped": "payer_wallet_unresolved",
            }
            # Видимый кейс в панели «Требует разбора» (owner-review), чтобы менеджер проверил
            # привязку счёта/завёл расход вручную. Кейс без bank_operation_id — закрывается
            # кнопкой «Отложить» (dismiss), классификация для него недоступна. paid-переход
            # под row-lock срабатывает один раз на черновик → один кейс, без дублей.
            session.add(
                ReconciliationCase(
                    kind="payer_wallet_unresolved",
                    status="pending",
                    provider="tbank",
                    payload={
                        "draft_id": str(draft.id),
                        "counterparty_id": str(draft.counterparty_id),
                        "amount": str(draft.amount),
                        "reason": "Не определён банк-счёт плательщика — проводка ДДС не заведена",
                    },
                )
            )
        # Standalone-черновик «банк по реквизитам» без накладной → создаём предоплату
        # (дебиторку) на контрагента + prebooked-проводку (её заберёт операция выписки).
        if draft.creates_prepayment and not draft_invoices:
            article_id = draft.prepayment_article_id or await session.scalar(
                select(DdsArticle.id).where(DdsArticle.code == "advance_to_supplier")
            )
            prepay_txn = None
            if bank_wallet is not None:
                prepay_txn = CashflowTransaction(
                    wallet_id=bank_wallet.id,
                    direction="out",
                    amount=draft.amount,
                    operation_date=op_date,
                    article_id=article_id,
                    counterparty_id=draft.counterparty_id,
                    source_kind="supplier_prepayment",
                    payment_purpose="Предоплата поставщику по статусу платежа",
                    quality_status="final",
                )
                session.add(prepay_txn)
                await session.flush()
            prepayment = SupplierPrepayment(
                counterparty_id=draft.counterparty_id,
                kind="goods",
                wallet_id=bank_wallet.id if bank_wallet else None,
                amount=draft.amount,
                amount_settled=0,
                status="open",
                cashflow_transaction_id=prepay_txn.id if prepay_txn else None,
                article_id=article_id,
            )
            session.add(prepayment)
            await session.flush()
            if prepay_txn is not None:
                prepay_txn.source_id = prepayment.id
        for invoice in draft_invoices:
            remaining = await _invoice_remaining(session, invoice)
            if remaining <= 0:
                continue
            if bank_wallet is not None:
                await _apply_wallet_payment(
                    session,
                    invoice=invoice,
                    wallet=bank_wallet,
                    amount=remaining,
                    operation_date=op_date,
                    article_id=supplier_article_id,
                    comment="Оплата по статусу платежа банка",
                    actor_user_id=actor_user_id,
                )
            else:
                session.add(
                    InvoicePaymentAllocation(
                        invoice_id=invoice.id,
                        source_kind="bank",
                        bank_operation_id=None,
                        amount=remaining,
                        created_by_user_id=actor_user_id,
                    )
                )
                await session.flush()
            await _recompute_status(session, invoice)
        draft.status = "paid"
        draft.synced_at = datetime.now(UTC)

    elif outcome in ("failed", "deleted") and draft.status in ("created", "updated"):
        # Платёж не состоялся — отклонён банком (failed) ИЛИ черновик удалён/отозван в банке
        # (deleted): деньги НЕ ушли. Возвращаем накладные в «неоплачено» (снимаем draft_id),
        # чтобы их можно было отправить заново. Фронт завязан на draft_id → бейдж «Отправлено
        # в банк» сам исчезает, накладная снова доступна к оплате.
        for invoice in await _draft_invoices(session, draft.id):
            invoice.draft_id = None
        draft.status = outcome
        draft.last_error = (
            "Черновик удалён в банке"
            if outcome == "deleted"
            else f"Платёж отклонён банком: {raw_status}"
        )[:500]
        draft.synced_at = datetime.now(UTC)

    if commit:
        await session.commit()
    return draft.status
