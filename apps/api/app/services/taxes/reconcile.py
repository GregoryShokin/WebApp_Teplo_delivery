"""Сверка трёх слоёв налогового контура — ради неё всё и строилось.

Три независимых источника на одно обязательство:

1. **Расчёт** — что должно быть (движок из выручки iiko): УСН и допвзнос 1%.
2. **Документ** — что сказал бухгалтер (распознанные платёжки из ``tax_document_intake``).
3. **Факт** — что реально ушло из банка (``tax_payment`` со ``source_kind='bank_statement'``).

Совпадают — тихо ``ok``. Расходятся — подсветка с вердиктом. Именно так нулевая платёжка
по УСН (документ = 0 при расчёте 478 376) ловится автоматически, а не глазами владельца.

Чистая функция от фактов: ``build_reconciliation`` берёт срез из БД и возвращает структуру
без побочных эффектов — её можно отрисовать на странице и проверить на числах в тесте.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tax import TaxDocumentIntake, TaxPayment, TaxPayrollLedger
from app.services.taxes.engine import (
    PERIOD_SEQ,
    compute_tax_state,
    fmt_money,
    period_end_date,
)
from app.services.taxes.enp_split import payroll_enp_due
from app.services.taxes.repository import load_tax_inputs

ZERO = Decimal("0")
# Допуск сверки: налог округляется до рубля, поэтому расхождение ≤ 1 ₽ — не сигнал.
TOLERANCE = Decimal("1")

PERIOD_TITLES = {
    "q1": "I квартал",
    "h1": "полугодие",
    "9m": "9 месяцев",
    "year": "год",
}

_MONTHS_RU_GENITIVE = [
    "январь", "февраль", "март", "апрель", "май", "июнь",
    "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
]


@dataclass(frozen=True)
class ReconLine:
    """Одно обязательство в трёх измерениях + вердикт."""

    label: str
    tax_kind: str
    period_code: str
    due_date: date | None
    calculated: Decimal | None  # что насчитал движок (УСН/допвзнос), иначе None
    documented: Decimal | None  # что сказала платёжка бухгалтера
    paid: Decimal | None  # что реально уплачено
    verdict: str
    severity: str  # 'ok' | 'info' | 'warning' | 'alert'
    messages: list[str] = field(default_factory=list)
    # Конкретный следующий шаг для владельца — что делать с этим красным/жёлтым.
    action: str | None = None
    # Сколько платить по этому обязательству (для кнопки «Отправить в банк»); None — платить нечего.
    payable_amount: Decimal | None = None


@dataclass(frozen=True)
class Reconciliation:
    year: int
    as_of: date
    lines: list[ReconLine]

    @property
    def alerts(self) -> list[ReconLine]:
        return [ln for ln in self.lines if ln.severity == "alert"]

    @property
    def has_alerts(self) -> bool:
        return any(ln.severity == "alert" for ln in self.lines)


def _diff(a: Decimal | None, b: Decimal | None) -> Decimal | None:
    if a is None or b is None:
        return None
    return a - b


def _classify(
    *,
    calculated: Decimal | None,
    documented: Decimal | None,
    paid: Decimal | None,
    due_date: date | None,
    as_of: date,
    documented_is_increment: bool = False,
) -> tuple[str, str, list[str]]:
    """Вердикт по трём значениям. Возвращает (verdict, severity, messages).

    ``documented_is_increment`` — платёжка содержит НЕ полную сумму обязательства, а остаток
    к доплате. Так устроен допвзнос 1%: расчёт ведётся нарастающим итогом за год, а бухгалтер
    выписывает платёжку на прирост за квартал. Сравнивать их напрямую нельзя — получится
    ложное расхождение, хотя `уплачено + платёжка` сходится с расчётом до копейки.
    """
    messages: list[str] = []

    # 1. Документ против расчёта — сердце сверки (ловит нулевую платёжку).
    # Для приростных платёжек документ сверяется с ОСТАТКОМ, а не с полным начислением.
    doc_base = calculated
    if documented_is_increment and calculated is not None:
        doc_base = calculated - (paid or ZERO)
    doc_vs_calc = _diff(documented, doc_base)
    if doc_vs_calc is not None and abs(doc_vs_calc) > TOLERANCE:
        expected = "остатком к доплате" if documented_is_increment else "расчётом"
        messages.append(
            f"Документ расходится с {expected} на {fmt_money(abs(doc_vs_calc))} ₽ "
            f"(документ {fmt_money(documented)}, ожидалось {fmt_money(doc_base)})."
        )
        # Если платёжка меньше ожидаемого — риск недоплаты, это alert.
        severity = "alert" if documented < doc_base else "warning"
        return "doc_mismatch", severity, messages

    # Приростная платёжка: остаток к доплате — это норма, а не расхождение с фактом.
    if documented_is_increment:
        if documented and documented > 0:
            messages.append(
                f"К доплате {fmt_money(documented)} ₽"
                + (f", срок {due_date.strftime('%d.%m.%Y')}." if due_date else ".")
                + (f" Ранее уплачено {fmt_money(paid)} ₽." if paid else "")
            )
            if due_date and due_date < as_of:
                return "overdue", "alert", messages
            return "due", "info", messages
        return "ok", "ok", messages

    # 2. Факт против документа — оплатили не по бумаге.
    paid_vs_doc = _diff(paid, documented)
    if paid_vs_doc is not None and abs(paid_vs_doc) > TOLERANCE:
        messages.append(
            f"Оплачено {fmt_money(paid)} ₽ при документе {fmt_money(documented)} — "
            f"расхождение {fmt_money(abs(paid_vs_doc))} ₽."
        )
        return "payment_mismatch", "warning", messages

    # 3. Ничего не уплачено, срок прошёл — просрочка.
    reference = documented if documented is not None else calculated
    if (paid is None or paid == 0) and reference and reference > 0:
        if due_date and due_date < as_of:
            messages.append(
                f"Не оплачено к сроку {due_date.strftime('%d.%m.%Y')} — "
                f"ожидается {fmt_money(reference)} ₽."
            )
            return "overdue", "alert", messages
        messages.append(f"К уплате {fmt_money(reference)} ₽" + (
            f", срок {due_date.strftime('%d.%m.%Y')}." if due_date else "."
        ))
        return "due", "info", messages

    # 4. Уплачено, всё сходится.
    if paid is not None and paid > 0:
        return "ok", "ok", messages

    # 5. Нет ни расчёта, ни документа, ни факта.
    return "no_data", "info", ["Нет данных по этому обязательству."]


def _payable(
    verdict: str,
    *,
    documented: Decimal | None,
    calculated: Decimal | None,
    expected: Decimal | None = None,
) -> Decimal | None:
    """Сколько платить по обязательству — для кнопки «Отправить в банк».

    Ждём уплаты (due/overdue) — сумма из документа бухгалтера, иначе из расчёта.
    Расхождение в БЕЗОПАСНУЮ сторону (документ больше расчёта → переплата зачтётся на ЕНС)
    тоже платёжное: кнопку даём на сумму документа. Опасная сторона (документ меньше) —
    кнопки нет, платить нельзя.
    """
    if verdict == "doc_mismatch":
        if (
            documented is not None
            and expected is not None
            and documented > expected
            and documented > ZERO
        ):
            return documented
        return None
    if verdict not in ("due", "overdue"):
        return None
    if documented is not None and documented > ZERO:
        return documented
    if calculated is not None and calculated > ZERO:
        return calculated
    return None


def _action_for(
    verdict: str,
    *,
    documented: Decimal | None = None,
    expected: Decimal | None = None,
) -> str | None:
    """Что делать владельцу с этим обязательством. None — действий не требуется.

    ``expected`` — сумма, с которой сверяли документ (расчёт либо остаток к доплате).
    Направление расхождения решает всё: документ МЕНЬШЕ ожидаемого — риск недоимки и пеней,
    платить нельзя; документ БОЛЬШЕ на копейки — безопасная переплата (зачтётся на ЕНС),
    запрет тут только пугает.
    """
    if verdict == "doc_mismatch":
        if documented is not None and documented == ZERO:
            return (
                "Бухгалтер прислала нулевую платёжку — запросите корректную "
                "и не платите по этой."
            )
        if documented is not None and expected is not None and documented > expected:
            return (
                f"Документ на {fmt_money(documented - expected)} ₽ больше расчёта — платить "
                "по нему можно: небольшая переплата зачтётся на ЕНС. Расхождение обычно в "
                "составе вычета, при желании сверьте с бухгалтером."
            )
        return (
            "Платить по этому документу нельзя — в нём меньше, чем начислено: возникнет "
            "недоимка и пени. Уточните сумму у бухгалтера."
        )
    if verdict == "overdue":
        return "Срок уплаты прошёл — оплатите скорее, иначе начнут капать пени."
    if verdict == "payment_mismatch":
        return "Факт из банка не сошёлся с документом — проверьте, сколько и по чему заплатили."
    return None


async def _documented_amount(
    session: AsyncSession, *, tax_kind: str, period_code: str
) -> tuple[Decimal | None, date | None]:
    """Сумма и срок из последней распознанной платёжки этого вида и периода.

    Берём самый свежий документ (бухгалтер мог прислать исправление — как нулевую УСН,
    затем корректную): сортируем по received_at, последний побеждает.
    """
    rows = (
        await session.execute(
            select(TaxDocumentIntake)
            .where(
                TaxDocumentIntake.document_type == "payment_order",
                TaxDocumentIntake.status.in_(("parsed", "needs_review", "promoted")),
            )
            .order_by(TaxDocumentIntake.received_at.asc())
        )
    ).scalars().all()

    amount: Decimal | None = None
    due: date | None = None
    for row in rows:
        rec = row.recognition or {}
        if rec.get("tax_kind") != tax_kind:
            continue
        if rec.get("period_hint") not in (period_code, None):
            continue
        if rec.get("amount") is not None:
            amount = Decimal(str(rec["amount"]))
        if rec.get("due_date"):
            due = date.fromisoformat(rec["due_date"])
    return amount, due


async def _paid_amount(
    session: AsyncSession, *, year: int, kind: str, period_code: str | None
) -> Decimal | None:
    """Фактически уплаченное по виду за год (для УСН — с учётом периода)."""
    from sqlalchemy import func

    stmt = select(func.coalesce(func.sum(TaxPayment.amount), 0)).where(
        TaxPayment.status == "paid",
        TaxPayment.for_year == year,
        TaxPayment.kind == kind,
    )
    if period_code is not None:
        stmt = stmt.where(TaxPayment.for_period == period_code)
    total = await session.scalar(stmt)
    value = Decimal(str(total or 0))
    return value if value > 0 else None


async def _payroll_accruals(
    session: AsyncSession, *, year: int
) -> dict[int, tuple[Decimal, Decimal]]:
    """Начисления из оборотк по месяцам: ``month -> (взносы за работников, НДФЛ)``."""
    rows = (
        await session.execute(
            select(
                TaxPayrollLedger.month,
                func.coalesce(func.sum(TaxPayrollLedger.contributions), 0),
                func.coalesce(func.sum(TaxPayrollLedger.ndfl), 0),
            )
            .where(TaxPayrollLedger.year == year)
            .group_by(TaxPayrollLedger.month)
        )
    ).all()
    return {int(m): (Decimal(str(c)), Decimal(str(n))) for m, c, n in rows}


async def _enp_payroll_documents(session: AsyncSession) -> dict[date, Decimal]:
    """Платёжки зарплатного ЕНП: ``срок уплаты -> сумма`` (НДФЛ и взносы одной суммой)."""
    rows = (
        await session.execute(
            select(TaxDocumentIntake).where(
                TaxDocumentIntake.document_type == "payment_order",
                TaxDocumentIntake.status.in_(("parsed", "needs_review", "promoted")),
            )
        )
    ).scalars().all()
    out: dict[date, Decimal] = {}
    for row in rows:
        rec = row.recognition or {}
        if rec.get("tax_kind") != "enp_payroll":
            continue
        raw_due, raw_amount = rec.get("due_date"), rec.get("amount")
        if raw_due and raw_amount is not None:
            out[date.fromisoformat(raw_due)] = Decimal(str(raw_amount))
    return out


async def _payroll_split_paid(
    session: AsyncSession, *, year: int, period: str
) -> Decimal | None:
    """Сколько разнесено по месяцу (НДФЛ + взносы) как уплаченное."""
    total = await session.scalar(
        select(func.coalesce(func.sum(TaxPayment.amount), 0)).where(
            TaxPayment.status == "paid",
            TaxPayment.for_year == year,
            TaxPayment.for_period == period,
            TaxPayment.kind.in_(("contrib_employees", "ndfl")),
        )
    )
    value = Decimal(str(total or 0))
    return value if value > 0 else None


async def build_reconciliation(
    session: AsyncSession, *, as_of: date
) -> Reconciliation:
    """Собрать сверку трёх слоёв на дату среза."""
    year = as_of.year
    lines: list[ReconLine] = []

    # ── УСН по каждому закрывшемуся отчётному периоду ────────────────────────
    for period_code in PERIOD_SEQ:
        p_end = period_end_date(year, period_code)
        if p_end > as_of:
            break
        state = compute_tax_state(await load_tax_inputs(session, as_of=p_end))
        calculated = state.amount_due
        documented, due = await _documented_amount(
            session, tax_kind="usn_advance", period_code=period_code
        )
        paid = await _paid_amount(
            session, year=year, kind="usn_advance", period_code=period_code
        )
        verdict, severity, messages = _classify(
            calculated=calculated,
            documented=documented,
            paid=paid,
            due_date=due,
            as_of=as_of,
        )
        lines.append(
            ReconLine(
                label=f"УСН, {PERIOD_TITLES[period_code]}",
                tax_kind="usn_advance",
                period_code=period_code,
                due_date=due,
                calculated=calculated,
                documented=documented,
                paid=paid,
                verdict=verdict,
                severity=severity,
                messages=messages,
                action=_action_for(verdict, documented=documented, expected=calculated),
                payable_amount=_payable(
                    verdict,
                    documented=documented,
                    calculated=calculated,
                    expected=calculated,
                ),
            )
        )

    # ── Допвзнос 1% (годовой, начисление нарастающим итогом) ─────────────────
    state = compute_tax_state(await load_tax_inputs(session, as_of=as_of))
    documented, due = await _documented_amount(
        session, tax_kind="contrib_extra_1pct", period_code="h1"
    )
    paid = await _paid_amount(
        session, year=year, kind="contrib_extra_1pct", period_code=None
    )
    verdict, severity, messages = _classify(
        calculated=state.extra_accrued,
        documented=documented,
        paid=paid,
        due_date=due,
        as_of=as_of,
        # Бухгалтер выписывает платёжку на ПРИРОСТ за квартал, а начисление ведётся
        # нарастающим итогом за год: 116 360 уплачено + 105 628 по платёжке = 221 988.
        documented_is_increment=True,
    )
    lines.append(
        ReconLine(
            label="Допвзнос 1%",
            tax_kind="contrib_extra_1pct",
            period_code="year",
            due_date=due,
            calculated=state.extra_accrued,
            documented=documented,
            paid=paid,
            verdict=verdict,
            severity=severity,
            messages=messages,
            # Приростная платёжка сверяется с ОСТАТКОМ к доплате — его и передаём как ожидание.
            action=_action_for(
                verdict,
                documented=documented,
                expected=state.extra_accrued - (paid or ZERO),
            ),
            payable_amount=_payable(
                verdict,
                documented=documented,
                calculated=state.extra_accrued,
                expected=state.extra_accrued - (paid or ZERO),
            ),
        )
    )

    # ── Зарплатный ЕНП: разнос из оборотки (НДФЛ + взносы за работников) ──────
    # Сверяем НАЧИСЛЕНО (оборотка) с фактом разноса и показываем платёжку справочно.
    # Помесячно платёжка ЕНП с начислением НЕ совпадает — НДФЛ платится «окнами» (за месяц N
    # частями до 28 N и до 5 N+1), поэтому платёжку не сверяем жёстко, а показываем состав.
    accruals = await _payroll_accruals(session, year=year)
    enp_docs = await _enp_payroll_documents(session)
    for month in sorted(accruals):
        contributions, ndfl = accruals[month]
        accrued = contributions + ndfl
        if accrued <= ZERO:
            continue
        period = f"{year}-{month:02d}"
        month_due = payroll_enp_due(year, month)
        documented = enp_docs.get(month_due)
        paid = (
            await _payroll_split_paid(session, year=year, period=period)
            if month_due <= as_of
            else None
        )

        month_name = _MONTHS_RU_GENITIVE[month - 1]
        messages = [
            f"«Расчёт» — это начислено за {month_name}: взносы за работников "
            f"{fmt_money(contributions)} ₽ (идут в вычет УСН) + НДФЛ {fmt_money(ndfl)} ₽ "
            f"(в вычет не идёт)."
        ]
        action: str | None = None
        if month_due <= as_of:
            verdict, severity = "ok", "ok"
            messages.append(f"Уплачено в составе ЕНП, срок {month_due.strftime('%d.%m.%Y')}.")
        else:
            verdict, severity = "due", "info"
            messages.append(
                f"Уплачивается в составе ЕНП до {month_due.strftime('%d.%m.%Y')}."
            )
            action = f"Оплатите в составе ближайшего ЕНП к сроку {month_due.strftime('%d.%m.%Y')}."
        # Платёжку показываем СПРАВОЧНО (не в колонке «Документ»), чтобы её сумма не читалась
        # как расхождение: помесячно она больше начисления из-за «окон» уплаты НДФЛ — это норма.
        if documented is not None:
            messages.append(
                f"Справочно: платёжка бухгалтера ЕНП на этот срок — {fmt_money(documented)} ₽. "
                f"Она больше начисления, потому что НДФЛ платится по датам выплат («окнами»), "
                f"а не ровно по месяцу. Расхождения тут нет."
            )

        lines.append(
            ReconLine(
                label=f"Зарплатный ЕНП за {month_name}",
                tax_kind="enp_payroll",
                period_code=period,
                due_date=month_due,
                calculated=accrued,
                documented=None,
                paid=paid,
                verdict=verdict,
                severity=severity,
                messages=messages,
                action=action,
                # Платить нужно сумму платёжки бухгалтера (она учитывает «окна» НДФЛ),
                # иначе — начисленное за месяц.
                payable_amount=_payable(
                    verdict, documented=documented, calculated=accrued
                ),
            )
        )

    return Reconciliation(year=year, as_of=as_of, lines=lines)
