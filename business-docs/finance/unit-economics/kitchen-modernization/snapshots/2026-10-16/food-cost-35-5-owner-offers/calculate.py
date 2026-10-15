"""Offline promo replay plus explicit FC-denominator scenarios."""
import hashlib
import json
import math
import sys
from pathlib import Path

ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[3]
OUTPUT = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(__file__).with_name('results.json')
sys.path.insert(0, str(ROOT / 'model'))
from offer_scenarios import Model

m = Model()
source = 'snapshots/2026-10-15/owner-offer-v2/results.json'
replay = json.loads((ROOT / source).read_text())
plans = {s['id']: s for s in replay['scenarios']}
plan = plans['plus_exclusions']
daily = plan['daily_revenue_delta']
dr0 = sum(daily)
gift = plan['gift_cost_saving_rub']
food_before = .355 * m.r0
revenue_after = m.r0 + dr0
fee = m.cfg['assumptions']['USN_rate'] + m.cfg['assumptions']['starter_rate'] + m.c[15] / m.r0

def estimate(q, mode, staff, equipment):
    scale = q / m.q0
    dr = 0 if mode == 'old_offers' else dr0 * scale
    if mode == 'old_offers':
        food0 = food_before
    elif mode == 'fixed_recipe_improvement_budget':
        food0 = food_before - gift
    elif mode == 'final_fc_355':
        food0 = .355 * revenue_after
    else:
        raise ValueError(mode)
    base = m.estimate(q, m.check0, food0 / m.r0, staff, equipment, True)
    pool_delta = 0 if mode == 'old_offers' else sum(
        m.day_pool(w * q * m.check0 + d * scale)
        for w, d in zip(m.daily_weights, daily)
    ) - m.pool(q * m.check0)
    # Food already includes removal/reinvestment. Do not add gift saving a second time.
    cost_adjustment = dr * fee + pool_delta
    revenue = base['revenue_rub'] + dr
    costs = base['costs_rub'] + cost_adjustment
    profit = base['profit_rub'] + dr - cost_adjustment
    assert abs(revenue - costs - profit) < .00001
    return {'orders': q, 'orders_per_day': q / 31,
            'revenue_rub': revenue, 'costs_rub': costs, 'profit_rub': profit,
            'food_rub': food0 * scale, 'food_cost': food0 * scale / revenue,
            'net_check_rub': revenue / q, 'promotion_revenue_delta_rub': dr,
            'promotion_tax_fee_delta_rub': dr * fee,
            'promotion_daily_payroll_pool_delta_rub': pool_delta}

def thresholds(mode, staff, equipment):
    vals = {q: estimate(q, mode, staff, equipment)['profit_rub'] for q in range(m.q0, 8001)}
    suffix = {}
    running = math.inf
    for q in reversed(vals):
        running = min(running, vals[q])
        suffix[q] = running
    out = {}
    for target in [0, 500000]:
        q = next(q for q in range(1850, 8001, 50) if suffix[q] >= target)
        out[str(target)] = estimate(q, mode, staff, equipment)
        assert out[str(target)]['profit_rub'] >= target
    return out

scenarios = []
for mode in ['old_offers', 'fixed_recipe_improvement_budget', 'final_fc_355']:
    for staff, equipment in [(0, 0), (100000, 10000)]:
        scenarios.append({'mode': mode, 'extra_staff_budget_rub': staff,
                          'extra_equipment_budget_rub': equipment,
                          'current_volume': estimate(m.q0, mode, staff, equipment),
                          'targets': thresholds(mode, staff, equipment)})

current = estimate(m.q0, 'old_offers', 100000, 10000)
fixed = estimate(m.q0, 'fixed_recipe_improvement_budget', 100000, 10000)
exact = estimate(m.q0, 'final_fc_355', 100000, 10000)
loss_saving = (m.c[1] + m.c[2]) * gift / m.c[0]
expected_gain = gift + dr0 * (1 - fee) - fixed['promotion_daily_payroll_pool_delta_rub'] + loss_saving
assert abs(fixed['profit_rub'] - current['profit_rub'] - expected_gain) < .01
assert abs(exact['food_cost'] - .355) < 1e-10
assert abs(fixed['food_rub'] - (food_before - gift)) < .01
assert abs(current['profit_rub'] + 467509.2913441763) < .01
old_saved = json.loads((ROOT / 'snapshots/2026-10-15/food-cost-35-5/results.json').read_text())
for s in scenarios:
    if s['mode'] == 'old_offers':
        old = next(x for x in old_saved['scenarios'] if x['extra_staff_budget_rub'] == s['extra_staff_budget_rub'] and x['extra_equipment_budget_rub'] == s['extra_equipment_budget_rub'])
        assert {k: v['orders'] for k, v in old['targets'].items()} == {k: v['orders'] for k, v in s['targets'].items()}

first_delta = sum(plans['remove_gifts_first15']['daily_revenue_delta'])
bulk_delta = sum(plans['plus_bulk']['daily_revenue_delta']) - first_delta
restriction_delta = dr0 - sum(plans['plus_bulk']['daily_revenue_delta'])
assert abs(first_delta + bulk_delta + restriction_delta - dr0) < .01
files = list(m.cfg['sources'].values()) + ['model/offer_scenarios.py', 'model/inputs-offers-2026-10-15.json', source,
        'snapshots/2026-10-15/owner-offer-v2/owner-decisions.json',
        'snapshots/2026-10-15/food-cost-35-5/results.json']
out = {
    'as_of': '2026-10-16', 'status': 'preliminary_first_month_replay_on_unclosed_august_2026',
    'primary_interpretation': '35.5% is final food divided by revenue after the new offers. Compare separately with keeping the previous recipe-improvement ruble budget and subtracting confirmed historical gift food.',
    'baseline_orders': m.q0, 'days': 31, 'old_net_check_rub': m.check0,
    'new_net_check_rub': revenue_after / m.q0,
    'policy': json.loads((ROOT / 'snapshots/2026-10-15/owner-offer-v2/owner-decisions.json').read_text()),
    'bridge_at_baseline_volume': {
        'gift_food_saved_rub': gift, 'ordinary_first15_revenue_rub': first_delta,
        'large_basket_revenue_rub': bulk_delta, 'bonus_product_exclusions_revenue_rub': restriction_delta,
        'net_revenue_change_rub': dr0, 'tax_fee_saving_rub': -dr0 * fee,
        'daily_payroll_pool_saving_rub': -fixed['promotion_daily_payroll_pool_delta_rub'],
        'assumed_writeoffs_inventory_saving_rub': loss_saving,
        'fixed_recipe_improvement_budget_profit_gain_rub': expected_gain,
        'food_cost_after_gift_removal': fixed['food_cost'],
        'extra_food_budget_to_return_to_355_rub': exact['food_rub'] - fixed['food_rub'],
    },
    'future_bonus_accrual': {'new_rates': [0, .03, .05, .07], 'new_purchase_thresholds_rub': [0, 5000, 15000, 35000],
                             'future_redemption_saving_rub': None, 'profit_credit_rub': 0,
                             'why': 'Need dated customer tiers and accrual/redemption/expiry/balance ledger. Preserve earned balances. Zero credited is not a conclusion of zero future benefit.'},
    'scenarios': scenarios,
    'source_sha256': {f: hashlib.sha256((ROOT / f).read_bytes()).hexdigest() for f in files},
    'validation': {'pnl_identities': True, 'old_355_thresholds_reproduced': True,
                   'offer_bridge_reconciled': True, 'gift_not_double_counted': True,
                   'daily_pool_and_channel_fees_recomputed': True},
    'limits': [
        'Owner policy applied through saved August order-level replay (1394 Starter orders); preserve its incomplete counterfactual coupon eligibility and mixed-basket limitations.',
        'The large-basket 10/15/20 discounts can replace existing offers; choose one best observed monetary benefit. Birthday retained, pickup10 retained pending analysis, ordinary first15, bonus redemption cap50.',
        'Gift saving uses confirmed cost for367/373 gifts. No cost for6 missing gifts treated as zero value; their potential saving is unquantified. No assumed inflation of gift recipes before cancellation.',
        'Pizza gift Piknik is removed along with roll gifts; unchanged recipe of other workshops means unchanged unit recipe, not unchanged number of free gifts. Additional recipe-improvement budget remains assigned to rolls.',
        'Removed doubling yields no automatic material saving at normalized per-unit price. No lower future bonus accrual saving credited without ledger.',
        'Same order count/basket/channel shares as replay; no estimated demand, retention, basket top-ups, pickup shift or bonus spending redistribution.',
        'Compared with owner-offer-v2, gift removal now also decreases the assumed writeoff/inventory budget under the existing proportional-food driver. The extra5413 RUB baseline saving is an assumption, not observed quality improvement.',
        'All40 base expense lines retained; USN6, Starter3 only own channel, acquiring historical effective rate, single daily payroll pool with tier discontinuities. Courier budget scales with delivery volume; fixed110k additional budget is provisional.',
        'FinalFC355 explicitly reallocates33528 RUB into food compared with fixed improvement budget at1819 orders; do not mistake this for automatic recipe cost caused by a discount change.',
        'Unclosed baseline, IP contribution/deduction and Yandex settlement gaps; future tax eligibility and whole-kitchen capacity unverified. Targets are conditional, not guaranteed net profit.',
    ],
}
OUTPUT.write_text(json.dumps(out, ensure_ascii=False, indent=2) + '\n')
print(json.dumps({'bridge': out['bridge_at_baseline_volume'], 'thresholds': [
    {'mode': s['mode'], 'extra_budget': s['extra_staff_budget_rub'] + s['extra_equipment_budget_rub'],
     'targets': {k: v['orders'] for k, v in s['targets'].items()}} for s in scenarios]}, ensure_ascii=False))
