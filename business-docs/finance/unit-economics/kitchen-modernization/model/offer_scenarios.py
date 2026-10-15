"""Replaceable offer economics, anchored to the complete working August register.

Offline analysis only. It does not alter prices, promotions, payroll or production.
Run: python3 model/offer_scenarios.py > <snapshot>/results.json
"""
import hashlib
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(rel):
    return json.loads((ROOT / rel).read_text())


class Model:
    def __init__(self):
        self.cfg = read('model/inputs-offers-2026-10-15.json')
        self.register = read(self.cfg['sources']['costs'])
        self.c = [float(row['aug_2026']) for row in self.register]
        self.base = read(self.cfg['sources']['reconciliation'])
        self.orders = read(self.cfg['sources']['orders'])['2026-08']
        self.input_base = read(self.cfg['sources']['base_inputs'])
        self.timing = read(self.cfg['sources']['timing'])
        self.q0 = self.orders['orders_uniq']
        self.r0 = float(self.base['net_revenue_rub'])
        self.p0 = float(self.base['known_profit_rub'])
        self.check0 = self.r0 / self.q0
        self.gross_check = sum(x['DishSumInt'] for x in self.orders['units_by_cooking_place']) / self.q0
        self.daily = self.input_base['payroll']['daily_revenue_base']['value']
        self.tiers = self.input_base['payroll']['percent_tiers']['value']
        self.daily_weights = [x / sum(self.daily) for x in self.daily]
        assert abs(sum(self.c) - float(self.base['known_costs_rub'])) < .01
        assert abs(self.r0 - sum(self.c) - self.p0) < .01

    def day_pool(self, revenue):
        return next((revenue * t['rate'] for t in reversed(self.tiers) if revenue >= t['min']), 0.)

    def pool(self, revenue):
        return sum(self.day_pool(revenue * w) for w in self.daily_weights)

    def offer(self, s):
        if s['kind'] == 'same_prices':
            check, fc = self.check0, s['food_cost']
        elif s['kind'] == 'reprice':
            check = self.gross_check * s['list_price_multiplier'] * (1 - s['aggregate_markdown'])
            fc = s['food_cost']
        elif s['kind'] == 'new_line_mix':
            share = s['new_line_order_share']
            check = (1 - share) * self.check0 + share * s['new_line_check']
            food_per_order = (1 - share) * self.c[0] / self.q0 + share * s['new_line_check'] * s['new_line_food_cost']
            fc = food_per_order / check
        else:
            raise ValueError(s['kind'])
        return check, fc

    def estimate(self, q, check, fc, staff=0, equipment=0, normalize=False):
        r, food = q * check, q * check * fc
        ratio_q, ratio_r = q / self.q0, r / self.r0
        a = self.cfg['assumptions']
        channels = self.orders['channels_net']
        starter_share = sum(float(v) for k, v in channels.items() if k.startswith('Starter')) / self.r0
        yandex_share = float(channels['Яндекс Еда']) / self.r0
        delta = {
            'food': food - self.c[0],
            'writeoffs_and_inventory': (self.c[1] + self.c[2]) * (food / self.c[0] - 1),
            'additional_packaging': self.c[3] * (ratio_q - 1),
            'consumables': a['consumables_variable_share'] * self.c[4] * (ratio_q - 1),
            'couriers_including_shift_guarantees': self.c[12] * (ratio_q - 1),
            'existing_electricity_and_water': a['utilities_variable_share'] * (self.c[24] + self.c[25]) * (ratio_q - 1),
            'acquiring': self.c[15] * (ratio_r - 1),
            'starter': a['starter_rate'] * starter_share * (r - self.r0),
            'yandex': a['yandex_marginal_rate'] * yandex_share * (r - self.r0),
            'partner_and_refunds': (self.c[18] + self.c[19]) * (ratio_r - 1),
            'business_tax': a['USN_rate'] * (r - self.r0),
            'shared_daily_production_pool': self.pool(r) - self.pool(self.r0),
            'additional_peak_staff_all_in_budget': staff,
            'new_equipment_all_in_monthly_budget': equipment,
            'remove_prior_penalty_and_other_income_gains': -self.c[7] - self.c[33] if normalize else 0.,
        }
        profit = self.p0 + r - self.r0 - sum(delta.values())
        return {'orders': q, 'orders_per_day': q / 31, 'revenue_rub': r,
                'costs_rub': sum(self.c) + sum(delta.values()), 'profit_rub': profit,
                'profit_delta_vs_working_base_rub': profit - self.p0,
                'cost_deltas_rub': delta}

    def load(self, q, forming, dispatch, roll_multiplier=1):
        p, k = self.timing['parameters'], self.timing['peak_assumptions']
        working = self.timing['profiles']['working']
        mat = p['before_mat_active_seconds']['value'] * (1 - p['mat_applicable_share']['value']) + working['mat_active_seconds']['value'] * p['mat_applicable_share']['value']
        cut = p['before_cut_active_seconds']['value'] * (1 - p['cut_applicable_share']['value']) + working['cut_active_seconds_per_roll']['value'] * p['cut_applicable_share']['value']
        former_sec = mat + k['forming_other_person_seconds_per_roll']['value']
        dispatch_sec = cut + k['dispatch_other_person_seconds_per_roll']['value']
        avail = k['available_fraction']['value']
        cap = min(forming * 3600 * avail / former_sec, dispatch * 3600 * avail / dispatch_sec)
        hours = read(self.cfg['sources']['hour_profile'])
        hour_share = max(x['share_rolls'] for x in hours['by_hour'])
        rolls_per_order = self.input_base['volume']['whole_rolls_month']['value'] / self.q0
        avg_hour = q * rolls_per_order * roll_multiplier * hour_share / 31
        stress = avg_hour * self.cfg['assumptions']['busy_day_stress_multiplier']
        return {'mean_busiest_arrival_hour_rolls': avg_hour, 'stress_hour_rolls': stress,
                'partial_manual_capacity_rolls_hour': cap, 'stress_to_partial_capacity': stress / cap,
                'whole_kitchen_verified': False}

    def thresholds(self, check, fc, staff, equipment):
        # Exhaustive search: the daily whole-revenue pool creates downward profit jumps.
        max_q = self.cfg['assumptions']['search_max_orders']
        profits = [self.estimate(q, check, fc, staff, equipment, True)['profit_rub']
                   for q in range(self.q0, max_q + 1)]
        suffix_min, running = {}, math.inf
        for q in range(max_q, self.q0 - 1, -1):
            running = min(running, profits[q - self.q0])
            suffix_min[q] = running
        targets = {}
        for target in [0, 500000]:
            q = next(q for q in range(math.ceil(self.q0 / 50) * 50, max_q + 1, 50)
                     if suffix_min[q] >= target)
            targets[str(target)] = self.estimate(q, check, fc, staff, equipment, True)
        return targets

    def results(self):
        out = {'status': 'conditional_scenario_on_unclosed_august_2026_base',
               'base_orders': self.q0, 'base_check': self.check0,
               'base_food_cost': self.c[0] / self.r0, 'base_profit_rub': self.p0,
               'gross_check': self.gross_check, 'aggregate_gross_to_net_markdown': 1 - self.check0 / self.gross_check,
               'source_sha256': {k: hashlib.sha256((ROOT/v).read_bytes()).hexdigest() for k,v in self.cfg['sources'].items()},
               'scenarios': []}
        for s in self.cfg['scenarios']:
            check, fc = self.offer(s)
            staff = s['additional_peak_staff_all_in_budget']
            equipment = self.cfg['assumptions']['equipment_all_in_monthly_budget']
            targets = self.thresholds(check, fc, staff, equipment)
            for result in targets.values():
                result['load'] = self.load(result['orders'], *s['peak_manual_crew'])
                result['load_with_20pct_more_rolls_per_order'] = self.load(result['orders'], *s['peak_manual_crew'], roll_multiplier=1.2)
            out['scenarios'].append({'id': s['id'], 'label': s['label'], 'paid_check_rub': check,
                'basket_food_cost': fc, 'food_budget_per_order_rub': check * fc,
                'additional_staff_budget_rub': staff, 'peak_manual_crew': s['peak_manual_crew'],
                'unchanged_order_count': self.estimate(self.q0, check, fc, staff, equipment, True),
                'targets': targets,
                'no_extra_staff_budget_counterfactual_not_capacity_proof': self.thresholds(check, fc, 0, equipment)})
        return out


if __name__ == '__main__':
    print(json.dumps(Model().results(), ensure_ascii=False, indent=2))
