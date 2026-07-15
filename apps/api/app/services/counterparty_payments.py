"""Counterparty payment drafts → T-Bank.

Generalises the payroll bank-draft flow (``payroll_payouts`` +
``banking.tbank.build_payment_draft_api_payload``) to supplier invoices: a manager
selects one or more invoices of a single legal entity and sends them as one bank
draft. A draft is not a payment — the invoices stay unpaid until the bank feed is
matched (see ``counterparty_matching``).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.models import (
    CashflowTransaction,
    Counterparty,
    CounterpartyPayableProfile,
    CounterpartyPaymentDraft,
    DdsArticle,
    ExpenseDraftLine,
    InvoicePaymentAllocation,
    SupplierInvoice,
    Wallet,
)
from app.services.banking import BankClient, TbankClient
from app.services.banking.exceptions import BankFetchError
from app.services.banking.ip_card_requisites import (
    load_owner_approved_ip_card_requisites,
)
from app.services.banking.payout import (
    channel_provider,
    payer_account_for,
    payout_client_for,
)
from app.services.banking.tbank import build_payment_draft_api_payload
from app.services.counterparty_matching import _invoice_remaining, _recompute_status
from app.services.new_payment import ensure_expense_article_allowed

MOCK_PAYER_ACCOUNT = "00000000000000000000"
DRAFTABLE_STATUSES = frozenset({"unpaid", "partially_paid"})
DRAFT_STATUSES = frozenset({"created", "updated", "paid", "failed"})
# DDS article a manual supplier payment books to by default.
DEFAULT_SUPPLIER_ARTICLE_CODE = "payment_to_supplier"


class CounterpartyPaymentError(RuntimeError):
    """Domain error for counterparty payments (maps to HTTP 409)."""


class RequisitesNotVerifiedError(CounterpartyPaymentError):
    """Raised when a draft is attempted without verified requisites."""


def _money(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _payer_account(settings: Settings) -> str:
    if settings.tbank_api_account_number:
        return settings.tbank_api_account_number
    if settings.teplo_bank_client_mode == "mock":
        return MOCK_PAYER_ACCOUNT
    raise CounterpartyPaymentError("Не настроен T-Bank расчётный счёт плательщика")


async def _allocated_amount(session: AsyncSession, invoice_id: uuid.UUID) -> Decimal:
    total = await session.scalar(
        select(func.coalesce(func.sum(InvoicePaymentAllocation.amount), 0)).where(
            InvoicePaymentAllocation.invoice_id == invoice_id
        )
    )
    return _money(total)


def _aggregate_vat(invoices: Sequence[SupplierInvoice]) -> dict[str, Decimal]:
    """Sum VAT across the pack per rate (e.g. 10% / 22%)."""
    aggregate: dict[str, Decimal] = {}
    for invoice in invoices:
        for rate, amount in (invoice.vat_breakdown or {}).items():
            aggregate[rate] = aggregate.get(rate, Decimal(0)) + Decimal(str(amount))
    return {rate: _money(amount) for rate, amount in aggregate.items() if _money(amount) > 0}


def _vat_suffix(vat: dict[str, Decimal]) -> str:
    if not vat:
        return "Без НДС."
    parts = [
        f"{rate}% - {format(vat[rate], 'f').replace('.', ',')} руб."
        for rate in sorted(vat, key=lambda rate: float(rate))
    ]
    # Each part ends with "руб." so the last one provides the terminal period.
    return "В т.ч. НДС: " + "; ".join(parts)


def _payment_purpose(
    counterparty: Counterparty, invoices: Sequence[SupplierInvoice], vat: dict[str, Decimal]
) -> str:
    # VAT info is legally required and never truncated; fit the descriptive base
    # into the remaining budget so the whole purpose stays within 210 chars.
    suffix = _vat_suffix(vat)
    numbers = ", ".join(inv.number for inv in invoices if inv.number)
    base = f"Оплата поставщику {counterparty.name}"
    if numbers:
        base = f"{base} по счетам {numbers}"
    base = " ".join(base.split())
    budget = 210 - len(suffix) - 2
    if budget <= 0:
        return suffix[:210]
    return f"{base[:budget].rstrip()}. {suffix}"[:210]


def _informal_payment_purpose(
    counterparty: Counterparty, invoices: Sequence[SupplierInvoice]
) -> str:
    """Назначение выплаты на карту ИП под закуп у неофициального поставщика.

    Без НДС-блока (это перевод на собственную карту, а не оплата поставщику по счёту) —
    только маркировка «кому и за какие счета», она же уходит в purpose целевого резерва.
    """
    numbers = [f"№{inv.number}" for inv in invoices if inv.number]
    base = f"Закуп: {counterparty.name}"
    if numbers:
        word = "счёт" if len(numbers) == 1 else "счета"
        base = f"{base}, {word} {', '.join(numbers)}"
    return " ".join(base.split())[:210]


def _purpose_with_match_marker(purpose: str, draft_id: uuid.UUID) -> str:
    """Добавить короткую внутреннюю метку для связи черновика с выпиской.

    Старые назначения остаются валидными для точного матча целиком. Для новых платежей
    метка даёт второй независимый признак поверх ``documentNumber``. Юридически значимую
    часть назначения не режем: если лимита 210 символов не хватает, отправляем исходный
    текст без метки.
    """
    normalized = " ".join(purpose.split())
    marker = f"[TPL-{draft_id.hex[:12].upper()}]"
    tagged = f"{normalized} {marker}"
    return tagged if len(tagged) <= 210 else normalized


async def _ip_card_requisites(session: AsyncSession) -> dict[str, Any]:
    """Единый, зафиксированный владельцем получатель выплат через Сейф."""

    return await load_owner_approved_ip_card_requisites(session)


def _safe_status(status: str | None) -> str:
    # Свежесозданный черновик может быть только created/updated. Терминальные paid/failed
    # выставляются ИСКЛЮЧИТЕЛЬНО через apply_payment_status — иначе платёж минует гашение/откат
    # и навсегда выпадет из polling (фильтр status in created,updated).
    return status if status in ("created", "updated") else "created"


async def create_payment_draft_for_invoices(
    session: AsyncSession,
    *,
    invoice_ids: Sequence[uuid.UUID],
    actor_user_id: uuid.UUID | None,
    bank_client: BankClient | None = None,
) -> CounterpartyPaymentDraft:
    unique_ids = list(dict.fromkeys(invoice_ids))
    if not unique_ids:
        raise CounterpartyPaymentError("Не выбраны накладные для оплаты")

    invoices = list(
        (await session.execute(select(SupplierInvoice).where(SupplierInvoice.id.in_(unique_ids))))
        .scalars()
        .all()
    )
    if len(invoices) != len(unique_ids):
        raise CounterpartyPaymentError("Некоторые накладные не найдены")

    counterparty_ids = {inv.counterparty_id for inv in invoices}
    if len(counterparty_ids) != 1:
        raise CounterpartyPaymentError(
            "В один черновик можно собрать только накладные одного юрлица"
        )
    counterparty_id = counterparty_ids.pop()

    if any(inv.payment_status not in DRAFTABLE_STATUSES for inv in invoices):
        raise CounterpartyPaymentError(
            "Среди выбранных есть оплаченные или аннулированные накладные"
        )
    if any(inv.direction != "payable" for inv in invoices):
        raise CounterpartyPaymentError("Доходные накладные нельзя отправить в банк")
    if any(inv.draft_id is not None for inv in invoices):
        raise CounterpartyPaymentError("Некоторые накладные уже отправлены в банк")
    # Контроль ошибочных цен: подозрительную (неподтверждённую) накладную в банк не пускаем —
    # сначала человек сверяет цены и подтверждает («ОК, всё верно»).
    flagged = [inv for inv in invoices if inv.price_control_status == "flagged"]
    if flagged:
        numbers = ", ".join(f"№{inv.number}" for inv in flagged if inv.number) or "выбранные"
        raise CounterpartyPaymentError(
            f"Накладные с подозрительными ценами ({numbers}) нельзя отправить в банк, пока цены "
            "не подтверждены («ОК, всё верно» в карточке накладной)."
        )

    counterparty = await session.get(Counterparty, counterparty_id)
    if counterparty is None:
        raise CounterpartyPaymentError("Контрагент не найден")
    if counterparty.status == "archived":
        raise CounterpartyPaymentError("Контрагент в архиве — отправка в банк недоступна")
    profile = await session.scalar(
        select(CounterpartyPayableProfile).where(
            CounterpartyPayableProfile.counterparty_id == counterparty_id
        )
    )
    # Неофициальный поставщик: своих банк-реквизитов у него нет и они не проверяются —
    # черновик выписывается на карту ИП (реквизиты зарплатных выплат), деньги приходят
    # на Сейф, а контроль «поставщик получил наличные» несёт целевой резерв Сейфа,
    # который заводит paid-переход (см. bank_payment_status.apply_payment_status).
    pays_via_safe = profile is not None and profile.relationship == "informal"
    if not pays_via_safe and (profile is None or not profile.requisites_verified):
        raise RequisitesNotVerifiedError(
            "Реквизиты контрагента не подтверждены — отправка в банк недоступна"
        )

    total = Decimal(0)
    for inv in invoices:
        total += _money(inv.amount) - await _allocated_amount(session, inv.id)
    total = _money(total)
    if total <= 0:
        raise CounterpartyPaymentError("Сумма к оплате равна нулю")

    settings = get_settings()
    payer_account = _payer_account(settings)

    requisites: dict[str, Any]
    if pays_via_safe:
        requisites = await _ip_card_requisites(session)
        purpose = _informal_payment_purpose(counterparty, invoices)
    else:
        requisites = dict(profile.requisites or {})
        requisites.setdefault("recipientName", counterparty.name)
        if counterparty.inn:
            requisites.setdefault("inn", counterparty.inn)
        purpose = _payment_purpose(counterparty, invoices, _aggregate_vat(invoices))

    draft = CounterpartyPaymentDraft(
        id=uuid.uuid4(),
        counterparty_id=counterparty_id,
        document_id="",
        amount=total,
        status="created",
        pays_via_safe=pays_via_safe,
        created_by_user_id=actor_user_id,
    )
    document_id = f"teplo-cp-{draft.id}"
    draft.document_id = document_id[:64]
    purpose = _purpose_with_match_marker(purpose, draft.id)

    try:
        payload = build_payment_draft_api_payload(
            document_id=document_id,
            amount=total,
            purpose=purpose,
            requisites=requisites,
            payer_account=payer_account,
        )
    except ValueError as exc:
        raise CounterpartyPaymentError(f"Реквизиты неполны: {exc}") from exc

    client = bank_client or TbankClient(session)
    try:
        result = await client.create_payment_draft(
            document_id=document_id,
            amount=total,
            purpose=purpose,
            requisites=requisites,
            payer_account=payer_account,
        )
    except BankFetchError as exc:
        draft.status = "failed"
        draft.payload = payload
        draft.last_error = str(exc)
        session.add(draft)
        await session.commit()
        raise

    draft.status = _safe_status(result.status)
    draft.provider_ref = result.provider_ref
    draft.payload = payload
    draft.synced_at = datetime.now(tz=UTC)
    session.add(draft)
    await session.flush()
    for inv in invoices:
        inv.draft_id = draft.id
    await session.commit()
    await session.refresh(draft)
    return draft


async def create_standalone_payment_draft(
    session: AsyncSession,
    *,
    counterparty_id: uuid.UUID,
    amount: Decimal,
    prepayment_article_id: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
    bank_client: BankClient | None = None,
) -> CounterpartyPaymentDraft:
    """Черновик «банк по реквизитам» без накладной — предоплата поставщику.

    Шлёт платёж в банк по реквизитам контрагента; при статусе «исполнен»
    (``apply_payment_status``) создаёт supplier_prepayment (дебиторку), а не гасит накладные.
    """
    total = _money(amount)
    if total <= 0:
        raise CounterpartyPaymentError("Сумма платежа должна быть больше нуля")

    counterparty = await session.get(Counterparty, counterparty_id)
    if counterparty is None:
        raise CounterpartyPaymentError("Контрагент не найден")
    if counterparty.status == "archived":
        raise CounterpartyPaymentError("Контрагент в архиве — отправка в банк недоступна")
    profile = await session.scalar(
        select(CounterpartyPayableProfile).where(
            CounterpartyPayableProfile.counterparty_id == counterparty_id
        )
    )
    if profile is not None and profile.relationship == "informal":
        raise CounterpartyPaymentError(
            "Контрагент оплачивается картой/наличными — отправка в банк недоступна"
        )
    if profile is None or not profile.requisites_verified:
        raise RequisitesNotVerifiedError(
            "Реквизиты контрагента не подтверждены — отправка в банк недоступна"
        )

    article_id = prepayment_article_id
    if article_id is None:
        article_id = await session.scalar(
            select(DdsArticle.id).where(DdsArticle.code == "advance_to_supplier")
        )
    elif await session.get(DdsArticle, article_id) is None:
        raise CounterpartyPaymentError("Статья ДДС не найдена")

    settings = get_settings()
    payer_account = _payer_account(settings)
    purpose = f"Предоплата поставщику {counterparty.name}"[:210]

    draft = CounterpartyPaymentDraft(
        id=uuid.uuid4(),
        counterparty_id=counterparty_id,
        document_id="",
        amount=total,
        status="created",
        creates_prepayment=True,
        prepayment_article_id=article_id,
        created_by_user_id=actor_user_id,
    )
    document_id = f"teplo-cp-{draft.id}"
    draft.document_id = document_id[:64]

    requisites: dict[str, Any] = dict(profile.requisites or {})
    requisites.setdefault("recipientName", counterparty.name)
    if counterparty.inn:
        requisites.setdefault("inn", counterparty.inn)

    try:
        payload = build_payment_draft_api_payload(
            document_id=document_id,
            amount=total,
            purpose=purpose,
            requisites=requisites,
            payer_account=payer_account,
        )
    except ValueError as exc:
        raise CounterpartyPaymentError(f"Реквизиты неполны: {exc}") from exc

    client = bank_client or TbankClient(session)
    try:
        result = await client.create_payment_draft(
            document_id=document_id,
            amount=total,
            purpose=purpose,
            requisites=requisites,
            payer_account=payer_account,
        )
    except BankFetchError as exc:
        draft.status = "failed"
        draft.payload = payload
        draft.last_error = str(exc)
        session.add(draft)
        await session.commit()
        raise

    draft.status = _safe_status(result.status)
    draft.provider_ref = result.provider_ref
    draft.payload = payload
    draft.synced_at = datetime.now(tz=UTC)
    session.add(draft)
    await session.commit()
    await session.refresh(draft)
    return draft


@dataclass(frozen=True)
class ExpenseLineInput:
    """Строка расхода из окна «Новый платёж»: статья + сумма + назначение.

    ``counterparty_id`` определяет не только атрибуцию, но и маршрут: официальный
    контрагент оплачивается напрямую по подтверждённым реквизитам; неофициальный или
    строка без получателя — через Сейф.
    """

    article_id: uuid.UUID
    amount: Decimal
    purpose: str = ""
    counterparty_id: uuid.UUID | None = None


async def create_expense_payment_draft(
    session: AsyncSession,
    *,
    lines: Sequence[ExpenseLineInput] | None = None,
    article_id: uuid.UUID | None = None,
    amount: Decimal | None = None,
    purpose: str | None = None,
    channel: str | None = None,
    allow_official_via_safe: bool = False,
    actor_user_id: uuid.UUID | None = None,
    bank_client: BankClient | None = None,
) -> CounterpartyPaymentDraft:
    """Банковский черновик расхода из окна «Новый платёж».

    Строка с официальным/бартерным контрагентом и указанными реквизитами всегда создаёт
    прямой платёж по подтверждённым реквизитам. Если реквизиты отсутствуют, маршрут на
    карту ИП → Сейф разрешается только после явного ``allow_official_via_safe``. Флаг не
    позволяет обойти проверку уже указанных реквизитов. Банковский документ с прямым
    получателем имеет одну строку. Неофициальные контрагенты и строки без получателя
    сохраняют прежний маршрут: один транш на карту ИП → Сейф с целёвками по строкам.

    ``channel`` выбирает банк-плательщика: ``bank_draft`` (по умолчанию, Т-Банк) или
    ``bank_draft_sber`` (Сбер). Черновик выписывается тем же интерфейсом (``BankClient``),
    но клиент и счёт-плательщик берутся через фабрику ``banking.payout``; провайдер
    запоминается на черновике (``bank_provider``), чтобы транзит р/с→Сейф сел на счёт
    нужного банка. Сбер задаётся ТОЛЬКО в этом (свободном) маршруте — накладные,
    предоплата и informal-закуп остаются в Т-Банке.
    """
    provider = channel_provider(channel) or "tbank"
    # Обратная совместимость: одиночный платёж = транш из одной строки.
    if lines is None:
        if article_id is None or amount is None:
            raise CounterpartyPaymentError("Не заданы платежи транша")
        lines = [ExpenseLineInput(article_id=article_id, amount=amount, purpose=purpose or "")]
    if not lines:
        raise CounterpartyPaymentError("Добавьте хотя бы один платёж")

    # Валидируем каждую строку (статья годна для свободного вывода, сумма > 0) и собираем
    # нормализованные (статья, сумма, назначение); пустое назначение → имя статьи.
    prepared: list[tuple[DdsArticle, Decimal, str, uuid.UUID | None]] = []
    direct_recipient: tuple[Counterparty, CounterpartyPayableProfile] | None = None
    total = Decimal("0")
    for line in lines:
        line_amount = _money(line.amount)
        if line_amount <= 0:
            raise CounterpartyPaymentError("Сумма платежа должна быть больше нуля")
        article = await session.get(DdsArticle, line.article_id)
        if article is None:
            raise CounterpartyPaymentError("Статья ДДС не найдена")
        try:
            ensure_expense_article_allowed(article)
        except ValueError as exc:
            raise CounterpartyPaymentError(str(exc)) from exc
        # Назначение необязательно: пустое → имя статьи (в банк и в целёвку Сейфа).
        line_purpose = " ".join((line.purpose or "").split()) or article.name
        if line.counterparty_id is not None:
            counterparty = await session.get(Counterparty, line.counterparty_id)
            if counterparty is None or counterparty.status == "archived":
                raise CounterpartyPaymentError("Контрагент не найден")
            profile = await session.scalar(
                select(CounterpartyPayableProfile).where(
                    CounterpartyPayableProfile.counterparty_id == counterparty.id
                )
            )
            if profile is None:
                raise CounterpartyPaymentError("Платёжный профиль контрагента не найден")
            if profile.relationship != "informal":
                if profile.requisites:
                    if not profile.requisites_verified:
                        raise RequisitesNotVerifiedError(
                            "Реквизиты контрагента не подтверждены — отправка в банк недоступна"
                        )
                    if direct_recipient is not None and direct_recipient[0].id != counterparty.id:
                        raise CounterpartyPaymentError(
                            "Платежи по реквизитам разных контрагентов оформите отдельно"
                        )
                    direct_recipient = (counterparty, profile)
                elif not allow_official_via_safe:
                    raise CounterpartyPaymentError(
                        "У официального контрагента не указаны реквизиты — "
                        "подтвердите вывод на карту ИП"
                    )
        prepared.append((article, line_amount, line_purpose, line.counterparty_id))
        total += line_amount

    if total <= 0:
        raise CounterpartyPaymentError("Сумма платежа должна быть больше нуля")
    if direct_recipient is not None and len(prepared) != 1:
        raise CounterpartyPaymentError(
            "Платёж по реквизитам оформляется одной строкой на одного контрагента"
        )

    settings = get_settings()
    payer_account = payer_account_for(settings, provider)
    if not payer_account:
        raise CounterpartyPaymentError(f"Не настроен расчётный счёт плательщика ({provider})")
    is_direct = direct_recipient is not None
    if is_direct:
        counterparty, profile = direct_recipient
        requisites = dict(profile.requisites or {})
        requisites.setdefault("recipientName", counterparty.name)
        if counterparty.inn:
            requisites.setdefault("inn", counterparty.inn)
    else:
        requisites = await _ip_card_requisites(session)

    single = len(prepared) == 1
    draft = CounterpartyPaymentDraft(
        id=uuid.uuid4(),
        counterparty_id=direct_recipient[0].id if direct_recipient else None,
        document_id="",
        amount=total,
        status="created",
        pays_via_safe=not is_direct,
        bank_provider=provider,
        # Одиночный платёж дублирует статью/назначение в поля черновика (наглядность и
        # обратная совместимость); транш из нескольких строк несёт разбивку только в строках.
        target_article_id=prepared[0][0].id if single else None,
        target_purpose=prepared[0][2] if single else None,
        created_by_user_id=actor_user_id,
    )
    document_id = f"teplo-cp-{draft.id}"
    draft.document_id = document_id[:64]
    # Назначение в банк (лимит платёжки): одиночный — назначение строки, транш — сводка.
    if single:
        bank_purpose = prepared[0][2][:210]
    else:
        summary = "; ".join(line_purpose for _, _, line_purpose, _ in prepared)
        bank_purpose = f"Транш {len(prepared)} платежей: {summary}"[:210]

    try:
        # payload — запись черновика в едином (Т-Банк) формате; ``accountNumber`` = счёт
        # выбранного провайдера, по нему paid-переход находит банк-кошелёк плательщика.
        payload = build_payment_draft_api_payload(
            document_id=document_id,
            amount=total,
            purpose=bank_purpose,
            requisites=requisites,
            payer_account=payer_account,
        )
    except ValueError as exc:
        raise CounterpartyPaymentError(f"Реквизиты неполны: {exc}") from exc

    client = bank_client or payout_client_for(provider, session)
    try:
        result = await client.create_payment_draft(
            document_id=document_id,
            amount=total,
            purpose=bank_purpose,
            requisites=requisites,
            payer_account=payer_account,
        )
    except BankFetchError as exc:
        draft.status = "failed"
        draft.payload = payload
        draft.last_error = str(exc)
        session.add(draft)
        await session.commit()
        raise

    draft.status = _safe_status(result.status)
    draft.provider_ref = result.provider_ref
    draft.payload = payload
    draft.synced_at = datetime.now(tz=UTC)
    session.add(draft)
    await session.flush()  # черновик должен существовать до строк (FK draft_id)
    for position, (article, line_amount, line_purpose, line_cp_id) in enumerate(prepared):
        session.add(
            ExpenseDraftLine(
                draft_id=draft.id,
                article_id=article.id,
                counterparty_id=line_cp_id,
                amount=line_amount,
                purpose=line_purpose,
                position=position,
            )
        )
    await session.commit()
    await session.refresh(draft)
    return draft


async def create_bank_safe_topup_draft(
    session: AsyncSession,
    *,
    amount: Decimal,
    purpose: str | None,
    channel: str | None = None,
    actor_user_id: uuid.UUID | None = None,
    bank_client: BankClient | None = None,
) -> CounterpartyPaymentDraft:
    """Черновик-пополнение Сейфа с банка (внутренний перевод банк→Сейф из «Нового платежа»).

    Как свободный вывод — банковский черновик на карту ИП, но с флагом ``topup_only``:
    при оплате paid-переход книжит ТОЛЬКО транзит р/с→Сейф, целёвку не создаёт (Сейф
    просто пополняется). ``channel`` выбирает банк-плательщика (Т-Банк по умолчанию / Сбер).
    """
    provider = channel_provider(channel) or "tbank"
    amt = _money(amount)
    if amt <= 0:
        raise CounterpartyPaymentError("Сумма перевода должна быть больше нуля")

    settings = get_settings()
    payer_account = payer_account_for(settings, provider)
    if not payer_account:
        raise CounterpartyPaymentError(f"Не настроен расчётный счёт плательщика ({provider})")
    requisites = await _ip_card_requisites(session)
    text = " ".join((purpose or "").split()) or "Пополнение Сейфа"

    draft = CounterpartyPaymentDraft(
        id=uuid.uuid4(),
        counterparty_id=None,
        document_id="",
        amount=amt,
        status="created",
        pays_via_safe=True,
        topup_only=True,
        bank_provider=provider,
        target_purpose=text,
        created_by_user_id=actor_user_id,
    )
    document_id = f"teplo-cp-{draft.id}"
    draft.document_id = document_id[:64]
    bank_purpose = text[:210]
    try:
        payload = build_payment_draft_api_payload(
            document_id=document_id,
            amount=amt,
            purpose=bank_purpose,
            requisites=requisites,
            payer_account=payer_account,
        )
    except ValueError as exc:
        raise CounterpartyPaymentError(f"Реквизиты неполны: {exc}") from exc

    client = bank_client or payout_client_for(provider, session)
    try:
        result = await client.create_payment_draft(
            document_id=document_id,
            amount=amt,
            purpose=bank_purpose,
            requisites=requisites,
            payer_account=payer_account,
        )
    except BankFetchError as exc:
        draft.status = "failed"
        draft.payload = payload
        draft.last_error = str(exc)
        session.add(draft)
        await session.commit()
        raise

    draft.status = _safe_status(result.status)
    draft.provider_ref = result.provider_ref
    draft.payload = payload
    draft.synced_at = datetime.now(tz=UTC)
    session.add(draft)
    await session.commit()
    await session.refresh(draft)
    return draft


async def cancel_payment_draft(session: AsyncSession, *, draft_id: uuid.UUID) -> None:
    """Unlink invoices and remove a draft that has not been paid/matched yet."""
    draft = await session.get(CounterpartyPaymentDraft, draft_id)
    if draft is None:
        raise CounterpartyPaymentError("Черновик не найден")
    if draft.status == "paid":
        raise CounterpartyPaymentError("Черновик уже оплачен — отмена недоступна")
    invoices = list(
        (await session.execute(select(SupplierInvoice).where(SupplierInvoice.draft_id == draft_id)))
        .scalars()
        .all()
    )
    for inv in invoices:
        inv.draft_id = None
    await session.delete(draft)
    await session.commit()


async def get_payment_draft(
    session: AsyncSession, draft_id: uuid.UUID
) -> CounterpartyPaymentDraft | None:
    return await session.get(CounterpartyPaymentDraft, draft_id)


async def list_draft_invoices(session: AsyncSession, draft_id: uuid.UUID) -> list[SupplierInvoice]:
    return list(
        (
            await session.execute(
                select(SupplierInvoice)
                .where(SupplierInvoice.draft_id == draft_id)
                .order_by(SupplierInvoice.invoice_date)
            )
        )
        .scalars()
        .all()
    )


async def pay_invoice_from_wallet(
    session: AsyncSession,
    *,
    invoice_id: uuid.UUID,
    wallet_id: uuid.UUID,
    amount: Decimal,
    operation_date: date,
    article_id: uuid.UUID | None = None,
    comment: str | None = None,
    actor_user_id: uuid.UUID | None = None,
) -> SupplierInvoice:
    """Manually pay (part of) a payable invoice from a DDS wallet — bypasses the bank.

    Creates a real DDS expense (``cashflow_transaction``, source_kind
    ``counterparty_payment``) on the chosen wallet and allocates it to the invoice,
    supporting splits and partial payments; the unpaid remainder can still be sent to
    the bank as a draft. Intended for cash/card payments that don't go through the bank
    transfer flow — the draft+reconcile path stays the preferred route for bank
    transfers (it also verifies requisites). If a manager does pay from a bank wallet,
    the bank-feed classifier links the imported statement operation to this entry rather
    than booking a second expense (see ``_find_prebooked_payment``), so no double expense.
    """
    invoice = await session.get(SupplierInvoice, invoice_id)
    if invoice is None:
        raise CounterpartyPaymentError("Накладная не найдена")
    if invoice.payment_status == "void":
        raise CounterpartyPaymentError("Накладная аннулирована")
    if invoice.direction != "payable":
        raise CounterpartyPaymentError("Доходную накладную нельзя оплатить со счёта")

    requested = _money(amount)
    remaining = await _invoice_remaining(session, invoice)
    if requested <= 0 or requested > remaining:
        raise CounterpartyPaymentError("Сумма оплаты вне допустимого остатка")

    wallet = await session.get(Wallet, wallet_id)
    if wallet is None or wallet.status != "active":
        raise CounterpartyPaymentError("Счёт не найден или неактивен")

    resolved_article_id = article_id
    if resolved_article_id is None:
        resolved_article_id = await session.scalar(
            select(DdsArticle.id).where(DdsArticle.code == DEFAULT_SUPPLIER_ARTICLE_CODE)
        )
    elif await session.get(DdsArticle, resolved_article_id) is None:
        raise CounterpartyPaymentError("Статья ДДС не найдена")

    await _apply_wallet_payment(
        session,
        invoice=invoice,
        wallet=wallet,
        amount=requested,
        operation_date=operation_date,
        article_id=resolved_article_id,
        comment=comment,
        actor_user_id=actor_user_id,
    )
    await _recompute_status(session, invoice)
    await session.commit()
    await session.refresh(invoice)
    return invoice


async def _apply_wallet_payment(
    session: AsyncSession,
    *,
    invoice: SupplierInvoice,
    wallet: Wallet,
    amount: Decimal,
    operation_date: date,
    article_id: uuid.UUID | None,
    comment: str | None,
    actor_user_id: uuid.UUID | None,
) -> CashflowTransaction:
    """Create the DDS expense + cash allocation for one wallet payment WITHOUT committing —
    the commit-less core shared by ``pay_invoice_from_wallet`` and the split orchestrator.
    Always creates a NEW ``CashflowTransaction`` (so no two invoices share one cash txn).
    The caller resolves the article, validates the remaining, recomputes status and commits.
    """
    requested = _money(amount)
    purpose = f"Оплата накладной {invoice.number}" if invoice.number else "Оплата поставщику"
    transaction = CashflowTransaction(
        wallet_id=wallet.id,
        direction="out",
        amount=requested,
        operation_date=operation_date,
        article_id=article_id,
        counterparty_id=invoice.counterparty_id,
        source_kind="counterparty_payment",
        source_id=invoice.id,
        payment_purpose=purpose,
        comment=comment,
        quality_status="final",
    )
    session.add(transaction)
    await session.flush()

    session.add(
        InvoicePaymentAllocation(
            invoice_id=invoice.id,
            source_kind="cash",
            cashflow_transaction_id=transaction.id,
            amount=requested,
            created_by_user_id=actor_user_id,
        )
    )
    await session.flush()
    return transaction
