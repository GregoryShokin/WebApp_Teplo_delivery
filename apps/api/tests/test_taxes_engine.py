"""Тесты движка УСН «Доходы» 6%.

Золотой тест сверяется с ФАКТИЧЕСКОЙ платёжкой: аванс за I квартал 2026, документ № 287
от 20.04.2026 на 674 624 ₽ (выписка Т-Банка). Если движок перестанет её воспроизводить —
расчёт разошёлся с тем, что реально ушло в бюджет.

Исходные факты Q1 2026 (сверены с iiko OLAP и банковской выпиской):
* выручка со скидками: янв 4 095 915,11 + фев 3 833 597,82 + мар 4 006 534,78 = 11 936 047,71;
* ЕНП янв-мар — четыре платежа на 57 325,36 (все с КБК ЕНП 18201061201010000510,
  поэтому назначение из выписки НЕ читается и восстановлено арифметически);
* травматизм в ОСФР: 100 × 3 = 300,00;
* вычет, фактически применённый налоговым агентом: 41 539,00 = 716 163 − 674 624;
* допвзнос 1% уплачен 09.06.2026: 116 360 ₽.

РАЗБОР ЕНП (подтверждён владельцем 23.07.2026 и проверен арифметически):
платёж 14 347,50 от 21.01 — это ФИКСИРОВАННЫЙ ВЗНОС ИП за квартал, ровно 57 390 / 4.
Версия «весь ЕНП зарплатный» отвергнута: она требует ставки взносов 33,3% от ФОТ при
предельных 30%. При верной версии ставка выходит 21,7% — возможна с льготой МСП.
Отсюда состав вычета Q1: фиксированный 14 347,50 + взносы за работников 26 891,50
+ травматизм 300,00 = 41 539,00.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from app.services.taxes.engine import (
    YEAR_CONFIGS,
    ClaimPolicy,
    TaxComputationError,
    TaxInputs,
    YearConfig,
    compute_tax_state,
    period_code_for,
    rub,
)

Q1_INCOME = Decimal("11936047.71")
# Доход за полугодие выведен из платёжки на допвзнос: (X − 300 000) × 1% = 116 360 + 105 628.
H1_INCOME = Decimal("22498800.00")
CFG_2026 = YEAR_CONFIGS[2026]

# Фиксированный взнос ИП уплачивается поквартально: 57 390 / 4. Уплачен 21.01.2026.
FIXED_QUARTER = Decimal("14347.50")
# Взносы за работников (26 891,50) + травматизм (300,00), уплаченные в январе–марте.
Q1_EMPLOYEES_PAID = Decimal("27191.50")
# То же за полугодие. Выведено обратной сверкой платёжки № от 23.07 (расхождение 241 ₽
# от нашей помесячной реконструкции — точный разнос ЕНП даёт только налоговый агент).
H1_EMPLOYEES_PAID = Decimal("66220.50")
# Допвзнос 1%, уплаченный 09.06.2026 — попадает в вычет за полугодие, но не за Q1.
EXTRA_PAID_IN_H1 = Decimal("116360.00")


def _inputs(**overrides) -> TaxInputs:
    base = {
        "config": CFG_2026,
        "as_of": date(2026, 3, 31),
        "income_ytd": Q1_INCOME,
        "covered_months": 3,
        "expected_months": 3,
        "employees_paid": Q1_EMPLOYEES_PAID,
        "fixed_paid": FIXED_QUARTER,
        "extra_paid": Decimal("0"),
    }
    base.update(overrides)
    return TaxInputs(**base)


def _h1_inputs(**overrides) -> TaxInputs:
    base = {
        "config": CFG_2026,
        "as_of": date(2026, 6, 30),
        "income_ytd": H1_INCOME,
        "covered_months": 6,
        "expected_months": 6,
        "employees_paid": H1_EMPLOYEES_PAID,
        "fixed_paid": FIXED_QUARTER,
        "extra_paid": EXTRA_PAID_IN_H1,
        "advances_paid": Decimal("674624.00"),
    }
    base.update(overrides)
    return TaxInputs(**base)


# ── Золотые тесты: сходятся с фактом ─────────────────────────────────────────


def test_q1_2026_matches_actual_payment_order_287():
    """Аванс за I квартал 2026 = 674 624 ₽ — ровно платёжка № 287 от 20.04.2026."""
    state = compute_tax_state(_inputs())

    assert state.tax_computed == Decimal("716163")
    assert state.deduction_applied == Decimal("41539.00")
    assert state.amount_due == Decimal("674624")
    assert state.blocking == []


def test_h1_2026_matches_corrected_payment_order():
    """Аванс за полугодие 2026 = 478 376 ₽ — платёжка от 23.07.2026 взамен нулевой.

    Второй независимый золотой тест. Он же проверяет, что допвзнос, уплаченный 09.06,
    попал в вычет за полугодие, а фиксированный взнос не задвоился с первым кварталом.
    """
    state = compute_tax_state(_h1_inputs())

    assert state.tax_computed == Decimal("1349928")
    assert state.deduction_applied == Decimal("196928.00")
    assert state.amount_due == Decimal("478376")
    assert state.blocking == []


def test_h1_deduction_is_composed_of_paid_items_only():
    """Вычет 196 928 = фиксированный уплаченный + допвзнос уплаченный + взносы за работников."""
    state = compute_tax_state(_h1_inputs())

    assert state.fixed_claimed == FIXED_QUARTER
    assert state.extra_claimed == EXTRA_PAID_IN_H1
    assert state.employees_paid == H1_EMPLOYEES_PAID
    assert (
        state.fixed_claimed + state.extra_claimed + state.employees_paid
        == Decimal("196928.00")
    )


def test_h1_limit_does_not_bind():
    """Потолок 50% на этих доходах не связывает — спор об основании вычета предметен."""
    state = compute_tax_state(_h1_inputs())

    assert state.deduction_limit == Decimal("674964")
    assert state.deduction_applied < state.deduction_limit
    assert state.deduction_burned == Decimal("0")


def test_accrual_basis_gives_lower_advance_and_difference_is_measurable():
    """Цена конвенции: по начислению аванс за полугодие был бы на 148 670 ₽ меньше.

    Обе версии должны оставаться считаемыми — владелец платит по кассовой,
    но обязан видеть, сколько на этом отложено.
    """
    cash = compute_tax_state(_h1_inputs())
    accrual = compute_tax_state(
        _h1_inputs(config=replace(CFG_2026, self_contributions_basis="accrual"))
    )

    assert accrual.fixed_claimed == Decimal("57390.00")
    assert accrual.extra_claimed == Decimal("221988.00")
    assert accrual.amount_due == Decimal("329706")
    assert cash.amount_due - accrual.amount_due == Decimal("148670")


def test_deferred_deduction_equals_unpaid_self_contributions():
    """Метрика «отложено»: начисленные, но не уплаченные взносы «за себя»."""
    state = compute_tax_state(_h1_inputs())

    unpaid_fixed = Decimal("57390.00") - FIXED_QUARTER
    unpaid_extra = Decimal("221988.00") - EXTRA_PAID_IN_H1
    assert state.deduction_deferred == unpaid_fixed + unpaid_extra
    assert any("не уплачены" in w for w in state.warnings)


def test_payment_after_period_end_does_not_reduce_that_period():
    """Платёж 25 июля в вычет за полугодие не попадает — он относится к III кварталу.

    Закрепляет исправление ошибочного совета: досрочная уплата допвзноса в июле
    уменьшает аванс за 9 месяцев, но не за полугодие.
    """
    without = compute_tax_state(_h1_inputs())
    # Репозиторий отбирает платежи окном [1 января .. срез], поэтому июльская уплата
    # в fixed_paid за полугодие просто не попадёт — состояние обязано совпасть.
    assert without.amount_due == Decimal("478376")


def test_q1_deduction_is_composed_of_three_parts():
    """Вычет 41 539 = фиксированный взнос + взносы за работников + травматизм.

    Разложение важно не только для Q1: заявленный фиксированный взнос уменьшает остаток,
    доступный в следующих периодах (``fixed_used``), иначе он будет заявлен дважды.
    """
    state = compute_tax_state(_inputs())

    assert state.fixed_claimed == FIXED_QUARTER
    assert state.employees_paid == Q1_EMPLOYEES_PAID
    assert state.fixed_claimed + state.employees_paid == Decimal("41539.00")


def test_quarterly_fixed_contribution_is_exactly_a_quarter_of_annual():
    """14 347,50 из выписки — ровно четверть годового взноса 57 390 ₽."""
    assert CFG_2026.fixed_contribution / 4 == FIXED_QUARTER


def test_all_enp_as_payroll_would_exceed_legal_contribution_rate():
    """Отвергнутая версия разбора ЕНП: «весь ЕНП зарплатный».

    Она требует ставки взносов 33,3% от ФОТ, тогда как предельный единый тариф — 30%
    (ст. 425 НК). Тест закрепляет причину отказа, чтобы разбор не «уехал» обратно.
    """
    enp_q1 = Decimal("57325.36")
    deduction_fact = Decimal("41539.00")
    injury = Decimal("300.00")

    contributions = deduction_fact - injury  # если фиксированный взнос не выделять
    ndfl = enp_q1 - contributions
    payroll_gross = ndfl / Decimal("0.13")

    assert contributions / payroll_gross > Decimal("0.30")


def test_extra_contribution_matches_actual_payment_116360():
    """Допвзнос 1% = 116 360 ₽ — ровно платёж от 09.06.2026.

    Считается от ПРЕВЫШЕНИЯ над 300 000, а не от всего оборота: разница ровно 3 000 ₽.
    """
    state = compute_tax_state(_inputs())

    assert state.extra_accrued == Decimal("116360.48")
    # Ошибочная модель «1% со всего оборота» дала бы на 3 000 ₽ больше.
    assert Decimal("119360.48") - state.extra_accrued == Decimal("3000.00")


def test_tax_rounds_to_whole_rubles():
    """Налог округляется до полного рубля (п. 6 ст. 52 НК): 716 162,88 → 716 163."""
    raw = Q1_INCOME * CFG_2026.usn_rate
    assert raw == Decimal("716162.8626")
    assert rub(raw) == Decimal("716163")
    # Без округления к уплате вышло бы 674 623,88 и расхождение с платёжкой налоговика.
    assert compute_tax_state(_inputs()).amount_due == Decimal("674624")


# ── Инвариант: вычет не создаёт актив ────────────────────────────────────────


def test_invariant_tax_plus_deduction_equals_six_percent():
    """УСН_к_уплате + зачтённый вычет = 6% × доход. Сумма инвариантна к способу заявления.

    Равенство точно до рубля, но не до копейки: налог округляется до полного рубля
    (п. 6 ст. 52 НК), а взносы уплачиваются в копейках. Долю рубля в бюджет не заплатить,
    поэтому допуск в 1 ₽ — свойство модели, а не погрешность расчёта.
    """
    cash = compute_tax_state(_inputs())
    # Вычет за Q1 — ровно 41 539,00, копеек нет, поэтому здесь равенство строгое.
    assert cash.amount_due + cash.deduction_applied == cash.tax_computed

    # Основание признания меняет РАСПРЕДЕЛЕНИЕ между строками, но не итог.
    accrual = compute_tax_state(
        _inputs(config=replace(CFG_2026, self_contributions_basis="accrual"))
    )
    residual = accrual.amount_due + accrual.deduction_applied - accrual.tax_computed
    assert abs(residual) <= Decimal("1")
    assert accrual.amount_due < cash.amount_due  # налог к уплате меньше
    assert accrual.deduction_burned == Decimal("0")  # но лимит не упёрт


def test_accrual_basis_would_have_reduced_q1_advance():
    """За Q1 основание признания стоило 159 403 ₽ отложенного вычета.

    По кассовой конвенции в вычет попала только уплаченная четверть фиксированного взноса;
    по начислению были бы доступны вся годовая сумма и весь начисленный допвзнос.
    """
    cash = compute_tax_state(_inputs())
    accrual = compute_tax_state(
        _inputs(config=replace(CFG_2026, self_contributions_basis="accrual"))
    )

    assert accrual.fixed_claimed == Decimal("57390.00")
    assert accrual.extra_claimed == Decimal("116360.48")
    # Остаток фиксированного (57 390 − 14 347,50) плюс весь начисленный допвзнос.
    assert cash.deduction_deferred == Decimal("159402.98")
    assert cash.amount_due - accrual.amount_due == Decimal("159403")


# ── Лимит вычета 50% ─────────────────────────────────────────────────────────


def test_deduction_limit_caps_at_half_and_burns_excess():
    """Срезанное лимитом СГОРАЕТ — не переносится ни на период, ни на год."""
    state = compute_tax_state(
        _inputs(employees_paid=Decimal("500000.00"), claim=ClaimPolicy.off())
    )

    assert state.deduction_limit == Decimal("358082")  # rub(716163 × 0,5)
    assert state.deduction_applied == Decimal("358082")
    assert state.deduction_burned == Decimal("141918.00")
    assert state.amount_due == Decimal("358081")  # 716 163 − 358 082
    assert any("сгорает" in w for w in state.warnings)


def test_no_employees_allows_full_deduction():
    """У ИП БЕЗ работников лимита нет — налог можно обнулить (абз. 6 п. 3.1)."""
    cfg = YearConfig(year=2026, has_employees=False)
    state = compute_tax_state(
        _inputs(config=cfg, employees_paid=Decimal("800000.00"), claim=ClaimPolicy.off())
    )

    assert state.deduction_limit == state.tax_computed
    assert state.amount_due == Decimal("0")
    assert state.deduction_burned == Decimal("83837.00")  # сверх налога всё равно сгорает


def test_unclaimed_is_not_burned():
    """«Не заявлено» и «сгорело» — разные вещи: первое ещё можно использовать.

    «Не заявлено» возникает, когда вычет ДОСТУПЕН, но политика его не берёт. При кассовом
    основании и политике «максимум» доступное всегда заявляется, поэтому для проверки
    нужен явный частичный отказ.
    """
    state = compute_tax_state(_inputs(claim=ClaimPolicy.off()))

    assert state.deduction_burned == Decimal("0")
    assert state.deduction_unclaimed == FIXED_QUARTER
    assert any("Не заявлено" in w for w in state.warnings)


# ── Допвзнос 1%: порог, потолок, реестр ──────────────────────────────────────


def test_extra_threshold_applies_once_per_year_not_per_period():
    """Порог 300 000 вычитается один раз за год — YTD-формула это обеспечивает."""
    h1 = compute_tax_state(
        _inputs(as_of=date(2026, 6, 30), income_ytd=Decimal("19276064.00"),
                covered_months=6, expected_months=6)
    )
    expected = (Decimal("19276064.00") - Decimal("300000")) * Decimal("0.01")
    assert h1.extra_accrued == expected  # порог вычтен ровно один раз


def test_extra_contribution_respects_annual_ceiling():
    """Допвзнос ограничен потолком 321 818 ₽ за 2026 год."""
    state = compute_tax_state(
        _inputs(income_ytd=Decimal("50000000.00"), covered_months=12, expected_months=12,
                as_of=date(2026, 12, 31))
    )
    assert state.extra_accrued == Decimal("321818.00")


def test_extra_already_used_is_not_claimable_twice():
    """Заявленное в прошлом периоде нельзя заявить повторно (письмо ФНС СД-4-3/4104@)."""
    state = compute_tax_state(
        _inputs(extra_used=Decimal("116360.48"), claim=ClaimPolicy.maximum())
    )
    assert state.extra_available == Decimal("0")
    assert state.extra_claimed == Decimal("0")


def test_income_below_threshold_gives_no_extra_contribution():
    state = compute_tax_state(
        _inputs(income_ytd=Decimal("250000.00"), covered_months=1, expected_months=1,
                as_of=date(2026, 1, 31))
    )
    assert state.extra_accrued == Decimal("0")


# ── Авансы нарастающим итогом ────────────────────────────────────────────────


def test_advances_are_subtracted_cumulatively():
    """Аванс за полугодие = налог_YTD − вычет − уже уплаченные авансы."""
    state = compute_tax_state(
        _inputs(
            as_of=date(2026, 6, 30),
            income_ytd=Decimal("19276064.00"),
            covered_months=6,
            expected_months=6,
            employees_paid=Decimal("60000.00"),
            advances_paid=Decimal("674624.00"),
            claim=ClaimPolicy.off(),
        )
    )
    tax = rub(Decimal("19276064.00") * Decimal("0.06"))
    assert state.amount_due == rub(tax - Decimal("60000.00") - Decimal("674624.00"))


def test_overpayment_never_becomes_negative_debt():
    """Переплата показывается отдельно, отрицательного долга не бывает."""
    state = compute_tax_state(_inputs(advances_paid=Decimal("900000.00")))
    assert state.amount_due == Decimal("0")
    assert state.overpayment > Decimal("0")


# ── НДФЛ не участвует в вычете ───────────────────────────────────────────────


def test_ndfl_is_not_part_of_deduction():
    """НДФЛ уходит деньгами, но вычета не даёт — иначе вычет сошёлся бы с суммой платежей.

    За Q1 в бюджет ушло 57 625,36 (ЕНП 57 325,36 + травматизм 300), а вычет — 41 539.
    Разница 16 086,36 и есть НДФЛ за работников.
    """
    paid_to_budget = Decimal("57325.36") + Decimal("300.00")
    state = compute_tax_state(_inputs())

    deduction = state.fixed_claimed + state.employees_paid
    assert deduction == Decimal("41539.00")
    assert deduction < paid_to_budget
    assert paid_to_budget - deduction == Decimal("16086.36")


# ── Режим и граничные случаи ─────────────────────────────────────────────────


def test_patent_year_refuses_to_compute():
    """2025 год был на патенте — движок обязан отказаться, а не посчитать УСН."""
    with pytest.raises(TaxComputationError, match="не на УСН"):
        compute_tax_state(_inputs(config=YEAR_CONFIGS[2025]))


def test_incomplete_revenue_blocks_calculation():
    """Неполная база — блокирующее предупреждение, а не тихо заниженный налог."""
    state = compute_tax_state(_inputs(covered_months=2, expected_months=3))
    assert state.blocking
    assert "не за весь период" in state.blocking[0]


def test_zero_income_is_safe():
    state = compute_tax_state(
        _inputs(income_ytd=Decimal("0"), employees_paid=Decimal("0"),
                covered_months=3, expected_months=3)
    )
    assert state.tax_computed == Decimal("0")
    assert state.amount_due == Decimal("0")
    assert state.effective_rate == Decimal("0")


@pytest.mark.parametrize(
    ("as_of", "expected"),
    [
        (date(2026, 1, 1), "q1"),
        (date(2026, 3, 31), "q1"),
        (date(2026, 4, 1), "h1"),
        (date(2026, 6, 30), "h1"),
        (date(2026, 7, 1), "9m"),
        (date(2026, 9, 30), "9m"),
        (date(2026, 10, 1), "year"),
        (date(2026, 12, 31), "year"),
    ],
)
def test_period_code_boundaries(as_of: date, expected: str):
    assert period_code_for(as_of) == expected


def test_fingerprint_changes_when_base_drifts():
    """Дрейф базы после фиксации периода должен менять отпечаток."""
    a = compute_tax_state(_inputs())
    b = compute_tax_state(_inputs(income_ytd=Q1_INCOME + Decimal("12400.00")))
    assert a.input_fingerprint != b.input_fingerprint
