#!/usr/bin/env python3
"""Модель «до/после» модернизации суши-цеха Тепла.

Только стандартная библиотека. Читает JSON входов (см. inputs-template.json),
печатает по четырём сценариям показатели и список недостающих входов.

Правила, зашитые в расчёт (решения владельца 14.10.2026):
- Единицы: партия риса -> целый ролл -> заказ -> месяц. Кусочки не используются.
- Неизвестный вход = None. Любой показатель, зависящий от None, тоже None
  («не определено»), а не 0.
- Сэкономленные минуты не уменьшают ФОТ, пока не изменились оплаченные смены
  (сценарий 2). Экономия ФОТ появляется только в сценарии 3 по реально
  снятым сменам. Процентный фонд кухни один на день: в сценариях 2–3 при
  прежней выручке он не меняется; в сценарии 4 считается по дням и порогам.
- Одни и те же высвобожденные часы не используются одновременно для экономии
  (сценарий 3) и для роста заказов (сценарий 4): у сценария 4 мощность берётся
  от состава смен сценария 2.
- Покупка оборудования — инвестиционный поток (capex), в прибыль идёт только
  амортизация. Денежная окупаемость = capex / Δденежного потока в месяц,
  где Δденежного потока = Δприбыли + амортизация (амортизация не платёж).
- ФОТ на ролл — справочный показатель распределения; в месячной прибыли ФОТ
  учитывается один раз, суммой месяца.

Запуск:  python3 model_before_after.py inputs.json [--json]
"""
from __future__ import annotations

import json
import sys
from typing import Any

MISSING: set[str] = set()


def get(d: dict, path: str) -> Any:
    """Достаёт значение по пути 'a.b.c'; None и отсутствие регистрирует как пробел."""
    cur: Any = d
    for key in path.split("."):
        if not isinstance(cur, dict) or key not in cur:
            MISSING.add(path)
            return None
        cur = cur[key]
    if isinstance(cur, dict) and "value" in cur:
        cur = cur["value"]
    if cur is None:
        MISSING.add(path)
    return cur


def op(fn, *args):
    """Применяет fn, если все аргументы известны; иначе None."""
    if any(a is None for a in args):
        return None
    return fn(*args)


def add(*xs):
    return op(lambda *v: sum(v), *xs)


def mul(*xs):
    def _m(*v):
        r = 1.0
        for x in v:
            r *= x
        return r
    return op(_m, *xs)


def div(a, b):
    return op(lambda x, y: None if y == 0 else x / y, a, b)


def sub(a, b):
    return op(lambda x, y: x - y, a, b)


def tier_rate(revenue: float, tiers: list[dict]) -> float | None:
    """Ставка процентного фонда кухни по дневной выручке: [min, max)."""
    for t in tiers:
        lo, hi = t["min"], t.get("max")
        if revenue >= lo and (hi is None or revenue < hi):
            return t["rate"]
    return None  # ниже нижнего порога ставки нет -> фонд 0 по коду, но это отдельный факт


def percent_pool(daily_revenue: list[float] | None, tiers: list[dict] | None):
    """Сумма теоретических дневных фондов P_d = R_d * r(R_d). Без floor долей."""
    if daily_revenue is None or tiers is None:
        return None
    total = 0.0
    for r in daily_revenue:
        rate = tier_rate(r, tiers)
        total += r * rate if rate is not None else 0.0
    return total


# ---------- Технология: труд, энергия, рис на месяц ----------

def technology_block(inp: dict, state: str, rolls_month, orders_month):
    """state = 'before' | 'after'. Возвращает показатели технологии на месяц."""
    p = f"tech.{state}"
    rice_g = get(inp, "volume.rice_cooked_g_per_roll_weighted")
    batch_yield_kg = get(inp, f"{p}.batch_cooked_yield_kg")
    useful_share = get(inp, f"{p}.rice_useful_share")
    cooked_needed_kg = div(mul(rolls_month, rice_g), 1000)
    cooked_produced_kg = div(cooked_needed_kg, useful_share)
    batches = div(cooked_produced_kg, batch_yield_kg)
    rice_loss_kg = sub(cooked_produced_kg, cooked_needed_kg)

    # Активные человеко-минуты (не длительность и не ожидание)
    m_batch = get(inp, f"{p}.active_min_per_batch")
    m_roll = add(
        get(inp, f"{p}.active_min_per_roll.mat"),
        get(inp, f"{p}.active_min_per_roll.fill_and_roll"),
        get(inp, f"{p}.active_min_per_roll.cut"),
        mul(get(inp, f"{p}.rework_share"), get(inp, f"{p}.rework_active_min")),
    )
    m_order = get(inp, f"{p}.active_min_per_order_pack")
    days = get(inp, "volume.shift_days_month")
    m_clean_day = get(inp, f"{p}.active_min_cleaning_per_day")  # мойка устройств (МО)
    active_hours = div(
        add(mul(batches, m_batch), mul(rolls_month, m_roll), mul(orders_month, m_order),
            mul(days, m_clean_day)), 60
    )

    # Энергия: циклы партий + резак/робот на ролл + прогрев/простой всех устройств
    # за день; паспорт не подставлять
    kwh = add(
        mul(batches, get(inp, f"{p}.kwh_per_batch")),
        mul(rolls_month, get(inp, f"{p}.kwh_per_roll_devices")),
        mul(days, get(inp, f"{p}.kwh_idle_and_warmup_per_day")),
    )
    tariff = get(inp, "prices.electricity_rub_per_kwh")
    energy_rub = mul(kwh, tariff)

    rice_cost_rub_per_kg_cooked = get(inp, "prices.cooked_rice_rub_per_kg")
    rice_loss_rub = mul(rice_loss_kg, rice_cost_rub_per_kg_cooked)
    rework_food_rub = mul(
        rolls_month, get(inp, f"{p}.rework_share"), get(inp, f"{p}.rework_food_share_of_roll"),
        get(inp, "prices.roll_material_rub_weighted"),
    )
    return {
        "batches_month": batches,
        "cooked_rice_produced_kg": cooked_produced_kg,
        "rice_loss_kg": rice_loss_kg,
        "active_min_per_roll": m_roll,
        "active_hours_month": active_hours,
        "kwh_month": kwh,
        "kwh_per_roll": div(kwh, rolls_month),
        "energy_rub_month": energy_rub,
        "energy_rub_per_roll": div(energy_rub, rolls_month),
        "rice_loss_rub_month": rice_loss_rub,
        "rework_food_rub_month": rework_food_rub,
    }


def peak_capacity(inp: dict, state: str):
    """Пропускная способность пика, целых роллов в час: минимум по этапам.

    Для каждого этапа: людей(или машин) на этапе * 60 / минут на ролл.
    Этапы идут параллельно разными людьми — берём минимум, а не сумму.
    """
    stages = get(inp, f"tech.{state}.peak_stages")
    if stages is None:
        return None, None
    best, name = None, None
    for s in stages:
        cap = div(mul(s.get("units"), 60), s.get("min_per_roll"))
        if cap is None:
            MISSING.add(f"tech.{state}.peak_stages[{s.get('stage')}]")
            return None, None
        if best is None or cap < best:
            best, name = cap, s.get("stage")
    return best, name


# ---------- Сценарии ----------

def equipment_month(inp: dict):
    items = get(inp, "equipment")
    if items is None:  # неизвестный состав оборудования — не ноль
        return None, None, None
    capex = depr = opex = 0.0
    for it in items:
        c = it.get("capex_with_delivery_and_start_rub")
        life = it.get("useful_life_months")
        o = add(it.get("maintenance_rub_month"), it.get("consumables_rub_month"))
        if c is None or life is None or o is None:
            MISSING.add(f"equipment[{it.get('name')}]")
            return None, None, None
        capex += c
        depr += c / life
        opex += o
    return capex, depr, opex


def run(inp: dict) -> dict:
    O = get(inp, "volume.orders_month")
    N = get(inp, "volume.whole_rolls_month")
    before = technology_block(inp, "before", N, O)
    after = technology_block(inp, "after", N, O)
    cap_b, bott_b = peak_capacity(inp, "before")
    cap_a, bott_a = peak_capacity(inp, "after")
    capex, depr, eq_opex = equipment_month(inp)

    # Сценарий 1: текущая технология — база, Δ = 0 по определению
    s1 = {"delta_profit_month": 0.0, "peak_rolls_per_hour": cap_b, "bottleneck": bott_b,
          **{f"tech_{k}": v for k, v in before.items()}}

    # Сценарий 2: то же количество заказов и те же оплаченные смены
    d_energy = sub(after["energy_rub_month"], before["energy_rub_month"])
    d_loss = sub(after["rice_loss_rub_month"], before["rice_loss_rub_month"])
    d_rework = sub(after["rework_food_rub_month"], before["rework_food_rub_month"])
    d_cost_s2 = add(d_energy, d_loss, d_rework, eq_opex, depr)  # ФОТ: 0, смены прежние
    s2_profit = op(lambda x: -x, d_cost_s2)
    s2 = {
        "delta_payroll_month": 0.0,
        "delta_energy_rub_month": d_energy,
        "delta_rice_loss_rub_month": d_loss,
        "delta_rework_food_rub_month": d_rework,
        "equipment_opex_rub_month": eq_opex,
        "depreciation_rub_month": depr,
        "delta_profit_month": s2_profit,
        "released_active_hours_month": sub(before["active_hours_month"], after["active_hours_month"]),
        "peak_rolls_per_hour": cap_a, "bottleneck": bott_a,
        "note": "Высвобожденные часы — мощность, не экономия ФОТ.",
    }

    # Сценарий 3: реально изменённый график (снятые смены задаёт владелец/управляющий)
    removed = get(inp, "scenario3.removed_shifts_month")  # список {category, hours, weekday_premium, fund_rate}
    shift_rates = get(inp, "payroll.shift_rate_12h_by_category")
    d_payroll_s3 = None
    if removed is not None and shift_rates is not None:
        total = 0.0
        for sh in removed:
            rate = shift_rates.get(sh.get("category"))
            if rate is None or sh.get("hours") is None:
                MISSING.add("scenario3.removed_shifts_month[rate/hours]")
                total = None
                break
            base = rate * min(sh["hours"], 12) / 12 + (sh.get("seniority_allowance") or 0) + (sh.get("weekday_premium") or 0)
            fund = int(base * (sh.get("fund_rate") or 0))  # floor по группе дня
            total += (base + fund) * (sh.get("count") or 1)  # count = сколько таких смен в месяце
        d_payroll_s3 = None if total is None else -total
    s3_profit = op(lambda a, b: a - b, s2_profit, d_payroll_s3)
    capacity_check = get(inp, "scenario3.peak_demand_rolls_per_hour")
    s3 = {
        "delta_payroll_month": d_payroll_s3,
        "delta_profit_month": s3_profit,
        "capacity_ok_after_removal": op(lambda c, d: c >= d, get(inp, "scenario3.peak_capacity_after_removal"), capacity_check),
        "note": "Процентный фонд дня не меняется от снятия смены: перераспределяется. Налоги на труд — отдельно.",
    }

    # Сценарий 4: рост заказов при подтверждённой мощности (от состава смен сценария 2)
    extra_orders = get(inp, "scenario4.extra_orders_month")
    contrib = get(inp, "unit.contribution_per_order_before_percent_pool")
    base_days = get(inp, "payroll.daily_revenue_base")
    tiers = get(inp, "payroll.percent_tiers")
    uplift = get(inp, "scenario4.daily_revenue_uplift")  # список той же длины, ₽ к каждому дню
    d_pool = None
    if base_days is not None and uplift is not None and tiers is not None:
        new_days = [r + u for r, u in zip(base_days, uplift)]
        d_pool = sub(percent_pool(new_days, tiers), percent_pool(base_days, tiers))
    extra_shift_cost = get(inp, "scenario4.extra_shifts_cost_month")  # 0 только при доказанной мощности
    s4_profit = op(lambda p2, q, m, dp, es: p2 + q * m - dp - es,
                   s2_profit, extra_orders, contrib, d_pool, extra_shift_cost)
    # Контроль: тот же рост на текущей технологии. Разница с ним и есть эффект
    # «предотвращённых доп. смен»; без контроля его нельзя приписывать машинам.
    extra_shift_cost_cur = get(inp, "scenario4.extra_shifts_cost_month_current_tech")
    s4_control = op(lambda q, m, dp, es: q * m - dp - es,
                    extra_orders, contrib, d_pool, extra_shift_cost_cur)
    s4 = {
        "delta_percent_pool_month": d_pool,
        "delta_profit_month": s4_profit,
        "control_current_tech_same_growth_delta_profit": s4_control,
        "equipment_effect_vs_control": sub(s4_profit, s4_control),
        "note": "Рост спроса — гипотеза. Вклад заказа без процента кухни; процент считается по дням.",
    }

    def payback(dp):
        return op(lambda c, d, a: None if (d + a) <= 0 else c / (d + a), capex, dp, depr)

    for s in (s2, s3, s4):
        s["capex_rub"] = capex
        s["cash_payback_months"] = payback(s["delta_profit_month"])

    return {
        "as_of": inp.get("as_of"),
        "base_month": inp.get("base_month"),
        "scenario_1_current": s1,
        "scenario_2_same_orders_same_shifts": s2,
        "scenario_3_changed_schedule": s3,
        "scenario_4_more_orders_confirmed_capacity": s4,
        "after_technology": after,
        "missing_inputs": sorted(MISSING),
    }


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    with open(sys.argv[1], encoding="utf-8") as f:
        inp = json.load(f)
    res = run(inp)
    if "--json" in sys.argv:
        print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
        return

    def fmt(v):
        if v is None:
            return "не определено"
        if isinstance(v, float):
            return f"{v:,.2f}".replace(",", " ")
        return str(v)

    for key in ("scenario_1_current", "scenario_2_same_orders_same_shifts",
                "scenario_3_changed_schedule", "scenario_4_more_orders_confirmed_capacity"):
        print(f"\n## {key}")
        for k, v in res[key].items():
            print(f"  {k}: {fmt(v)}")
    print(f"\nНедостающие входы ({len(res['missing_inputs'])}):")
    for m in res["missing_inputs"]:
        print("  -", m)


if __name__ == "__main__":
    main()
