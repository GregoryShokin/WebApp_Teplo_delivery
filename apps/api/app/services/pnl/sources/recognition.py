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
from app.models.enums import SELF_ACCRUED_INVOICE_SOURCES
from app.services.expense_recognition_report import spread_over_months


@dataclass(slots=True)
class RecognitionBucket:
    amount: Decimal = Decimal("0.00")
    count: int = 0
    counterparties: set[uuid.UUID] = field(default_factory=set)
    accrual_ids: list[uuid.UUID] = field(default_factory=list)


@dataclass(slots=True)
class RecognitionLayer:
    """Признанный расход месяца в разрезе статей и пар «контрагент × статья»."""

    by_article: dict[uuid.UUID | None, RecognitionBucket] = field(default_factory=dict)
    by_pair: dict[tuple[uuid.UUID, uuid.UUID | None], Decimal] = field(
        default_factory=lambda: defaultdict(Decimal)
    )
    total: Decimal = Decimal("0.00")
    # Расход без статьи ДДС: в отчёт его отнести некуда, и молчать об этом нельзя.
    unattributed: Decimal = Decimal("0.00")
    # Расход без первичного документа: самоакт, коммуналка по расчёту арендодателя, строка
    # ручного платежа. В управленческую прибыль он идёт полноправно — деньги потрачены,
    # услуга оказана. В налоговую базу УСН он идти НЕ МОЖЕТ. Две разные цифры, складывать их
    # в одну нельзя.
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
    default_articles = {
        counterparty_id: article_id
        for counterparty_id, article_id in (
            await session.execute(
                select(
                    CounterpartyPayableProfile.counterparty_id,
                    CounterpartyPayableProfile.default_dds_article_id,
                ).where(CounterpartyPayableProfile.default_dds_article_id.is_not(None))
            )
        ).all()
    }

    rows = (
        await session.execute(
            select(SupplierExpenseAccrual, SupplierInvoice.source)
            .outerjoin(SupplierInvoice, SupplierInvoice.id == SupplierExpenseAccrual.invoice_id)
            .where(SupplierExpenseAccrual.status == "recognized")
        )
    ).all()

    layer = RecognitionLayer()
    for accrual, invoice_source in rows:
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

        # Первичка есть только у документа, присланного контрагентом. Самоакт мы выписали
        # себе сами; у начисления по строке платежа документа нет вовсе; счёт Водоканала
        # выставлен арендодателю-физлицу, и наш расход подтверждается его расчётом, а не
        # документом на ИП.
        no_primary = invoice_source is None or invoice_source in SELF_ACCRUED_INVOICE_SOURCES
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
            if article_id is None:
                layer.unattributed += share
            if no_primary:
                layer.without_primary += share
            if accrual.location_id is None:
                layer.without_location += share

    return layer
