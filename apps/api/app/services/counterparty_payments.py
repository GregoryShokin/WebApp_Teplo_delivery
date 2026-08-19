"""Counterparty payment drafts → T-Bank.

Generalises the payroll bank-draft flow (``payroll_payouts`` +
``banking.tbank.build_payment_draft_api_payload``) to supplier invoices: a manager
selects one or more invoices of a single legal entity and sends them as one bank
draft. A draft is not a payment — the invoices stay unpaid until the bank feed is
matched (see ``counterparty_matching``).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace
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
from app.services import clock
from app.services import supplier_service_periods as service_periods
from app.services.asset_analytics import AssetLinkError, resolve_asset_context
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
from app.services.location_analytics import (
    LocationAnalyticsError,
    resolve_location_context,
)
from app.services.new_payment import ensure_expense_article_allowed
from app.services.owner_analytics import OwnerAnalyticsError, ensure_owner_context
from app.services.vat import normalize_vat_rate, vat_amount_for_rate

MOCK_PAYER_ACCOUNT = "00000000000000000000"
DRAFTABLE_STATUSES = frozenset({"unpaid", "partially_paid"})
DRAFT_STATUSES = frozenset({"created", "updated", "paid", "failed"})
ARCHIVED_COUNTERPARTY_STATUSES = frozenset({"archived", "inactive"})
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
    """Sum VAT across the pack per rate (e.g. 10% / 22%).

    Пустой ключ — «сумма налога известна, ставка нет». Так приходит ЭДО (СБИС даёт только
    «сумма» и «сумма без НДС») и часть распознанных счетов, где напечатана одна строка НДС.
    Без этой ветки налог таких счетов не доезжал до назначения вовсе: разбивка по ставкам
    пуста, и платёжка уходила в банк с «Без НДС.» при живом налоге в документе.
    """
    aggregate: dict[str, Decimal] = {}
    for invoice in invoices:
        breakdown = invoice.vat_breakdown or {}
        if not breakdown and _money(invoice.vat_total) > 0:
            breakdown = {"": invoice.vat_total}
        for rate, amount in breakdown.items():
            aggregate[rate] = aggregate.get(rate, Decimal(0)) + Decimal(str(amount))
    return {rate: _money(amount) for rate, amount in aggregate.items() if _money(amount) > 0}


def _vat_suffix(vat: dict[str, Decimal]) -> str:
    if not vat:
        return "Без НДС."
    # Ставку не выдумываем: где её нет, печатаем одну сумму — банку этого достаточно, а
    # вычисленный из отношения процент на счёте со смешанными ставками был бы неправдой.
    # Безставочная доля идёт последней (сортировка ставит её в конец через float('inf')).
    parts = [
        (f"{rate}% - " if rate else "") + f"{format(vat[rate], 'f').replace('.', ',')} руб."
        for rate in sorted(vat, key=lambda rate: float(rate) if rate else float("inf"))
    ]
    # Each part ends with "руб." so the last one provides the terminal period.
    return "В т.ч. НДС: " + "; ".join(parts)


def vat_suffix_for_rate(total: Decimal, rate: str | None) -> str:
    """Хвост назначения по ставке: «В т.ч. НДС: 22% - 1439,90 руб.» либо «Без НДС.».

    Тот же формат, что у платёжки по счёту (``_vat_suffix``): банк и налоговая читают
    строку одинаково, откуда бы платёж ни пришёл — из счёта или из окна «Новый платёж».
    """
    amount = vat_amount_for_rate(total, rate)
    if amount <= 0:
        return _vat_suffix({})
    return _vat_suffix({normalize_vat_rate(rate): amount})


# Место, которое занимает метка связи черновика с выпиской: « [TPL-0123456789AB]».
MATCH_MARKER_BUDGET = len(" [TPL-") + 12 + len("]")


def _with_vat_suffix(base: str, suffix: str, *, reserve: int = 0) -> str:
    """Пристыковать НДС-хвост к назначению, ни при каких длинах его не срезая.

    Налог юридически значим, описательная часть — нет: в лимит платёжки (210 символов)
    ужимается именно она. Хвост стоит последним и заканчивается точкой.

    ``reserve`` — сколько символов оставить следующему хвосту (метке ``[TPL-…]``).
    Без него длинное описание вместе с НДС занимало все 210, и
    ``_purpose_with_match_marker`` молча выбрасывал метку — второй независимый признак
    матча черновика с операцией выписки. Ужимать надо описание, а не терять признак.
    """
    base = " ".join(base.split())
    budget = 210 - len(suffix) - 2 - reserve
    if budget <= 0:
        return suffix[:210]
    # rstrip(" ."): назначение из окна человек часто заканчивает точкой сам — иначе
    # получилось бы «Оплата связи.. Без НДС.».
    return f"{base[:budget].rstrip(' .')}. {suffix}"[: 210 - reserve]


def _payment_purpose(
    counterparty: Counterparty, invoices: Sequence[SupplierInvoice], vat: dict[str, Decimal]
) -> str:
    # VAT info is legally required and never truncated; fit the descriptive base
    # into the remaining budget so the whole purpose stays within 210 chars.
    numbers = ", ".join(inv.number for inv in invoices if inv.number)
    base = f"Оплата поставщику {counterparty.name}"
    if numbers:
        base = f"{base} по счетам {numbers}"
    return _with_vat_suffix(base, _vat_suffix(vat))


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


_MATCH_MARKER_RE = re.compile(r"\s*\[TPL-[0-9A-F]{12}\]", re.IGNORECASE)


def strip_match_marker(purpose: str) -> str:
    """Срезать метку ``[TPL-…]`` там, где назначение идёт людям (проводки, резервы),
    а не в банк: техметка нужна только для матча черновика с операцией выписки."""
    return _MATCH_MARKER_RE.sub("", purpose).strip()


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
    channel: str | None = None,
    allow_official_via_safe: bool = False,
) -> CounterpartyPaymentDraft:
    """Черновик оплаты накладных. ``channel`` выбирает банк-плательщика: ``bank_draft``
    (по умолчанию, Т-Банк) или ``bank_draft_sber`` (Сбер) — тем же интерфейсом, что и
    свободный вывод из окна «Новый платёж»; провайдер запоминается на черновике.

    ``allow_official_via_safe`` — осознанное подтверждение оператора «у получателя нет
    реквизитов, вывести на карту ИП» (галочка в окне отправки счёта). Работает ровно так же,
    как в окне «Новый платёж»: маршрут подменяется ТОЛЬКО когда реквизитов в карточке нет
    вовсе; там, где счёт получателя известен, флаг ничего не обходит."""
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
    # Правило 4 канона: закрывающий документ действует своей датой. Пока она не наступила
    # (activation_status='pending'), долга нет — платить нечего, и целить в него деньги нельзя.
    # Это ЕДИНСТВЕННАЯ дверь, проставляющая draft_id, поэтому проверка здесь закрывает контур
    # целиком: пере-разбор письма документ в черновике не передатирует (_resync_closing_after_edit
    # работает только при draft_id is None), а гашение наличными из Сейфа
    # (_settle_draft_invoices) ходит строго по накладным черновика.
    # Чинить ниже по течению нельзя: у накладной оплаченного черновика draft_id НЕ снимается,
    # а auto_settle_invoice_from_open_prepayments для документов с draft_id всегда возвращает 0
    # (защита от двойной оплаты). Заведи мы на такой платёж предоплату — она зависла бы навсегда,
    # а документ в свою дату встал бы ещё и кредиторкой: ДЗ и КЗ на одни и те же деньги.
    not_in_force = [
        inv for inv in invoices if inv.doc_kind == "closing" and inv.activation_status != "active"
    ]
    informational = [inv for inv in invoices if inv.informational]
    if informational:
        first = informational[0]
        number = f"№{first.number}" if first.number else "Документ"
        raise CounterpartyPaymentError(
            f"{number} справочный: расход по нему уже начислен договором услуги, "
            "и оплачивать его отдельно нельзя. Платите по договору."
        )
    if not_in_force:
        first = not_in_force[0]
        number = f"№{first.number}" if first.number else "документ"
        date_hint = f" вступает в силу {first.invoice_date:%d.%m.%Y}" if first.invoice_date else ""
        raise CounterpartyPaymentError(
            f"{number}{date_hint} — до этой даты он не является долгом и оплатить его нельзя. "
            "Дождитесь даты документа или оплатите по счёту."
        )
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
    if counterparty.status in ARCHIVED_COUNTERPARTY_STATUSES:
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
    # Тот же маршрут — официальному получателю, у которого реквизитов нет вовсе (арендодатель
    # с возмещением коммуналки, физлицо со счётом на бумаге): оператор ставит галочку «без
    # реквизитов» в окне отправки и берёт на себя вывод на карту ИП. Условие ровно как в окне
    # «Новый платёж»: подменяем маршрут, только пока карточка пуста. Появились реквизиты —
    # платим по ним, и флаг не отменяет их подтверждения: иначе одна забытая галочка увела бы
    # деньги мимо поставщика, у которого счёт в системе уже есть.
    if not pays_via_safe and allow_official_via_safe and not (profile and profile.requisites):
        pays_via_safe = True
    if profile is not None and profile.service_period_required:
        missing_period = [inv for inv in invoices if inv.service_period_status != "ready"]
        if missing_period:
            raise CounterpartyPaymentError(
                "Для контрагента обязателен период оказания услуг — заполните его у каждого счёта"
            )
    # Разные периоды в одном платеже по счетам РАЗРЕШЕНЫ (владелец 02.08.2026): за один визит
    # энергетик привозит два акта — доплату за прошлый месяц и аванс за текущий, — а платятся
    # они одной суммой одному получателю, потому что перевод один. Дробить платёж ради учёта
    # значит заставлять человека врать банку.
    # Разрешать безопасно, потому что период у пачки счетов НИГДЕ не общий: начисление расхода
    # заводится по каждому счёту (``sync_invoice_accrual`` ниже), дебиторка оплаченного счёта
    # берёт период с него же (``_invoice_period_fields``), а поле периода у черновика никто не
    # читает — ``_expense_line_period`` ходит по ``ExpenseDraftLine``, которых у черновика из
    # накладных нет вовсе. Для свободных строк расхода запрет остаётся: там период у платежа
    # действительно один на всех.
    period_keys = {(inv.service_period_start, inv.service_period_end) for inv in invoices}
    if not pays_via_safe and (profile is None or not profile.requisites_verified):
        raise RequisitesNotVerifiedError(
            "Реквизиты контрагента не подтверждены — отправка в банк недоступна"
        )

    total = Decimal(0)
    for inv in invoices:
        if inv.barter_role == "loan":
            # Долг займа частично гасится ТОВАРОМ (леджер BarterReturnLine) и прощённым
            # недовесом — в аллокациях этого нет, поэтому «сумма − аллокации» завышена. Без
            # поправки черновик уходит в банк на полную сумму, и мы платим за уже возвращённое.
            from app.services.warehouse_invoices import loan_settled_value

            total += _money(inv.amount) - await loan_settled_value(session, inv)
        else:
            total += _money(inv.amount) - await _allocated_amount(session, inv.id)
    total = _money(total)
    if total <= 0:
        raise CounterpartyPaymentError("Сумма к оплате равна нулю")

    settings = get_settings()
    provider = channel_provider(channel) or "tbank"
    payer_account = payer_account_for(settings, provider) if channel else _payer_account(settings)
    if not payer_account:
        raise CounterpartyPaymentError(f"Не настроен расчётный счёт плательщика ({provider})")

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

    # Период на черновике заполняем, только когда он у счетов один. Выбрать один из двух —
    # соврать о платеже; пусто честно отсылает к периодам самих счетов, где они и живут.
    period_start, period_end = next(iter(period_keys)) if len(period_keys) == 1 else (None, None)
    draft = CounterpartyPaymentDraft(
        id=uuid.uuid4(),
        counterparty_id=counterparty_id,
        document_id="",
        amount=total,
        status="created",
        pays_via_safe=pays_via_safe,
        service_period_start=period_start,
        service_period_end=period_end,
        created_by_user_id=actor_user_id,
        bank_provider=provider,
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

    client = bank_client or payout_client_for(provider, session)
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
        await service_periods.sync_invoice_accrual(session, inv)
    await session.commit()
    await session.refresh(draft)
    return draft


async def create_standalone_payment_draft(
    session: AsyncSession,
    *,
    counterparty_id: uuid.UUID,
    amount: Decimal,
    prepayment_article_id: uuid.UUID | None = None,
    vat_rate: str | None = None,
    actor_user_id: uuid.UUID | None = None,
    bank_client: BankClient | None = None,
) -> CounterpartyPaymentDraft:
    """Черновик «банк по реквизитам» без накладной — предоплата поставщику.

    Шлёт платёж в банк по реквизитам контрагента; при статусе «исполнен»
    (``apply_payment_status``) создаёт supplier_prepayment (дебиторку), а не гасит накладные.

    ``vat_rate`` — ставка НДС аванса; налог выделяется из суммы «в том числе» и уходит хвостом
    назначения. Пустая ставка даёт «Без НДС.» — до этого назначение аванса молчало о налоге
    вовсе, хотя платёж уходит внешнему получателю по реквизитам.
    """
    total = _money(amount)
    if total <= 0:
        raise CounterpartyPaymentError("Сумма платежа должна быть больше нуля")

    counterparty = await session.get(Counterparty, counterparty_id)
    if counterparty is None:
        raise CounterpartyPaymentError("Контрагент не найден")
    if counterparty.status in ARCHIVED_COUNTERPARTY_STATUSES:
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
    vat_rate_clean = normalize_vat_rate(vat_rate)
    vat_value = vat_amount_for_rate(total, vat_rate_clean)
    purpose = _with_vat_suffix(
        f"Предоплата поставщику {counterparty.name}",
        vat_suffix_for_rate(total, vat_rate_clean),
        reserve=MATCH_MARKER_BUDGET,
    )

    draft = CounterpartyPaymentDraft(
        id=uuid.uuid4(),
        counterparty_id=counterparty_id,
        document_id="",
        amount=total,
        status="created",
        creates_prepayment=True,
        prepayment_article_id=article_id,
        vat_rate=vat_rate_clean or None,
        vat_amount=vat_value if vat_value > 0 else None,
        created_by_user_id=actor_user_id,
    )
    document_id = f"teplo-cp-{draft.id}"
    draft.document_id = document_id[:64]
    purpose = _purpose_with_match_marker(purpose, draft.id)

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
    service_period_start: date | None = None
    service_period_end: date | None = None
    # Абонентская оплата вперёд: число месяцев и признак помесячного признания расхода.
    # Переезжают на дебиторку при оплате — по ним она делится на месячные доли.
    service_period_months: int | None = None
    auto_recognize_monthly: bool = False
    location_id: uuid.UUID | None = None
    lease_id: uuid.UUID | None = None
    # Основное средство строки: окно «Новый платёж» — главный вход покупки оборудования.
    asset_id: uuid.UUID | None = None


@dataclass(frozen=True)
class PreparedExpenseLine:
    article: DdsArticle
    amount: Decimal
    purpose: str
    counterparty_id: uuid.UUID | None
    service_period_start: date | None
    service_period_end: date | None
    service_period_months: int | None
    auto_recognize_monthly: bool
    location_id: uuid.UUID | None
    lease_id: uuid.UUID | None
    asset_id: uuid.UUID | None


async def create_expense_payment_draft(
    session: AsyncSession,
    *,
    lines: Sequence[ExpenseLineInput] | None = None,
    article_id: uuid.UUID | None = None,
    amount: Decimal | None = None,
    purpose: str | None = None,
    vat_rate: str | None = None,
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

    ``vat_rate`` — ставка НДС платежа («22», «10», …) или пусто. Налог выделяется из итоговой
    суммы черновика («в том числе») и уходит хвостом назначения; пустая ставка даёт «Без НДС.».
    Ставка и вычисленная сумма запоминаются на черновике — платёжку потом читает не только банк.

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
    prepared: list[PreparedExpenseLine] = []
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
        try:
            location_context = await resolve_location_context(
                session,
                article=article,
                location_id=line.location_id,
                lease_id=line.lease_id,
                counterparty_id=line.counterparty_id,
                on_date=clock.moscow_today(),
            )
        except LocationAnalyticsError as exc:
            raise CounterpartyPaymentError(str(exc)) from exc
        try:
            await ensure_owner_context(
                session, article=article, counterparty_id=line.counterparty_id
            )
        except OwnerAnalyticsError as exc:
            raise CounterpartyPaymentError(str(exc)) from exc
        # Основное средство — то же правило и та же причина проверять ДО записей. Гейт нужен
        # именно здесь, а не при разборе будущей проводки: к тому моменту человек, который
        # знает, ЧТО купили, давно ушёл — платёж создаёт один, а выписку разбирает другой и
        # через неделю. Так покупки и уходили в расход мимо баланса.
        try:
            asset_context = await resolve_asset_context(
                session, article=article, asset_id=line.asset_id, amount=line_amount
            )
        except AssetLinkError as exc:
            raise CounterpartyPaymentError(str(exc)) from exc
        # Аренда знает арендодателя — он и определяет маршрут (informal → Сейф).
        line = replace(line, counterparty_id=location_context.counterparty_id)
        # Назначение необязательно: пустое → имя статьи (в банк и в целёвку Сейфа).
        line_purpose = " ".join((line.purpose or "").split()) or article.name
        if line.counterparty_id is not None:
            counterparty = await session.get(Counterparty, line.counterparty_id)
            # in ARCHIVED_...: легаси-статус 'inactive' — тоже архив, иначе гард дыряв.
            if counterparty is None or counterparty.status in ARCHIVED_COUNTERPARTY_STATUSES:
                raise CounterpartyPaymentError("Контрагент не найден")
            profile = await session.scalar(
                select(CounterpartyPayableProfile).where(
                    CounterpartyPayableProfile.counterparty_id == counterparty.id
                )
            )
            if profile is None:
                raise CounterpartyPaymentError("Платёжный профиль контрагента не найден")
            try:
                if profile.service_period_required or (
                    line.service_period_start is not None or line.service_period_end is not None
                ):
                    service_periods.validate_period(
                        line.service_period_start, line.service_period_end
                    )
            except service_periods.ServicePeriodError as exc:
                raise CounterpartyPaymentError(str(exc)) from exc
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
        prepared.append(
            PreparedExpenseLine(
                article=article,
                amount=line_amount,
                purpose=line_purpose,
                counterparty_id=line.counterparty_id,
                service_period_start=line.service_period_start,
                service_period_end=line.service_period_end,
                service_period_months=line.service_period_months,
                auto_recognize_monthly=line.auto_recognize_monthly,
                location_id=location_context.location_id,
                lease_id=location_context.lease_id,
                asset_id=asset_context.asset_id,
            )
        )
        total += line_amount

    if total <= 0:
        raise CounterpartyPaymentError("Сумма платежа должна быть больше нуля")
    if direct_recipient is not None and len(prepared) != 1:
        raise CounterpartyPaymentError(
            "Платёж по реквизитам оформляется одной строкой на одного контрагента"
        )
    period_keys = {
        (line.service_period_start, line.service_period_end)
        for line in prepared
        if line.counterparty_id is not None
    }
    if len(period_keys) > 1:
        raise CounterpartyPaymentError(
            "Платежи с разными периодами оказания услуг оформите отдельными черновиками"
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
    draft_period = next(iter(period_keys)) if period_keys else (None, None)
    vat_rate_clean = normalize_vat_rate(vat_rate)
    vat_value = vat_amount_for_rate(total, vat_rate_clean)
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
        target_article_id=prepared[0].article.id if single else None,
        target_purpose=prepared[0].purpose if single else None,
        service_period_start=draft_period[0],
        service_period_end=draft_period[1],
        vat_rate=vat_rate_clean or None,
        vat_amount=vat_value if vat_value > 0 else None,
        created_by_user_id=actor_user_id,
    )
    document_id = f"teplo-cp-{draft.id}"
    draft.document_id = document_id[:64]
    # Назначение в банк (лимит платёжки): одиночный — назначение строки, транш — сводка.
    # НДС-хвост общий на черновик: банк списывает одну сумму, из неё налог и выделяется.
    if single:
        bank_purpose = prepared[0].purpose
    else:
        summary = "; ".join(line.purpose for line in prepared)
        bank_purpose = f"Транш {len(prepared)} платежей: {summary}"
    bank_purpose = _with_vat_suffix(
        bank_purpose, vat_suffix_for_rate(total, vat_rate_clean), reserve=MATCH_MARKER_BUDGET
    )
    bank_purpose = _purpose_with_match_marker(bank_purpose, draft.id)

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
    for position, prepared_line in enumerate(prepared):
        line = ExpenseDraftLine(
            draft_id=draft.id,
            article_id=prepared_line.article.id,
            counterparty_id=prepared_line.counterparty_id,
            amount=prepared_line.amount,
            purpose=prepared_line.purpose,
            position=position,
            service_period_start=prepared_line.service_period_start,
            service_period_end=prepared_line.service_period_end,
            service_period_months=prepared_line.service_period_months,
            auto_recognize_monthly=prepared_line.auto_recognize_monthly,
            service_period_source=(
                "manual" if prepared_line.service_period_start is not None else None
            ),
            location_id=prepared_line.location_id,
            lease_id=prepared_line.lease_id,
            asset_id=prepared_line.asset_id,
        )
        session.add(line)
        await session.flush()
        await service_periods.sync_expense_line_accrual(session, line)
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
    bank_purpose = _purpose_with_match_marker(text[:210], draft.id)
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
    if invoice.barter_role == "loan":
        # Долг займа частично гасится ТОВАРОМ (леджер BarterReturnLine, не аллокации), поэтому
        # остаток по аллокациям завышен: без этой поправки прямая дверь оплаты закрыла бы уже
        # возвращённое вторично. Ленивый импорт — warehouse_invoices импортирует этот модуль.
        from app.services.warehouse_invoices import loan_settled_value

        remaining = _money(invoice.amount) - await loan_settled_value(session, invoice)
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
