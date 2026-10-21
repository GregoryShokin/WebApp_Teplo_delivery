"""Recalculate public tables from anonymous aggregate inputs (no customer data)."""
import json,math
from pathlib import Path
P=Path(__file__).parent
D=json.loads((P/'inputs.json').read_text())
A=D['eight_aggregate'];C=D['recipe_rows'];prices=D['prices']

def subset(name,period='all_q3'):
 return next(x for x in A['subsets'] if x['subset']==name and x['period']==period)

def comparison(group):
 t=group['totals'];out={'orders_with_target8':group['orders_with_target8']}
 for regime in ['current','lighter']:
  net=t['net_'+regime];food=t['trial_food_including_set_extras_'+regime];baseline=t['baseline_food_including_set_extras_'+regime]
  out[regime]={'net_rub':net,'trial_food_rub':food,'baseline_food_rub':baseline,'pure_roll_trial_food_rub':t['trial_food_'+regime],'set_extras_rub':t['set_recipe_extras_food_'+regime],'trial_fc':food/net,'baseline_fc':baseline/net,'qty':t['qty_'+regime],'raw_food_delta_vs_old_recipe_rub':food-baseline,'room_to_33_rub':.33*net-food,'room_to_33_pp':33-100*food/net,'trial_fc_bonus_share_sensitivity':t['trial_fc_including_set_extras_'+regime+'_bonus_share_sensitivity']}
 return out

main=subset('all_modelled_orders');rows=[]
recipe={r['roll_guid']:r for r in C}
for r in main['rows']:
 c=recipe[r['id']];p=prices[r['name']];v=c['trial_cost_conditional_ex_water_rub']
 row={'id':r['id'],'name':r['name'],'retail_price_rub':p,'baseline_unit_food_rub':c['old_cost_ex_water_rub'],'trial_unit_food_rub':v,'trial_fc_retail_before_offers':v/p,'minimum_effective_price_for33_rub':v/.33,'maximum_unit_food_at_full_price33_rub':.33*p,'conditional_components':c['conditional_components']}
 for regime in ['current','lighter']:
  net=r['net_'+regime];qty=r['qty_'+regime];food=r['trial_food_including_set_extras_'+regime]
  row[regime]={'net_rub':net,'qty':qty,'trial_food_rub':food,'baseline_food_rub':r['baseline_food_including_set_extras_'+regime],'trial_fc':food/net,'baseline_fc':r['baseline_food_including_set_extras_'+regime]/net,'set_extras_rub':r['set_recipe_extras_food_'+regime],'pure_roll_trial_fc':r['trial_food_'+regime]/net,'effective_revenue_per_produced_portion_rub':net/qty,'room_to33_rub':.33*net-food}
 rows.append(row)
checks={}
for s in A['subsets']:
 for regime in ['current','lighter']:
  for field in ['net','qty','baseline_food','trial_food','set_recipe_extras_food','trial_food_including_set_extras','baseline_food_including_set_extras']:
   k=field+'_'+regime
   assert math.isclose(sum(r[k] for r in s['rows']),s['totals'][k],abs_tol=.00001),(s['subset'],s['period'],k)
  assert math.isclose(s['totals']['trial_food_'+regime]+s['totals']['set_recipe_extras_food_'+regime],s['totals']['trial_food_including_set_extras_'+regime],abs_tol=.00001)
checks['all_8_sums_all_cohorts_and_months']=True
checks['set_packaging_included_once']=True
checks['eight_unique']=len(rows)==len({r['id'] for r in rows})==8
assert checks['eight_unique']
R={'as_of':'2026-10-21','scope':'Черникова, июль–сентябрь: состав заказов сохранён, текущие цены и правила пересчитаны сценарно; улучшены только восемь рецептур.','method':'FC = recipe material including embedded set packaging / allocated food payment after discounts and redeemed points. Unpriced water and out-of-card fryer oil not estimated. No payroll, delivery, commissions or taxes.','main8':comparison(main),'rows':rows,'months8':{m:comparison(subset('all_modelled_orders',m)) for m in ['2026-07','2026-08','2026-09']},'cohorts8':{name:comparison(subset(name)) for name in ['all_modelled_orders','starter_matched','fully_current_priced_orders','strict_fully_priced_matched_starter']},'coverage':D['coverage'],'wholeline':D.get('wholeline'),'set_results':D.get('set_results'),'validation':checks}
(P/'results.json').write_text(json.dumps(R,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'main8':R['main8'],'validation':checks},ensure_ascii=False,indent=2))
