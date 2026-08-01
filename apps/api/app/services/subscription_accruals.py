"""Абонентские платежи: помесячное признание расхода без закрывающего документа.

ЗАДАЧА. Часть поставщиков работает по абонентской плате помесячно, а закрывающие документы
присылает не всегда: Наумченко не присылает вовсе, Микроэль присылает, но за май пропустила.
Платят им нередко вперёд за несколько месяцев — 9 000 ₽ за апрель-июнь одним переводом. Такие
деньги висели дебиторкой целиком до конца всего периода, а расход не признавался ни в одном
месяце: на 01.08.2026 так стояло 311 969 ₽ у десяти контрагентов.

РЕШЕНИЕ — ТО ЖЕ, ЧТО У АРЕНДЫ. Раз в месяц по такому платежу заводится внутренний закрывающий
документ на долю периода: 9 000 за три месяца → 3 000 в апреле, 3 000 в мае, 3 000 в июне. Он
гасит дебиторку и признаёт расход в своём месяце. Ровно этим механизмом уже живёт аренда
(``lease_accruals``): арендодатель документов не выставляет, но долг известен и считается сам.
Никакой новой сущности здесь нет — тот же ``SupplierInvoice``, поэтому все витрины (ДЗ/КЗ,
признание расходов, сверка) видят его без единой правки.

ЧЕМ ОН ОТЛИЧАЕТСЯ ОТ НАСТОЯЩЕГО ДОКУМЕНТА. ``source='self_billed'`` — контрагент его не
выставлял и не подтверждал. Расход в управленческом P&L он даёт, а в налоговую базу УСН идти
не может: без первички инспекция такой расход снимет. Признак нужен именно полем, а не
догадкой по косвенным данным.

ЗАМЕЩЕНИЕ НАСТОЯЩИМ УПД. Если документ за тот же период всё-таки приходит, внутренний
аннулируется (``supersede_self_billed``), а дебиторку гасит настоящий. Без этого расход
задвоился бы: один раз по самоакту, второй — по УПД, и P&L месяца вырос бы вдвое.

ОКРУГЛЕНИЕ. Доли считаются как ``сумма / N`` вниз до копейки, а последний месяц забирает
остаток: 10 000 / 3 = 3 333,33 + 3 333,33 + 3 333,34. Иначе на длинных периодах копейки
накапливались бы и дебиторка не закрывалась бы до нуля.
"""

from __future__ import annotations

import calendar
import uuid
from datetime import date
from decimal import ROUND_DOWN, Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import InvoicePaymentAllocation, SupplierInvoice, SupplierPrepayment
from app.services import supplier_service_periods as periods

SELF_BILLED_SOURCE = "self_billed"
# Предоплаты, которые ещё держат дебиторку и потому подлежат помесячному признанию.
OPEN_PREPAYMENT_STATUSES = ("open", "partially_settled")


def month_bounds(month: date) -> tuple[date, date]:
    first = month.replace(day=1)
    return first, first.replace(day=calendar.monthrange(first.year, first.month)[1])


def add_months(month: date, count: int) -> date:
    total = month.month - 1 + count
    return date(month.year + total // 12, total % 12 + 1, 1)


def self_billed_external_id(prepayment_id: uuid.UUID, month: date) -> str:
    """Ключ идемпотентности под существующим уникумом ``source + external_id``.

    Повторный прогон джобы (и ручной пересбор) ничего не задваивает — ровно как у аренды.
    """
    return f"self:{prepayment_id}:{month:%Y-%m}"


def monthly_shares(amount: Decimal, months: int) -> list[Decimal]:
    """Разбить сумму на ``months`` долей; остаток от округления забирает последняя.

    Вниз, а не арифметически: при делении 10 000 / 3 три доли по 3 333,33 дают недобор
    в копейку, и его отдаём последнему месяцу. Округление вверх дало бы перебор — дебиторка
    ушла бы в минус на копейку, а это уже расхождение баланса.
    """
    if months < 1:
        raise ValueError("Период должен покрывать хотя бы один месяц")
    total = periods.money(amount)
    base = (total / months).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    shares = [base] * (months - 1)
    shares.append(total - base * (months - 1))
    return shares


def covered_months(prepayment: SupplierPrepayment) -> list[date]:
    """Месяцы, которые покрывает платёж: от начала периода, по одному на каждый месяц."""
    if prepayment.service_period_start is None:
        return []
    count = prepayment.service_period_months or 1
    first = prepayment.service_period_start.replace(day=1)
    return [add_months(first, index) for index in range(count)]


async def _real_closing_exists(
    session: AsyncSession, prepayment: SupplierPrepayment, month: date
) -> bool:
    """Есть ли за этот месяц НАСТОЯЩИЙ закрывающий документ от контрагента.

    Если есть — самоакт не нужен: расход подтверждён первичкой, и второй документ на тот же
    период удвоил бы и расход, и гашение дебиторки.
    """
    first, last = month_bounds(month)
    found = await session.scalar(
        select(SupplierInvoice.id).where(
            SupplierInvoice.counterparty_id == prepayment.counterparty_id,
            SupplierInvoice.direction == "payable",
            SupplierInvoice.doc_kind == "closing",
            SupplierInvoice.payment_status != "void",
            SupplierInvoice.source != SELF_BILLED_SOURCE,
            SupplierInvoice.service_period_start <= last,
            SupplierInvoice.service_period_end >= first,
        )
    )
    return found is not None


async def ensure_month_accrual(
    session: AsyncSession,
    prepayment: SupplierPrepayment,
    month: date,
    *,
    as_of: date,
) -> SupplierInvoice | None:
    """Завести внутренний закрывающий документ за месяц. Идемпотентно. Не коммитит.

    ``None`` возвращается, когда признавать нечего: помесячный режим выключен, месяц ещё не
    закончился, самоакт уже есть, настоящий документ пришёл или дебиторка исчерпана.
    """
    if not prepayment.auto_recognize_monthly:
        return None
    if prepayment.status not in OPEN_PREPAYMENT_STATUSES:
        return None
    months = covered_months(prepayment)
    if month.replace(day=1) not in months:
        return None

    _first, last = month_bounds(month)
    # Строго после окончания месяца: весь последний день услуга ещё оказывается. Та же
    # граница, что у recognize_due_expenses, — иначе расход признавался бы на день раньше.
    if last >= as_of:
        return None

    external_id = self_billed_external_id(prepayment.id, month)
    existing = await session.scalar(
        select(SupplierInvoice).where(
            SupplierInvoice.source == SELF_BILLED_SOURCE,
            SupplierInvoice.external_id == external_id,
        )
    )
    if existing is not None:
        return None
    if await _real_closing_exists(session, prepayment, month):
        return None

    index = months.index(month.replace(day=1))
    share = monthly_shares(periods.money(prepayment.amount), len(months))[index]
    remaining = periods.money(prepayment.amount) - periods.money(prepayment.amount_settled)
    if remaining <= 0:
        return None
    # Дебиторка могла частично уйти на настоящий УПД за другой месяц — тогда признаём только
    # то, что осталось: документ не имеет права гасить больше, чем есть.
    share = min(share, remaining)

    period_start, period_end = month_bounds(month)
    invoice = SupplierInvoice(
        counterparty_id=prepayment.counterparty_id,
        source=SELF_BILLED_SOURCE,
        external_id=external_id,
        direction="payable",
        doc_kind="closing",
        # finance — иначе ни правило 1 канона, ни авто-зачёт предоплат этот документ не увидят.
        operational_scope="finance",
        number=f"Без документа {month:%m.%Y}",
        invoice_date=period_end,
        amount=share,
        dds_article_id=prepayment.article_id,
        service_period_start=period_start,
        service_period_end=period_end,
        service_period_source="self_billed",
        # ready — без этого sync_invoice_accrual молча не создаст строку признания расхода.
        service_period_status="ready",
    )
    session.add(invoice)
    await session.flush()

    # Гасим ИМЕННО ту дебиторку, из которой документ родился, а не FIFO по всем открытым:
    # у контрагента может висеть несколько платежей за разные периоды, и FIFO закрыл бы
    # чужой — расход месяца сошёлся бы, а связь «эти деньги за этот период» потерялась.
    session.add(
        InvoicePaymentAllocation(
            invoice_id=invoice.id,
            prepayment_id=prepayment.id,
            amount=share,
            source_kind="prepayment",
        )
    )
    prepayment.amount_settled = periods.money(prepayment.amount_settled) + share
    prepayment.status = (
        "settled"
        if periods.money(prepayment.amount_settled) >= periods.money(prepayment.amount)
        else "partially_settled"
    )
    invoice.payment_status = "paid"
    await periods.sync_invoice_accrual(session, invoice)
    await session.flush()
    return invoice


async def accrue_due_months(
    session: AsyncSession, *, as_of: date | None = None, commit: bool = True
) -> list[SupplierInvoice]:
    """Признать все истёкшие месяцы по абонентским платежам. Идемпотентно."""
    today = as_of or date.today()
    prepayments = list(
        (
            await session.scalars(
                select(SupplierPrepayment).where(
                    SupplierPrepayment.auto_recognize_monthly.is_(True),
                    SupplierPrepayment.status.in_(OPEN_PREPAYMENT_STATUSES),
                )
            )
        ).all()
    )
    created: list[SupplierInvoice] = []
    for prepayment in prepayments:
        for month in covered_months(prepayment):
            invoice = await ensure_month_accrual(session, prepayment, month, as_of=today)
            if invoice is not None:
                created.append(invoice)
    if commit:
        await session.commit()
    else:
        await session.flush()
    return created


async def supersede_self_billed(
    session: AsyncSession, invoice: SupplierInvoice
) -> list[SupplierInvoice]:
    """Аннулировать самоакты за период пришедшего НАСТОЯЩЕГО закрывающего документа.

    Иначе месяц оказался бы закрыт дважды: самоактом и УПД, — и расход в P&L, и гашение
    дебиторки удвоились бы. Возвращённая дебиторка тут же доступна настоящему документу:
    его собственное гашение идёт следом обычным путём (``apply_closing_document``).

    Пришедший УПД сильнее самоакта всегда: первичка есть только у него, а самоакт — наша
    временная замена на случай, когда документа нет.
    """
    if invoice.source == SELF_BILLED_SOURCE or invoice.doc_kind != "closing":
        return []
    if invoice.service_period_start is None or invoice.service_period_end is None:
        return []

    victims = list(
        (
            await session.scalars(
                select(SupplierInvoice).where(
                    SupplierInvoice.counterparty_id == invoice.counterparty_id,
                    SupplierInvoice.source == SELF_BILLED_SOURCE,
                    SupplierInvoice.payment_status != "void",
                    SupplierInvoice.service_period_start <= invoice.service_period_end,
                    SupplierInvoice.service_period_end >= invoice.service_period_start,
                )
            )
        ).all()
    )
    for victim in victims:
        allocations = list(
            (
                await session.scalars(
                    select(InvoicePaymentAllocation).where(
                        InvoicePaymentAllocation.invoice_id == victim.id
                    )
                )
            ).all()
        )
        for allocation in allocations:
            if allocation.prepayment_id is not None:
                prepayment = await session.get(SupplierPrepayment, allocation.prepayment_id)
                if prepayment is not None:
                    prepayment.amount_settled = max(
                        periods.money(prepayment.amount_settled) - periods.money(allocation.amount),
                        Decimal("0.00"),
                    )
                    prepayment.status = (
                        "open"
                        if periods.money(prepayment.amount_settled) <= 0
                        else "partially_settled"
                    )
            await session.delete(allocation)
        victim.payment_status = "void"
        await periods.cancel_invoice_accrual(session, victim.id)
    await session.flush()
    return victims
