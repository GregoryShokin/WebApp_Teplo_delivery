"""Verify dated financial evidence and scenario invariants, without network calls."""
import hashlib,json,math,sys
from pathlib import Path
S=Path(sys.argv[1]);R=Path(sys.argv[2]);a=json.loads((S/'analysis.json').read_text())
def close(x,y):
 assert math.isclose(x,y,rel_tol=0,abs_tol=.011),(x,y)
for name,sha in a['private_sources_sha256'].items():
 assert hashlib.sha256((R/name).read_bytes()).hexdigest()==sha,name
D=[json.loads(x) for x in (R/'orders-aug-detail-private.jsonl').read_text().splitlines()]
L=json.loads((R/'orders-aug-list-private.json').read_text());manifest=json.loads((R/'starter-manifest.json').read_text())
assert len({x['id'] for x in D})==len(D)==len(L)==manifest['total_before']['total']==manifest['total_after']['total']==1394
assert manifest['errors']==[]
close(sum(x['bonuses'] for x in D),a['starter_scope']['bonus_redeemed_rub'])
for id,rate,count in [('ce55c07c-a0eb-4465-bcac-7cc34a64c0f0',.2,61),('f8f244ac-69b2-44c9-bbea-5f9cd6036979',.15,3)]:
 hits=[(x,d) for x in D for d in x['discounts'] if d['id']==id]
 assert len(hits)==count
 assert all(abs(d['sum']-x['price']*rate)<=1.01 for x,d in hits)
G=json.loads((R/'gift-cost-private.json').read_text());assert sum(x['qty'] for x in G)==484
assert sum(x['qty'] for x in G if x['cost'] is not None)==478
rows={x['id']:x for x in a['scenarios']};base=rows['unchanged']['at_1819_orders']
for x in rows.values():
 v=x['at_1819_orders'];im=x['observed_month_impact']
 close(sum(im['daily_recovery_rub']),im['recovery_rub'])
 close(v['new_food_cost_rub'],base['new_food_cost_rub']-v['food_saving_rub'])
 close(v['new_revenue_rub'],base['new_revenue_rub']+v['gross_revenue_recovered_rub'])
 close(v['new_food_cost_fraction'],v['new_food_cost_rub']/v['new_revenue_rub'])
 close(v['incremental_profit_rub'],v['gross_revenue_recovered_rub']+v['food_saving_rub']-v['additional_taxes_fees_rub']-v['additional_daily_pool_rub'])
 close(v['profit_rub'],base['profit_rub']+v['incremental_profit_rub'])
 assert x['current_menu_break_even_orders']%10==0
close(rows['structural_no_bonus_cap']['observed_month_impact']['recovery_rub'],32691/2+28377/2+1309/3)
close(rows['bonus20']['observed_month_impact']['recovery_rub'],33218.6)
close(rows['ladder30']['at_1819_orders']['incremental_profit_rub'],a['gift_ladder_costed_rub']*.3)
# Monetary promo cuts must never increase food spend; bonus cap does not cancel balances.
assert rows['bonus20']['at_1819_orders']['new_food_cost_fraction']<base['new_food_cost_fraction']
assert rows['pickup5']['at_1819_orders']['new_food_cost_rub']==base['new_food_cost_rub']
assert rows['combined_ladder30']['at_1819_orders']['profit_rub']<0
settings=json.loads((S/'current-settings-safe.json').read_text());assert settings['loyalty']['withdrawPercent']==50
for path in [S/'analysis.json',S/'current-settings-safe.json',S/'starter-manifest.json']:
 def scan(v):
  if isinstance(v,dict):
   for k,w in v.items():
    assert k not in {'customer','phone','email','address','clientSecret','access_token','password'},(path,k)
    scan(w)
  elif isinstance(v,list):
   for w in v:scan(w)
 scan(json.loads(path.read_text()))
print('PASS: raw hashes, 1394/1394 coverage, historical 20%/15% rates, 478/484 gift costing, no double-counting, food conservation, profit bridge, scoped settings, no customer/secrets in aggregate files.')
