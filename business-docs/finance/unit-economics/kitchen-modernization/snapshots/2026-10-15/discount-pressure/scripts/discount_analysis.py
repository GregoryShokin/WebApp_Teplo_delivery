"""Offline August discount attribution and conditional first-month counterfactuals.
Raw order data stays private; output is aggregate and contains no order/customer IDs.
"""
import collections,datetime,hashlib,json,math,sys
from pathlib import Path
RAW=Path(sys.argv[1] if len(sys.argv)>1 else '/tmp/teplo-discount-audit')
MODEL=Path(sys.argv[2]) if len(sys.argv)>2 else Path(__file__).resolve().parents[4]/'model'
sys.path.insert(0,str(MODEL))
from offer_scenarios import Model
m=Model()
D=[json.loads(l) for l in (RAW/'orders-aug-detail-private.jsonl').read_text().splitlines()]
L={x['id']:x for x in json.loads((RAW/'orders-aug-list-private.json').read_text())}
I=json.loads((RAW/'iiko-orders-private.json').read_text())
G=json.loads((RAW/'gift-cost-private.json').read_text())
N=collections.defaultdict(list)
for x in I['orders_aug']['data']:
 if x['DishSumInt'] or x['DishDiscountSumInt']:N[str(x['OrderNum'])].append(x)
J={x['id']:N[L[x['id']]['posId']][0] for x in D if len(N[L[x['id']]['posId']])==1}
PICKUP='9631fb04-b855-4116-ab5a-700b9a4a6853'
FIRST={'ce55c07c-a0eb-4465-bcac-7cc34a64c0f0','f8f244ac-69b2-44c9-bbea-5f9cd6036979'}
ladder_names={'Шкала подарков от 1999','Шкала подарков от 2399','Шкала подарков от 2699','Шкала подарков от 2999'}
byid=collections.defaultdict(lambda: {'names':set(),'types':set(),'orders':set(),'sum':0.})
for x in D:
 for z in x['discounts']:
  a=byid[z['id']];a['names'].add(z['name']);a['types'].add(z['type']);a['orders'].add(x['id']);a['sum']+=z['sum']
rank=[{'id':k,'names':sorted(v['names']),'types':sorted(v['types']),'orders':len(v['orders']),'sum':round(v['sum'],2)} for k,v in byid.items()]
rank.sort(key=lambda x:x['sum'],reverse=True)
gifts=collections.defaultdict(lambda:{'orders':set(),'qty':0,'costed_qty':0,'ingredient_cost_rub':0.})
for x in G:
 a=gifts[x['rule_names']];a['orders'].add(x['starter_order_id']);a['qty']+=x['qty']
 if x['cost'] is not None:a['costed_qty']+=x['qty'];a['ingredient_cost_rub']+=x['cost']
gift_rank=[{'rule':k,**{n:(len(v) if n=='orders' else round(v,2) if isinstance(v,float) else v) for n,v in a.items()}} for k,a in gifts.items()]
ladder_cost=sum(x['cost'] or 0 for x in G if x['rule_names'] in ladder_names)

def impact(plan):
 daily=[0.]*31;affected=set();revenue=0.;saving=0.
 for x in D:
  gain=0.
  if 'bonus_cap' in plan and x.get('bonuses'):
   # Legacy totalPrice before bonus validated against iiko for matched orders; no guessed sumToPay.
   gain+=max(0,x['bonuses']-round(plan['bonus_cap']*x['totalPrice'],2))
  for z in x['discounts']:
   if z['id']==PICKUP:gain+=z['sum']*plan.get('pickup_recovery_share',0)
   if z['id'] in FIRST:gain+=z['sum']*plan.get('first_recovery_shares',{}).get(z['id'],0)
  if gain:
   affected.add(x['id']);revenue+=gain
   day=J[x['id']]['OpenDate.Typed'] if x['id'] in J else datetime.datetime.fromisoformat(x['deliveryTime']).astimezone(datetime.timezone(datetime.timedelta(hours=3))).date().isoformat()
   assert day.startswith('2026-08-');daily[int(day[-2:])-1]+=gain
 saving=ladder_cost*plan.get('ladder_food_saving_share',0)
 return {'recovery_rub':revenue,'food_saving_rub':saving,'daily_recovery_rub':daily,'affected_cash_orders':len(affected)}

def calc(q,im,quality=False):
 staff,gear=(100000,10000) if quality else (0,0)
 fc=.32 if quality else m.c[0]/m.r0
 base=m.estimate(q,m.check0,fc,staff,gear,True)
 scale=q/m.q0;gain=im['recovery_rub']*scale;save=im['food_saving_rub']*scale
 newdays=[w*q*m.check0+g*scale for w,g in zip(m.daily_weights,im['daily_recovery_rub'])]
 extra_pool=sum(m.day_pool(x) for x in newdays)-m.pool(q*m.check0)
 extra_fees=gain*(.06+.03+m.c[15]/m.r0)
 delta=gain+save-extra_pool-extra_fees
 return {'profit_rub':base['profit_rub']+delta,'incremental_profit_rub':delta,'new_revenue_rub':q*m.check0+gain,'gross_revenue_recovered_rub':gain,'food_saving_rub':save,'new_food_cost_rub':q*m.check0*fc-save,'new_food_cost_fraction':(q*m.check0*fc-save)/(q*m.check0+gain),'additional_taxes_fees_rub':extra_fees,'additional_daily_pool_rub':extra_pool}

def threshold(im,target,quality=False,start=1000,stop=6000,step=10):
 # Precompute suffix minima to account for discontinuous daily payroll tiers.
 values={q:calc(q,im,quality)['profit_rub'] for q in range(start,stop+1)}
 suffix={};running=math.inf
 for q in reversed(values):running=min(running,values[q]);suffix[q]=running
 return next(q for q in range(math.ceil(start/step)*step,stop+1,step) if suffix[q]>=target)
first_to_10={'ce55c07c-a0eb-4465-bcac-7cc34a64c0f0':.5,'f8f244ac-69b2-44c9-bbea-5f9cd6036979':1/3}
plans=[
 {'id':'unchanged','label':'Без изменений'},
 {'id':'pickup5','label':'Самовывоз 10% -> 5%','pickup_recovery_share':.5},
 {'id':'bonus20','label':'Лимит бонусной оплаты 50% -> 20%','bonus_cap':.2},
 {'id':'bonus20_pickup5','label':'Лимит бонусов 20% + самовывоз 5%','bonus_cap':.2,'pickup_recovery_share':.5},
 {'id':'bonus20_pickup5_first10','label':'Плюс первый заказ 20% -> 10%; рефералы 15% -> 10%','bonus_cap':.2,'pickup_recovery_share':.5,'first_recovery_shares':first_to_10},
 {'id':'combined_ladder30','label':'Плюс себестоимость подарочной шкалы -30%','bonus_cap':.2,'pickup_recovery_share':.5,'first_recovery_shares':first_to_10,'ladder_food_saving_share':.3},
 {'id':'ladder30','label':'Себестоимость подарочной шкалы -30%','ladder_food_saving_share':.3},
 {'id':'ladder30_first10','label':'Себестоимость шкалы -30% + первый заказ/рефералы 10%','first_recovery_shares':first_to_10,'ladder_food_saving_share':.3},
 {'id':'structural_no_bonus_cap','label':'Без изменения бонусов: самовывоз 5%, первый заказ 10%, себестоимость шкалы -30%','pickup_recovery_share':.5,'first_recovery_shares':first_to_10,'ladder_food_saving_share':.3},
 {'id':'bonus0_upper_bound','label':'Без бонусной оплаты: только верхняя граница, не готовое решение','bonus_cap':0},
]
results=[];none=impact(plans[0]);baseprofit=calc(m.q0,none)['profit_rub']
for p in plans:
 im=impact(p);current=calc(m.q0,im);keep=threshold(im,baseprofit,stop=4000,step=1)
 row={'id':p['id'],'label':p['label'],'assumptions':{k:v for k,v in p.items() if k not in ['id','label']},'observed_month_impact':im,'at_1819_orders':current,'current_menu_break_even_orders':threshold(im,0),'quality32_budget_break_even_orders':threshold(im,0,True),'all_business_order_loss_to_erase_gain_fraction':1-keep/m.q0}
 results.append(row)
 print(p['id'],'delta',round(current['incremental_profit_rub']),'profit',round(current['profit_rub']),'BE',row['current_menu_break_even_orders'],'qualityBE',row['quality32_budget_break_even_orders'],'loss',round(row['all_business_order_loss_to_erase_gain_fraction']*100,1),flush=True)
byevents=collections.defaultdict(list)
for r in I['order_discounts_aug']['data']:byevents[str(r['OrderNum'])].append(r)
bonus=[x for x in D if x.get('bonuses')]
bonus_exact=sum(abs(sum(r['DiscountSum'] for r in byevents[L[x['id']]['posId']] if r.get('ItemSaleEventDiscountType')=='Скидка iikoCard')-x['bonuses'])<.011 for x in bonus)
summary={'as_of':'2026-10-15','status':'preliminary_discount_counterfactual_not_live_configuration','period':'2026-08','starter_scope':{'done_orders':len(D),'shop_ids':[3],'complete':len(D)==1394,'bonus_redemption_orders':len(bonus),'bonus_redeemed_rub':sum(x['bonuses'] for x in bonus),'positive_named_discount_rub':sum(z['sum'] for x in D for z in x['discounts'] if z['sum']>0)},'join_checks':{'unique_positive_iiko_order_number_match':len(J),'cash_net_matches':sum(abs(x['totalPrice']-x['bonuses']-J[x['id']]['DishDiscountSumInt'])<.011 for x in D if x['id'] in J),'bonus_raw_iiko_match':bonus_exact,'bonus_total':len(bonus),'caution':'Starter and POS differ for edited orders; no forced full reconciliation. Item code is not iiko SKU. Order number/date and financial fields checked; zero-value duplicate checks not treated as extra clients.'},'gift_ladder_costed_rub':ladder_cost,'cash_discount_ranking':rank,'gift_ingredient_ranking':gift_rank,'scenarios':results,'method':{'baseline':'All 40 August expenses; original known profit -116175.67. Forecast removes -8573 payroll penalty and -6120.75 other-income benefits: normalized -130869.42 at current volumes. Current-menu cases do not include new equipment/staff; quality32 cases preserve +100000 peak staffing and +10000 equipment from preliminary model.','financial':'Recipe RUB/order remains fixed when cash discount is reduced. Recoveries are only Starter, so incremental tax6% + Starter3% + historical acquiring mix1.404%; Yandex and partner fees not applied to these recoveries. Shared daily payroll pool recalculated on dated recoveries. Gift changes subtract verified ingredient cost only; no unverified labor/packaging savings.','pickup_channel':'Cutting pickup discount can shift purchases to delivery and add courier cost. Fixed-channel replay does not estimate this substitution. Do not treat pickup discount as pure waste.', 'first_order_rates':'Observed August regular promo 20% (61 orders/28377 RUB); referral 15% (3/1309). Current rule cards confirm both. Recovery to 10% is half regular discount plus one third referral discount, not one common fraction.', 'response':'No order loss assumed in the headline. Loss sensitivity scales all-business mix uniformly, not a causal churn forecast. Fixed prices, basket mix, channel shares and existing volume-driver assumptions remain.','bonus_limit':'Historical first-month replay at unchanged balances/orders. Lower cap postpones redemption, does not cancel balances or prove durable saving. Existing obligations, employee exceptions, bonus replenishment and customer retention need separate design. No promise of recurring monthly benefit.','gifts':'Iiko cost of matching SKU in the same order divided by quantity times free units. Paid/free units share the within-order cost. Alias Пепперони/Пеперони explicitly inspected; unmatched items excluded, not assumed zero. Gift cost is not menu value.','new_recipes':'30% cheaper gift ladder is a target budget, not an approved recipe.','boundaries':'Existing tax/Yandex baseline reconciliation gaps remain. No production prices, settings, coupons or balances changed.'},'private_sources_sha256':{n:hashlib.sha256((RAW/n).read_bytes()).hexdigest() for n in ['starter-manifest.json','orders-aug-list-private.json','orders-aug-detail-private.jsonl','iiko-raw.json','iiko-orders-private.json','gift-cost-private.json']}}
summary['model_commit']='b9b4fcb5'
summary['model_sources_sha256']={name:hashlib.sha256((MODEL.parent/path).read_bytes()).hexdigest() for name,path in m.cfg['sources'].items()}
summary['model_sources_sha256']['calculator']=hashlib.sha256((MODEL/'offer_scenarios.py').read_bytes()).hexdigest()
summary['model_sources_sha256']['offer_inputs']=hashlib.sha256((MODEL/'inputs-offers-2026-10-15.json').read_bytes()).hexdigest()
day_gaps=collections.Counter()
for x in D:
 if x['id'] not in J:continue
 day=datetime.datetime.fromisoformat(x['deliveryTime']).astimezone(datetime.timezone(datetime.timedelta(hours=3))).date()
 day_gaps[str((day-datetime.date.fromisoformat(J[x['id']]['OpenDate.Typed'])).days)]+=1
summary['join_checks']['delivery_local_date_minus_pos_date_days']=dict(day_gaps)
summary['method']['date_exceptions']='Two one-day exceptions retained after review: one exact cash match; the other has matching original basket plus additional shawarma and delivery in POS. Shared unique monthly receipt number and SKU identity corroborate the link; mismatching totals remain marked, not force-reconciled.'
unreconciled_bonus=[x for x in bonus if x['id'] not in J or abs(x['totalPrice']-x['bonuses']-J[x['id']]['DishDiscountSumInt'])>=.011]
unreconciled_cap20=sum(max(0,x['bonuses']-round(.2*x['totalPrice'],2)) for x in unreconciled_bonus)
all_cap20=impact({'bonus_cap':.2})['recovery_rub']
summary['join_checks'].update({'bonus_cash_total_unreconciled_orders':len(unreconciled_bonus),'bonus20_recovery_on_unreconciled_cash_orders_rub':round(unreconciled_cap20,2),'bonus20_recovery_on_cash_reconciled_orders_rub':round(all_cap20-unreconciled_cap20,2)})
summary['method'].update({'threshold_rounding':'Scan each integer volume through 6000, check later payroll jumps, round up to 10 orders. Prior offer table rounds up to 50, so 2360 and published 2400 are the same baseline at different presentation precision.','bonus_uncertainty':'Cap replay includes edited/unreconciled cash totals; see join_checks for amount on reconciled orders and the provisional remainder. Do not claim cent-level accuracy or a permanent benefit.'})
(RAW/'analysis.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
