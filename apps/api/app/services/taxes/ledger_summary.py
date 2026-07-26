"""Сводка «начислено / уплачено / осталось» по всем налогам и взносам за год.

Отвечает на вопрос владельца (27.07.2026): «сколько было начислено за весь период,
сколько уплатили, сколько осталось заплатить». Прежний блок «Как сложился вычет»
показывал внутреннюю кухню вычета («заявлено», «не заявлено») и на этот вопрос не
отвечал вовсе.

Одна строка — один вид платежа, три числа:

* **начислено** — сколько должно быть по расчёту/документам за год нарастающим итогом;
* **уплачено** — сколько РЕАЛЬНО ушло из банка (как и в вычете: конвенция «в срок
  считаем уплаченным» деньгами не является и сюда не входит — она отдельной пометкой);
* **осталось** — разница, но не меньше нуля (переплата показывается отдельным полем).

Здесь НЕ считается вычет: он живёт в движке и отвечает на другой вопрос — «насколько
уплаченные взносы уменьшили налог». Разделение намеренное: смешение этих двух вопросов
и делало старый блок нечитаемым.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tax import TaxPayment, TaxPayrollLedger
from app.services.taxes.engine import TaxInputs, TaxState, YearConfig

ZERO = Decimal("0")


@dataclass(frozen=True)
class LedgerRow:
    """Строка сводки: вид платежа в трёх числах."""

    kind: str
    title: str
    accrued: Decimal | None  # None — начисление не считаем (нет источника)
    paid: Decimal
    # None — сравнивать не с чем: начисления известны не за весь период (нет оборотки).
    left: Decimal | None
    # Уменьшает ли этот платёж налог УСН — ключевое различие для владельца
    # (НДФЛ не уменьшает никогда: это налог работника, удержанный из его зарплаты).
    reduces_tax: bool
    recipient: str  # 'fns' | 'sfr'
    # Принято уплаченным по сроку, но банковского факта нет (дыра факт-слоя).
    paid_by_convention: Decimal = ZERO
    note: str | None = None


@dataclass(frozen=True)
class LedgerSummary:
    year: int
    as_of: date
    rows: list[LedgerRow]

    @property
    def accrued_total(self) -> Decimal:
        return sum((r.accrued or ZERO for r in self.rows), ZERO)

    @property
    def paid_total(self) -> Decimal:
        return sum((r.paid for r in self.rows), ZERO)

    @property
    def left_total(self) -> Decimal:
        return sum((r.left or ZERO for r in self.rows), ZERO)


@dataclass(frozen=True)
class _PayrollAccrued:
    """Начислено по оборотке за год + покрытие: за сколько месяцев она вообще есть."""

    contributions: Decimal
    ndfl: Decimal
    injury: Decimal
    months_covered: int


async def _payroll_accrued(session: AsyncSession, *, year: int) -> _PayrollAccrued:
    """Начислено за год по оборотке и за сколько месяцев она загружена."""
    row = (
        await session.execute(
            select(
                func.coalesce(func.sum(TaxPayrollLedger.contributions), 0),
                func.coalesce(func.sum(TaxPayrollLedger.ndfl), 0),
                func.coalesce(func.sum(TaxPayrollLedger.injury), 0),
                func.count(func.distinct(TaxPayrollLedger.month)),
            ).where(TaxPayrollLedger.year == year)
        )
    ).one()
    return _PayrollAccrued(
        contributions=Decimal(str(row[0])),
        ndfl=Decimal(str(row[1])),
        injury=Decimal(str(row[2])),
        months_covered=int(row[3]),
    )


async def _paid_by_kind(
    session: AsyncSession, *, year: int, kinds: tuple[str, ...], as_of: date
) -> tuple[Decimal, Decimal]:
    """Уплачено на дату среза: ``(реальные списания, принятое по сроку)``.

    «Уплачено» — это ДЕНЬГИ ИЗ БАНКА, ровно как считает движок (``exclude_source=
    ('tax_notice',)``, repository.py). Разнос оборотки (``tax_notice``) — не факт уплаты,
    а конвенция «в срок считаем уплаченным»; складывать его с банковскими фактами нельзя:
    исторический платёж без периода (39 029 ₽ = взносы за три месяца) и помесячная
    конвенция за те же месяцы дают ДВОЙНОЙ СЧЁТ — так на стенде и получилось «уплачено
    больше начисленного» (находка владельца 27.07.2026).

    Вторым числом возвращаем сумму конвенции — её показываем отдельной пометкой, чтобы
    было видно, где банковский факт ещё не найден (дыра факт-слоя, закрывается бэкфиллом).
    """
    rows = (
        await session.execute(
            select(
                TaxPayment.source_kind,
                func.coalesce(func.sum(TaxPayment.amount), 0),
            )
            .where(
                TaxPayment.status == "paid",
                TaxPayment.for_year == year,
                TaxPayment.kind.in_(kinds),
                TaxPayment.paid_on <= as_of,
            )
            .group_by(TaxPayment.source_kind)
        )
    ).all()
    bank = ZERO
    notice = ZERO
    for source, total in rows:
        if source == "tax_notice":
            notice += Decimal(str(total))
        else:
            bank += Decimal(str(total))
    return bank, notice


def _left(accrued: Decimal | None, paid: Decimal, *, comparable: bool = True) -> Decimal | None:
    """Осталось заплатить — не меньше нуля: переплата это не долг.

    ``comparable=False`` — начисления известны не за весь период (бухгалтер прислала
    оборотку не за все месяцы), сравнивать с уплаченным нельзя: показали бы «осталось 0»
    там, где просто не с чем сравнивать (находка владельца 27.07.2026).
    """
    if accrued is None or not comparable:
        return None
    return max(accrued - paid, ZERO)


async def build_ledger_summary(
    session: AsyncSession,
    *,
    state: TaxState,
    inputs: TaxInputs,
    cfg: YearConfig,
    as_of: date,
) -> LedgerSummary:
    """Собрать сводку по всем видам за год на дату среза."""
    year = as_of.year
    payroll = await _payroll_accrued(session, year=year)
    # Месяцы, по которым уже должна быть оборотка: закрывшиеся месяцы года до среза.
    months_expected = max(as_of.month - 1, 0) if as_of.year == year else 12
    payroll_full = payroll.months_covered >= months_expected
    payroll_note = (
        None
        if payroll_full
        else (
            f"Оборотка есть за {payroll.months_covered} мес. из {months_expected} — "
            f"начислено показано только по ним"
        )
    )

    rows: list[LedgerRow] = []

    # УСН: начислено — налог за вычетом того, что уменьшили взносами.
    usn_accrued = max(state.tax_computed - state.deduction_applied, ZERO)
    rows.append(
        LedgerRow(
            kind="usn_advance",
            title="УСН 6%",
            accrued=usn_accrued,
            paid=state.advances_paid,
            left=_left(usn_accrued, state.advances_paid),
            reduces_tax=False,
            recipient="fns",
            note="Налог 6% минус вычет по взносам",
        )
    )

    contrib_paid, contrib_convention = await _paid_by_kind(
        session, year=year, kinds=("contrib_employees",), as_of=as_of
    )
    rows.append(
        LedgerRow(
            kind="contrib_employees",
            title="Страховые взносы за работников",
            accrued=payroll.contributions if payroll.contributions > ZERO else None,
            paid=contrib_paid,
            left=_left(
                payroll.contributions if payroll.contributions > ZERO else None,
                contrib_paid,
                comparable=payroll_full,
            ),
            reduces_tax=True,
            recipient="fns",
            paid_by_convention=contrib_convention,
            note=payroll_note or "Начисления — из оборотки бухгалтера",
        )
    )

    ndfl_paid, ndfl_convention = await _paid_by_kind(
        session, year=year, kinds=("ndfl",), as_of=as_of
    )
    rows.append(
        LedgerRow(
            kind="ndfl",
            title="НДФЛ за работников",
            accrued=payroll.ndfl if payroll.ndfl > ZERO else None,
            paid=ndfl_paid,
            left=_left(
                payroll.ndfl if payroll.ndfl > ZERO else None, ndfl_paid, comparable=payroll_full
            ),
            reduces_tax=False,
            recipient="fns",
            paid_by_convention=ndfl_convention,
            note=(
                payroll_note
                or "Налог работника: удерживается из зарплаты, налог ИП не уменьшает"
            ),
        )
    )

    rows.append(
        LedgerRow(
            kind="contrib_fixed",
            title="Взносы ИП «за себя», фиксированные",
            accrued=cfg.fixed_contribution,
            paid=inputs.fixed_paid,
            left=_left(cfg.fixed_contribution, inputs.fixed_paid),
            reduces_tax=True,
            recipient="fns",
            note="Фиксированная сумма за год, срок 28 декабря",
        )
    )

    rows.append(
        LedgerRow(
            kind="contrib_extra_1pct",
            title="Взносы ИП «за себя», 1% с дохода",
            accrued=state.extra_accrued,
            paid=inputs.extra_paid,
            left=_left(state.extra_accrued, inputs.extra_paid),
            reduces_tax=True,
            recipient="fns",
            note="1% с дохода свыше 300 000 ₽, срок 1 июля следующего года",
        )
    )

    injury_paid, injury_convention = await _paid_by_kind(
        session, year=year, kinds=("contrib_injury",), as_of=as_of
    )
    rows.append(
        LedgerRow(
            kind="contrib_injury",
            title="Взносы на травматизм",
            accrued=payroll.injury if payroll.injury > ZERO else None,
            paid=injury_paid,
            left=_left(
                payroll.injury if payroll.injury > ZERO else None,
                injury_paid,
                comparable=payroll_full,
            ),
            reduces_tax=True,
            recipient="sfr",
            paid_by_convention=injury_convention,
            note=payroll_note or "Платится в СФР отдельной платёжкой, не через ЕНП",
        )
    )

    return LedgerSummary(year=year, as_of=as_of, rows=rows)
