from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.session import AsyncSessionLocal
from app.models import (
    Account,
    BankOperation,
    CounterpartyPaymentDraft,
    DepositBankDraft,
    EmployeePayout,
    OwnAccountsRegistry,
    PayrollBankDraft,
    ReconciliationCase,
    SalaryAdvanceBankDraft,
    Wallet,
)
from app.services.bank_payment_status import apply_payment_status
from app.services.banking.base import AccountMeta, BankClient, NormalizedBankOperation, clean_digits
from app.services.banking.classifier import (
    absorb_auto_classified_counterparty_payment,
    create_or_update_reconciliation_case,
    reconcile_needs_review_prebooked,
    run_classification_rules,
)
from app.services.banking.exceptions import BankCredentialsError, BankFetchError
from app.services.banking.own_accounts import sync_own_accounts
from app.services.banking.payout import payout_client_for
from app.services.banking.sber import SberClient
from app.services.banking.tbank import TbankClient, _document_number
from app.services.couriers.iiko_attendance_sync import sync_attendance
from app.services.couriers.iiko_olap_sync import sync_courier_olap_deliveries
from app.services.couriers.shift_matching import recalculate_matches
from app.services.deposit_bank_draft import apply_deposit_draft_status
from app.services.dismissal_reconciliation_service import reconcile_all_dismissing
from app.services.employee_payouts import apply_employee_payout_status
from app.services.kassa.iiko_cashshift_sync import sync_iiko_cashshifts
from app.services.payroll_advance_service import apply_advance_draft_status
from app.services.payroll_payouts import apply_payroll_draft_status
from app.services.payroll_runner import refresh_current_week_advance_window
from app.services.shift_schedule_service import auto_commit_living_schedule

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
MOSCOW_TZ = ZoneInfo("Europe/Moscow")
LEGAL_ENTITY = "ИП Шокина Е.А."
IIKO_COURIER_JOB_RETRIES = 3
SUPPORTED_BANK_PROVIDERS = ("sber", "tbank")


@scheduler.scheduled_job(
    "cron",
    hour=3,
    minute=15,
    id="maintain_sber_credentials",
    max_instances=1,
    coalesce=True,
)
async def maintain_sber_credentials() -> None:
    """Rotate Sber's short-lived client secret and surface mTLS renewal work.

    The bank does not expose a TLS-certificate renewal API: an operator obtains a
    new certificate, while this job gives at least 30 days' notice to install it.
    """
    if "sber" not in _bank_sync_providers():
        return

    async with AsyncSessionLocal() as session:
        client = SberClient(session)
        try:
            secret_result = await client.rotate_client_secret_if_due()
            if secret_result["status"] == "rotated":
                logger.info("Sber client secret rotated")
        except BankCredentialsError as exc:
            await session.rollback()
            await _create_invalid_credentials_case(session, "sber", str(exc))
            logger.warning("Sber client-secret maintenance failed: %s", exc)

        try:
            certificate_expires_at = await client.tls_certificate_expires_at()
            alert_before = datetime.now(UTC) + timedelta(
                days=get_settings().sber_tls_certificate_alert_before_days
            )
            if certificate_expires_at <= alert_before:
                await create_or_update_reconciliation_case(
                    session,
                    kind="sber_tls_certificate_expiring",
                    provider="sber",
                    payload={"expires_at": certificate_expires_at.isoformat()},
                )
                logger.warning(
                    "Sber mTLS certificate expires at %s", certificate_expires_at.isoformat()
                )
        except BankCredentialsError as exc:
            await session.rollback()
            await _create_invalid_credentials_case(session, "sber", str(exc))
            logger.warning("Sber TLS certificate maintenance failed: %s", exc)
        await session.commit()


@scheduler.scheduled_job(
    "cron",
    minute=0,
    hour="*",
    id="poll_banks",
    max_instances=1,
    coalesce=True,
)
async def poll_banks() -> None:
    # Почасовой polling выписки банков (Sber + T-Bank). Для Sber это ЕДИНСТВЕННЫЙ источник
    # данных — вебхука у него нет; для T-Bank — сверка/фоллбэк к realtime-вебхуку «операция по
    # счёту» (если webhook не доставит, polling доберёт). Сохранённую Transaction на следующем
    # минутном ``poll_payment_statuses`` можно связать с черновиком по сумме/назначению даже при
    # застрявшем SUBMITTED. Дедуп по operationId + стабильному ключу выписки в
    # ``ingest_operations`` делает повторный прогон идемпотентным.
    for provider in _bank_sync_providers():
        await run_bank_sync_job(provider=provider)


@scheduler.scheduled_job(
    "cron",
    hour=9,
    minute=30,
    id="escalate_pending_cheques",
    max_instances=1,
    coalesce=True,
)
async def escalate_pending_cheques() -> None:
    """Поднять в «Требует разбора» ручные чеки, которые банк не подтвердил дольше порога,
    и чеки, ждущие возврат карт-покупки дольше порога."""
    from app.services.kassa.cheque import (
        escalate_missing_cheque_refunds,
        escalate_overdue_pending_cheques,
    )

    async with AsyncSessionLocal() as session:
        created = await escalate_overdue_pending_cheques(session)
        created_refunds = await escalate_missing_cheque_refunds(session)
        await session.commit()
    if created:
        logger.info("Эскалация пендинг-чеков: %s новых кейсов «банк не передал»", created)
    if created_refunds:
        logger.info("Эскалация возвратов: %s новых кейсов «возврат не пришёл»", created_refunds)


@scheduler.scheduled_job(
    "interval",
    minutes=60,
    id="reconcile_dismissing_employees",
    max_instances=1,
    coalesce=True,
)
async def reconcile_dismissing_employees_job() -> None:
    """Завершает отложенные увольнения: переводит `dismissing` → `inactive`, если
    все расчёты закрыты (депозит, ЗП, займы, штрафы ревизии). Страховка к
    событийным хукам; идемпотентен (пропускает незакрытые расчёты)."""
    async with AsyncSessionLocal() as session:
        flipped = await reconcile_all_dismissing(session)
        if flipped:
            logger.info("dismissal-reconcile: завершено увольнений: %d", len(flipped))


@scheduler.scheduled_job(
    "cron",
    hour=0,
    minute=0,
    id="auto_commit_shift_schedule",
    max_instances=1,
    coalesce=True,
)
async def auto_commit_shift_schedule_job() -> None:
    """В 00:00 МСК авто-фиксируем график, если управляющий забыл нажать «Зафиксировать».

    Живой график в статусе «редактируемый» (``draft``) не участвует в учёте смен/план-факте/
    надбавках — их читает только зафиксированный (``published``). Этот ночной прогон —
    страховка: держит график зафиксированным к началу нового дня. Идемпотентен (уже
    зафиксированный не трогает), системная фиксация (``published_by_user_id = None``)."""
    async with AsyncSessionLocal() as session:
        committed = await auto_commit_living_schedule(session)
    if committed is not None:
        logger.info("auto-commit графика: зафиксирован живой график %s", committed.id)


@scheduler.scheduled_job(
    "cron",
    hour=0,
    minute=30,
    id="refresh_production_advance_window",
    max_instances=1,
    coalesce=True,
)
async def refresh_production_advance_window_job() -> None:
    """Ночной свип «окна заработанного» для авансов производственникам (повар/кассир).

    В 00:30 МСК, после закрытия заведения (смены закрыты), заводит недельный период
    текущей (идущей) недели вт→пн и перечитывает явки iiko за отработанные дни —
    чтобы аванс был доступен по факту заработанного среди недели, а не только в день
    выплаты, когда стартует расчёт. Полный расчёт не запускает; депозит/фонд не
    задеваются (двигаются строго в finalize). Идемпотентно."""
    async with AsyncSessionLocal() as session:
        period = await refresh_current_week_advance_window(session)
    if period is not None:
        logger.info(
            "advance-window: обновлено окно недели %s–%s",
            period.start_date,
            period.end_date,
        )


@scheduler.scheduled_job(
    "interval",
    minutes=1,
    id="poll_payment_statuses",
    max_instances=1,
    coalesce=True,
)
async def poll_payment_statuses() -> None:
    """Периодический добор статуса исходящих платежей — страховка к статусному webhook.

    Основной (realtime) путь гашения — статусный webhook платёжного документа по
    ``provider_ref``. Но webhook хрупок: любое пересоздание контейнера api (деплой) роняет
    входящий webhook в 502, а банк после серии отказов перестаёт слать (инцидент 30.06→07.07:
    оплата черновиков замолчала на неделю, деньги ушли из банка без отражения в ДДС).
    Поэтому polling возвращён как надёжная сверка: каждую минуту опрашивает все «отправленные
    в банк» черновики (``created/updated``), применяет ``EXECUTED→paid`` / ``failed`` / ``DELETED``
    и сводит осиротевшие needs_review-операции с prebooked-проводками. Идемпотентен."""
    async with AsyncSessionLocal() as session:
        await run_payment_status_poll(session)


async def run_payment_status_poll(
    session: AsyncSession, *, client: BankClient | None = None
) -> dict[str, int]:
    """Опросить статус по всем «отправленным в банк» черновикам и применить его. Вынесено из
    джоба для тестируемости (можно передать фейковый клиент)."""
    status_clients: dict[str, BankClient] = {}

    def status_client_for(provider: str | None) -> BankClient:
        if client is not None:
            return client
        bank_provider = provider or "tbank"
        status_client = status_clients.get(bank_provider)
        if status_client is None:
            status_client = payout_client_for(bank_provider, session)
            status_clients[bank_provider] = status_client
        return status_client

    drafts = (
        await session.scalars(
            select(CounterpartyPaymentDraft).where(
                CounterpartyPaymentDraft.status.in_(("created", "updated")),
                CounterpartyPaymentDraft.provider_ref.is_not(None),
            )
        )
    ).all()
    result = {
        "checked": 0,
        "paid": 0,
        "failed": 0,
        "deleted": 0,
        "errors": 0,
        "matched_by_operation": 0,
        "reconciled": 0,
        "absorbed": 0,
    }
    for draft in drafts:
        try:
            raw = await status_client_for(draft.bank_provider).get_payment_status(
                draft.provider_ref or ""
            )
        except BankCredentialsError:
            # Токен один на все платежи — дальше опрашивать бессмысленно; уже применённое
            # коммитим (как делает run_bank_sync_job), джоб не падает наружу.
            logger.warning("payment-status poll: credentials error, прерываю опрос", exc_info=True)
            result["errors"] += 1
            break
        except Exception:  # noqa: BLE001 - сетевая/банк-ошибка одного платежа не валит весь проход
            logger.warning("payment-status poll: ошибка по черновику %s", draft.id, exc_info=True)
            result["errors"] += 1
            continue
        result["checked"] += 1
        if raw is None:
            continue
        status = await apply_payment_status(session, draft=draft, raw_status=raw, commit=False)
        if status == "paid":
            result["paid"] += 1
        elif status == "failed":
            result["failed"] += 1
        elif status == "deleted":
            result["deleted"] += 1

    # Те же статусы для payroll-черновиков: при «исполнен» заводим внутренний перевод банк→Сейф.
    payroll_drafts = (
        await session.scalars(
            select(PayrollBankDraft).where(
                PayrollBankDraft.status.in_(("created", "updated")),
                PayrollBankDraft.provider_ref.is_not(None),
            )
        )
    ).all()
    for payroll_draft in payroll_drafts:
        try:
            raw = await status_client_for(payroll_draft.bank_provider).get_payment_status(
                payroll_draft.provider_ref or ""
            )
        except BankCredentialsError:
            logger.warning("payment-status poll: credentials error, прерываю опрос", exc_info=True)
            result["errors"] += 1
            break
        except Exception:  # noqa: BLE001 - сетевая/банк-ошибка одного платежа не валит весь проход
            logger.warning(
                "payment-status poll: ошибка по payroll-черновику %s",
                payroll_draft.id,
                exc_info=True,
            )
            result["errors"] += 1
            continue
        result["checked"] += 1
        if raw is None:
            continue
        payroll_status = await apply_payroll_draft_status(
            session, draft=payroll_draft, raw_status=raw, commit=False
        )
        if payroll_status == "paid":
            result["paid"] += 1
        elif payroll_status == "failed":
            result["failed"] += 1
        elif payroll_status == "deleted":
            result["deleted"] += 1

    # Те же статусы для банк-выдачи авансов/займов: при «исполнен» — транзит банк→Сейф +
    # резерв Сейфа под выдачу (фактическая выдача сотруднику подтверждается «Выплачено»).
    advance_drafts = (
        await session.scalars(
            select(SalaryAdvanceBankDraft).where(
                SalaryAdvanceBankDraft.status.in_(("created", "updated")),
                SalaryAdvanceBankDraft.provider_ref.is_not(None),
            )
        )
    ).all()
    for advance_draft in advance_drafts:
        try:
            raw = await status_client_for(advance_draft.bank_provider).get_payment_status(
                advance_draft.provider_ref or ""
            )
        except BankCredentialsError:
            logger.warning("payment-status poll: credentials error, прерываю опрос", exc_info=True)
            result["errors"] += 1
            break
        except Exception:  # noqa: BLE001 - сетевая/банк-ошибка одного платежа не валит весь проход
            logger.warning(
                "payment-status poll: ошибка по advance-черновику %s",
                advance_draft.id,
                exc_info=True,
            )
            result["errors"] += 1
            continue
        result["checked"] += 1
        if raw is None:
            continue
        advance_status = await apply_advance_draft_status(
            session, draft=advance_draft, raw_status=raw, commit=False
        )
        if advance_status == "paid":
            result["paid"] += 1
        elif advance_status == "failed":
            result["failed"] += 1

    # Те же статусы для банк-выдачи депозитов: при «исполнен» — транзит р/с→Сейф + резерв Сейфа
    # (депозит-счёт списывается лишь при фактической выдаче резерва). Для Сбера это единственный
    # путь довести черновик до paid (вебхука у него нет).
    deposit_drafts = (
        await session.scalars(
            select(DepositBankDraft).where(
                DepositBankDraft.status.in_(("created", "updated")),
                DepositBankDraft.provider_ref.is_not(None),
            )
        )
    ).all()
    for deposit_draft in deposit_drafts:
        try:
            raw = await status_client_for(deposit_draft.bank_provider).get_payment_status(
                deposit_draft.provider_ref or ""
            )
        except BankCredentialsError:
            logger.warning("payment-status poll: credentials error, прерываю опрос", exc_info=True)
            result["errors"] += 1
            break
        except Exception:  # noqa: BLE001 - сетевая/банк-ошибка одного платежа не валит весь проход
            logger.warning(
                "payment-status poll: ошибка по deposit-черновику %s",
                deposit_draft.id,
                exc_info=True,
            )
            result["errors"] += 1
            continue
        result["checked"] += 1
        if raw is None:
            continue
        deposit_status = await apply_deposit_draft_status(
            session, draft=deposit_draft, raw_status=raw, commit=False
        )
        if deposit_status == "paid":
            result["paid"] += 1
        elif deposit_status == "failed":
            result["failed"] += 1

    # Те же статусы для разовых выплат сотрудникам (окно «Новый платёж», ЗП собственника):
    # при «исполнен» — транзит р/с→Сейф + резерв Сейфа под выдачу. Провайдер берётся по СЧЁТУ
    # СПИСАНИЯ выплаты (Т-Банк или Сбер): у Сбера вебхука нет, и этот опрос — единственный путь
    # довести его черновик до paid.
    employee_payout_rows = (
        await session.execute(
            select(EmployeePayout, Account.bank_code)
            .join(Wallet, Wallet.id == EmployeePayout.wallet_id)
            .join(Account, Account.id == Wallet.account_id)
            .where(
                EmployeePayout.status == "pending",
                EmployeePayout.provider_ref.is_not(None),
            )
        )
    ).all()
    for employee_payout, payout_provider in employee_payout_rows:
        try:
            raw = await status_client_for(payout_provider or "tbank").get_payment_status(
                employee_payout.provider_ref or ""
            )
        except BankCredentialsError:
            logger.warning("payment-status poll: credentials error, прерываю опрос", exc_info=True)
            result["errors"] += 1
            break
        except Exception:  # noqa: BLE001 - сетевая/банк-ошибка одного платежа не валит весь проход
            logger.warning(
                "payment-status poll: ошибка по выплате сотруднику %s",
                employee_payout.id,
                exc_info=True,
            )
            result["errors"] += 1
            continue
        result["checked"] += 1
        if raw is None:
            continue
        payout_status = await apply_employee_payout_status(
            session, payout=employee_payout, raw_status=raw, commit=False
        )
        if payout_status == "paid":
            result["paid"] += 1
        elif payout_status == "failed":
            result["failed"] += 1

    # Статус черновика T-Банка может застрять на SUBMITTED уже после фактического списания.
    # В таком случае подтверждённая Transaction из выписки закрывает черновик по точному
    # многофакторному совпадению (сумма + назначение + счёт + documentNumber/метка).
    result["matched_by_operation"] = await settle_drafts_from_recorded_operations(session)
    result["paid"] += result["matched_by_operation"]

    # Сводим «требующие проверки» операции с prebooked-проводками, появившимися позже них
    # (гонка вебхук↔поллинг) — иначе один платёж висит двумя строками в журнале.
    result["reconciled"] = await reconcile_needs_review_prebooked(session)
    # И поглощаем авто-классифицированные (правилом) операции той же prebooked-проводкой оплаты —
    # иначе платёж поставщику с правилом классификации двоится (auto-строка + prebooked-проводка).
    result["absorbed"] = await absorb_auto_classified_counterparty_payment(session)
    await session.commit()
    return result


async def settle_counterparty_draft_from_operation(
    session: AsyncSession,
    *,
    operation: NormalizedBankOperation | BankOperation,
) -> str | None:
    """Закрыть черновик по подтверждённой операции T-Банка без ожидания ``EXECUTED``.

    ``payment/status`` у черновика может оставаться ``SUBMITTED``, хотя выписка уже содержит
    подтверждённую ``Transaction``. Поэтому источник истины здесь — проведённая операция,
    но автозакрытие допускается только при точном совпадении суммы, нормализованного
    назначения и счёта списания. ``documentNumber`` (если есть) обязан совпасть; у новых
    платежей назначение дополнительно содержит метку ``[TPL-…]``. Неоднозначность или любой
    конфликт оставляют операцию в ручном разборе. Не коммитит.
    """
    if operation.provider != "tbank" or operation.direction != "out":
        return None
    raw_payload = operation.raw_payload if isinstance(operation.raw_payload, dict) else {}
    operation_stage = str(raw_payload.get("operationStatus") or "").strip().casefold()
    if operation_stage != "transaction":
        return None

    operation_purpose = _normalize_payment_purpose(operation.payment_purpose)
    if not operation_purpose:
        return None

    candidates = (
        await session.scalars(
            select(CounterpartyPaymentDraft).where(
                CounterpartyPaymentDraft.status.in_(("created", "updated")),
                CounterpartyPaymentDraft.provider_ref.is_not(None),
                CounterpartyPaymentDraft.bank_provider == "tbank",
                CounterpartyPaymentDraft.amount == operation.amount,
            )
        )
    ).all()

    operation_account = await _operation_account_number(session, operation)
    raw_num = (operation.document_number or "").strip()
    matches: list[CounterpartyPaymentDraft] = []
    for draft in candidates:
        draft_purpose = _normalize_payment_purpose((draft.payload or {}).get("paymentPurpose"))
        if draft_purpose != operation_purpose:
            continue
        if draft.created_at is not None and operation.operation_date < draft.created_at.date():
            continue
        draft_account = clean_digits((draft.payload or {}).get("accountNumber"))
        if operation_account and draft_account and operation_account != draft_account:
            continue
        if raw_num and raw_num != _document_number(draft.document_id):
            continue
        # Без documentNumber разрешаем матч только для нового назначения с уникальной TPL-меткой.
        if not raw_num and not _PURPOSE_MARKER_RE.search(operation_purpose):
            continue
        matches.append(draft)

    if len(matches) != 1:
        # 0 — операция не от нашего черновика; >1 — неоднозначно. Оставляем ручному разбору.
        return None

    draft = matches[0]
    status = await apply_payment_status(
        session,
        draft=draft,
        # ``Transaction`` официально означает подтверждённую операцию; передаём штатному
        # идемпотентному обработчику его нормализованный терминальный эквивалент.
        raw_status="executed",
        operation_date=operation.operation_date,
        commit=False,
    )
    if status == "paid":
        # Свежая prebooked-проводка должна забрать эту же операцию (иначе две строки до поллинга).
        await reconcile_needs_review_prebooked(session)
        # Операция уже могла быть авто-классифицирована правилом в свою строку (у поставщика есть
        # правило) — поглощаем её той же prebooked-проводкой, иначе платёж останется задвоенным.
        await absorb_auto_classified_counterparty_payment(session)
    return status


_PURPOSE_MARKER_RE = re.compile(r"\[tpl-[0-9a-f]{12}\]", re.IGNORECASE)


def _normalize_payment_purpose(value: object) -> str:
    """Сравнивать назначение строго, нормализуя только регистр и пробельные символы."""
    return " ".join(str(value or "").replace("\u00a0", " ").split()).casefold()


async def _operation_account_number(
    session: AsyncSession, operation: NormalizedBankOperation | BankOperation
) -> str:
    if isinstance(operation, NormalizedBankOperation):
        return clean_digits(operation.account_number)
    if operation.account_id is None:
        return ""
    account_number = await session.scalar(
        select(Account.account_number).where(Account.id == operation.account_id)
    )
    return clean_digits(account_number)


async def settle_drafts_from_recorded_operations(session: AsyncSession) -> int:
    """Добрать активные T-Банк-черновики по уже сохранённым Transaction из ДДС.

    Это страховка для двух случаев: webhook пришёл до деплоя новой логики либо операция была
    получена почасовым polling выписки. Скан ограничен суммами и датой активных черновиков.
    """
    active_drafts = (
        await session.scalars(
            select(CounterpartyPaymentDraft).where(
                CounterpartyPaymentDraft.status.in_(("created", "updated")),
                CounterpartyPaymentDraft.provider_ref.is_not(None),
                CounterpartyPaymentDraft.bank_provider == "tbank",
            )
        )
    ).all()
    if not active_drafts:
        return 0

    amounts = {draft.amount for draft in active_drafts}
    created_dates = [draft.created_at.date() for draft in active_drafts if draft.created_at]
    oldest_date = min(created_dates) if created_dates else datetime.now(MOSCOW_TZ).date()
    operations = (
        await session.scalars(
            select(BankOperation)
            .where(
                BankOperation.provider == "tbank",
                BankOperation.direction == "out",
                BankOperation.amount.in_(amounts),
                BankOperation.operation_date >= oldest_date,
            )
            .order_by(BankOperation.operation_date, BankOperation.imported_at)
        )
    ).all()

    matched = 0
    for operation in operations:
        status = await settle_counterparty_draft_from_operation(session, operation=operation)
        if status == "paid":
            matched += 1
    return matched


@scheduler.scheduled_job(
    "interval",
    minutes=5,
    id="push_iiko_invoice_payments",
    max_instances=1,
    coalesce=True,
)
async def push_iiko_invoice_payments() -> None:
    """Зеркалировать в iiko оплаты iiko-накладных, оплаченных через банк-черновик (Cloud
    add_payment). Сверочный: сканирует оплаченные накладные без успешного пуша и шлёт; путь
    гашения НЕ трогает. Ошибки изолированы по накладной, ретраи с капом попыток."""
    from app.services.counterparty_iiko_payment import (
        mirror_paid_iiko_invoices,
        mirror_paid_kassa_invoices,
        sweep_unsettled_iiko_payments,
    )

    async with AsyncSessionLocal() as session:
        result = await mirror_paid_iiko_invoices(session)
    if result.get("ok") or result.get("error"):
        logger.info("push_iiko_invoice_payments: %s", result)
    # Те же Cloud add_payment, но для оплаченных чеков/накладных Кассы (товарная часть).
    async with AsyncSessionLocal() as session:
        kassa = await mirror_paid_kassa_invoices(session)
    if kassa.get("ok") or kassa.get("error"):
        logger.info("push_iiko_kassa_payments: %s", kassa)
    # Дозор: свести в единый видимый список owner-review причины «оплачено у нас — в iiko нет»,
    # которые джобы выше не видят (мимо-черновика/коррекция/кап), и закрыть восстановленные кейсы.
    async with AsyncSessionLocal() as session:
        swept = await sweep_unsettled_iiko_payments(session)
    if any(swept.values()):
        logger.info("sweep_unsettled_iiko_payments: %s", swept)


@scheduler.scheduled_job(
    "interval",
    minutes=30,
    id="verify_iiko_invoice_payments",
    max_instances=1,
    coalesce=True,
)
async def verify_iiko_invoice_payments() -> None:
    """Подтвердить, что отправленные в iiko оплаты реально стали проводками.

    ``add_payment`` отвечает 201 с ``accountingTransactionId`` даже когда проводка не создаётся
    (сверка 22.07.2026: 4 «тихие потери» из 55 отправок), поэтому статус ``ok`` в журнале сам по
    себе ничего не доказывает. Джоб сверяет журнал с OLAP-отчётом iiko и переотправляет платежи,
    которых в учёте нет."""
    from app.services.iiko_payment_verify import verify_mirrored_payments

    async with AsyncSessionLocal() as session:
        try:
            result = await verify_mirrored_payments(session)
        except Exception:  # noqa: BLE001 — недоступный OLAP не должен ронять планировщик
            logger.warning("verify_iiko_invoice_payments: сверка не удалась", exc_info=True)
            return
    if result.get("verified") or result.get("resent") or result.get("manual"):
        logger.info("verify_iiko_invoice_payments: %s", result)


@scheduler.scheduled_job(
    "interval",
    minutes=get_settings().mail_poll_interval_minutes,
    id="poll_mail_invoices",
    max_instances=1,
    coalesce=True,
)
async def poll_mail_invoices() -> None:
    """«Страница на оплату» (Фаза 1): циклический разбор почты. Оба ящика (личный +
    корпоративный) → распознать новые PDF-счета/УПД (гибрид: регексы + Claude) → создать
    ``SupplierInvoice(source='email')``. Деньги НЕ двигает — наполняет входящий список.
    realtime-канала у mail.ru нет, поэтому только поллинг; идемпотентность по SHA-256."""
    settings = get_settings()
    if not settings.mail_poll_enabled:
        return
    from app.services.email_invoice_ingest import poll_and_ingest

    async with AsyncSessionLocal() as session:
        try:
            result = await poll_and_ingest(session, settings=settings)
        except Exception:  # noqa: BLE001 - проход почты не должен ронять планировщик
            logger.warning("poll_mail_invoices: проход завершился ошибкой", exc_info=True)
            return
    if result.get("status") != "not_configured":
        logger.info("poll_mail_invoices: %s", result)


@scheduler.scheduled_job(
    "interval",
    minutes=get_settings().tax_document_poll_interval_minutes,
    id="poll_tax_documents",
    max_instances=1,
    coalesce=True,
)
async def poll_tax_documents() -> None:
    """Модуль «Налоги»: циклический забор документов бухгалтера из почты (платёжки/ведомости/
    оборотки) → staging ``tax_document_intake``. Дублирует ручную кнопку «Проверить почту»,
    чтобы свежие документы подтягивались сами. Денег не двигает; идемпотентно по SHA-256."""
    settings = get_settings()
    if not settings.tax_document_poll_enabled:
        return
    from app.services.taxes.document_ingest import (
        ingest_tax_documents,
        resolve_tax_senders,
    )

    senders = resolve_tax_senders(settings.tax_document_senders)
    async with AsyncSessionLocal() as session:
        try:
            result = await ingest_tax_documents(session, settings=settings, senders=senders)
        except Exception:  # noqa: BLE001 - проход почты не должен ронять планировщик
            logger.warning("poll_tax_documents: проход завершился ошибкой", exc_info=True)
            return
    if result.get("status") != "not_configured":
        logger.info("poll_tax_documents: %s", result)


@scheduler.scheduled_job(
    "cron",
    hour=7,
    minute=0,
    id="send_scheduled_payments",
    max_instances=1,
    coalesce=True,
)
async def send_scheduled_payments() -> None:
    """«Страница на оплату»: ежедневная авто-отправка счетов с наступившей плановой датой в банк
    (банк-черновик, как ручная кнопка «Отправить в банк»). Реквизиты не подтверждены / банк
    недоступен → счёт пропускается до следующего прохода."""
    from app.services.email_invoice_ingest import run_scheduled_sends

    async with AsyncSessionLocal() as session:
        result = await run_scheduled_sends(session)
    if result.get("due"):
        logger.info("send_scheduled_payments: %s", result)


@scheduler.scheduled_job(
    "interval",
    minutes=30,
    id="iiko_courier_attendance_sync",
    max_instances=1,
    coalesce=True,
)
async def iiko_courier_attendance_sync_job() -> None:
    await run_with_iiko_backoff(run_iiko_courier_attendance_sync_once)


@scheduler.scheduled_job(
    "interval",
    hours=1,
    id="iiko_courier_shift_matching",
    max_instances=1,
    coalesce=True,
    next_run_time=datetime.now(MOSCOW_TZ),
)
async def iiko_courier_shift_matching_job() -> None:
    await run_iiko_courier_shift_matching_once()


@scheduler.scheduled_job(
    "interval",
    minutes=20,
    id="iiko_courier_delivery_sync",
    max_instances=1,
    coalesce=True,
)
async def iiko_courier_delivery_sync_job() -> None:
    await run_with_iiko_backoff(run_iiko_courier_delivery_sync_once)


async def run_iiko_courier_attendance_sync_once() -> dict[str, object]:
    now = datetime.now(MOSCOW_TZ)
    date_from = now.date() - timedelta(days=2)
    date_to = now.date() + timedelta(days=1)
    async with AsyncSessionLocal() as session:
        report = await sync_attendance(
            session,
            from_date=date_from,
            to_date=date_to,
            run_reason="cron",
        )
    logger.info("iiko courier attendance sync completed: %s", report.as_dict())
    # Триггерим matching сразу после attendance sync — иначе свежие смены
    # остаются как no_show до следующего matching-тика (раз в час).
    try:
        await run_iiko_courier_shift_matching_once()
    except Exception:  # noqa: BLE001
        logger.exception("post-attendance matching recalc failed; continuing")
    return report.as_dict()


async def run_iiko_courier_shift_matching_once() -> dict[str, object]:
    now = datetime.now(MOSCOW_TZ)
    date_from = now.date() - timedelta(days=2)
    date_to = now.date() + timedelta(days=1)
    async with AsyncSessionLocal() as session:
        report = await recalculate_matches(session, date_from, date_to)
        await session.commit()
    logger.info("iiko courier shift matching completed: %s", report.as_dict())
    return report.as_dict()


async def run_iiko_courier_delivery_sync_once() -> dict[str, object]:
    now = datetime.now(MOSCOW_TZ)
    date_from = now.date() - timedelta(days=1)
    date_to = now.date()
    async with AsyncSessionLocal() as session:
        result = await sync_courier_olap_deliveries(session, date_from=date_from, date_to=date_to)
        await session.commit()
    payload = result.as_dict()
    logger.info("iiko courier delivery sync completed: %s", payload)
    return payload


@scheduler.scheduled_job(
    "interval",
    minutes=20,
    id="iiko_cashshift_sync",
    max_instances=1,
    coalesce=True,
)
async def iiko_cashshift_sync_job() -> None:
    await run_with_iiko_backoff(run_iiko_cashshift_sync_once)


async def run_iiko_cashshift_sync_once() -> dict[str, object]:
    now = datetime.now(MOSCOW_TZ)
    date_from = now.date() - timedelta(days=2)
    date_to = now.date() + timedelta(days=1)
    async with AsyncSessionLocal() as session:
        report = await sync_iiko_cashshifts(session, date_from=date_from, date_to=date_to)
        await session.commit()
    payload = report.as_dict()
    logger.info("iiko cashshift sync completed: %s", payload)
    return payload


@scheduler.scheduled_job(
    "cron",
    hour=3,
    minute=15,
    id="archive_expired_freelancer_cards",
    max_instances=1,
    coalesce=True,
)
async def archive_expired_freelancer_cards() -> None:
    """Ежедневная автоархивация внештатников с истёкшим периодом (03:15 МСК).

    Карточка уходит в inactive, плейсхолдер освобождается (сброс ПИН). Физического
    удаления нет — история смен/ведомостей/выплат остаётся на архивной строке.
    """
    from app.services.freelancer import pool as freelancer_pool

    async with AsyncSessionLocal() as session:
        archived = await freelancer_pool.archive_expired_cards(session)
        await session.commit()
    if archived:
        logger.info("archive_expired_freelancer_cards: архивировано карточек=%s", archived)


async def run_with_iiko_backoff(operation: Callable[[], Awaitable[object]]) -> None:
    for attempt in range(1, IIKO_COURIER_JOB_RETRIES + 1):
        try:
            await operation()
            return
        except Exception as exc:  # noqa: BLE001 - scheduler must log and survive iiko outages
            if attempt == IIKO_COURIER_JOB_RETRIES or not is_iiko_retryable_error(exc):
                logger.exception("iiko courier attendance sync job failed")
                return
            delay = 2 ** (attempt - 1)
            logger.warning(
                "iiko courier attendance sync retry %s/%s in %ss after: %s",
                attempt + 1,
                IIKO_COURIER_JOB_RETRIES,
                delay,
                exc,
            )
            await asyncio.sleep(delay)


def is_iiko_retryable_error(exc: BaseException) -> bool:
    status = getattr(exc, "status", None)
    if status in {401, 403, 429}:
        return True
    message = str(exc).casefold()
    return any(
        marker in message
        for marker in (
            "auth",
            "unauthorized",
            "forbidden",
            "rate",
            "too many",
            "invalid key",
            "не авториз",
            "неверный ключ",
            "лимит",
        )
    )


async def run_bank_sync_job(
    *,
    provider: str,
    date_from: date | None = None,
    date_to: date | None = None,
    job_id: uuid.UUID | None = None,
) -> dict[str, object]:
    job_id = job_id or uuid.uuid4()
    async with AsyncSessionLocal() as session:
        if date_from is None or date_to is None:
            auto_from, auto_to = await _default_sync_period(session, provider)
            date_from = date_from or auto_from
            date_to = date_to or auto_to
        try:
            result = await sync_bank_provider(
                session,
                provider=provider,
                date_from=date_from,
                date_to=date_to,
            )
            await session.commit()
            return {"job_id": job_id, **result}
        except BankCredentialsError as exc:
            await session.rollback()
            await _create_invalid_credentials_case(session, provider, str(exc))
            await session.commit()
            logger.warning("Bank credentials error for %s: %s", provider, exc)
            return {"job_id": job_id, "provider": provider, "status": "invalid_credentials"}
        except BankFetchError as exc:
            # Isolate a provider's fetch/HTTP failure so it cannot block the other
            # providers in poll_banks (which syncs sber then tbank sequentially).
            await session.rollback()
            logger.warning("Bank fetch error for %s: %s", provider, exc)
            return {"job_id": job_id, "provider": provider, "status": "fetch_error"}


async def sync_bank_provider(
    session: AsyncSession, *, provider: str, date_from: date, date_to: date
) -> dict[str, object]:
    client = _client_for_provider(provider, session)
    metadata = await client.fetch_account_metadata()
    await _upsert_accounts_from_metadata(session, provider, metadata)
    operations = await client.fetch_statement(date_from=date_from, date_to=date_to)
    result = await ingest_operations(session, provider=provider, operations=operations)
    return {
        "provider": provider,
        "status": "completed",
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "fetched": len(operations),
        **result,
    }


async def ingest_operations(
    session: AsyncSession,
    *,
    provider: str,
    operations: list[NormalizedBankOperation],
) -> dict[str, object]:
    """Записать нормализованные операции выписки в журнал и прогнать классификацию.

    Единый сток для ОБОИХ источников: периодического поллинга выписки
    (``sync_bank_provider``) и realtime-вебхука «Операция по счёту»
    (``/webhooks/tbank/account-operation``). Дедуп по ``(provider,
    provider_operation_id)`` делает повторную доставку (поллинг после вебхука, дубль-фаер
    вебхука, перекрытие периодов выписки) идемпотентной — UPDATE вместо второй вставки,
    баланс не задваивается. ⚠️ operationId T-Банка НЕ стабилен: банк присваивает РАЗНЫЕ
    operationId одной и той же операции при вебхуке vs повторном поллинге выписки → дедуп только
    по нему её не ловит (прод: гарант-фонд/tbank_main задваивались). Поэтому есть fallback-дедуп
    по стабильному ключу выписки (document_number + счёт + направление + сумма + дата операции).
    Порядок шагов критичен и совпадает с прежним поведением поллинга:
    upsert → ``sync_own_accounts`` (регистрирует новые счета и включает правило внутренних
    переводов) → (tbank) свод ручных пендинг-чеков Кассы ДО классификации →
    ``run_classification_rules`` (внутри парует внутренние переводы по уже сохранённым ногам).
    """
    # Сериализуем ингест одного провайдера (поллинг vs вебхук vs ручной /bank-sync) на
    # уровне транзакции: снимает гонку SELECT-then-INSERT и двойной claim prebooked-платежа
    # двумя почти одновременными прогонами. Лок берётся в текущей транзакции, освобождается
    # на commit.
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
        {"key": f"bank_ingest:{provider}"},
    )

    inserted = 0
    updated = 0
    # Внутрибатчевый дедуп по operationId: перекрывающиеся периоды выписки могут вернуть одну
    # операцию дважды — без in-memory карты два add дали бы IntegrityError на flush (строки
    # ещё не во flush, SELECT их не видит).
    seen: dict[str, BankOperation] = {}
    # Стабильный ключ выписки на случай нестабильного operationId (см. docstring): один и тот же
    # платёжный документ на одном счёте с тем же направлением/суммой/датой = та же операция.
    # account_id+direction+amount в ключе, чтобы НЕ слить встречные ноги перевода (у них общий
    # document_number, но разные счёт/направление).
    seen_stable: dict[tuple, BankOperation] = {}
    for operation in operations:
        account = await _account_for_operation(session, provider, operation)
        account_id = account.id if account else None
        stable_key = (
            (
                account_id,
                operation.document_number,
                operation.direction,
                operation.amount,
                operation.operation_date,
            )
            if operation.document_number
            else None
        )
        batch_row = seen.get(operation.provider_operation_id)
        if batch_row is None and stable_key is not None:
            batch_row = seen_stable.get(stable_key)
        if batch_row is not None:
            # Дубль в этом же батче — переприменяем поля, но НЕ двоим счётчики.
            _update_bank_operation(batch_row, operation, account_id)
            continue
        existing = await session.scalar(
            select(BankOperation).where(
                BankOperation.provider == provider,
                BankOperation.provider_operation_id == operation.provider_operation_id,
            )
        )
        # Fallback: тот же документ пришёл под НОВЫМ operationId (вебхук↔поллинг) → это дубль,
        # обновляем существующую строку, а не вставляем вторую.
        if existing is None and stable_key is not None:
            existing = await session.scalar(
                select(BankOperation).where(
                    BankOperation.provider == provider,
                    BankOperation.account_id == account_id,
                    BankOperation.document_number == operation.document_number,
                    BankOperation.direction == operation.direction,
                    BankOperation.amount == operation.amount,
                    BankOperation.operation_date == operation.operation_date,
                )
            )
        if existing is None:
            existing = _bank_operation_from_normalized(operation, account_id)
            session.add(existing)
            inserted += 1
        else:
            await _flag_amount_change_on_classified(session, provider, existing, operation)
            _update_bank_operation(existing, operation, account_id)
            updated += 1
        seen[operation.provider_operation_id] = existing
        if stable_key is not None:
            seen_stable[stable_key] = existing

    await session.flush()
    own_accounts_added = await sync_own_accounts(session, provider=provider)
    if provider == "tbank":
        # Свести ручные пендинг-чеки Кассы с только что импортированными card-операциями ДО
        # общей классификации — тогда сматченные операции уже не уйдут в needs_review.
        from app.services.kassa.cheque import (
            match_card_refund_operations,
            match_pending_cheque_operations,
        )

        await match_pending_cheque_operations(session)
        # Возвраты карт-покупок (refundIn) — к чекам, ждущим возврат: тоже ДО классификации,
        # чтобы возврат не осел в needs_review.
        await match_card_refund_operations(session)
        await session.flush()
    pending_operations = (
        await session.scalars(
            select(BankOperation).where(
                BankOperation.provider == provider,
                BankOperation.classification_status == "pending",
            )
        )
    ).all()
    classification = await run_classification_rules(session, pending_operations)
    # Налоговый факт-слой: списания на реквизиты ФНС/СФР → tax_payment (слой «Факт»
    # вычета и сверки). Отдельно от классификатора ДДС намеренно: факт нужен независимо
    # от того, размечена ли операция статьёй. Ошибка здесь не должна ронять ингест —
    # выписка важнее, недоделанное доберёт следующий прогон (сервис идемпотентен).
    tax_facts: dict | None = None
    try:
        from app.services.taxes.bank_facts import sync_tax_facts_from_bank

        tax_facts = (await sync_tax_facts_from_bank(session)).as_dict()
    except Exception:  # noqa: BLE001 - факт-слой не должен останавливать приём выписки
        logger.warning("bank ingest: налоговый факт-слой не отработал", exc_info=True)
    return {
        "inserted": inserted,
        "updated": updated,
        "own_accounts_added": own_accounts_added,
        "classification": classification.__dict__,
        "tax_facts": tax_facts,
    }


def _client_for_provider(provider: str, session: AsyncSession) -> SberClient | TbankClient:
    if provider == "sber":
        return SberClient(session)
    if provider == "tbank":
        return TbankClient(session)
    raise ValueError(f"Unsupported bank provider: {provider}")


def _bank_sync_providers() -> tuple[str, ...]:
    providers: list[str] = []
    for raw_provider in get_settings().bank_sync_providers.split(","):
        provider = raw_provider.strip().casefold()
        if not provider:
            continue
        if provider not in SUPPORTED_BANK_PROVIDERS:
            logger.warning("Ignoring unsupported bank sync provider: %s", provider)
            continue
        if provider not in providers:
            providers.append(provider)
    return tuple(providers) or ("tbank",)


async def _default_sync_period(session: AsyncSession, provider: str) -> tuple[date, date]:
    latest = await session.scalar(
        select(func.max(BankOperation.operation_date)).where(BankOperation.provider == provider)
    )
    today = datetime.now(MOSCOW_TZ).date()
    if latest is None:
        return today - timedelta(days=30), today
    return latest - timedelta(days=1), today


async def _upsert_accounts_from_metadata(
    session: AsyncSession, provider: str, metadata: list[AccountMeta]
) -> None:
    for row in metadata:
        account = await _account_for_number(session, provider, row.account_number)
        if account is None:
            account = await _claim_seed_account(session, provider)
        if account is None:
            account = Account(
                bank_code=provider,
                account_number=row.account_number,
                bic=row.bic,
                legal_entity=LEGAL_ENTITY,
                currency=row.currency,
                status="active",
            )
            session.add(account)
            await session.flush()
        else:
            account.account_number = account.account_number or row.account_number
            account.bic = row.bic or account.bic
            account.currency = row.currency or account.currency
            account.status = "active"
        await _upsert_own_account_registry(session, provider, account, row)
    await session.flush()


async def _account_for_operation(
    session: AsyncSession, provider: str, operation: NormalizedBankOperation
) -> Account | None:
    number = clean_digits(operation.account_number)
    if not number:
        return None
    account = await _account_for_number(session, provider, number)
    if account is not None:
        return account
    account = await _claim_seed_account(session, provider)
    if account is None:
        account = Account(
            bank_code=provider,
            account_number=number,
            legal_entity=LEGAL_ENTITY,
            currency=operation.currency,
            status="active",
        )
        session.add(account)
    else:
        account.account_number = number
        account.currency = operation.currency or account.currency
    await session.flush()
    return account


async def _account_for_number(
    session: AsyncSession, provider: str, account_number: str
) -> Account | None:
    number = clean_digits(account_number)
    if not number:
        return None
    return await session.scalar(
        select(Account).where(
            Account.bank_code == provider,
            Account.account_number == number,
        )
    )


async def _claim_seed_account(session: AsyncSession, provider: str) -> Account | None:
    return await session.scalar(
        select(Account)
        .join(Wallet, Wallet.account_id == Account.id)
        .where(
            Account.bank_code == provider,
            Account.account_number.is_(None),
            Wallet.type == "bank",
            Wallet.status == "active",
            Wallet.is_internal_transfer_eligible.is_(True),
        )
        .order_by(Wallet.code)
    )


async def _upsert_own_account_registry(
    session: AsyncSession, provider: str, account: Account, metadata: AccountMeta
) -> None:
    if not account.account_number:
        return
    row = await session.scalar(
        select(OwnAccountsRegistry).where(
            OwnAccountsRegistry.bank_code == provider,
            OwnAccountsRegistry.account_number == account.account_number,
        )
    )
    if row is None:
        session.add(
            OwnAccountsRegistry(
                bank_code=provider,
                account_number=account.account_number,
                bic=metadata.bic or account.bic,
                legal_entity_inn=metadata.legal_entity_inn,
                account_id=account.id,
                is_active=True,
                metadata_json={"source": "bank_metadata"},
            )
        )
    else:
        row.bic = metadata.bic or account.bic or row.bic
        row.legal_entity_inn = metadata.legal_entity_inn or row.legal_entity_inn
        row.account_id = account.id
        row.is_active = True


def _bank_operation_from_normalized(
    operation: NormalizedBankOperation, account_id: uuid.UUID | None
) -> BankOperation:
    return BankOperation(
        provider=operation.provider,
        provider_operation_id=operation.provider_operation_id,
        account_id=account_id,
        operation_date=operation.operation_date,
        posted_at=operation.posted_at,
        direction=operation.direction,
        amount=operation.amount,
        currency=operation.currency,
        counterparty_name_raw=operation.counterparty_name_raw,
        counterparty_inn_raw=operation.counterparty_inn_raw,
        counterparty_account_raw=operation.counterparty_account_raw,
        payment_purpose=operation.payment_purpose,
        document_number=operation.document_number,
        raw_payload=operation.raw_payload,
        classification_status="pending",
    )


async def _flag_amount_change_on_classified(
    session: AsyncSession,
    provider: str,
    existing: BankOperation,
    operation: NormalizedBankOperation,
) -> None:
    """Не дать сумме/направлению уже классифицированной операции «уехать» молча.

    При рефайре (поллинг после вебхука / правка проводки банком) ``_update_bank_operation``
    обновит ``BankOperation.amount`` (баланс по выписке станет верным), но связанный
    ``CashflowTransaction`` журнала ДДС НЕ пересоберётся (re-classify идёт только по
    ``pending``) → баланс и журнал разойдутся. Журнал автоматически не трогаем (чтобы не
    затереть ручную разметку), а заводим reconciliation-кейс на ручную сверку. Если сумма
    та же — кейса нет (типичный рефайр того же значения проходит молча).
    """
    if existing.classification_status == "pending":
        return  # ещё не классифицирована — переразметится штатно
    if existing.amount == operation.amount and existing.direction == operation.direction:
        return
    logger.warning(
        "bank ingest %s: сумма/направление операции %s изменились после классификации "
        "(%s %s → %s %s) — кейс на сверку журнала",
        provider,
        existing.provider_operation_id,
        existing.direction,
        existing.amount,
        operation.direction,
        operation.amount,
    )
    await create_or_update_reconciliation_case(
        session,
        kind="operation_amount_changed",
        provider=provider,
        bank_operation_id=existing.id,
        payload={
            "provider_operation_id": existing.provider_operation_id,
            "old_amount": str(existing.amount),
            "new_amount": str(operation.amount),
            "old_direction": existing.direction,
            "new_direction": operation.direction,
            "classification_status": existing.classification_status,
        },
    )


def _update_bank_operation(
    row: BankOperation, operation: NormalizedBankOperation, account_id: uuid.UUID | None
) -> None:
    row.account_id = account_id
    row.operation_date = operation.operation_date
    row.posted_at = operation.posted_at
    row.direction = operation.direction
    row.amount = operation.amount
    row.currency = operation.currency
    row.counterparty_name_raw = operation.counterparty_name_raw
    row.counterparty_inn_raw = operation.counterparty_inn_raw
    row.counterparty_account_raw = operation.counterparty_account_raw
    row.payment_purpose = operation.payment_purpose
    row.document_number = operation.document_number
    row.raw_payload = operation.raw_payload


async def _create_invalid_credentials_case(
    session: AsyncSession, provider: str, error: str
) -> ReconciliationCase:
    return await create_or_update_reconciliation_case(
        session,
        kind="invalid_credentials",
        provider=provider,
        payload={"provider": provider, "error": error},
    )
