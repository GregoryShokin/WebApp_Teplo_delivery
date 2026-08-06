"""Слой признанного расхода: начисления, разложенные по календарным месяцам периода услуги.

ПОЧЕМУ НЕ ЗОВЁМ ``build_expense_report`` НАПРЯМУЮ. Готовый отчёт признания схлопывает всё в
пару «месяц × статья» и теряет контрагента, а он нужен здесь дважды: чтобы сопоставить
признание с кассой той же пары и чтобы показать в подсказке, чьего документа ждём. Расширять
ключ агрегации живого отчёта нельзя — у него есть боевой потребитель, экран «Учёт ДЗ/КЗ», и
изменение мощности результата сломало бы его молча.

Поэтому здесь своя выборка, но ДВА ключевых правила переиспользуются из общего модуля, а не
пишутся заново: разбивка периода по календарным месяцам (``spread_over_months``) и подстановка
статьи из карточки контрагента. Разнобой в способе деления дал бы расхождение с экраном
признания на копейки в каждом месяце, а при сверке это ищется часами.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    CounterpartyPayableProfile,
    SupplierExpenseAccrual,
    SupplierInvoice,
)
from app.models.enums import SELF_ACCRUED_INVOICE_SOURCES, UTILITY_INVOICE_SOURCE
from app.services.expense_recognition_report import spread_over_months

#: Чем подтверждён признанный расход. Различие не косметическое: владелец 04.08.2026 спросил
#: про строку SEO — «почему пишут, что без первички, хотя по SEO приходят счета?». Счета от
#: Синапсиса действительно приходят, но признание в его режиме («счёт за период») строится
#: САМОАКТОМ по окончании месяца и закрывающего документа не ждёт вовсе. Слово «первичка»
#: читалось как упрёк там, где всё работает как задумано.
ORIGIN_DOCUMENT = "document"
ORIGIN_LEASE = "lease"
ORIGIN_UTILITY = "utility"
#: Самоакт у контрагента, от которого закрывающих не ждут по его режиму расчётов.
ORIGIN_BY_TARIFF = "by_tariff"
#: Самоакт там, где документ ЖДУТ, — вот это настоящий пробел.
ORIGIN_AWAITING_DOCUMENT = "awaiting_document"
#: Начисление по строке платежа: документа нет и не было.
ORIGIN_PAYMENT_LINE = "payment_line"

#: Режимы расчётов, при которых закрывающих документов не ждут: договор начисляет сам, тариф
#: известен заранее, разовая работа документами не сопровождается. Тот же набор, по которому
#: экран признания прячет контрагента из сводки разрывов.
DOCUMENT_NOT_EXPECTED_MODES = frozenset({"agreement", "fixed_tariff", "one_off"})


def classify_origin(invoice_source: str | None, billing_mode: str | None) -> str:
    """Чем подтверждён расход — по источнику документа и режиму расчётов контрагента."""
    if invoice_source is None:
        return ORIGIN_PAYMENT_LINE
    if invoice_source == "lease":
        return ORIGIN_LEASE
    if invoice_source == UTILITY_INVOICE_SOURCE:
        return ORIGIN_UTILITY
    if invoice_source not in SELF_ACCRUED_INVOICE_SOURCES:
        return ORIGIN_DOCUMENT
    if billing_mode in DOCUMENT_NOT_EXPECTED_MODES:
        return ORIGIN_BY_TARIFF
    return ORIGIN_AWAITING_DOCUMENT


@dataclass(slots=True)
class RecognitionBucket:
    amount: Decimal = Decimal("0.00")
    count: int = 0
    counterparties: set[uuid.UUID] = field(default_factory=set)
    accrual_ids: list[uuid.UUID] = field(default_factory=list)


@dataclass(slots=True)
class RecognitionDetail:
    """Доля одного начисления, попавшая в месяц, — строка расшифровки.

    Хранится всегда, а не по запросу: начислений за месяц полсотни, а собранные отдельным
    проходом они разошлись бы с отчётом на той же развилке, где уже ошибались, — подстановке
    статьи из карточки и делении периода по месяцам.
    """

    accrual_id: uuid.UUID
    counterparty_id: uuid.UUID | None
    article_id: uuid.UUID | None
    amount: Decimal
    service_period_start: date | None
    service_period_end: date | None
    has_primary: bool
    #: Чем подтверждён расход — см. ``ORIGIN_*``. Отдельно от ``has_primary``, потому что
    #: «документа нет» и «документа не ждут» это разные новости для владельца.
    origin: str
    #: Документ этого начисления уже оплачен, то есть за расходом стоят СВОИ деньги. Нужно
    #: сверке «оплачено, но не признано»: такое признание не вправе гасить тревогу по другому
    #: платежу. Месяц денег при этом не важен и знать его не нужно — акт за июль оплачивают
    #: и четвёртого августа.
    settled: bool = False


@dataclass(slots=True)
class RecognitionLayer:
    """Признанный расход месяца в разрезе статей и пар «контрагент × статья»."""

    by_article: dict[uuid.UUID | None, RecognitionBucket] = field(default_factory=dict)
    details: list[RecognitionDetail] = field(default_factory=list)
    by_pair: dict[tuple[uuid.UUID, uuid.UUID | None], Decimal] = field(
        default_factory=lambda: defaultdict(Decimal)
    )
    total: Decimal = Decimal("0.00")
    # Расход без статьи ДДС: в отчёт его отнести некуда, и молчать об этом нельзя.
    unattributed: Decimal = Decimal("0.00")
    # Расход, у которого закрывающего документа НЕТ, ХОТЯ ЕГО ЖДУТ. Самоакт по тарифу,
    # аренда по договору и коммуналка по расчёту арендодателя сюда НЕ входят: там документа
    # не ждут по построению, и считать их пробелом значило бы держать вечно красную цифру,
    # на которую перестанут смотреть. В управленческую прибыль всё это идёт полноправно —
    # деньги потрачены, услуга оказана.
    without_primary: Decimal = Decimal("0.00")
    without_location: Decimal = Decimal("0.00")


async def build_recognition_layer(
    session: AsyncSession, month_start: date, month_end: date
) -> RecognitionLayer:
    """Признанный расход, относящийся к месяцу.

    Начисление хранит одну дату признания, но документ может покрывать несколько месяцев:
    акт за квартал целиком падал бы в последний. Здесь сумма раскладывается по календарным
    месяцам периода равными долями — теми же, что применяет признание из предоплаты.
    """
    profiles = (
        await session.execute(
            select(
                CounterpartyPayableProfile.counterparty_id,
                CounterpartyPayableProfile.default_dds_article_id,
                CounterpartyPayableProfile.service_billing_mode,
            )
        )
    ).all()
    default_articles = {
        counterparty_id: article_id
        for counterparty_id, article_id, _mode in profiles
        if article_id is not None
    }
    billing_modes = {counterparty_id: mode for counterparty_id, _article, mode in profiles}

    rows = (
        await session.execute(
            select(
                SupplierExpenseAccrual,
                SupplierInvoice.source,
                SupplierInvoice.payment_status,
            )
            .outerjoin(SupplierInvoice, SupplierInvoice.id == SupplierExpenseAccrual.invoice_id)
            .where(SupplierExpenseAccrual.status == "recognized")
        )
    ).all()

    layer = RecognitionLayer()
    for accrual, invoice_source, invoice_payment_status in rows:
        article_id = accrual.article_id
        if article_id is None:
            article_id = default_articles.get(accrual.counterparty_id)

        period_start = accrual.service_period_start
        period_end = accrual.service_period_end
        if period_start is None or period_end is None:
            # Периода нет — относим к месяцу признания: другого ответа у нас просто нет.
            month = accrual.recognition_month
            parts = [(month.replace(day=1), accrual.amount)] if month else []
        else:
            parts = spread_over_months(accrual.amount, period_start, period_end)

        origin = classify_origin(invoice_source, billing_modes.get(accrual.counterparty_id))
        # Пробел — только там, где документ ЖДУТ. Самоакт по тарифу, аренда по договору и
        # коммуналка по расчёту арендодателя пробелом не являются: у них закрывающего не
        # бывает по построению.
        no_primary = origin in (ORIGIN_AWAITING_DOCUMENT, ORIGIN_PAYMENT_LINE)
        for part_month, share in parts:
            if part_month < month_start or part_month > month_end:
                continue
            bucket = layer.by_article.setdefault(article_id, RecognitionBucket())
            bucket.amount += share
            bucket.count += 1
            bucket.accrual_ids.append(accrual.id)
            if accrual.counterparty_id is not None:
                bucket.counterparties.add(accrual.counterparty_id)
                layer.by_pair[(accrual.counterparty_id, accrual.article_id)] += share
            layer.total += share
            layer.details.append(
                RecognitionDetail(
                    accrual_id=accrual.id,
                    counterparty_id=accrual.counterparty_id,
                    article_id=article_id,
                    amount=share,
                    service_period_start=period_start,
                    service_period_end=period_end,
                    has_primary=not no_primary,
                    origin=origin,
                    # Только полностью оплаченный: у частично оплаченного документа часть
                    # расхода деньгами и правда не обеспечена.
                    settled=invoice_payment_status == "paid",
                )
            )
            if article_id is None:
                layer.unattributed += share
            if no_primary:
                layer.without_primary += share
            if accrual.location_id is None:
                layer.without_location += share

    return layer
