"""Offline August replay: simplified owner offers, unchanged loyalty, FC35.5.

No requests to Starter/iiko and no customer-level output. Source archive is private.
"""
import collections
import datetime as dt
import hashlib
import json
import math
import sys
from pathlib import Path

ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[3]
OUTPUT = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(__file__).with_name('results.json')
RAW = Path(sys.argv[3]) if len(sys.argv) > 3 else Path('/Users/grigorii/Documents/Teplo/Unit Economics/2026-10-15/discount-pressure')
sys.path.insert(0, str(ROOT / 'model'))
from offer_scenarios import Model

m = Model()
D = [json.loads(line) for line in (RAW / 'orders-aug-detail-private.jsonl').read_text().splitlines()]
L = {x['id']: x for x in json.loads((RAW / 'orders-aug-list-private.json').read_text())}
I = json.loads((RAW / 'iiko-orders-private.json').read_text())
G = json.loads((RAW / 'gift-cost-private.json').read_text())
assert len(D) == 1394 and len({x['id'] for x in D}) == len(D)
by_receipt = collections.defaultdict(list)
for row in I['orders_aug']['data']:
    if row['DishSumInt'] or row['DishDiscountSumInt']:
        by_receipt[str(row['OrderNum'])].append(row)

FIRST = 'ce55c07c-a0eb-4465-bcac-7cc34a64c0f0'
PICKUP = '9631fb04-b855-4116-ab5a-700b9a4a6853'
BIRTHDAY = '56732553-832f-45d1-b716-07fcafe83eaa'
REFERRAL = 'f8f244ac-69b2-44c9-bbea-5f9cd6036979'
EXCLUDED = {'Акционный набор №1', 'Акционный набор №2', 'Акционный набор №3',
            '2 пиццы 30см', '3 пиццы 30см', '2 пиццы 40см', '3 пиццы 40см',
            '3 пиццы за 1499р', 'Легкое комбо'}
agg = collections.Counter()
counts = collections.Counter()
daily = [0.] * 31
pickup_daily = [0.] * 31
old_total = new_total = 0.
for x in D:
    discounts = [z for z in x['discounts'] if z['sum'] > 0]
    old_disc = sum(z['sum'] for z in discounts)
    bonus = x['bonuses']
    assert not (old_disc > 0 and bonus > 0), 'Replay must handle stacking explicitly'
    excluded = sum(t['totalPrice'] for t in x['orderItems']
                   if t['totalPrice'] > 0 and t['menuItem']['name'].strip() in EXCLUDED)
    allowed_bonus = min(bonus, max(0., .5 * (x['totalPrice'] - excluded)))
    first_save = sum(.25 * z['sum'] for z in discounts if z['id'] == FIRST)
    pickup_save = sum(z['sum'] for z in discounts if z['id'] == PICKUP)
    # Referral entrants can still use the ordinary first15 offer; preserve this saving.
    retained = old_disc - first_save - pickup_save
    benefit_before = old_disc + bonus
    benefit_after = max(retained, allowed_bonus)
    dr = benefit_before - benefit_after
    assert dr >= -.01
    assert abs(dr - first_save - pickup_save - bonus + allowed_bonus) < .01
    matches = by_receipt[str(L[x['id']]['posId'])]
    if len(matches) == 1:
        date = matches[0]['OpenDate.Typed']
        counts['unique_receipt_date'] += 1
    else:
        date = dt.datetime.fromisoformat(x['deliveryTime']).astimezone(dt.timezone(dt.timedelta(hours=3))).date().isoformat()
        counts['delivery_date_fallback'] += 1
    assert date.startswith('2026-08-'), date
    index = int(date[-2:]) - 1
    daily[index] += dr
    pickup_daily[index] += pickup_save
    old_total += benefit_before
    new_total += benefit_after
    agg['first15_revenue_recovery_rub'] += first_save
    agg['pickup_removal_revenue_recovery_rub'] += pickup_save
    agg['product_redemption_cap_recovery_rub'] += bonus - allowed_bonus
    agg['old_bonus_spent_rub'] += bonus
    agg['new_bonus_spent_rub'] += allowed_bonus
    counts['changed_orders'] += abs(dr) > .011
    counts['restricted_bonus_orders'] += bonus - allowed_bonus > .011
    counts['pickup_discount_orders'] += pickup_save > 0
    counts['ordinary_first_orders'] += first_save > 0
    for z in discounts:
        if z['id'] == BIRTHDAY:
            agg['birthday_preserved_rub'] += z['sum']
        elif z['id'] == REFERRAL:
            agg['referral_first_replaced_with_ordinary15_rub'] += z['sum']
        elif z['id'] not in {FIRST, PICKUP}:
            agg['other_non_targeted_manual_discount_rub'] += z['sum']

gifts = [g for g in G if g['rule_names'].startswith('Шкала подарков')]
gift = sum(g['cost'] for g in gifts if g['cost'] is not None)
gift_by_item = collections.Counter()
for g in gifts:
    if g['cost'] is not None:
        gift_by_item[g['item_name'].strip()] += g['cost']
dr0 = sum(daily)
assert abs(old_total - new_total - dr0) < .01
assert abs(agg['first15_revenue_recovery_rub'] - 7094.25) < .01
assert abs(agg['pickup_removal_revenue_recovery_rub'] - 32691) < .01
assert abs(agg['birthday_preserved_rub'] - 10600) < .01
assert abs(agg['referral_first_replaced_with_ordinary15_rub'] - 1309) < .01
assert abs(gift - 45671.514833333335) < .01
fee = m.cfg['assumptions']['USN_rate'] + m.cfg['assumptions']['starter_rate'] + m.c[15] / m.r0
food_before = .355 * m.r0

def estimate(q, mode, staff, equipment, pickup_substitution=0.):
    """Substitution is hypothetical bonus use, not an observed customer forecast."""
    scale = q / m.q0
    changes = [0.] * 31 if mode == 'old_offers' else [d - pickup_substitution * p for d, p in zip(daily, pickup_daily)]
    delta0 = sum(changes)
    if mode == 'old_offers':
        food0 = food_before
    elif mode == 'fixed_recipe_improvement_budget':
        food0 = food_before - gift
    elif mode == 'final_fc_355':
        food0 = .355 * (m.r0 + delta0)
    else:
        raise ValueError(mode)
    base = m.estimate(q, m.check0, food0 / m.r0, staff, equipment, True)
    dr = delta0 * scale
    dp = sum(m.day_pool(w * q * m.check0 + d * scale) for w, d in zip(m.daily_weights, changes)) - m.pool(q * m.check0)
    revenue = base['revenue_rub'] + dr
    costs = base['costs_rub'] + dr * fee + dp
    profit = base['profit_rub'] + dr * (1 - fee) - dp
    assert abs(revenue - costs - profit) < .00001
    return {'orders': q, 'orders_per_day': q / 31, 'revenue_rub': revenue,
            'costs_rub': costs, 'profit_rub': profit, 'food_rub': food0 * scale,
            'food_cost': food0 * scale / revenue, 'net_check_rub': revenue / q,
            'promotion_revenue_delta_rub': dr, 'promotion_tax_fee_delta_rub': dr * fee,
            'promotion_daily_payroll_pool_delta_rub': dp}

def thresholds(mode, staff, equipment, pickup_substitution=0.):
    values = {q: estimate(q, mode, staff, equipment, pickup_substitution)['profit_rub'] for q in range(m.q0, 8001)}
    suffix = {}
    running = math.inf
    for q in reversed(values):
        running = min(running, values[q])
        suffix[q] = running
    targets = {}
    for target in [0, 500000]:
        q = next(q for q in range(math.ceil(m.q0 / 50) * 50, 8001, 50) if suffix[q] >= target)
        targets[str(target)] = estimate(q, mode, staff, equipment, pickup_substitution)
        assert targets[str(target)]['profit_rub'] >= target
    return targets

scenarios = []
for mode in ['old_offers', 'fixed_recipe_improvement_budget', 'final_fc_355']:
    for staff, equipment in [(0, 0), (100000, 10000)]:
        scenarios.append({'mode': mode, 'extra_staff_budget_rub': staff,
                          'extra_equipment_budget_rub': equipment,
                          'current_volume': estimate(m.q0, mode, staff, equipment),
                          'targets': thresholds(mode, staff, equipment)})
sensitivity = [{'pickup_discount_replaced_with_bonus_share': share,
                'targets': thresholds('final_fc_355', 100000, 10000, share)} for share in [.5, 1.]]
base = estimate(m.q0, 'old_offers', 100000, 10000)
fixed = estimate(m.q0, 'fixed_recipe_improvement_budget', 100000, 10000)
exact = estimate(m.q0, 'final_fc_355', 100000, 10000)
loss_saving = (m.c[1] + m.c[2]) * gift / m.c[0]
gain = gift + dr0 * (1 - fee) - fixed['promotion_daily_payroll_pool_delta_rub'] + loss_saving
assert abs(fixed['profit_rub'] - base['profit_rub'] - gain) < .01
assert abs(exact['food_cost'] - .355) < 1e-10
old_saved = json.loads((ROOT / 'snapshots/2026-10-15/food-cost-35-5/results.json').read_text())
for s in scenarios:
    if s['mode'] == 'old_offers':
        old = next(o for o in old_saved['scenarios'] if o['extra_staff_budget_rub'] == s['extra_staff_budget_rub'] and o['extra_equipment_budget_rub'] == s['extra_equipment_budget_rub'])
        assert {k: v['orders'] for k, v in old['targets'].items()} == {k: v['orders'] for k, v in s['targets'].items()}

out = {
    'as_of': '2026-10-16', 'status': 'preliminary_owner_simplified_policy_not_live_configuration',
    'baseline': {'month': '2026-08', 'closed': False, 'orders': m.q0, 'days': 31,
                 'starter_orders_replayed': len(D), 'revenue_rub': m.r0, 'net_check_rub': m.check0},
    'policy': {'gift_ladder': False, 'large_basket_discount_ladder': False, 'pickup10': False,
               'referral_new_benefits': False, 'ordinary_first_discount': .15,
               'birthday': 'preserved per prior explicit owner decision',
               'loyalty_accrual': 'unchanged original 5/7/10%, thresholds 0/10000/30000 RUB',
               'redemption_cap': .5, 'redemption_exclusions': sorted(EXCLUDED),
               'excluded_product_accrual': 'unchanged; only redemption exclusion requested',
               'earned_balances': 'preserved', 'discount_and_redemption': 'one benefit, no stacking',
               'doubling': 'earlier removal decision preserved; normalize unit price, no automatic food saving',
               'other_manual_discount': '200 RUB historical non-targeted amount retained; purpose not established'},
    'replay': {'counts': dict(counts), 'money': dict(agg), 'old_benefit_rub': old_total,
               'new_benefit_rub': new_total, 'daily_revenue_delta_rub': daily,
               'net_revenue_delta_rub': dr0, 'net_check_after_rub': (m.r0 + dr0) / m.q0,
               'gift_rows': len(gifts), 'gift_rows_costed': sum(g['cost'] is not None for g in gifts),
               'gift_food_saving_rub': gift, 'gift_food_by_item_rub': dict(gift_by_item)},
    'bridge_fixed_recipe_budget': {'gift_food_saved_rub': gift, 'revenue_change_rub': dr0,
        'tax_fee_cost_increase_rub': dr0 * fee,
        'daily_payroll_pool_cost_increase_rub': fixed['promotion_daily_payroll_pool_delta_rub'],
        'assumed_writeoffs_inventory_saving_rub': loss_saving,
        'profit_gain_rub': gain, 'food_cost_after_changes': fixed['food_cost'],
        'extra_food_reinvestment_to_return_to_355_rub': exact['food_rub'] - fixed['food_rub'],
        'profit_gain_final_355_rub': exact['profit_rub'] - base['profit_rub']},
    'scenarios': scenarios, 'pickup_bonus_substitution_sensitivity': sensitivity,
    'future_referral_saving': {'credited_rub': 0, 'estimated_rub': None,
        'why': 'Referral rewards stop in policy, but historical issued/spent/expired ledger is absent. Point issuance is not immediate cash cost. Referred newcomers still qualify for ordinary first15.'},
    'limits': [
        'Fixed historical basket/order/channel mix, observed bonus spending capped by eligible items. No demand/retention forecast or full available customer balances.',
        'Pickup removal may cause bonus substitution or delivery-channel shift. Sensitivity varies the former, no courier effect from latter quantified.',
        'Redemption can move to other goods or later orders. The cap recovery is a first-period conditional cash effect, not permanent profit.',
        'Meal-name membership used for historical replay; exact current IDs and mixed-basket enforcement with iikoCloud still require validation before launch.',
        'Gift saving covers367 of373 rows. Unknown six are unquantified, not declared free. Removal includes pizza Piknik; other workshop recipes unchanged.',
        'FinalFC355 holds food/revenue at35.5 after offer changes. Keeping same ruble recipe improvement budget is a different scenario; additional investment belongs only to rolls.',
        'All40 baseline expense lines retained. USN6, Starter3 only own-channel revenue, historical effective acquiring, daily payroll tiers, volume-linked courier budget. Source rates are dated inputs, not future tax/legal advice.',
        '100k peak staff +10k equipment/energy/depreciation is the prior conditional allowance, not a quote or staffing/capacity proof. No savings in payroll assumed.',
        'Unclosed August baseline: IP extra contribution/deduction and Yandex settlement completeness unresolved. Thresholds are conditional, rounded up to50 with payroll jumps checked through8000 orders.',
        'Two application modules differ from old skill audit manifest; this calculation uses dated cost register and unchanged scenario model, not a new live P&L export.',
    ],
    'validation': {'pnl_identity': True, 'old_thresholds_reproduced': True,
                   'discount_bonus_no_stacking_checked': True, 'revenue_bridge_reconciled': True,
                   'gift_food_not_double_counted': True, 'daily_payroll_and_channel_fees_recomputed': True},
    'source_sha256': {f: hashlib.sha256((ROOT / f).read_bytes()).hexdigest() for f in
        list(m.cfg['sources'].values()) + ['model/offer_scenarios.py', 'model/inputs-offers-2026-10-15.json',
        'snapshots/2026-10-15/food-cost-35-5/results.json']},
    'private_source_sha256': {str(RAW / f): hashlib.sha256((RAW / f).read_bytes()).hexdigest() for f in
        ['orders-aug-detail-private.jsonl', 'orders-aug-list-private.json', 'iiko-orders-private.json', 'gift-cost-private.json']},
}
OUTPUT.write_text(json.dumps(out, ensure_ascii=False, indent=2) + '\n')
print(json.dumps({'replay': out['replay'], 'bridge': out['bridge_fixed_recipe_budget'],
    'thresholds': [{'mode': s['mode'], 'extra_budget': s['extra_staff_budget_rub'] + s['extra_equipment_budget_rub'],
                    'current_profit': s['current_volume']['profit_rub'],
                    'targets': {k: v['orders'] for k, v in s['targets'].items()}} for s in scenarios],
    'sensitivity': [{'substitution': s['pickup_discount_replaced_with_bonus_share'],
                     'targets': {k: v['orders'] for k, v in s['targets'].items()}} for s in sensitivity]}, ensure_ascii=False))
