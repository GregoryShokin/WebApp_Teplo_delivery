#!/usr/bin/env python3
"""Replaceable preliminary timing parameters; no production/API writes.

Run: python3 preliminary_scenario.py inputs-preliminary-2026-10-15.json [--json]
This is an assumption layer, not a new measured payroll or capacity norm.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path


def number(cell, name, *, minimum=None, maximum=None, allow_none=False):
    value = cell.get("value") if isinstance(cell, dict) else cell
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name}: требуется конечное число; неизвестное не заменяется нулём")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name}: значение меньше {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name}: значение больше {maximum}")
    return value


def manual_peak_limits(config, before_mat, before_cut, after_mat, after_cut,
                       mat_share, cut_share, dispatch_other_override=None):
    """One shared resource per role, not an independent worker for each operation.

    Conditional manual-station limits only; thermal devices, machines, rice supply,
    administration and delivery can impose a lower actual capacity.
    """
    peak = config["peak_assumptions"]
    formers = number(peak["forming_people"], "forming_people", minimum=0.001)
    dispatch = number(peak["dispatch_people"], "dispatch_people", minimum=0.001)
    availability = number(peak["available_fraction"], "available_fraction", minimum=0.001, maximum=1)
    forming_other = number(peak["forming_other_person_seconds_per_roll"], "forming_other", minimum=0.001)
    dispatch_other = number(peak["dispatch_other_person_seconds_per_roll"] if dispatch_other_override is None else dispatch_other_override,
                            "dispatch_other", minimum=0.001)

    def state(mat, cut):
        loads = {"forming": mat + forming_other, "dispatch": cut + dispatch_other}
        capacities = {"forming": formers * 3600 * availability / loads["forming"],
                      "dispatch": dispatch * 3600 * availability / loads["dispatch"]}
        limit = min(capacities, key=capacities.get)
        return {"load_person_seconds_per_roll": loads,
                "resource_limits_rolls_hour": capacities,
                "partial_manual_limit_rolls_hour": capacities[limit],
                "limiting_manual_resource": limit}

    before = state(before_mat, before_cut)
    after = state(before_mat * (1 - mat_share) + after_mat * mat_share,
                  before_cut * (1 - cut_share) + after_cut * cut_share)
    return {"status": "assumed_two_manual_resources_not_complete_kitchen_capacity",
            "dispatch_other_seconds": dispatch_other, "before": before, "after": after,
            "relative_partial_capacity_change": after["partial_manual_limit_rolls_hour"] / before["partial_manual_limit_rolls_hour"] - 1}


def calculate(config, base, reconciliation):
    if base["base_month"] != reconciliation["period"]:
        raise ValueError("Период выручки/объёма не совпадает с периодом прибыли")
    volume = base["volume"]
    rolls = number(volume["whole_rolls_month"], "whole_rolls_month", minimum=0)
    orders = number(volume["orders_month"], "orders_month", minimum=0)
    days = number(volume["shift_days_month"], "shift_days_month", minimum=0)
    rice_g = number(volume["rice_cooked_g_per_roll_weighted"], "rice_g", minimum=0)
    batch_kg = number(base["tech"]["before"]["batch_cooked_yield_kg"], "batch_kg", minimum=0.001)
    batches_theoretical = rolls * rice_g / 1000 / batch_kg
    baseline_profit = float(reconciliation["known_profit_rub"])
    if not math.isfinite(baseline_profit):
        raise ValueError("Неопределённая базовая прибыль")
    params = config["parameters"]
    before_mat = number(params["before_mat_active_seconds"], "before_mat", minimum=0)
    before_cut = number(params["before_cut_active_seconds"], "before_cut", minimum=0)
    mat_share = number(params["mat_applicable_share"], "mat_share", minimum=0, maximum=1)
    cut_share = number(params["cut_applicable_share"], "cut_share", minimum=0, maximum=1)
    clean_minutes = number(params["extra_service_person_minutes_per_day"], "extra_service", minimum=0)
    mixer_saved = number(params["mixer_person_seconds_saved_per_batch"], "mixer_saved")
    profiles = []
    for key, profile in config["profiles"].items():
        after_mat = number(profile["mat_active_seconds"], f"{key}.mat", minimum=0)
        after_cut = number(profile["cut_active_seconds_per_roll"], f"{key}.cut", minimum=0)
        forming_hours = rolls * mat_share * (before_mat - after_mat) / 3600
        dispatch_hours = rolls * cut_share * (before_cut - after_cut) / 3600
        mixer_hours = batches_theoretical * mixer_saved / 3600
        service_hours = days * clean_minutes / 60
        profiles.append({
            "id": key, "label": profile["label"],
            "status": "assumption_not_measured_machine_performance",
            "mat_active_seconds_after": after_mat,
            "cut_active_seconds_per_roll_after": after_cut,
            "forming_person_hours_released": forming_hours,
            "dispatch_person_hours_released": dispatch_hours,
            "mixer_person_hours_credited": mixer_hours,
            "extra_service_person_hours": service_hours,
            "net_person_hours_released": forming_hours + dispatch_hours + mixer_hours - service_hours,
            "payroll_savings_rub_same_schedule": 0,
            "whole_roll_elapsed_seconds_after": None,
            "kitchen_peak_capacity_rolls_hour": None,
            "manual_peak_scenario": manual_peak_limits(config, before_mat, before_cut, after_mat, after_cut, mat_share, cut_share),
            "dispatch_workload_sensitivity": [manual_peak_limits(config, before_mat, before_cut, after_mat, after_cut, mat_share, cut_share, other)
                                              for other in config["peak_assumptions"]["dispatch_other_sensitivity_seconds"]],
        })

    target = number(config["financial"]["target_net_profit_rub_month"], "target", minimum=0)
    costs = config["financial"]["equipment_cost_inputs"]
    missing = []
    values = {}
    for key in ("incremental_kwh_month", "maintenance_rub_month", "consumables_rub_month", "capex_landed_rub", "useful_life_months"):
        # Energy delta may be negative when new equipment replaces old consumption.
        minimum = None if key == "incremental_kwh_month" else (0.001 if key == "useful_life_months" else 0)
        values[key] = number(costs[key], key, minimum=minimum, allow_none=True)
        if values[key] is None:
            missing.append(key)
    tariff = number(params["electricity_rub_per_kwh"], "electricity_tariff", minimum=0)
    equipment_cost = None
    if not missing:
        equipment_cost = (values["incremental_kwh_month"] * tariff
                          + values["maintenance_rub_month"] + values["consumables_rub_month"]
                          + values["capex_landed_rub"] / values["useful_life_months"])
    sensitivities = []
    for cell in config["financial"]["all_in_monthly_cost_budgets_rub"]:
        budget = number(cell, "cost_budget", minimum=0)
        profit = baseline_profit - budget
        sensitivities.append({
            "all_in_incremental_equipment_cost_rub_month": budget,
            "status": "budget_sensitivity_not_vendor_quote_or_forecast",
            "profit_same_orders_same_schedule_rub": profit,
            "additional_operating_gain_to_cover_equipment_rub": budget,
            "gap_to_target_rub": max(0, target - profit),
        })
    volume_tradeoffs = []
    for factor in config["financial"]["contribution_per_order_remaining_factors"]:
        factor = number(factor, "contribution_remaining_factor", minimum=0.001, maximum=1)
        volume_tradeoffs.append({
            "contribution_rub_per_order_remaining_factor": factor,
            "order_volume_multiplier_to_preserve_total_contribution": 1 / factor,
            "order_growth_fraction": 1 / factor - 1,
            "required_orders_month_rounded_up": math.ceil(orders / factor),
            "status": "same_fixed_costs_same_basket_not_a_demand_forecast_or_target_profit_plan",
        })
    return {
        "as_of": config["as_of"], "base_month": base["base_month"],
        "mode": "preliminary_parameters_owner_authorized",
        "primary_objective": config["primary_objective"],
        "baseline_status": reconciliation["status"],
        "baseline_known_profit_rub": baseline_profit,
        "whole_rolls_month": rolls, "orders_month": orders,
        "theoretical_rice_batches_month_not_measured": batches_theoretical,
        "observed_full_cycle_seconds": config["observations"]["full_cycle_seconds"],
        "profiles": profiles,
        "financial_handoff": {
            "payroll_delta_same_schedule_rub": 0,
            "sales_growth_assumed_orders": 0,
            "quality_or_food_loss_savings_credited_rub": 0,
            "equipment_cost_rub_month_from_components": equipment_cost,
            "profit_same_orders_from_components_rub": None if equipment_cost is None else baseline_profit - equipment_cost,
            "missing_equipment_cost_inputs": missing,
            "budget_sensitivities": sensitivities,
            "contribution_volume_tradeoffs": volume_tradeoffs,
            "target_profit_condition": "N_new * contribution_new - N_base * contribution_base >= target_profit - baseline_profit + incremental_fixed_costs",
        },
        "limitations": [
            f"{before_mat:g}/{before_cut:g} секунд до автоматизации — допущения об активном труде по двум видео, не производственная норма.",
            "До: только ручная нарезка; после: загрузка+рез+выгрузка на ролл. Подготовка ножа не вычтена — границы предстоит уточнить.",
            "Время нарезки после включает долю загрузки/выгрузки и самого реза на фактический ролл партии.",
            "Доля подходящих роллов и обслуживание машин — заменяемые допущения.",
            "Полный цикл, пик и возможность снять смены не выводятся из суммы человеко-секунд.",
            "Отдельный пик рассчитан только для общих ручных ресурсов при заданном ассортименте и допущениях; тепловая техника, сами машины, рис, администратор и доставка пока не покрыты.",
            "Месячный рост заказов не равен росту спроса в пик: требуется распределение по времени и состав заказа.",
            "Ретеншен, новый спрос от цены и финансовый эффект стабильного качества пока не измерены и не добавляются автоматически.",
            "0 экономии ФОТ/роста спроса/потерь — условия этого сценария, не доказанное отсутствие будущего эффекта.",
            "Бюджет оборудования включает энергию, сервис, расходники и амортизацию; не прибавлять их повторно.",
            "Бюджетная сетка независима от скоростей; неизвестные комплектующие расходов не заменены нулями.",
            "Месячная прибыль унаследовала незакрытый статус августовской базы; прочие цеха/расходы уже в ней.",
        ],
    }


def load_run(input_path):
    input_path = Path(input_path).resolve()
    config = json.loads(input_path.read_text())
    source_paths = [input_path]
    loaded = []
    for key in ("baseline_inputs", "baseline_reconciliation"):
        path = (input_path.parent / config["sources"][key]).resolve()
        source_paths.append(path)
        loaded.append(json.loads(path.read_text()))
    result = calculate(config, *loaded)
    result["source_manifest"] = [
        {"file": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        for path in source_paths
    ]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = load_run(args.inputs)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return
    print("Приоритет: мощность в пик и стабильность качества для роста заказов при меньшем вкладе с заказа.")
    print("Условные пределы двух ручных участков. Это НЕ подтверждённая мощность всей кухни.")
    print("| Вариант | До, роллов/ч | После, роллов/ч | Изменение | Ограничивающий ручной участок |")
    print("|---|---:|---:|---:|---|")
    for item in result["profiles"]:
        peak = item["manual_peak_scenario"]
        print(f"| {item['label']} | {peak['before']['partial_manual_limit_rolls_hour']:.1f} | {peak['after']['partial_manual_limit_rolls_hour']:.1f} | {peak['relative_partial_capacity_change']:.1%} | {peak['after']['limiting_manual_resource']} |")
    print("\nСнижение рублёвого вклада с заказа и необходимый объём при прежних постоянных расходах:")
    for row in result["financial_handoff"]["contribution_volume_tradeoffs"]:
        print(f"Вклад -{1-row['contribution_rub_per_order_remaining_factor']:.0%} -> заказов +{row['order_growth_fraction']:.1%}")
    print("Это сохранение прежнего суммарного вклада, не достижение целевой прибыли. Расходы оборудования отдельно.")
    print("\nДополнительный показатель: высвобождение человеко-часов, не автоматическая экономия зарплаты.")
    print("| Вариант | Мат: чел.-с | Нарезка: чел.-с/ролл | Высвобождено чел.-ч/мес с обслуживанием | Экономия ФОТ при прежнем графике |")
    print("|---|---:|---:|---:|---:|")
    for item in result["profiles"]:
        print(f"| {item['label']} | {item['mat_active_seconds_after']:.0f} | {item['cut_active_seconds_per_roll_after']:.0f} | {item['net_person_hours_released']:.1f} | 0 ₽ |")
    print("\nБюджет оборудования — отдельная чувствительность, не оценка стоимости выбранных машин.")
    for row in result["financial_handoff"]["budget_sensitivities"]:
        print(f"Расход {row['all_in_incremental_equipment_cost_rub_month']:.0f} ₽/мес -> условная прибыль {row['profit_same_orders_same_schedule_rub']:.2f} ₽")


if __name__ == "__main__":
    main()
