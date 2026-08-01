"""Ежемесячное обязательство по договору услуги — для тех, кто документов не присылает.

ЗАЧЕМ. Услуга оказывается каждый месяц, документов нет, а долг всё равно есть. Пока его никто
не считал, происходило вот что: платёж приходил, гасить ему было нечего, и деньги оседали
дебиторкой навсегда — а расход не признавался ни в одном месяце.

Платёж ИП Наумченко 30.07.2026 — ровно этот случай, и он же показывает, почему начисление
важнее «признания из платежа»: 9 000 ₽ заплачены за апрель-июнь, то есть за период, который
УЖЕ ЗАКОНЧИЛСЯ. Дебиторки там не должно быть в принципе — должна была помесячно копиться
КРЕДИТОРКА по 3 000 ₽, а платёж её гасить. Признание же, привязанное к платежу, восстановило
бы историю задним числом из денег, которых в апреле ещё не было.

КАК. Тот же механизм, что у аренды (``lease_accruals``): раз в месяц по каждому действующему
договору заводится закрывающий ``SupplierInvoice``. Никакой своей таблицы обязательств — только
так долг попадает в готовые витрины: КЗ считается по накладным, признание расхода в P&L —
по ``SupplierExpenseAccrual``.

ОТЛИЧИЕ ОТ АРЕНДЫ — ОДНО: ``source='self_billed'``. Это уже заведённый признак «первички нет»
(миграция 0233): расход идёт в управленческий P&L, но не в налоговую базу УСН, и в сверке он
подписан «признано нами · без первички». Аренда живёт под своим ``source='lease'``, потому что
у неё есть договор с живой подписью, — здесь такого договора может не быть вовсе.

ГАШЕНИЕ НЕ ПИШЕМ РУКАМИ. Документ заводится неоплаченным, и дальше всё делает штатный контур:
``apply_closing_document`` сам гасит его открытой дебиторкой, если платёж уже был (предоплата),
а если денег ещё не было — обязательство честно висит кредиторкой, и её погасит будущий платёж
правилом 1 канона. Порядок событий разный, механика одна.

ПРИШЁЛ НАСТОЯЩИЙ ДОКУМЕНТ — наше начисление аннулируется (``supersede_self_billed``), потому
что оно всегда слабее первички. Поэтому у договора с ``documents_mode='official'`` начисление
не делается вовсе: там документ придёт, и удваивать расход в ожидании нечего.
"""

from __future__ import annotations

import calendar
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CounterpartyServiceAgreement, SupplierInvoice
from app.services import supplier_prepayments, supplier_service_periods
from app.services.subscription_accruals import SELF_BILLED_SOURCE, covers_month

__all__ = [
    "accrue_month",
    "agreement_covers_month",
    "agreement_external_id",
    "ensure_agreement_invoice",
]


def agreement_external_id(agreement_id: uuid.UUID, month: date) -> str:
    """Ключ идемпотентности под существующим уникумом ``source + external_id``.

    Префикс свой (``svc``), чтобы начисления по договору не сталкивались с признанием из
    предоплаты (``self:``) — source у них общий, а сущности разные.
    """
    return f"svc:{agreement_id}:{month:%Y-%m}"


def month_bounds(month: date) -> tuple[date, date]:
    first = month.replace(day=1)
    last = first.replace(day=calendar.monthrange(first.year, first.month)[1])
    return first, last


def agreement_covers_month(agreement: CounterpartyServiceAgreement, month: date) -> bool:
    first, last = month_bounds(month)
    if agreement.started_on > last:
        return False
    return agreement.ended_on is None or agreement.ended_on >= first


async def covered_by_agreement(
    session: AsyncSession,
    counterparty_id: uuid.UUID,
    *,
    on: date,
    article_id: uuid.UUID | None = None,
) -> bool:
    """Закрыт ли этот период договором услуги, по которому долг считаем сами.

    Отвечает на вопрос «нужен ли нам вообще документ контрагента за этот период». Если
    договор действует — источник истины он, а пришедшая бумага только информация: провести
    её как настоящую значило бы отменить собственное начисление и переписать расход месяца
    суммой, которую мы не заказывали.

    Статью сверяем, когда она известна у обеих сторон: у контрагента бывает несколько услуг,
    и договор на одну не должен обесценивать документ по другой.
    """
    conditions = [
        CounterpartyServiceAgreement.counterparty_id == counterparty_id,
        CounterpartyServiceAgreement.accrual_enabled.is_(True),
        CounterpartyServiceAgreement.documents_mode == "informal",
        CounterpartyServiceAgreement.started_on <= on,
        or_(
            CounterpartyServiceAgreement.ended_on.is_(None),
            CounterpartyServiceAgreement.ended_on >= on,
        ),
    ]
    if article_id is not None:
        conditions.append(
            or_(
                CounterpartyServiceAgreement.dds_article_id.is_(None),
                CounterpartyServiceAgreement.dds_article_id == article_id,
            )
        )
    found = await session.scalar(select(CounterpartyServiceAgreement.id).where(*conditions))
    return found is not None


async def _real_closing_exists(
    session: AsyncSession, agreement: CounterpartyServiceAgreement, month: date
) -> bool:
    """Пришёл ли за этот месяц НАСТОЯЩИЙ закрывающий документ по этой же услуге.

    Проверять обязательно, и одного ключа идемпотентности мало: замещение
    (``supersede_self_billed``) освобождает ключ месяца — иначе месяц нельзя было бы признать
    заново. Без этой проверки ночная джоба на следующий же день завела бы начисление поверх
    пришедшего акта, и расход задвоился бы.

    Сравниваем ещё и статью ДДС: у контрагента бывает несколько услуг (у АО «АЙКО» две
    лицензии), и документ по одной из них не должен глушить начисление по другой. Документ
    без статьи считаем относящимся к любой — иначе он не закроет ничего.
    """
    found = await session.scalar(
        select(SupplierInvoice.id).where(
            SupplierInvoice.counterparty_id == agreement.counterparty_id,
            SupplierInvoice.direction == "payable",
            SupplierInvoice.doc_kind == "closing",
            SupplierInvoice.payment_status != "void",
            SupplierInvoice.source != SELF_BILLED_SOURCE,
            or_(
                SupplierInvoice.dds_article_id.is_(None),
                SupplierInvoice.dds_article_id == agreement.dds_article_id,
            ),
            covers_month(
                SupplierInvoice.service_period_start,
                SupplierInvoice.service_period_end,
                SupplierInvoice.invoice_date,
                month,
            ),
        )
    )
    return found is not None


async def ensure_agreement_invoice(
    session: AsyncSession,
    agreement: CounterpartyServiceAgreement,
    month: date,
    *,
    as_of: date | None = None,
) -> SupplierInvoice | None:
    """Завести обязательство по договору услуги за месяц. Идемпотентно. Не коммитит.

    ``None`` — начислять нечего: выключено, документы обещаны, нет статьи, договор не действовал
    в этом месяце, месяц ещё не кончился или обязательство уже есть.
    """
    if not agreement.accrual_enabled:
        return None
    if agreement.documents_mode != "informal":
        # Документы обещаны — ждём их. Начислив сами, мы получили бы двойной расход в тот день,
        # когда УПД всё-таки придёт.
        return None
    if agreement.dds_article_id is None:
        # Без статьи расход некуда отнести, а платёж по ней не найдёт этот договор.
        return None
    if Decimal(agreement.monthly_amount) <= 0:
        return None
    if not agreement_covers_month(agreement, month):
        return None

    today = as_of or date.today()
    _first, last = month_bounds(month)
    # Строго после окончания месяца: весь последний день услуга ещё оказывается. Та же граница,
    # что у признания расходов и у самоактов из предоплаты, — иначе долг возникал бы на день
    # раньше, чем услуга оказана.
    if last >= today:
        return None

    external_id = agreement_external_id(agreement.id, month)
    existing = await session.scalar(
        select(SupplierInvoice).where(
            SupplierInvoice.source == SELF_BILLED_SOURCE,
            SupplierInvoice.external_id == external_id,
        )
    )
    if existing is not None:
        return None
    if await _real_closing_exists(session, agreement, month):
        return None

    period_start, period_end = month_bounds(month)
    invoice = SupplierInvoice(
        counterparty_id=agreement.counterparty_id,
        source=SELF_BILLED_SOURCE,
        external_id=external_id,
        direction="payable",
        doc_kind="closing",
        # finance — иначе ни правило 1 канона, ни авто-зачёт предоплат этот документ не увидят.
        operational_scope="finance",
        number=f"{agreement.title} {month:%m.%Y}",
        invoice_date=period_end,
        amount=Decimal(agreement.monthly_amount),
        dds_article_id=agreement.dds_article_id,
        service_period_start=period_start,
        service_period_end=period_end,
        service_period_source="self_billed",
        # ready — без этого sync_invoice_accrual молча не создаст строку признания расхода.
        service_period_status="ready",
    )
    session.add(invoice)
    await session.flush()

    # Порядок важен: сначала проводим документ (гашение открытой дебиторки, если платили
    # вперёд), затем строка признания расхода в P&L. Аллокацию руками НЕ пишем — при
    # постоплате её и не должно быть: обязательство висит кредиторкой до платежа.
    await supplier_prepayments.apply_closing_document(session, invoice, as_of=as_of)
    await supplier_service_periods.sync_invoice_accrual(session, invoice)
    return invoice


async def accrue_month(
    session: AsyncSession, as_of: date, *, months_back: int = 3
) -> list[SupplierInvoice]:
    """Начислить закончившиеся месяцы по всем действующим договорам услуг. Не коммитит.

    Смотрим не только прошлый месяц: договор могли завести задним числом (услуга шла, а в
    системе её не было), и тогда начислить надо всю его историю в пределах окна. Идемпотентность
    делает это безопасным — уже начисленный месяц вернёт ``None``.
    """
    agreements = (
        await session.scalars(
            select(CounterpartyServiceAgreement).where(
                CounterpartyServiceAgreement.accrual_enabled.is_(True),
                CounterpartyServiceAgreement.documents_mode == "informal",
            )
        )
    ).all()

    created: list[SupplierInvoice] = []
    for agreement in agreements:
        first_of_current = as_of.replace(day=1)
        for step in range(1, months_back + 1):
            year = first_of_current.year
            month_index = first_of_current.month - step
            while month_index <= 0:
                month_index += 12
                year -= 1
            month = date(year, month_index, 1)
            if month < agreement.started_on.replace(day=1):
                continue
            invoice = await ensure_agreement_invoice(session, agreement, month, as_of=as_of)
            if invoice is not None:
                created.append(invoice)
    return created
