"""Денежный слой ОПиУ: разложить проводки месяца по строкам отчёта, ничего не потеряв.

ЗДЕСЬ ЖИВЁТ ГЛАВНОЕ ПРАВИЛО ВСЕГО ОТЧЁТА — как не сложить один расход дважды, когда часть
суммы по статье приходит начислением, а часть кассой.

Наивные решения и почему они неверны:

* «Исключать кассу по статьям, которые ведутся признанием» — не работает: одна статья
  обслуживает и признаваемых, и остальных. «Транспортные услуги» — это СДЭК с документами
  и разовый перевозчик без них.
* «Флаг на карточке контрагента» — его забудут повернуть, и обе стороны отказа тихие:
  забыли включить — задвоили, забыли выключить — потеряли.
* «Контрагент ведётся признанием → вся его касса не расход» — теряет деньги молча. На июле
  2026 арендодатель дал 267 276,59 ₽ кассы против 109 654,25 ₽ признания: правило выбросило
  бы 157 622,34 ₽ без единой пометки.

Принятое правило работает на паре КОНТРАГЕНТ × СТАТЬЯ и сравнивает суммы:

1. Есть признание за месяц → расход берётся из него, касса исключается целиком.
   Если оплачено больше признанного, разница НЕ добавляется в расход: это либо аванс, либо
   документ, который ещё не приехал. Она уходит в ``unrecognized_paid`` и показывается
   владельцу отдельной цифрой. Добавить её значило бы записать расход дважды — сейчас
   деньгами и потом документом.
2. Контрагент в контуре признания, но за месяц признания нет → касса исключается, строка
   получает статус «ждём документ» с суммой оплаты в подсказке. Это НОРМАЛЬНОЕ состояние:
   владелец подтвердил 03.08.2026, что коммуналка приходит раздельными счетами и за июль
   пришла только вода.
3. Контрагент вне контура → касса и есть расход. Для строк, которые ждут документа по своей
   природе, сумма помечается ``cash_proxy``: месяц взят по деньгам за неимением документа.

Цена правила честная и односторонняя: занижение с ярлыком, никогда не задвоение. Задвоение
шумит — расходятся итоги; занижение молчит, поэтому оно обязано быть подписано.
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
    CashflowTransaction,
    CounterpartyPayableProfile,
    InvoicePaymentAllocation,
    PnlArticleRule,
    PnlCashOrigin,
    SupplierExpenseAccrual,
)
from app.services.pnl.types import Verdict

#: Проводка, помеченная как исключённая при разборе выписки, не расход и не доход.
EXCLUDED_QUALITY_STATUS = "excluded"


@dataclass(slots=True)
class CashBucket:
    """Накопитель по одной строке ОПиУ."""

    amount: Decimal = Decimal("0.00")
    count: int = 0
    excluded_amount: Decimal = Decimal("0.00")
    excluded_count: int = 0
    unrecognized_paid: Decimal = Decimal("0.00")
    waiting_counterparties: set[uuid.UUID] = field(default_factory=set)


@dataclass(slots=True)
class CashLayer:
    """Результат разбора денежного слоя за месяц."""

    buckets: dict[str, CashBucket] = field(default_factory=dict)
    by_verdict: dict[str, Decimal] = field(default_factory=lambda: defaultdict(Decimal))
    by_verdict_count: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    out_total: Decimal = Decimal("0.00")
    in_total: Decimal = Decimal("0.00")
    unmapped: Decimal = Decimal("0.00")
    unmapped_count: int = 0
    # Статьи, по которым касса встретилась, а правила нет. Пустой список — инвариант.
    unmapped_articles: set[uuid.UUID | None] = field(default_factory=set)


async def _recognition_circuit(
    session: AsyncSession, month_start: date, month_end: date
) -> tuple[set[uuid.UUID], dict[tuple[uuid.UUID, uuid.UUID | None], Decimal]]:
    """Контур признания месяца: кто в нём и сколько признано по паре контрагент × статья.

    Членство выводится ИЗ ДАННЫХ, а не из флага, который можно забыть повернуть: контрагент
    в контуре, если у него включён режим периодов услуги ЛИБО если за месяц есть признанное
    начисление. Поэтому УПД за июль, пришедший в сентябре, немедленно переводит июльскую
    кассу в «исключено» и ставит признание — без окна рассинхрона и без ручного действия.
    """
    profile_rows = await session.execute(
        select(CounterpartyPayableProfile.counterparty_id).where(
            CounterpartyPayableProfile.service_period_required.is_(True)
        )
    )
    circuit: set[uuid.UUID] = {row for row in profile_rows.scalars()}

    accrual_rows = await session.execute(
        select(
            SupplierExpenseAccrual.counterparty_id,
            SupplierExpenseAccrual.article_id,
            SupplierExpenseAccrual.amount,
            SupplierExpenseAccrual.service_period_start,
            SupplierExpenseAccrual.service_period_end,
            SupplierExpenseAccrual.recognition_month,
        ).where(SupplierExpenseAccrual.status == "recognized")
    )
    recognized: dict[tuple[uuid.UUID, uuid.UUID | None], Decimal] = defaultdict(Decimal)
    for counterparty_id, article_id, amount, start, end, recognition_month in accrual_rows:
        period_start = start or recognition_month
        period_end = end or recognition_month
        if period_start is None or period_end is None:
            continue
        if period_start > month_end or period_end < month_start:
            continue
        circuit.add(counterparty_id)
        recognized[(counterparty_id, article_id)] += amount or Decimal("0.00")
    return circuit, recognized


async def _settled_transactions(session: AsyncSession) -> set[uuid.UUID]:
    """Проводки, которые закрывают документ с начислением.

    Жёсткий якорь: связка «платёж ↔ накладная» существует явно и покрывает частичные оплаты
    и сплиты. Эвристика по ``source_id`` здесь не годится — он двусмыслен, разные контуры
    кладут в него то накладную, то черновик платежа.
    """
    rows = await session.execute(
        select(InvoicePaymentAllocation.cashflow_transaction_id)
        .join(
            SupplierExpenseAccrual,
            SupplierExpenseAccrual.invoice_id == InvoicePaymentAllocation.invoice_id,
        )
        .where(
            SupplierExpenseAccrual.status.in_(("recognized", "scheduled")),
            InvoicePaymentAllocation.cashflow_transaction_id.is_not(None),
        )
    )
    return {row for row in rows.scalars() if row is not None}


async def build_cash_layer(
    session: AsyncSession,
    month_start: date,
    month_end: date,
    sign_roles: dict[str, int] | None = None,
) -> CashLayer:
    """Разложить денежные проводки месяца по строкам ОПиУ и вердиктам.

    ``sign_roles`` — роль каждой строки из справочника (+1 доход, −1 расход). Нужна, чтобы
    правильно повернуть знак: для расходной строки отток даёт расход, а приток (возврат,
    компенсация) его уменьшает; для доходной строки всё зеркально. Без этой карты приход по
    доходной статье уходил бы в минус — «Прочие доходы» показывали бы −1 200 ₽ вместо +1 200.
    """
    sign_roles = sign_roles or {}
    rules = {
        rule.article_id: rule
        for rule in (
            await session.execute(select(PnlArticleRule).where(PnlArticleRule.is_active.is_(True)))
        ).scalars()
    }
    origins: dict[tuple[str, uuid.UUID | None], PnlCashOrigin] = {
        (origin.source_kind, origin.article_id): origin
        for origin in (await session.execute(select(PnlCashOrigin))).scalars()
    }
    circuit, recognized = await _recognition_circuit(session, month_start, month_end)
    settled = await _settled_transactions(session)
    # Статьи, по которым за месяц есть хоть одно признание. Нужны для проводок БЕЗ
    # контрагента: у коммуналки за июль 2026 таких четыре на 106 281 ₽, и сопоставить их с
    # начислением по контрагенту невозможно. Оставить их в расходе значит задвоить месяц
    # (признание по той же статье уже есть), поэтому касса исключается, а сумма уходит в
    # «оплачено, документа нет». Занижение с ярлыком лучше молчаливого задвоения.
    recognized_articles = {article_id for _, article_id in recognized}

    layer = CashLayer()
    transactions = (
        await session.execute(
            select(CashflowTransaction).where(
                CashflowTransaction.operation_date >= month_start,
                CashflowTransaction.operation_date <= month_end,
            )
        )
    ).scalars()

    for tx in transactions:
        amount = tx.amount or Decimal("0.00")
        if tx.direction == "out":
            layer.out_total += amount
        else:
            layer.in_total += amount

        verdict, line_code = _classify(tx, rules, origins, circuit, recognized_articles, settled)
        layer.by_verdict[verdict.value] += amount
        layer.by_verdict_count[verdict.value] += 1

        if verdict is Verdict.UNMAPPED:
            layer.unmapped += amount
            layer.unmapped_count += 1
            layer.unmapped_articles.add(tx.article_id)
            continue
        if line_code is None:
            continue

        bucket = layer.buckets.setdefault(line_code, CashBucket())
        if verdict is Verdict.INCLUDED:
            # Расходная строка при оттоке даёт положительную величину расхода; приход по ней
            # (возврат, компенсация) — отрицательную. Доходная строка ведёт себя зеркально.
            # Знак выводится из направления и роли строки, а не хранится в правиле: правило
            # не должно повторять то, что уже сказано справочником.
            rule = rules[tx.article_id]
            expense_line = sign_roles.get(line_code, -1) == -1
            natural = (tx.direction == "out") == expense_line
            bucket.amount += amount * rule.sign * (1 if natural else -1)
            bucket.count += 1
        else:
            bucket.excluded_amount += amount
            bucket.excluded_count += 1
            if verdict is Verdict.EXCLUDED_ACCRUAL_COUNTERPARTY:
                key = (tx.counterparty_id, tx.article_id)
                if tx.counterparty_id is None or key not in recognized:
                    # Деньги ушли, а признания именно этой пары за месяц нет — «ждём документ».
                    # Сумма НЕ идёт в расход: когда документ приедет, месяц получил бы расход
                    # дважды. Показываем её отдельной цифрой и статусом строки.
                    if tx.counterparty_id is not None:
                        bucket.waiting_counterparties.add(tx.counterparty_id)
                    bucket.unrecognized_paid += amount

    return layer


def _classify(
    tx: CashflowTransaction,
    rules: dict[uuid.UUID, PnlArticleRule],
    origins: dict[tuple[str, uuid.UUID | None], PnlCashOrigin],
    circuit: set[uuid.UUID],
    recognized_articles: set[uuid.UUID | None],
    settled: set[uuid.UUID],
) -> tuple[Verdict, str | None]:
    """Вердикт проводки. Ровно один исход — это и замыкает уравнение сходимости.

    Порядок проверок значим и не переставляется: сначала то, что вообще не деньги отчёта
    (исключённое качество, перевод), затем разметка, затем принадлежность чужому слою, и
    только потом контур признания.
    """
    if tx.quality_status == EXCLUDED_QUALITY_STATUS:
        return Verdict.EXCLUDED_QUALITY, None
    if tx.transfer_group_id is not None:
        return Verdict.EXCLUDED_TRANSFER, None
    if tx.article_id is None:
        return Verdict.UNMAPPED, None

    rule = rules.get(tx.article_id)
    if rule is None:
        return Verdict.UNMAPPED, None
    if not rule.in_pnl:
        return Verdict.EXCLUDED_OUT_OF_PNL, None

    # Исключение выдач ПАРОЙ «механизм + статья», а не механизмом: через выплату из Сейфа
    # идут и зарплата, и наличные траты администраторов на содержание точек и питание
    # персонала. Исключить механизм целиком — выбросить реальный операционный расход.
    if tx.source_kind is not None and (
        (tx.source_kind, tx.article_id) in origins or (tx.source_kind, None) in origins
    ):
        return Verdict.EXCLUDED_OWNED_BY_LAYER, rule.line_code

    if tx.id in settled:
        return Verdict.EXCLUDED_ACCRUAL_SETTLEMENT, rule.line_code
    if tx.counterparty_id is not None:
        if tx.counterparty_id in circuit:
            return Verdict.EXCLUDED_ACCRUAL_COUNTERPARTY, rule.line_code
    elif tx.article_id in recognized_articles:
        # Контрагента у проводки нет, а по её статье за месяц признание есть. Различить
        # «это тот же расход» и «это другой поставщик» нечем, поэтому выбираем сторону
        # занижения: расход берём из признания, кассу помечаем ожиданием документа.
        return Verdict.EXCLUDED_ACCRUAL_COUNTERPARTY, rule.line_code

    return Verdict.INCLUDED, rule.line_code
