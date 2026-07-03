from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta
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
    OwnAccountsRegistry,
    PayrollBankDraft,
    ReconciliationCase,
    SalaryAdvanceBankDraft,
    Wallet,
)
from app.services.bank_payment_status import apply_payment_status
from app.services.banking.base import AccountMeta, NormalizedBankOperation, clean_digits
from app.services.banking.classifier import (
    create_or_update_reconciliation_case,
    reconcile_needs_review_prebooked,
    run_classification_rules,
)
from app.services.banking.exceptions import BankCredentialsError, BankFetchError
from app.services.banking.own_accounts import sync_own_accounts
from app.services.banking.sber import SberClient
from app.services.banking.tbank import TbankClient
from app.services.couriers.iiko_attendance_sync import sync_attendance
from app.services.couriers.iiko_olap_sync import sync_courier_olap_deliveries
from app.services.couriers.shift_matching import recalculate_matches
from app.services.kassa.iiko_cashshift_sync import sync_iiko_cashshifts
from app.services.payroll_advance_service import apply_advance_draft_status
from app.services.payroll_payouts import apply_payroll_draft_status

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
MOSCOW_TZ = ZoneInfo("Europe/Moscow")
LEGAL_ENTITY = "ИП Шокина Е.А."
IIKO_COURIER_JOB_RETRIES = 3
SUPPORTED_BANK_PROVIDERS = ("sber", "tbank")


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
    # счёту» (если вебхук не доставит, поллинг доберёт). Черновики этим путём НЕ гасятся (их
    # доводит статусный webhook по provider_ref); анти-дубль с оплатой черновика — prebooked-
    # механизм классификатора. Дедуп по operationId + стабильному ключу выписки в
    # ingest_operations делает повторный прогон идемпотентным.
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
    """Поднять в «Требует разбора» ручные чеки, которые банк не подтвердил дольше порога."""
    from app.services.kassa.cheque import escalate_overdue_pending_cheques

    async with AsyncSessionLocal() as session:
        created = await escalate_overdue_pending_cheques(session)
        await session.commit()
    if created:
        logger.info("Эскалация пендинг-чеков: %s новых кейсов «банк не передал»", created)


async def poll_payment_statuses() -> None:
    """РУЧНОЙ добор статуса исходящих платежей.

    Автоматический scheduler отключён (снят @scheduler.scheduled_job): основной путь гашения
    накладных/выплат — статусный webhook платёжного документа по ``provider_ref``. Функция
    оставлена для ручного добора статусов, в т.ч. поимки удаления черновика в банке (статус
    DELETED → откат накладных в «неоплачено»)."""
    async with AsyncSessionLocal() as session:
        await run_payment_status_poll(session)


async def run_payment_status_poll(
    session: AsyncSession, *, client: TbankClient | None = None
) -> dict[str, int]:
    """Опросить статус по всем «отправленным в банк» черновикам и применить его. Вынесено из
    джоба для тестируемости (можно передать фейковый клиент)."""
    client = client or TbankClient(session)
    drafts = (
        await session.scalars(
            select(CounterpartyPaymentDraft).where(
                CounterpartyPaymentDraft.status.in_(("created", "updated")),
                CounterpartyPaymentDraft.provider_ref.is_not(None),
            )
        )
    ).all()
    result = {"checked": 0, "paid": 0, "failed": 0, "errors": 0, "reconciled": 0}
    for draft in drafts:
        try:
            raw = await client.get_payment_status(draft.provider_ref or "")
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
            raw = await client.get_payment_status(payroll_draft.provider_ref or "")
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
            raw = await client.get_payment_status(advance_draft.provider_ref or "")
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

    # Сводим «требующие проверки» операции с prebooked-проводками, появившимися позже них
    # (гонка вебхук↔поллинг) — иначе один платёж висит двумя строками в журнале.
    result["reconciled"] = await reconcile_needs_review_prebooked(session)
    await session.commit()
    return result


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
        result = await sync_courier_olap_deliveries(
            session, date_from=date_from, date_to=date_to
        )
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
            (account_id, operation.document_number, operation.direction,
             operation.amount, operation.operation_date)
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
        from app.services.kassa.cheque import match_pending_cheque_operations

        await match_pending_cheque_operations(session)
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
    return {
        "inserted": inserted,
        "updated": updated,
        "own_accounts_added": own_accounts_added,
        "classification": classification.__dict__,
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
