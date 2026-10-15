from pathlib import Path
import json,math,hashlib,sys
S=Path(sys.argv[1]);K=Path(sys.argv[2]);j=json.loads((S/'results.json').read_text());base=json.loads((K/'snapshots/2026-10-15/baseline-reconciliation.json').read_text());r=j['roll_scope']
def close(a,b):assert abs(a-b)<.011,(a,b)
close(r['roll_food_rub']+r['other_food_rub'],962639.64)
close(r['extra_food_for_rolls32_rub'],.32*r['roll_revenue_allocated_rub']-r['roll_food_rub'])
close(r['blended_fc_with_rolls32'],(962639.64+r['extra_food_for_rolls32_rub'])/3282430.64)
assert r['blended_fc_with_rolls32']<.32
assert len([x for x in j['roll_sku_allocation'] if x['mixed_bundle']])==3
close(sum(x['roll_food_rub'] for x in j['roll_sku_allocation']),r['roll_food_rub'])
close(sum(x['roll_revenue_rub'] for x in j['roll_sku_allocation']),r['roll_revenue_allocated_rub'])
s={x['id']:x for x in j['scenarios']}
close(s['current']['at_1819']['profit_rub'],-130869.42)
for v in s.values():
 a=v['at_1819'];close(a['delta_vs_same_recipe_base_rub'],a['revenue_change_rub']+a['food_saving_rub']-a['tax_fee_delta_rub']-a['daily_pool_delta_rub'])
 close(sum(v['daily_revenue_delta']),a['revenue_change_rub'])
 assert v['thresholds_rolls32_with_110k_budget']['0']>=v['thresholds_current_recipes']['0']
close(s['remove_gifts_first15']['at_1819']['revenue_change_rub'],28377*.25)
close(sum(v['discount_rub'] for v in j['bulk_histogram'].values()),83482.25)
assert sum(v['orders'] for v in j['bulk_histogram'].values())==181
assert s['plus_bulk']['at_1819']['delta_vs_same_recipe_base_rub']<s['remove_gifts_first15']['at_1819']['delta_vs_same_recipe_base_rub']
assert len(j['exclusion_items'])==9
for p in j['pickup_before_after']:
 assert p['observed_days']==28
 close(sum(v['orders'] for v in p['channels'].values()),p['all_orders'])
for filename,sha in j['source_sha256'].items():assert hashlib.sha256(Path(filename).read_bytes()).hexdigest()==sha
assert json.loads((S/'owner-decisions.json').read_text())['production_applied'] is False
print('PASS: component revenue/food conservation, unchanged other recipes, complete expense anchor, protected first/referral handling, ladder burden, order-window coverage and source hashes.')
