"""Offline 35.5% whole-business FC sensitivity; no production changes."""
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[3]
OUTPUT = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(__file__).with_name('results.json')
sys.path.insert(0, str(ROOT / 'model'))
from offer_scenarios import Model

m = Model()
fc = 0.355
scope_path = 'snapshots/2026-10-15/owner-offer-v2/results.json'
scope = json.loads((ROOT / scope_path).read_text())['roll_scope']
base = m.estimate(m.q0, m.check0, m.c[0] / m.r0, normalize=True)
at_current = m.estimate(m.q0, m.check0, fc, normalize=True)
food_delta = m.r0 * fc - m.c[0]
loss_delta = (m.c[1] + m.c[2]) * food_delta / m.c[0]
assert abs(base['profit_rub'] + 130869.42) < .01
assert abs(at_current['profit_rub'] - (base['profit_rub'] - food_delta - loss_delta)) < .01
assert abs(scope['other_food_rub'] + scope['roll_food_rub'] - m.c[0]) < .01

scenarios = []
for name, staff, equipment in [('no_additional_budget', 0, 0), ('equipment_only', 0, 10000), ('peak_staff_and_equipment', 100000, 10000)]:
    targets = m.thresholds(m.check0, fc, staff, equipment)
    for target, result in targets.items():
        assert result['profit_rub'] >= float(target)
        result['growth_orders_pct'] = (result['orders'] / m.q0 - 1) * 100
        # Crew/load is only shown for the scenario that budgets extra peak workers.
        if staff:
            result['partial_load_3_formers_2_dispatchers'] = m.load(result['orders'], 3, 2)
    scenarios.append({
        'id': name, 'extra_staff_budget_rub': staff, 'extra_equipment_budget_rub': equipment,
        'current_volume': m.estimate(m.q0, m.check0, fc, staff, equipment, True),
        'targets': targets,
        'volume_grid': [m.estimate(q, m.check0, fc, staff, equipment, True) for q in [1819, 2000, 2500, 3000, 3500, 4000]],
    })

files = list(m.cfg['sources'].values()) + ['model/offer_scenarios.py', 'model/inputs-offers-2026-10-15.json', scope_path]
out = {
    'as_of': '2026-10-15', 'status': 'preliminary_sensitivity_on_unclosed_august_2026',
    'food_cost_scope': '35.5% of whole-business net revenue after August discounts; all incremental recipe cost assigned to rolls; other departments retain their original food rubles per basket.',
    'promotions_scope': 'Historical August rules and prices. Owner-offer-v2 reform is a separate scenario and is not applied here. No automatic saving from lower future bonus accruals.',
    'baseline': {'orders': m.q0, 'days': 31, 'net_revenue_rub': m.r0, 'net_check_rub': m.check0, 'normalized_profit_rub': base['profit_rub'], 'food_cost': m.c[0] / m.r0},
    'target_food_cost': fc, 'food_per_order_rub': m.check0 * fc,
    'food_month_rub': m.r0 * fc, 'food_month_delta_rub': food_delta,
    'assumed_extra_writeoffs_inventory_rub': loss_delta,
    'implied_roll_food_cost': (m.r0 * fc - scope['other_food_rub']) / scope['roll_revenue_allocated_rub'],
    'source_sha256': {f: hashlib.sha256((ROOT / f).read_bytes()).hexdigest() for f in files},
    'limits': [
        'Same average basket, channel mix, daily revenue shape, and physical rolls per order. No claimed demand or retention increase.',
        'All 40 baseline rows retained. 3% Starter only on its incremental channel revenue; acquiring and business tax separate. Daily payroll tiers apply to entire day revenue.',
        'Losses scale with product cost, all courier costs with volume, 50% of utilities and consumables with volume; provisional drivers, not measurements.',
        '100000 monthly extra staff and 10000 equipment are conditional budgets, not an approved staffing schedule or equipment quote. Without extra staff the financial threshold is not evidence of kitchen feasibility.',
        'Fixed 110000 extra budget is sensitivity only; actual staffing may require further steps at higher volume. No payroll savings credited.',
        'Thresholds rounded up to 50 and checked against subsequent payroll tier drops through 8000 orders; not a certified operating range.',
        '35.5% excludes separately booked inventory losses/writeoffs. Rice/nori/prototype ingredient quantities are not designed by this aggregate budget.',
        'Kitchen capacity is unverified. Partial manual resources only; larger prototype rolls may change timings and equipment fit.',
        'Unclosed base: additional IP contribution with tax deductions and Yandex settlement completeness remain unreconciled. Scenario holds historical tax assumptions; future regime applicability is not certified.',
    ],
    'scenarios': scenarios,
    'validation': {'baseline_reconciled': True, 'current_volume_delta_independently_reconciled': True, 'department_food_reconciled': True},
}
OUTPUT.write_text(json.dumps(out, ensure_ascii=False, indent=2) + '\n')
print(json.dumps({'output': str(OUTPUT), 'normalized_current_profit': at_current['profit_rub'], 'targets': {s['id']: {k: v['orders'] for k, v in s['targets'].items()} for s in scenarios}}, ensure_ascii=False))
