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

from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tax import TaxBankDraft, TaxDocumentIntake, TaxPayment, TaxPayrollLedger
from app.services.taxes.bank_facts import payroll_facts_without_turnover
from app.services.taxes.engine import (
    PERIOD_SEQ,
    compute_tax_state,
    fixed_contribution_due,
    fmt_money,
    period_end_date,
)
from app.services.taxes.enp_split import payroll_enp_due
from app.services.taxes.ens_wallet import compute_ens_wallet
from app.services.taxes.official_payroll import (
    months_without_turnover,
    official_month_accrual,
)
from app.services.taxes.repository import load_tax_inputs, year_config

ZERO = Decimal("0")
# Допуск сверки: налог округляется до рубля, поэтому расхождение ≤ 1 ₽ — не сигнал.
TOLERANCE = Decimal("1")

# Владелец думает кварталами (26.07.2026): «полугодие»/«9 месяцев» — язык деклараций,
# а платёж за полугодие — это доплата за II квартал. Показываем кварталы.
PERIOD_TITLES = {
    "q1": "I квартал",
    "h1": "II квартал",
    "9m": "III квартал",
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
    # Конкретный следующий шаг для владельца — ИМПЕРАТИВ до 60 знаков (в строке таблицы).
    action: str | None = None
    # Почему так: причина, суммы, методика. Уезжает под иконку «i» (дизайн-ревизия 27.07.2026).
    action_why: str | None = None
    # Сколько платить по этому обязательству (для кнопки «Отправить в банк»); None — платить нечего.
    payable_amount: Decimal | None = None
    # Платёж по обязательству уже в работе: 'ready_to_send' (подготовлен в окне активных
    # платежей) или 'in_bank' (отправлен в банк). Кнопка «Отправить в банк» при этом не
    # показывается — иначе она плодила бы дубли и врала о состоянии.
    draft_status: str | None = None


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
    doc_gap_allowance: Decimal = ZERO,
    allowance_reason: str = "",
) -> tuple[str, str, list[str]]:
    """Вердикт по трём значениям. Возвращает (verdict, severity, messages).

    ``documented_is_increment`` — платёжка содержит НЕ полную сумму обязательства, а остаток
    к доплате. Так устроен допвзнос 1%: расчёт ведётся нарастающим итогом за год, а бухгалтер
    выписывает платёжку на прирост за квартал. Сравнивать их напрямую нельзя — получится
    ложное расхождение, хотя `уплачено + платёжка` сходится с расчётом до копейки.

    ``doc_gap_allowance`` — насколько платёжка МОЖЕТ законно превышать наш расчёт из-за
    известной конвенции бухгалтера (владелец 27.07.2026: «Наталья травматизм не вычитает,
    считая это копейками»). В пределах этой суммы превышение — не расхождение, а разная
    методика: краснеть каждый квартал по одному и тому же поводу сверка не должна. Всё, что
    сверх, остаётся расхождением; платёжка МЕНЬШЕ расчёта — расхождение всегда.
    """
    messages: list[str] = []

    # 1. Документ против расчёта — сердце сверки (ловит нулевую платёжку).
    # Для приростных платёжек документ сверяется с ОСТАТКОМ, а не с полным начислением.
    doc_base = calculated
    if documented_is_increment and calculated is not None:
        doc_base = calculated - (paid or ZERO)
    doc_vs_calc = _diff(documented, doc_base)
    if (
        doc_vs_calc is not None
        and doc_gap_allowance > ZERO
        and TOLERANCE < doc_vs_calc <= doc_gap_allowance + TOLERANCE
    ):
        messages.append(
            f"Платёжка больше расчёта на {fmt_money(doc_vs_calc)} ₽ — "
            f"{allowance_reason or 'разная методика вычета'}. Это не расхождение: платите по "
            f"платёжке, разница осядет на ЕНС и зачтётся."
        )
        doc_vs_calc = ZERO  # дальше классифицируем как совпадение
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
    paid: Decimal | None = None,
) -> Decimal | None:
    """Сколько платить по обязательству — для кнопки «Отправить в банк».

    Ждём уплаты (due/overdue) — сумма из документа бухгалтера, иначе из расчёта.
    Расхождение в БЕЗОПАСНУЮ сторону (документ больше расчёта → переплата зачтётся на ЕНС)
    тоже платёжное: кнопку даём на сумму документа. Опасная сторона (документ меньше) —
    кнопки нет, платить нельзя.

    УПЛАЧЕННОЕ не платёжное ни при каком вердикте: уплаченный аванс УСН за I квартал с
    безопасным расхождением (документ 674 624 при расчёте 674 324) иначе снова попадал в
    «к уплате» и в кредиторскую задолженность — второй раз на ту же сумму.
    """
    if paid is not None and documented is not None and paid + TOLERANCE >= documented:
        return None
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
) -> tuple[str | None, str | None]:
    """Что делать владельцу — КОРОТКО и ПОЧЕМУ. ``(None, None)`` — действий не требуется.

    Разделение по решению владельца 27.07.2026 (дизайн-ревизия): в строке таблицы стоит
    императив до 60 знаков, а причина, суммы и методика уезжают под иконку «i» — иначе
    колонка «Вердикт» превращается в абзац и владелец её не читает.

    ``expected`` — сумма, с которой сверяли документ (расчёт либо остаток к доплате).
    Направление расхождения решает всё: документ МЕНЬШЕ ожидаемого — риск недоимки и пеней,
    платить нельзя; документ БОЛЬШЕ на копейки — безопасная переплата (зачтётся на ЕНС),
    запрет тут только пугает.
    """
    if verdict == "doc_mismatch":
        if documented is not None and documented == ZERO:
            return (
                "Не платите — запросите корректную платёжку",
                "Бухгалтер прислала нулевую платёжку.",
            )
        if documented is not None and expected is not None and documented > expected:
            return (
                "Платить можно — переплата зачтётся",
                f"Документ на {fmt_money(documented - expected)} ₽ больше расчёта; "
                "небольшая переплата зачтётся на ЕНС.",
            )
        return (
            "Платить нельзя — уточните сумму",
            "В документе меньше, чем начислено: возникнет недоимка и пени.",
        )
    if verdict == "overdue":
        return "Оплатите — идут пени", "Срок уплаты прошёл."
    if verdict == "payment_mismatch":
        return (
            "Проверьте платёж",
            "Факт из банка не сошёлся с документом — проверьте, сколько и по чему заплатили.",
        )
    return None, None


def _offset_by_ens(
    line: ReconLine, *, expected: Decimal | None, wallet_balance: Decimal
) -> ReconLine:
    """Смягчить «документ меньше начисленного», если разницу покрывает переплата на ЕНС.

    Решение владельца 26.07.2026: если бухгалтер прислала платёжку меньше начисления,
    а на ЕНС лежит расчётная переплата не меньше разницы — это не недоимка, это трата
    переплаты (налоговая доберёт остаток из кошелька). Красный запрет платить в этом
    случае врёт. Нулевую платёжку зачётом не оправдываем: это сломанный документ.
    Частичное покрытие (переплата меньше разницы) alert не снимает — только поясняет.
    """
    if line.verdict != "doc_mismatch" or line.severity != "alert":
        return line
    if line.documented is None or line.documented <= ZERO or expected is None:
        return line
    gap = expected - line.documented
    if gap <= ZERO:
        return line
    if wallet_balance + TOLERANCE >= gap:
        return replace(
            line,
            severity="warning",
            messages=[
                *line.messages,
                f"Разница {fmt_money(gap)} ₽ покрыта расчётной переплатой на ЕНС "
                f"({fmt_money(wallet_balance)} ₽): налоговая доберёт её из кошелька.",
            ],
            action="Платить можно — добор из переплаты ЕНС",
            action_why=(
                f"Недостающие {fmt_money(gap)} ₽ спишутся из переплаты на ЕНС. "
                f"Если хотите — сверьте остаток кошелька с бухгалтером."
            ),
            payable_amount=line.documented,
        )
    if wallet_balance > ZERO:
        return replace(
            line,
            messages=[
                *line.messages,
                f"Расчётная переплата на ЕНС ({fmt_money(wallet_balance)} ₽) покрывает "
                f"разницу лишь частично — не хватает {fmt_money(gap - wallet_balance)} ₽.",
            ],
        )
    return line


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
            # Тай-брейк по id: вложения одного письма несут ОДИН received_at (дата письма),
            # без него «последний побеждает» недетерминирован (находка аудита 27.07.2026).
            .order_by(TaxDocumentIntake.received_at.asc(), TaxDocumentIntake.id.asc())
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
            select(TaxDocumentIntake)
            .where(
                TaxDocumentIntake.document_type == "payment_order",
                TaxDocumentIntake.status.in_(("parsed", "needs_review", "promoted")),
            )
            # Детерминированный «последний побеждает»: при двух платёжках на один срок
            # (оригинал + исправление) верх берёт более поздняя по (received_at, id) —
            # без ORDER BY победителя выбирал физический порядок строк Postgres.
            .order_by(TaxDocumentIntake.received_at.asc(), TaxDocumentIntake.id.asc())
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


async def _payroll_paid_breakdown(
    session: AsyncSession, *, year: int, period: str
) -> tuple[Decimal, Decimal]:
    """Уплачено по месяцу в разрезе ``(взносы за работников, НДФЛ)``.

    Приоритет — БАНКОВСКИЕ факты (разнос ЕНП из выписки); без них — кассовая конвенция
    разноса оборотки (tax_notice: «считаем уплаченным в срок»). Суммировать оба слоя
    нельзя: за один месяц они описывают ОДИН и тот же платёж — сумма задваивалась бы
    (находка аудита 27.07.2026, вскрылась после защиты банковских строк от rebuild).
    """
    rows = (
        await session.execute(
            select(
                TaxPayment.source_kind,
                TaxPayment.kind,
                func.coalesce(func.sum(TaxPayment.amount), 0),
            )
            .where(
                TaxPayment.status == "paid",
                TaxPayment.for_year == year,
                TaxPayment.for_period == period,
                TaxPayment.kind.in_(("contrib_employees", "ndfl")),
            )
            .group_by(TaxPayment.source_kind, TaxPayment.kind)
        )
    ).all()
    layers: dict[str, dict[str, Decimal]] = {"bank": {}, "notice": {}}
    for source, kind, total in rows:
        layer = "notice" if source == "tax_notice" else "bank"
        layers[layer][kind] = layers[layer].get(kind, ZERO) + Decimal(str(total))
    chosen = layers["bank"] if sum(layers["bank"].values(), ZERO) > ZERO else layers["notice"]
    return chosen.get("contrib_employees", ZERO), chosen.get("ndfl", ZERO)


async def _payroll_split_paid(
    session: AsyncSession, *, year: int, period: str
) -> Decimal | None:
    """Сколько уплачено по месяцу всего (НДФЛ + взносы); None — платежей нет."""
    contributions, ndfl = await _payroll_paid_breakdown(session, year=year, period=period)
    value = contributions + ndfl
    return value if value > 0 else None


async def _unallocated_fns_facts(session: AsyncSession, *, year: int, as_of: date) -> Decimal:
    """Платежи в ФНС, которые факт-слой не смог разнести по видам (нет оборотки месяца).

    Их взносная часть НЕ попадает в вычет (консервативно), поэтому расчётный УСН выше,
    чем у бухгалтера, у которой разнос есть. Сверка обязана называть эту причину — иначе
    «документ ≠ расчёт» выглядит ошибкой бухгалтера, хотя это дыра в наших данных
    (вопрос владельца 27.07.2026: «почему сходиться перестало?»).
    """
    total = await session.scalar(
        select(func.coalesce(func.sum(TaxPayment.amount), 0)).where(
            TaxPayment.status == "paid",
            TaxPayment.kind == "other",
            TaxPayment.quality_status == "requires_review",
            TaxPayment.source_kind == "bank_statement",
            TaxPayment.for_year == year,
            TaxPayment.paid_on <= as_of,
        )
    )
    return Decimal(str(total or 0))


async def _injury_in_deduction(
    session: AsyncSession, *, year: int, as_of: date
) -> Decimal:
    """Травматизм, реально уплаченный с 1 января по срез (часть нашего вычета).

    Подп. 1 п. 3.1 ст. 346.21 НК прямо называет взносы «от несчастных случаев на
    производстве» среди уменьшающих налог, и движок их зачитывает. Бухгалтер считает вычет
    БЕЗ них — поэтому её платёжка систематически больше расчёта примерно на эту сумму.
    Сверка должна называть причину, а не отправлять владельца искать её «где-то в вычете».
    """
    total = await session.scalar(
        select(func.coalesce(func.sum(TaxPayment.amount), 0)).where(
            TaxPayment.status == "paid",
            TaxPayment.kind == "contrib_injury",
            TaxPayment.source_kind != "tax_notice",
            TaxPayment.paid_on >= date(year, 1, 1),
            TaxPayment.paid_on <= as_of,
        )
    )
    return Decimal(str(total or 0))


def _explain_doc_gap(why: str | None, *, gap: Decimal, injury: Decimal) -> str | None:
    """Дописать к причине расхождения долю травматизма — сколько из зазора объяснено."""
    if why is None or gap <= ZERO:
        return why
    if injury <= ZERO:
        return why + " Причина — в составе вычета; сверьте его с бухгалтером."
    covered = min(gap, injury)
    tail = gap - covered
    extra = (
        f" Из них {fmt_money(covered)} ₽ — уплаченный травматизм: движок зачёл его в вычет "
        f"(подп. 1 п. 3.1 ст. 346.21 НК), бухгалтер — нет."
    )
    if tail > ZERO:
        # Копейки — это округление налога и разница в сумме дохода; заметный остаток
        # объяснять нечем, и говорить про «округления» в таком случае — врать владельцу.
        extra += (
            f" Остальные {fmt_money(tail)} ₽ — округления и разница в сумме дохода."
            if tail <= Decimal("5")
            else f" Остальные {fmt_money(tail)} ₽ ничем не объяснены — уточните у бухгалтера."
        )
    return why + extra


async def _kind_totals(
    session: AsyncSession, *, year: int, kind: str
) -> tuple[Decimal, Decimal, date | None]:
    """``(по платёжкам бухгалтера, уплачено из банка, ближайший непогашенный срок)``."""
    from app.services.taxes.obligations import is_settled  # поздний импорт: разрыв цикла

    rows = (
        await session.scalars(
            select(TaxPayment).where(
                TaxPayment.for_year == year,
                TaxPayment.kind == kind,
                TaxPayment.status.in_(("planned", "paid")),
            )
        )
    ).all()
    documented = sum(
        (r.amount for r in rows if r.source_kind == "tax_notice"), ZERO
    )
    paid_rows = [r for r in rows if r.status == "paid"]
    paid = sum(
        (r.amount for r in paid_rows if r.source_kind != "tax_notice"),
        ZERO,
    )
    # Срок берём у первой плановой строки, которую НЕ закрыл банковский факт. Плановая
    # строка остаётся в статусе 'planned' и после уплаты (погашение считается на лету, как
    # в календаре), поэтому «самый ранний плановый срок» — не то же самое, что «ближайший
    # непогашенный». Без проверки сводка винила февральский срок за июльский долг:
    # травматизм январь–июнь уплачен, открыт июль со сроком 15.08, а строка краснела
    # «Просрочено, срок 15.02.2026» (найдено владельцем 28.07.2026).
    # Один set на проход: месячные платёжки травматизма одинаковы по сумме, и без «расхода»
    # фактов один платёж закрыл бы сразу несколько.
    used: set = set()
    open_due = next(
        (
            row.paid_on
            for row in sorted(
                (r for r in rows if r.status == "planned" and r.paid_on is not None),
                key=lambda r: r.paid_on,
            )
            if not is_settled(row, paid_rows, used_fact_ids=used)
        ),
        None,
    )
    return documented, paid, open_due


def _due_verdict(
    outstanding: Decimal, due: date | None, as_of: date
) -> tuple[str, str]:
    """Вердикт обязательства с известным остатком: уплачено / к уплате / просрочено."""
    if outstanding <= TOLERANCE:
        return "ok", "ok"
    if due is not None and due < as_of:
        return "overdue", "alert"
    return "due", "info"


async def _fixed_line(session: AsyncSession, *, year: int, as_of: date) -> ReconLine:
    """Фиксированные взносы ИП: сумма года известна заранее (ст. 430 НК)."""
    cfg = year_config(year)
    documented, paid, _ = await _kind_totals(session, year=year, kind="contrib_fixed")
    accrued = cfg.fixed_contribution
    outstanding = max(accrued - paid, ZERO)
    due = fixed_contribution_due(year)
    verdict, severity = _due_verdict(outstanding, due, as_of)
    messages = [
        f"Сумма года {fmt_money(accrued)} ₽ (ст. 430 НК), уплачено {fmt_money(paid)} ₽.",
    ]
    if outstanding > ZERO:
        messages.append(
            f"Остаток {fmt_money(outstanding)} ₽ — крайний срок {due.strftime('%d.%m.%Y')}. "
            f"Платить можно частями: каждый уплаченный рубль сразу уменьшает УСН, поэтому "
            f"выгоднее закрывать до конца квартала, а не в декабре."
        )
    return ReconLine(
        label="Взносы ИП «за себя», фиксированные",
        tax_kind="contrib_fixed",
        period_code="year",
        due_date=due,
        calculated=accrued,
        documented=documented if documented > ZERO else None,
        paid=paid if paid > ZERO else None,
        verdict=verdict,
        severity=severity,
        messages=messages,
        action="Оплатите — уменьшит налог" if outstanding > ZERO else None,
        action_why=(
            f"Не уплачено {fmt_money(outstanding)} ₽ из годовой суммы; вычет по УСН работает "
            f"только по факту уплаты. Платёж готовится в окне «Активные платежи»."
            if outstanding > ZERO
            else None
        ),
        # Платёж ведут документный и прогнозный слои обязательств: годовая строка сверки —
        # ЗЕРКАЛО долга, а не второй его экземпляр (иначе окно покажет сумму дважды).
        payable_amount=None,
    )


async def _injury_line(
    session: AsyncSession, *, year: int, as_of: date
) -> ReconLine | None:
    """Травматизм: начислено по оборотке, платится ОТДЕЛЬНОЙ платёжкой в СФР, не через ЕНП."""
    accrued = Decimal(
        str(
            await session.scalar(
                select(func.coalesce(func.sum(TaxPayrollLedger.injury), 0)).where(
                    TaxPayrollLedger.year == year
                )
            )
            or 0
        )
    )
    # Месяцы без оборотки добираем прогнозом официального контура — иначе строка занизила бы
    # начисление, а прогнозный слой обязательств выставил бы за те же месяцы ВТОРУЮ строку.
    projected = ZERO
    try:
        cfg = year_config(year)
        for month in await months_without_turnover(session, year=year, today=as_of):
            accrual = await official_month_accrual(
                session, year=year, month=month, cfg=cfg
            )
            if accrual is not None:
                projected += accrual.injury
    except Exception:  # noqa: BLE001 - нет конфига года: работаем по одной оборотке
        projected = ZERO
    accrued += projected

    documented, paid, open_due = await _kind_totals(
        session, year=year, kind="contrib_injury"
    )
    if accrued <= ZERO and documented <= ZERO and paid <= ZERO:
        return None
    reference = accrued if accrued > ZERO else documented
    outstanding = max(reference - paid, ZERO)
    verdict, severity = _due_verdict(outstanding, open_due, as_of)
    messages = [
        f"Начислено {fmt_money(accrued)} ₽ (0,2 % от ФОТ), уплачено {fmt_money(paid)} ₽."
        + (
            f" Из них {fmt_money(projected)} ₽ — прогноз по месяцам без оборотки."
            if projected > ZERO
            else ""
        ),
        "Уходит отдельной платёжкой в СФР по своим реквизитам — в ЕНП не входит.",
    ]
    if outstanding > ZERO and open_due is not None:
        messages.append(
            f"Остаток {fmt_money(outstanding)} ₽, срок {open_due.strftime('%d.%m.%Y')} "
            f"(15 число следующего месяца, 125-ФЗ ст. 22)."
        )
    return ReconLine(
        label="Взносы на травматизм",
        tax_kind="contrib_injury",
        period_code="year",
        due_date=open_due,
        calculated=accrued if accrued > ZERO else None,
        documented=documented if documented > ZERO else None,
        paid=paid if paid > ZERO else None,
        verdict=verdict,
        severity=severity,
        messages=messages,
        action="Оплатите — платёжка в СФР" if outstanding > ZERO else None,
        action_why=(
            f"Не уплачено {fmt_money(outstanding)} ₽. Реквизиты у взноса свои (ОСФР), "
            f"через ЕНП он не проходит. Платёж готовится в окне «Активные платежи»."
            if outstanding > ZERO
            else None
        ),
        payable_amount=None,  # см. комментарий в _fixed_line
    )


async def build_reconciliation(
    session: AsyncSession, *, as_of: date
) -> Reconciliation:
    """Собрать сверку трёх слоёв на дату среза."""
    year = as_of.year
    lines: list[ReconLine] = []
    # Расчётная переплата на ЕНС — для смягчения «документ меньше начисленного»:
    # бухгалтер может законно тратить переплату, присылая платёжку меньше расчёта.
    wallet = await compute_ens_wallet(session, as_of=as_of)
    unallocated = await _unallocated_fns_facts(session, year=year, as_of=as_of)

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
        # Бухгалтер не берёт травматизм в вычет («копейки»), движок берёт — её платёжка
        # систематически больше нашего расчёта ровно на уплаченный травматизм. Это разная
        # методика, а не ошибка: держим её как допуск, чтобы сверка не краснела каждый квартал.
        injury_claimed = await _injury_in_deduction(session, year=year, as_of=p_end)
        verdict, severity, messages = _classify(
            calculated=calculated,
            documented=documented,
            paid=paid,
            due_date=due,
            as_of=as_of,
            doc_gap_allowance=injury_claimed,
            allowance_reason=(
                "бухгалтер не берёт в вычет уплаченный травматизм "
                f"({fmt_money(injury_claimed)} ₽ с начала года), а мы берём "
                "(подп. 1 п. 3.1 ст. 346.21 НК)"
            ),
        )
        _usn_action = _action_for(verdict, documented=documented, expected=calculated)
        if verdict == "doc_mismatch" and documented is not None and documented > calculated:
            _usn_action = (
                _usn_action[0],
                _explain_doc_gap(
                    _usn_action[1],
                    gap=documented - calculated,
                    injury=injury_claimed,
                ),
            )
        lines.append(
            _offset_by_ens(
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
                    action=_usn_action[0],
                    action_why=_usn_action[1],
                    payable_amount=_payable(
                        verdict,
                        documented=documented,
                        calculated=calculated,
                        expected=calculated,
                        paid=paid,
                    ),
                ),
                expected=calculated,
                wallet_balance=wallet.balance,
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
    # Приростная платёжка сверяется с ОСТАТКОМ к доплате — его и передаём как ожидание.
    _extra_action = _action_for(
        verdict, documented=documented, expected=state.extra_accrued - (paid or ZERO)
    )
    lines.append(
        _offset_by_ens(
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
                action=_extra_action[0],
                action_why=_extra_action[1],
                payable_amount=_payable(
                    verdict,
                    documented=documented,
                    calculated=state.extra_accrued,
                    expected=state.extra_accrued - (paid or ZERO),
                ),
            ),
            expected=state.extra_accrued - (paid or ZERO),
            wallet_balance=wallet.balance,
        )
    )

    # ── Взносы ИП «за себя» и травматизм ─────────────────────────────────────
    # Вопрос владельца 27.07.2026: «травматизм начислен, но не уплачен — почему его нет в
    # сводке как платежа к уплате?». Оба обязательства были только в «Активных платежах»,
    # а на главном экране их не было вовсе: начисление известно, остаток тоже, показывать
    # обязаны. Дедуп с окном — по (вид, период='year'), как у остальных строк.
    lines.append(await _fixed_line(session, year=year, as_of=as_of))
    injury_line = await _injury_line(session, year=year, as_of=as_of)
    if injury_line is not None:
        lines.append(injury_line)

    # ── Зарплатный ЕНП: разнос из оборотки (НДФЛ + взносы за работников) ──────
    # Сверяем НАЧИСЛЕНО (оборотка) с фактом разноса и показываем платёжку справочно.
    # Помесячно платёжка ЕНП с начислением НЕ совпадает — НДФЛ платится «окнами» (за месяц N
    # частями до 28 N и до 5 N+1), поэтому платёжку не сверяем жёстко, а показываем состав.
    accruals = await _payroll_accruals(session, year=year)
    reconstructed = await payroll_facts_without_turnover(session, year=year)
    accruals.update(reconstructed)
    enp_docs = await _enp_payroll_documents(session)
    for month in sorted(accruals):
        contributions, ndfl = accruals[month]
        accrued = contributions + ndfl
        if accrued <= ZERO:
            continue
        period = f"{year}-{month:02d}"
        month_due = payroll_enp_due(year, month)
        documented = enp_docs.get(month_due)
        # Месяц без оборотки восстановлен ИЗ ФАКТА уплаты — он закрыт по определению, даже
        # если срок ещё не наступил (платят и досрочно). Иначе строка звала бы платить второй
        # раз то, что уже уплачено.
        settled = month_due <= as_of or month in reconstructed
        paid_contributions, paid_ndfl = (
            await _payroll_paid_breakdown(session, year=year, period=period)
            if settled
            else (ZERO, ZERO)
        )
        paid_total = paid_contributions + paid_ndfl
        paid = paid_total if paid_total > ZERO else None

        month_name = _MONTHS_RU_GENITIVE[month - 1]
        messages = [
            f"«Расчёт» — это начислено за {month_name}: взносы за работников "
            f"{fmt_money(contributions)} ₽ (идут в вычет УСН) + НДФЛ {fmt_money(ndfl)} ₽ "
            f"(в вычет не идёт)."
        ]
        if month in reconstructed:
            messages.append(
                f"Оборотки за {month_name} бухгалтер не присылала — состав восстановлен из "
                f"уплаченного ЕНП: взносы по платёжке травматизма (0,2 % от ФОТ), НДФЛ — "
                f"остаток платежа. Запросите оборотку, если хотите подтвердить цифры."
            )
        action: str | None = None
        action_why: str | None = None
        if settled:
            verdict, severity = "ok", "ok"
            messages.append(f"Уплачено в составе ЕНП, срок {month_due.strftime('%d.%m.%Y')}.")
            # Прямой ответ на «расчёт с фактом не сходится»: сверять надо ВЗНОСЫ (они и идут
            # в вычет) — они совпадают копейка в копейку. Разница целиком в НДФЛ: начисляется
            # он помесячно, а платится «окнами» по датам выплат (до 28 числа — за 1–22, до
            # 5 числа следующего — за остаток), поэтому в платёж месяца попадает НДФЛ с аванса
            # СЛЕДУЮЩЕГО месяца и не попадает часть текущего.
            gap = paid_total - accrued
            if paid_contributions > ZERO and abs(gap) > TOLERANCE:
                if abs(paid_contributions - contributions) <= TOLERANCE:
                    messages.append(
                        f"Взносы сошлись: начислено и уплачено {fmt_money(contributions)} ₽ — "
                        f"в вычет УСН идут именно они. Разница с фактом "
                        f"{fmt_money(abs(gap))} ₽ — это НДФЛ: начислено за месяц "
                        f"{fmt_money(ndfl)} ₽, уплачено в этом ЕНП {fmt_money(paid_ndfl)} ₽ "
                        f"(«окна» уплаты, не расхождение)."
                    )
                else:
                    messages.append(
                        f"Взносы: начислено {fmt_money(contributions)} ₽, уплачено "
                        f"{fmt_money(paid_contributions)} ₽ — проверьте месяц с бухгалтером."
                    )
        else:
            verdict, severity = "due", "info"
            messages.append(
                f"Уплачивается в составе ЕНП до {month_due.strftime('%d.%m.%Y')}."
            )
            action = "Оплатите с ближайшим ЕНП"
            action_why = f"Срок уплаты — {month_due.strftime('%d.%m.%Y')}."
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
                action_why=action_why,
                # Платить нужно сумму платёжки бухгалтера (она учитывает «окна» НДФЛ),
                # иначе — начисленное за месяц.
                payable_amount=_payable(
                    verdict, documented=documented, calculated=accrued, paid=paid
                ),
            )
        )

    # Синхронизация с окном «Активные платежи»: если по обязательству уже есть живой
    # черновик платёжки, строка сверки это знает — кнопка «Отправить в банк» гаснет и
    # сменяется статусом. Допвзнос 1% матчим по одному виду: сверка кодирует его годом,
    # платёжка приходит на прирост квартала — сравнивать периоды напрямую нельзя.
    active_drafts = (
        await session.scalars(
            select(TaxBankDraft).where(
                TaxBankDraft.status.in_(("ready_to_send", "in_bank"))
            )
        )
    ).all()

    def _draft_status(tax_kind: str, period_code: str) -> str | None:
        for draft in active_drafts:
            if draft.tax_kind != tax_kind:
                continue
            if draft.for_period == period_code or tax_kind == "contrib_extra_1pct":
                return draft.status
        return None

    lines = [
        replace(line, draft_status=_draft_status(line.tax_kind, line.period_code))
        for line in lines
    ]

    # Неразнесённые платежи в ФНС — вероятная ПРИЧИНА «документ меньше расчёта» по УСН:
    # их взносная часть не в вычете, расчёт завышен, а платёжка бухгалтера (у неё разнос
    # есть) выглядит заниженной. Называем причину прямо в строке расхождения.
    if unallocated > ZERO:
        hint = (
            f"Возможная причина: в банке есть неразнесённые платежи в ФНС на "
            f"{fmt_money(unallocated)} ₽ (нет оборотки за их месяцы) — их взносная часть "
            f"не в вычете, и наш расчёт завышен. Пришлите оборотки — расхождение, "
            f"скорее всего, уйдёт."
        )
        lines = [
            replace(line, messages=[*line.messages, hint])
            if line.tax_kind == "usn_advance"
            and line.verdict == "doc_mismatch"
            and line.documented is not None
            and line.calculated is not None
            and line.documented < line.calculated
            else line
            for line in lines
        ]
    return Reconciliation(year=year, as_of=as_of, lines=lines)
