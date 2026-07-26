"""Движок расчёта УСН «Доходы» 6% нарастающим итогом.

Расчёт — ЧИСТАЯ ФУНКЦИЯ от фактов (``compute_tax_state``): выручка, платежи, конфиг года.
Никаких начислений в БД нет, поэтому пересчёт всегда безопасен и сторнировать нечего.
Загрузка фактов из БД вынесена в ``repository.py``, чтобы математику можно было проверять
на числах без базы — золотой тест сверяется с фактической платёжкой Q1 2026.

Методология и нормы — см. докстринг ``app/models/tax.py``. Три правила, которые чаще всего
путают и на которых держится корректность:

1. Взносы ЗА РАБОТНИКОВ дают вычет ТОЛЬКО по факту уплаты (кассовый принцип, подп. 1 п. 3.1
   ст. 346.21), а взносы ИП ЗА СЕБЯ — по начислению (абз. 7 п. 3.1 в ред. 389-ФЗ).
2. НДФЛ не входит в вычет никогда: это налог работника. В ЕНП он уходит одной суммой со
   взносами, поэтому платёж обязан раскладываться на назначения.
3. Лимит 50% применяется к СОВОКУПНОСТИ вычета, и срезанная им часть СГОРАЕТ — она не
   переносится ни на следующий период, ни на следующий год. Это отличает её от «не заявленного»
   вычета, который внутри года ещё можно использовать.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

ZERO = Decimal("0")
ONE = Decimal("1")

# Налог исчисляется в ПОЛНЫХ РУБЛЯХ (п. 6 ст. 52 НК РФ). Взносы — в копейках.
_RUB = Decimal("1")
_KOPECK = Decimal("0.01")

# Границы отчётных периодов УСН (месяц, день) и их порядок нарастающим итогом.
PERIOD_END = {"q1": (3, 31), "h1": (6, 30), "9m": (9, 30), "year": (12, 31)}
PERIOD_SEQ = ("q1", "h1", "9m", "year")


class TaxComputationError(RuntimeError):
    """Расчёт невозможен: нет конфига года, другой налоговый режим, битые входные данные."""


def rub(value: Decimal) -> Decimal:
    """Округление до полного рубля (п. 6 ст. 52 НК РФ) — только для сумм НАЛОГА.

    Без него цифра разойдётся с платёжкой налогового агента уже на первом квартале:
    11 936 048 × 6% = 716 162,88, а в бюджет уходит 716 163.
    """
    return value.quantize(_RUB, rounding=ROUND_HALF_UP)


def money(value: Decimal) -> Decimal:
    """Округление до копейки — для ВЗНОСОВ (они уплачиваются в копейках, ст. 431 НК)."""
    return value.quantize(_KOPECK, rounding=ROUND_HALF_UP)


def clamp(value: Decimal, low: Decimal, high: Decimal) -> Decimal:
    return max(low, min(value, high))


def fmt_money(value: Decimal) -> str:
    """Сумма в русском формате: неразрывный пробел между разрядами, запятая в дробной части."""
    return f"{value:,.2f}".replace(",", " ").replace(".", ",")


def period_code_for(as_of: date) -> str:
    """Отчётный период УСН, в который попадает дата."""
    month = as_of.month
    if month <= 3:
        return "q1"
    if month <= 6:
        return "h1"
    if month <= 9:
        return "9m"
    return "year"


def period_end_date(year: int, period_code: str) -> date:
    month, day = PERIOD_END[period_code]
    return date(year, month, day)


def fixed_contribution_due(year: int) -> date:
    """Срок уплаты фиксированных взносов ИП «за себя» — 28 декабря (ст. 432 НК)."""
    return date(year, 12, 28)


@dataclass(frozen=True)
class YearConfig:
    """Параметры налогового года. Меняются законом ежегодно — держим данными, не константами."""

    year: int
    regime: str = "usn_income"
    usn_rate: Decimal = Decimal("0.06")
    has_employees: bool = True
    deduction_limit_pct: Decimal = Decimal("0.50")
    # Фиксированные взносы ИП «за себя» за год (ст. 430 НК). 2026 — 57 390 ₽, срок 28.12.2026.
    fixed_contribution: Decimal = Decimal("57390.00")
    # Допвзнос 1%: база — превышение дохода над порогом, сверху ограничен потолком.
    extra_threshold: Decimal = Decimal("300000.00")
    extra_rate: Decimal = Decimal("0.01")
    extra_ceiling: Decimal = Decimal("321818.00")
    # Метрика выручки iiko, принятая за базу. Решение владельца 23.07.2026 — «со скидками».
    base_metric: str = "revenue_net"
    # ── Зарплатные параметры официального контура (прогноз начислений) ───────────
    # МРОТ и тарифы меняются законом ежегодно — данные, не константы. Формула взносов
    # МСП сверена с обороткой бухгалтера копейка в копейку: июль 2026, оклад 50 000 →
    # 40 639,50 × 30% + 9 360,50 × 15% = 13 595,93.
    mrot: Decimal = Decimal("27093.00")
    ndfl_rate: Decimal = Decimal("0.13")
    # Взносы МСП: полный тариф в пределах порога (множитель МРОТ), льготный — свыше.
    msp_full_rate: Decimal = Decimal("0.30")
    msp_reduced_rate: Decimal = Decimal("0.15")
    msp_threshold_mrot: Decimal = Decimal("1.5")
    # Травматизм (НС и ПЗ), I класс риска — платится в СФР отдельно, не через ЕНС.
    injury_rate: Decimal = Decimal("0.002")
    # Момент признания взносов ИП «ЗА СЕБЯ» (фиксированных и допвзноса 1%):
    #
    # * ``cash``    — только фактически уплаченные внутри периода. Так считает налоговый агент,
    #                 и так построены обе платёжки 2026 года (674 624 за Q1 и 478 376 за H1).
    #                 Решение владельца 23.07.2026: следуем этой конвенции.
    # * ``accrual`` — вся сумма, подлежащая уплате в налоговом периоде, независимо от факта
    #                 оплаты (абз. 7 п. 3.1 ст. 346.21 в ред. 389-ФЗ). Юридически, по нашей
    #                 проверке, верен именно этот вариант, но адресного разъяснения ФНС по
    #                 связке «ИП-работодатель + свои взносы» не существует.
    #
    # Взносов ЗА РАБОТНИКОВ переключатель НЕ касается: для них кассовый принцип прямо
    # закреплён подп. 1 п. 3.1 и действует всегда.
    self_contributions_basis: str = "cash"


SELF_CONTRIBUTION_BASES: tuple[str, ...] = ("cash", "accrual")

# Конфиги известных лет. 2025 — патент, УСН неприменим; движок обязан это увидеть и отказаться.
YEAR_CONFIGS: dict[int, YearConfig] = {
    2025: YearConfig(year=2025, regime="patent"),
    2026: YearConfig(year=2026),
}


@dataclass(frozen=True)
class ClaimPolicy:
    """Сколько взносов ИП «за себя» заявляем к вычету в этом периоде.

    Это РЕШЕНИЕ, а не вычисление: НК даёт право выбора, каким периодом воспользоваться
    (письмо ФНС от 08.04.2024 № СД-4-3/4104@). За Q1 2026 налоговый агент не заявлял ничего —
    политика ``off`` воспроизводит фактическую платёжку.

    ``None`` в сумме означает «взять максимум доступного».
    """

    fixed: Decimal | None = None
    extra_current: Decimal | None = None
    extra_prior: Decimal | None = None

    @staticmethod
    def off() -> ClaimPolicy:
        return ClaimPolicy(fixed=ZERO, extra_current=ZERO, extra_prior=ZERO)

    @staticmethod
    def maximum() -> ClaimPolicy:
        return ClaimPolicy()

    def resolve(self, slot: str, available: Decimal) -> Decimal:
        requested = getattr(self, slot)
        if requested is None:
            return available
        return clamp(requested, ZERO, available)


@dataclass(frozen=True)
class TaxInputs:
    """Факты, из которых считается состояние. Всё уже отобрано по году и дате среза."""

    config: YearConfig
    as_of: date
    income_ytd: Decimal
    # Покрытие базы: сколько месяцев года загружено против ожидаемых.
    covered_months: int
    expected_months: int
    # Взносы за работников и травматизм, ФАКТИЧЕСКИ уплаченные в [1 января .. as_of].
    employees_paid: Decimal
    # Взносы ИП «за себя», ФАКТИЧЕСКИ уплаченные в [1 января .. as_of].
    # Используются при ``self_contributions_basis='cash'``; при 'accrual' игнорируются.
    fixed_paid: Decimal = ZERO
    extra_paid: Decimal = ZERO
    # Уже израсходованные суммы взносов «за себя» — из зафиксированных периодов.
    fixed_used: Decimal = ZERO
    extra_used: Decimal = ZERO
    # Допвзнос за ПРОШЛЫЙ расчётный период, если он остался незаявленным.
    extra_prior_accrued: Decimal = ZERO
    extra_prior_used: Decimal = ZERO
    # Авансы по УСН, уплаченные ранее ЗА ЭТОТ ЖЕ ГОД (ключ — for_year, не дата платежа).
    advances_paid: Decimal = ZERO
    # Уплаченные взносы всех видов за год — для метрики совокупной нагрузки.
    contributions_paid: Decimal = ZERO
    claim: ClaimPolicy = field(default_factory=ClaimPolicy.maximum)


@dataclass(frozen=True)
class TaxState:
    """Результат расчёта на дату среза."""

    year: int
    as_of: date
    period_code: str

    income_ytd: Decimal
    tax_computed: Decimal

    employees_paid: Decimal
    fixed_available: Decimal
    fixed_claimed: Decimal
    extra_accrued: Decimal
    extra_available: Decimal
    extra_claimed: Decimal
    extra_prior_available: Decimal
    extra_prior_claimed: Decimal

    deduction_total: Decimal
    deduction_limit: Decimal
    deduction_applied: Decimal
    deduction_burned: Decimal
    deduction_unclaimed: Decimal
    # Начислено, но ещё не уплачено по взносам «за себя». При основании 'cash' эта сумма
    # в вычет не попала и переносится на будущие периоды года — цена конвенции.
    deduction_deferred: Decimal

    advances_paid: Decimal
    amount_due: Decimal
    overpayment: Decimal

    total_burden: Decimal
    effective_rate: Decimal

    input_fingerprint: str
    warnings: list[str]
    blocking: list[str]


def compute_tax_state(inputs: TaxInputs) -> TaxState:
    """Посчитать УСН нарастающим итогом с 1 января. Чистая функция, без обращений к БД."""
    cfg = inputs.config
    if cfg.regime != "usn_income":
        raise TaxComputationError(
            f"{cfg.year} год не на УСН «Доходы» (режим: {cfg.regime}) — расчёт неприменим"
        )

    as_of = inputs.as_of
    warnings: list[str] = []
    blocking: list[str] = []

    # ── 1. Доход нарастающим итогом ──────────────────────────────────────────
    income_ytd = money(inputs.income_ytd)
    if inputs.covered_months < inputs.expected_months:
        blocking.append(
            f"Выручка загружена не за весь период: {inputs.covered_months} мес. "
            f"из {inputs.expected_months}. База занижена — обновите выручку из iiko."
        )

    # ── 2. Налог исчисленный — в полных рублях ───────────────────────────────
    tax_computed = rub(income_ytd * cfg.usn_rate)

    # ── 3. Вычет А: взносы за работников — только по факту уплаты ────────────
    employees_paid = money(inputs.employees_paid)

    # ── 4. Вычет Б: фиксированные взносы ИП «за себя» ────────────────────────
    # Основание признания задаётся конфигом года (см. YearConfig.self_contributions_basis):
    # при 'accrual' доступна вся годовая сумма сразу, при 'cash' — только уплаченное.
    if cfg.self_contributions_basis == "accrual":
        fixed_source = cfg.fixed_contribution
    else:
        fixed_source = money(inputs.fixed_paid)
    fixed_available = max(fixed_source - inputs.fixed_used, ZERO)
    fixed_claimed = inputs.claim.resolve("fixed", fixed_available)

    # ── 5. Вычет В: допвзнос 1% — с потолком и реестром израсходованного ─────
    # Начисление считается всегда: оно нужно и для платёжки по взносу, и для сравнения
    # двух оснований. А вот в вычет при 'cash' попадает только фактически уплаченное.
    # Порог 300 000 вычитается ОДИН РАЗ ЗА ГОД — это обеспечивает YTD-формула.
    extra_accrued = min(
        money(max(income_ytd - cfg.extra_threshold, ZERO) * cfg.extra_rate),
        cfg.extra_ceiling,
    )
    if cfg.self_contributions_basis == "accrual":
        extra_source = extra_accrued
    else:
        # Уплатить вперёд больше начисленного можно, но вычет ограничен начислением.
        extra_source = min(money(inputs.extra_paid), extra_accrued)
    extra_available = max(extra_source - inputs.extra_used, ZERO)
    extra_claimed = inputs.claim.resolve("extra_current", extra_available)

    extra_prior_available = max(inputs.extra_prior_accrued - inputs.extra_prior_used, ZERO)
    extra_prior_claimed = inputs.claim.resolve("extra_prior", extra_prior_available)

    # ── 6. Лимит 50% к совокупности вычета ───────────────────────────────────
    deduction_total = employees_paid + fixed_claimed + extra_claimed + extra_prior_claimed
    deduction_potential = (
        employees_paid + fixed_available + extra_available + extra_prior_available
    )
    limit_pct = cfg.deduction_limit_pct if cfg.has_employees else ONE
    deduction_limit = rub(tax_computed * limit_pct)
    deduction_applied = min(deduction_total, deduction_limit)
    deduction_burned = deduction_total - deduction_applied
    # «Сгорело» ≠ «не заявили»: первое потеряно навсегда, второе внутри года ещё можно заявить.
    deduction_unclaimed = max(
        min(deduction_potential, deduction_limit) - deduction_applied, ZERO
    )

    # Сколько вычета отложено из-за того, что взносы «за себя» ещё не уплачены.
    # Ограничиваем свободным местом под лимитом: то, что и так сгорело бы, ценой не является.
    if cfg.self_contributions_basis == "cash":
        unpaid_self = max(cfg.fixed_contribution - money(inputs.fixed_paid), ZERO) + max(
            extra_accrued - money(inputs.extra_paid), ZERO
        )
        deduction_deferred = min(unpaid_self, max(deduction_limit - deduction_applied, ZERO))
    else:
        deduction_deferred = ZERO

    if deduction_burned > ZERO:
        warnings.append(
            f"Лимит 50% упёрт: {fmt_money(deduction_burned)} ₽ вычета сгорает и не "
            f"переносится ни на следующий период, ни на следующий год."
        )
    if deduction_deferred > ZERO:
        warnings.append(
            f"Взносы «за себя» на {fmt_money(deduction_deferred)} ₽ начислены, но не уплачены. "
            f"По принятой конвенции (по оплате) вычет отложен: уплатите их до конца "
            f"следующего отчётного периода — и налог уменьшится на эту сумму."
        )
    if deduction_unclaimed > ZERO:
        warnings.append(
            f"Не заявлено {fmt_money(deduction_unclaimed)} ₽ вычета — эту сумму ещё можно "
            f"заявить в текущем году, налог к уплате уменьшится на неё."
        )

    # ── 7. К уплате за период ────────────────────────────────────────────────
    balance = tax_computed - deduction_applied - inputs.advances_paid
    amount_due = rub(max(balance, ZERO))
    overpayment = rub(max(-balance, ZERO))

    # ── 8. Контроль инварианта: вычет не создаёт актив ───────────────────────
    # При не-упёртом лимите совокупный «налоговый» отток равен ровно 6% от дохода:
    # уплаченные авансы + остаток к уплате + зачтённый вычет.
    if deduction_burned == ZERO and income_ytd > ZERO:
        actual = inputs.advances_paid + amount_due + deduction_applied
        if abs(actual - tax_computed) > ONE:
            warnings.append(
                f"Контроль не сошёлся: {fmt_money(actual)} ₽ вместо "
                f"{fmt_money(tax_computed)} ₽. Вероятно, ЕНП разнесён неверно "
                f"или потерян платёж."
            )

    total_burden = inputs.advances_paid + amount_due + money(inputs.contributions_paid)
    effective_rate = (total_burden / income_ytd) if income_ytd > ZERO else ZERO

    fingerprint = _fingerprint(cfg, income_ytd, employees_paid, inputs.advances_paid)

    return TaxState(
        year=cfg.year,
        as_of=as_of,
        period_code=period_code_for(as_of),
        income_ytd=income_ytd,
        tax_computed=tax_computed,
        employees_paid=employees_paid,
        fixed_available=fixed_available,
        fixed_claimed=fixed_claimed,
        extra_accrued=extra_accrued,
        extra_available=extra_available,
        extra_claimed=extra_claimed,
        extra_prior_available=extra_prior_available,
        extra_prior_claimed=extra_prior_claimed,
        deduction_total=deduction_total,
        deduction_limit=deduction_limit,
        deduction_applied=deduction_applied,
        deduction_burned=deduction_burned,
        deduction_unclaimed=deduction_unclaimed,
        deduction_deferred=deduction_deferred,
        advances_paid=money(inputs.advances_paid),
        amount_due=amount_due,
        overpayment=overpayment,
        total_burden=money(total_burden),
        effective_rate=effective_rate,
        input_fingerprint=fingerprint,
        warnings=warnings,
        blocking=blocking,
    )


def _fingerprint(
    cfg: YearConfig, income: Decimal, employees_paid: Decimal, advances: Decimal
) -> str:
    """Отпечаток входных данных — по нему ловится дрейф базы после фиксации периода."""
    payload = f"{cfg.year}|{cfg.usn_rate}|{income}|{employees_paid}|{advances}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
