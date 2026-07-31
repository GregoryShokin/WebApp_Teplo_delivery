"""Авто-гашение накладных по статусу банковского платежа (T-Банк).

Статусный webhook «Статус платежа» (или ручной добор ``GET /payment/{id}``) двигает черновик
``CounterpartyPaymentDraft`` и привязанные к нему накладные. Сопоставление — по ``provider_ref``
(идентификатор платежа у банка), без мэчинга выписки по суммам/реквизитам.

При статусе «исполнен» оплата накладной И ДДС-проводка создаются ИЗ статуса черновика: одна
``CashflowTransaction`` на платёж (на банк-кошелёк плательщика) + аллокации по накладным.
Webhook-и «операция по счёту» в ДДС не пропускаются, поэтому второй проводки поверх статусной
оплаты не возникает. Если банк-кошелёк плательщика не определён — накладные всё равно гасятся
статус-аллокацией без ДДС-проводки (+ кейс owner-review). Статусы ``deleted/удалён`` (черновик
отозван в банке) трактуются как ``failed``: накладные возвращаются в «неоплачено».

Черновик «через Сейф» (``pays_via_safe``, неофициальный поставщик — платёж на карту ИП):
вместо расхода «Оплата поставщикам» paid-переход заводит транзит р/с→Сейф и целевой
резерв Сейфа на поставщика. Накладные при этом НЕ гасятся — деньги ещё внутри компании
(«Деньги в Сейфе» в UI); и расход по статье, и гашение накладных случаются при выплате
резерва (``safe_allocations.pay_allocation``).
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import (
    Account,
    CashflowTransaction,
    CounterpartyPaymentDraft,
    DdsArticle,
    ExpenseDraftLine,
    InvoicePaymentAllocation,
    ReconciliationCase,
    SafeAllocation,
    SupplierInvoice,
    SupplierPrepayment,
    Wallet,
)
from app.services.banking.base import clean_digits
from app.services.banking.payout import payer_account_for
from app.services.banking.safe_allocations import create_allocation
from app.services.counterparty_matching import (
    _draft_invoices,
    _invoice_remaining,
    _recompute_status,
)
from app.services.counterparty_payments import strip_match_marker
from app.services.wallets import (
    DDS_ARTICLE_TRANSFER_IN_CODE,
    DDS_ARTICLE_TRANSFER_OUT_CODE,
    SAFE_WALLET_CODE,
)

logger = logging.getLogger(__name__)

# Статья ДДС для оплаты поставщику по умолчанию (та же, что у ручной оплаты с кошелька).
_SUPPLIER_ARTICLE_CODE = "payment_to_supplier"
# source_kind транзитной пары р/с→Сейф при оплате черновика «через Сейф»
# (source_id = черновик). По образцу payroll_payouts.book_bank_to_safe_transfer.
SUPPLIER_BANK_TO_SAFE_SOURCE_KIND = "supplier_bank_to_safe"


async def _resolve_payer_bank_wallet(
    session: AsyncSession, draft: CounterpartyPaymentDraft
) -> Wallet | None:
    """Банк-кошелёк, с которого ушёл платёж (по номеру счёта плательщика из тела черновика
    или из настроек). Нужен, чтобы завести prebooked-проводку оплаты, которую заберёт
    приходящая операция выписки (prebooked-claim) — иначе операция уходит в needs_review.

    Fallback-счёт берётся по банку черновика (``bank_provider``): у свободного вывода через
    Сбер это Сбер-счёт, у прочих — Т-Банк (по умолчанию), чтобы транзит р/с→Сейф сел на
    кошелёк нужного банка."""
    account_number = None
    if isinstance(draft.payload, dict):
        account_number = draft.payload.get("accountNumber")
    fallback_account = payer_account_for(get_settings(), draft.bank_provider or "tbank")
    account_number = clean_digits(account_number or fallback_account or "")
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


async def _settle_draft_via_safe(
    session: AsyncSession,
    *,
    draft: CounterpartyPaymentDraft,
    bank_wallet: Wallet | None,
    article_id: uuid.UUID | None,
    operation_date: date,
) -> None:
    """Оплата черновика «через Сейф» (платёж на карту ИП): вместо расхода по статье —
    транзит р/с→Сейф + целевой резерв Сейфа.

    Два случая: неофициальный поставщик (резерв «Оплата поставщикам» на контрагента,
    purpose — из назначения платежа по накладным) и «просто трата» без получателя
    (окно «Новый платёж»: резерв ЦЕЛЕВОЙ статьи черновика ``target_article_id`` с
    назначением ``target_purpose``, контрагента нет).

    Расход по статье случится позже, при выплате резерва (``pay_allocation``). Идемпотентно:
    транзит — по (source_kind, source_id = черновик), резерв — по ``source_draft_id``
    (уникальный индекс как страховка поверх row-lock черновика). Резерв создаётся
    безусловно, без проверки «свободно»: транзит уже пополнил Сейф ровно на эту сумму.
    Устойчиво: отсутствие Сейф-кошелька не валит вебхук — громкий лог + кейс разбора.
    """
    safe_wallet = await session.scalar(
        select(Wallet).where(Wallet.code == SAFE_WALLET_CODE, Wallet.status == "active")
    )
    if safe_wallet is None:
        logger.warning(
            "apply_payment_status: кошелёк «Сейф» не найден (draft=%s cp=%s amount=%s) — "
            "транзит и целевой резерв не заведены",
            draft.id,
            draft.counterparty_id,
            draft.amount,
        )
        draft.payload = {
            **(draft.payload or {}),
            "safe_transfer_skipped": "safe_wallet_missing",
        }
        session.add(
            ReconciliationCase(
                kind="safe_wallet_unresolved",
                status="pending",
                provider=draft.bank_provider or "tbank",
                payload={
                    "draft_id": str(draft.id),
                    "counterparty_id": (
                        str(draft.counterparty_id) if draft.counterparty_id else None
                    ),
                    "amount": str(draft.amount),
                    "reason": "Кошелёк «Сейф» не найден — перевод и целевой резерв не заведены",
                },
            )
        )
        return

    purpose = (
        (draft.target_purpose or "").strip()
        or strip_match_marker(str((draft.payload or {}).get("paymentPurpose") or ""))
        or "Закуп у неофициального поставщика"
    )

    existing_transfer = await session.scalar(
        select(CashflowTransaction.id).where(
            CashflowTransaction.source_kind == SUPPLIER_BANK_TO_SAFE_SOURCE_KIND,
            CashflowTransaction.source_id == draft.id,
        )
    )
    if existing_transfer is None:
        transfer_purpose = f"Перевод на Сейф — {purpose}"
        transfer_out_article = await session.scalar(
            select(DdsArticle.id).where(DdsArticle.code == DDS_ARTICLE_TRANSFER_OUT_CODE)
        )
        transfer_in_article = await session.scalar(
            select(DdsArticle.id).where(DdsArticle.code == DDS_ARTICLE_TRANSFER_IN_CODE)
        )
        if bank_wallet is not None:
            # Банк-нога — prebooked-цель для исходящей операции выписки: баланс банка идёт
            # от выписки, эта проводка его не двигает (как в book_bank_to_safe_transfer).
            session.add(
                CashflowTransaction(
                    wallet_id=bank_wallet.id,
                    direction="out",
                    amount=draft.amount,
                    operation_date=operation_date,
                    article_id=transfer_out_article,
                    source_kind=SUPPLIER_BANK_TO_SAFE_SOURCE_KIND,
                    source_id=draft.id,
                    payment_purpose=transfer_purpose,
                    quality_status="final",
                )
            )
        # Сейф-нога двигает баланс Сейфа: деньги пришли на карту ИП. Заводится и без
        # банк-ноги (payer-кошелёк не привязан — кейс уже создан выше по paid-переходу):
        # приход на карту от этого не менее реален.
        session.add(
            CashflowTransaction(
                wallet_id=safe_wallet.id,
                direction="in",
                amount=draft.amount,
                operation_date=operation_date,
                article_id=transfer_in_article,
                source_kind=SUPPLIER_BANK_TO_SAFE_SOURCE_KIND,
                source_id=draft.id,
                payment_purpose=transfer_purpose,
                quality_status="final",
            )
        )
        await session.flush()

    # Черновик-пополнение (внутренний перевод банк→Сейф): только транзит, резерв не нужен.
    if getattr(draft, "topup_only", False):
        return

    # Транш «Нового платежа»: строки разбивки → по одной целёвке Сейфа на строку
    # (идемпотентность по строке). Расход разнесётся по статьям при выплате с Сейфа.
    lines = (
        await session.scalars(
            select(ExpenseDraftLine)
            .where(ExpenseDraftLine.draft_id == draft.id)
            .order_by(ExpenseDraftLine.position)
        )
    ).all()
    if lines:
        for line in lines:
            existing_line_alloc = await session.scalar(
                select(SafeAllocation.id).where(SafeAllocation.source_draft_line_id == line.id)
            )
            if existing_line_alloc is not None:
                continue
            logger.info(
                "apply_payment_status: авто-резерв Сейфа под строку %s черновика %s на %s",
                line.id,
                draft.id,
                line.amount,
            )
            await create_allocation(
                session,
                wallet_id=safe_wallet.id,
                amount=line.amount,
                free_amount=None,
                article_id=line.article_id,
                # Атрибуция расхода: кому платим (если у статьи есть привязанные контрагенты).
                counterparty_id=line.counterparty_id,
                purpose=line.purpose,
                source_draft_id=draft.id,
                source_draft_line_id=line.id,
                # Аналитика «где» доезжает до проводки через оплату резерва.
                location_id=line.location_id,
                lease_id=line.lease_id,
                # Объект — тем же путём: черновик → резерв → проводка. Потеряй его здесь, и
                # покупка дойдёт до ДДС без карточки, то есть мимо баланса.
                asset_id=line.asset_id,
            )
        return

    # Одиночный via-safe резерв без строк транша: «просто трата» старого формата
    # (target-статья) или неофициальный закуп (дефолтная «Оплата поставщикам»).
    existing_allocation = await session.scalar(
        select(SafeAllocation.id).where(
            SafeAllocation.source_draft_id == draft.id,
            SafeAllocation.source_draft_line_id.is_(None),
        )
    )
    if existing_allocation is None:
        logger.info(
            "apply_payment_status: авто-резерв Сейфа под черновик %s на %s "
            "(без проверки «свободно» — транзит уже пополнил Сейф)",
            draft.id,
            draft.amount,
        )
        await create_allocation(
            session,
            wallet_id=safe_wallet.id,
            amount=draft.amount,
            free_amount=None,
            # «Просто трата» несёт целевую статью в черновике; неофициальный закуп —
            # дефолтную «Оплата поставщикам» (расход признается при выплате резерва).
            article_id=draft.target_article_id or article_id,
            counterparty_id=draft.counterparty_id,
            purpose=purpose,
            source_draft_id=draft.id,
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
        "delete",
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


async def _payment_booked_by_statement_row(
    session: AsyncSession, draft: CounterpartyPaymentDraft
) -> bool:
    """Деньги этого черновика уже разобраны СОБСТВЕННОЙ проводкой операции выписки?

    Гонка «правило классификации ↔ paid-переход»: если операция выписки пришла первой, её мог
    разобрать классификатор (по правилу контрагента или руками оператора) в отдельную строку
    ``bank_operation`` — и правило 1 там уже отработало (``classifier.apply_operation_action``).
    Тогда повторный прогон по prebooked-строке считал бы те же деньги второй раз: пул правила 1
    берётся от суммы своей проводки, а мост «операция↔проводка» для только что созданной строки
    ещё пуст, поэтому «бюджет платежа» дубля не видит.

    Ключ поиска — тот же детерминированный, что у ``absorb_auto_classified_counterparty_payment``:
    номер документа черновика (мы сами его отправляли банку) + сумма + направление. Признак
    «уже разобрано» — зависимости на строке операции (зачёты кредиторки или предоплата).
    """
    if not draft.document_id:
        return False
    from app.models import BankOperation
    from app.services.banking.classifier import _cashflow_row_has_dependents
    from app.services.banking.tbank import _document_number

    docnum = _document_number(draft.document_id)
    if not docnum:
        return False
    rows = (
        await session.scalars(
            select(BankOperation).where(
                BankOperation.direction == "out",
                BankOperation.amount == draft.amount,
                BankOperation.document_number == docnum,
                BankOperation.cashflow_transaction_id.is_not(None),
            )
        )
    ).all()
    for operation in rows:
        if await _cashflow_row_has_dependents(session, operation.cashflow_transaction_id):
            return True
    return False


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
        # Гасим накладные черновика. Если знаем банк-кошелёк плательщика — заводим ОДНУ prebooked-
        # проводку «Оплата поставщикам» на весь платёж (её заберёт приходящая операция выписки
        # через prebooked-claim → операция не уходит в needs_review). Иначе fallback: статус-
        # аллокация без bank_operation_id.
        # Черновик «через Сейф» (pays_via_safe, неофициальный поставщик) — отдельная ветка:
        # накладные гасятся аллокациями БЕЗ расходной проводки, вместо неё транзит р/с→Сейф
        # и целевой резерв Сейфа (см. _settle_draft_via_safe).
        bank_wallet = await _resolve_payer_bank_wallet(session, draft)
        supplier_article_id = await session.scalar(
            select(DdsArticle.id).where(DdsArticle.code == _SUPPLIER_ARTICLE_CODE)
        )
        draft_invoices = await _draft_invoices(session, draft.id)
        op_date = operation_date or datetime.now(UTC).date()
        if bank_wallet is None and (
            draft_invoices
            or draft.creates_prepayment
            or draft.pays_via_safe
            or draft.target_article_id is not None
        ):
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
                    provider=draft.bank_provider or "tbank",
                    payload={
                        "draft_id": str(draft.id),
                        "counterparty_id": (
                            str(draft.counterparty_id) if draft.counterparty_id else None
                        ),
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
        # Свободный расход официальному контрагенту из окна «Новый платёж»: деньги уходят
        # сразу получателю по реквизитам, поэтому paid-переход фиксирует расход на выбранную
        # статью напрямую, без транзита и резерва Сейфа.
        if (
            not draft.pays_via_safe
            and not draft.creates_prepayment
            and not draft_invoices
            and draft.counterparty_id is not None
            and draft.target_article_id is not None
            and bank_wallet is not None
        ):
            free_txn = CashflowTransaction(
                wallet_id=bank_wallet.id,
                direction="out",
                amount=draft.amount,
                operation_date=op_date,
                article_id=draft.target_article_id,
                counterparty_id=draft.counterparty_id,
                source_kind="counterparty_payment",
                source_id=draft.id,
                payment_purpose=(
                    (draft.target_purpose or "").strip()
                    or "Оплата контрагенту по статусу платежа банка"
                ),
                quality_status="final",
            )
            session.add(free_txn)
            await session.flush()
            # Правило 1 канона: платёж поставщику без ссылки на документ гасит его открытую
            # кредиторку (FIFO по финансовым УПД/актам), а излишек становится дебиторкой.
            # Без этого вызова свободный платёж уходил мимо контура ДЗ/КЗ целиком: приходящая
            # операция выписки заклеймит эту проводку prebooked-claim'ом и вернётся ДО правила 1
            # (classifier: короткое замыкание), второго шанса учесть деньги не будет.
            #
            # НО только если этот платёж ещё не разобран отдельной проводкой операции выписки:
            # при обратном порядке (выписка пришла раньше paid-перехода) правило 1 уже отработало
            # в классификаторе на её собственной строке, и повторный прогон удвоил бы зачёт
            # кредиторки и завёл дебиторку из воздуха.
            if await _payment_booked_by_statement_row(session, draft):
                logger.info(
                    "apply_payment_status: платёж draft=%s уже разобран операцией выписки — "
                    "правило 1 повторно не применяем",
                    draft.id,
                )
            else:
                from app.services.supplier_prepayments import (
                    ensure_prepayment_from_bank_transaction,
                )

                await ensure_prepayment_from_bank_transaction(session, free_txn)
        # Накладные к гашению + статья каждой (счета услуг «Страницы на оплату» несут свою
        # dds_article_id, складские — дефолтную «Оплата поставщикам»).
        payable: list[tuple[SupplierInvoice, Decimal, uuid.UUID | None]] = []
        for invoice in draft_invoices:
            remaining = await _invoice_remaining(session, invoice)
            if remaining > 0:
                payable.append((invoice, remaining, invoice.dds_article_id or supplier_article_id))

        if bank_wallet is not None and not draft.pays_via_safe:
            # ОДНА проводка на платёж (на статью), а не по-накладно: операция выписки = Σ накладных
            # сматчится с ней через prebooked-claim. Дробление по накладным оставляло бы Σ-операцию
            # без пары (части ≠ сумме). Группируем по статье — обычно одна группа = одна проводка.
            by_article: dict[uuid.UUID | None, list[tuple[SupplierInvoice, Decimal]]] = defaultdict(
                list
            )
            for invoice, remaining, article_id in payable:
                by_article[article_id].append((invoice, remaining))
            for article_id, items in by_article.items():
                group_total = sum((remaining for _, remaining in items), Decimal("0"))
                txn = CashflowTransaction(
                    wallet_id=bank_wallet.id,
                    direction="out",
                    amount=group_total,
                    operation_date=op_date,
                    article_id=article_id,
                    counterparty_id=draft.counterparty_id,
                    source_kind="counterparty_payment",
                    source_id=draft.id,
                    payment_purpose="Оплата по статусу платежа банка",
                    quality_status="final",
                )
                session.add(txn)
                await session.flush()
                for invoice, remaining in items:
                    session.add(
                        InvoicePaymentAllocation(
                            invoice_id=invoice.id,
                            source_kind="cash",
                            cashflow_transaction_id=txn.id,
                            amount=remaining,
                            created_by_user_id=actor_user_id,
                        )
                    )
                await session.flush()
                for invoice, _ in items:
                    await _recompute_status(session, invoice)
        elif not draft.pays_via_safe:
            # Банк-кошелёк не резолвится — гашение статус-аллокацией без проводки ДДС.
            for invoice, remaining, _ in payable:
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
        # Черновик «через Сейф»: накладные при исполнении платежа НЕ гасятся — деньги пока
        # внутри компании (на Сейфе), поставщик наличных не получил. Накладная остаётся
        # «неоплаченной» с draft_id (UI показывает «Деньги в Сейфе»), а гашение случится при
        # выплате целевого резерва (pay_allocation → аллокации по накладным черновика).
        if draft.pays_via_safe and not draft.creates_prepayment:
            await _settle_draft_via_safe(
                session,
                draft=draft,
                bank_wallet=bank_wallet,
                article_id=supplier_article_id,
                operation_date=op_date,
            )
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
