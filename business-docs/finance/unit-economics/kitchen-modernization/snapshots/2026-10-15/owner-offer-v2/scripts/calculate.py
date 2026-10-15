"""Offline owner proposal. No live offer changes; no future bonus saving invented."""
from pathlib import Path
import json,collections,csv,datetime,math,sys,hashlib
K=Path(sys.argv[1]) if len(sys.argv)>1 else Path(__file__).resolve().parents[4]
P=Path(sys.argv[2]) if len(sys.argv)>2 else Path('/tmp/teplo-owner-offer-20261015');R=Path(sys.argv[3]) if len(sys.argv)>3 else Path('/Users/grigorii/Documents/Teplo/Unit Economics/2026-10-15/discount-pressure')
sys.path.insert(0,str(K/'model'));from offer_scenarios import Model
m=Model();D=[json.loads(l) for l in (R/'orders-aug-detail-private.jsonl').read_text().splitlines()];L={x['id']:x for x in json.loads((R/'orders-aug-list-private.json').read_text())};I=json.loads((R/'iiko-orders-private.json').read_text());G=json.loads((R/'gift-cost-private.json').read_text());fresh=json.loads((P/'iiko-workshop-pickup.json').read_text())
ids={x['roll_guid'] for x in csv.DictReader(open(K/'snapshots/2026-10-14/a-recipes-sales/data/rolls-physical-monthly.csv'))}|{x['set_guid'] for x in json.loads((K/'snapshots/2026-10-14/a-recipes-sales/data/set-sales-monthly.json').read_text())}
rows=fresh['aug_workshop_sku']['data'];kids=collections.defaultdict(list)
for x in rows:
 if x['SoldWithDish.Id']:kids[x['SoldWithDish.Id']].append(x)
rollr=rollc=0.;allocated=[]
for x in rows:
 if x['SoldWithDish.Id']:continue
 children=kids[x['DishId']];allc=x['ProductCostBase.ProductCost']+sum(z['ProductCostBase.ProductCost'] for z in children);rc=(x['ProductCostBase.ProductCost'] if x['DishId'] in ids else 0)+sum(z['ProductCostBase.ProductCost'] for z in children if z['DishId'] in ids)
 rr=x['DishDiscountSumInt']*(rc/allc) if children and allc else x['DishDiscountSumInt'] if x['DishId'] in ids else 0
 rollr+=rr;rollc+=rc
 if rc:allocated.append({'sku':x['DishId'],'name':x['DishName'],'roll_revenue_rub':rr,'roll_food_rub':rc,'mixed_bundle':bool(children),'all_component_food_rub':allc})
assert abs(sum(x['ProductCostBase.ProductCost'] for x in rows)-m.c[0])<.011
assert abs(sum(x['DishDiscountSumInt'] for x in rows)-m.r0)<.011
N=collections.defaultdict(list)
for x in I['orders_aug']['data']:
 if x['DishSumInt'] or x['DishDiscountSumInt']:N[str(x['OrderNum'])].append(x)
exclude_names={'Акционный набор №1','Акционный набор №2','Акционный набор №3','2 пиццы 30см','3 пиццы 30см','2 пиццы 40см','3 пиццы 40см','3 пиццы за 1499р','Легкое комбо'}
first='ce55c07c-a0eb-4465-bcac-7cc34a64c0f0';laddercost=sum(x['cost'] or 0 for x in G if x['rule_names'].startswith('Шкала подарков'))
ladderids={x['starter_order_id'] for x in G if x['rule_names'].startswith('Шкала подарков')}
def tier(p):return .2 if p>=7000 else .15 if p>=5000 else .1 if p>=3000 else 0.
rec=[];hist=collections.defaultdict(lambda:{'orders':0,'basket_rub':0.,'discount_rub':0.});ex=collections.defaultdict(lambda:{'qty':0.,'sales_before_order_discount_rub':0.,'orders':set()})
for x in D:
 paid=[t for t in x['orderItems'] if t['totalPrice']>0]
 basket=sum(t['totalPrice'] for t in paid)
 excluded=sum(t['totalPrice'] for t in paid if t['menuItem']['name'].strip() in exclude_names)
 for t in paid:
  name=t['menuItem']['name'].strip()
  if name in exclude_names:
   ex[name]['qty']+=t['quantity'];ex[name]['sales_before_order_discount_rub']+=t['totalPrice'];ex[name]['orders'].add(x['id'])
 olddisc=sum(z['sum'] for z in x['discounts'] if z['sum']>0)
 afterfirst=olddisc-sum(z['sum']*.25 for z in x['discounts'] if z['id']==first)
 # Referral entrants can use the ordinary 15% first-order offer: no invented extra saving on these three.
 bonus=x['bonuses'];allowed=min(bonus,max(0,.5*(x['totalPrice']-excluded)))
 bulk=tier(basket)*basket
 j=N[L[x['id']]['posId']];day=j[0]['OpenDate.Typed'] if len(j)==1 else datetime.datetime.fromisoformat(x['deliveryTime']).astimezone(datetime.timezone(datetime.timedelta(hours=3))).date().isoformat()
 rec.append({'day':int(day[-2:])-1,'pickup':len(j)==1 and j[0]['OrderType']=='Самовывоз Стартер','basket':basket,'reported_price':x['price'],'old_benefit':olddisc+bonus,'existing_discount_after_first15':afterfirst,'old_bonus':bonus,'allowed_bonus':allowed,'bulk':bulk,'bulk_soft':basket*(.1 if basket>=7000 else .07 if basket>=5000 else .05 if basket>=3000 else 0),'excluded':excluded,'had_ladder':x['id'] in ladderids})
 if bulk:
  h=hist[str(int(tier(basket)*100))];h['orders']+=1;h['basket_rub']+=basket;h['discount_rub']+=bulk
plans=[('current','Текущая экономика',False,False,False),('remove_gifts_first15','Без подарочной шкалы + обычный первый15%',True,False,False),('plus_bulk','Плюс большая корзина 10/15/20%; одна лучшая выгода',True,True,False),('plus_exclusions','Плюс исключение акционных наборов и пицца-комбо из списания',True,True,True),('soft_bulk','Альтернатива для сравнения: 5/7/10% вместо10/15/20%',True,'soft',True)]
fee=.06+.03+m.c[15]/m.r0
results=[]
def estimate(q,daily,save,quality=False):
 fc=(m.c[0]+(rollr*.32-rollc if quality else 0))/m.r0
 base=m.estimate(q,m.check0,fc,100000 if quality else 0,10000 if quality else 0,True)
 scale=q/m.q0;dr=sum(daily)*scale;foodsave=save*scale
 nd=[w*q*m.check0+v*scale for w,v in zip(m.daily_weights,daily)]
 dp=sum(m.day_pool(v) for v in nd)-m.pool(q*m.check0)
 change=dr*(1-fee)+foodsave-dp
 return {'profit_rub':base['profit_rub']+change,'delta_vs_same_recipe_base_rub':change,'revenue_change_rub':dr,'food_saving_rub':foodsave,'daily_pool_delta_rub':dp,'tax_fee_delta_rub':dr*fee}
def thresholds(daily,save,quality):
 vals={q:estimate(q,daily,save,quality)['profit_rub'] for q in range(1000,6001)};suffix={};running=math.inf
 for q in reversed(vals):running=min(running,vals[q]);suffix[q]=running
 return {str(t):next(q for q in range(1000,6001,10) if suffix[q]>=t) for t in [0,500000]}
for id,label,remove,large,restrict in plans:
 daily=[0.]*31;changed=0;selected=collections.Counter();grossnew=oldtot=0.
 for x in rec:
  old=x['old_benefit'];disc=x['existing_discount_after_first15'] if remove else old-x['old_bonus'];bon=x['allowed_bonus'] if restrict else x['old_bonus']
  if restrict and bon<x['old_bonus'] and x['pickup']:disc=max(disc,.1*x['basket'])
  candidates={'existing':disc,'bonus':bon,'large':x['bulk_soft'] if large=='soft' else x['bulk'] if large else 0};choice=max(candidates,key=candidates.get);new=candidates[choice]
  # Existing August cash promotions and bonuses did not stack. For current snapshot preserve exact benefit.
  if not remove:new=old
  daily[x['day']]+=old-new;grossnew+=new;oldtot+=old;changed+=abs(old-new)>.011;selected[choice]+=1
 save=laddercost if remove else 0
 r={'id':id,'label':label,'daily_revenue_delta':daily,'gift_cost_saving_rub':save,'changed_orders':changed,'best_benefit_choices':dict(selected),'old_benefit_rub':oldtot,'new_benefit_rub':grossnew,'at_1819':estimate(1819,daily,save),'at_1819_rolls32':estimate(1819,daily,save,True),'thresholds_current_recipes':thresholds(daily,save,False),'thresholds_rolls32_with_110k_budget':thresholds(daily,save,True)}
 results.append(r);print(id,round(r['at_1819']['delta_vs_same_recipe_base_rub']),round(r['at_1819']['profit_rub']),r['thresholds_current_recipes'],r['thresholds_rolls32_with_110k_budget'],flush=True)
periods=[]
for name,lo,hi in [('before','2026-02-24','2026-03-24'),('after','2026-03-24','2026-04-21'),('after2','2026-04-21','2026-05-19')]:
 d=collections.defaultdict(lambda:{'orders':0,'net_rub':0.});days=set()
 for x in fresh['daily_channels_2026']['data']:
  if lo<=x['OpenDate.Typed']<hi:d[x['OrderType']]['orders']+=x['UniqOrderId'];d[x['OrderType']]['net_rub']+=x['DishDiscountSumInt'];days.add(x['OpenDate.Typed'])
 total=sum(v['orders'] for v in d.values());pick=sum(v['orders'] for k,v in d.items() if k.startswith('Самовывоз'));st=d['Самовывоз Стартер']['orders'];std=d['Доставка Курьер Стартер']['orders']
 periods.append({'label':name,'from_inclusive':lo,'to_exclusive':hi,'observed_days':len(days),'channels':dict(d),'all_orders':total,'all_pickup_orders':pick,'pickup_share':pick/total,'starter_pickup_share':st/(st+std)})
summary={'as_of':'2026-10-15','status':'preliminary_owner_proposal_not_live_configuration','roll_scope':{'roll_revenue_allocated_rub':rollr,'roll_food_rub':rollc,'roll_fc_base':rollc/rollr,'other_food_rub':m.c[0]-rollc,'extra_food_for_rolls32_rub':rollr*.32-rollc,'blended_fc_with_rolls32':(m.c[0]+rollr*.32-rollc)/m.r0,'allocation':'Recipe-confirmed roll/roll-set IDs. Mixed parent bundle revenue split proportionally to observed component food cost; zero parent cost not zero bundle cost. No double-counting child food. Sauce/bar/pizza/other recipes unchanged. Allocation is explicit analytic assumption.'},'roll_sku_allocation':allocated,'bulk_histogram':dict(hist),'exclusion_items':[{'name':k,**{n:(len(v) if n=='orders' else v) for n,v in a.items()}} for k,a in ex.items()],'exclusion_bonus_cap_recovery_rub':sum(x['old_bonus']-x['allowed_bonus'] for x in rec),'basket_checks':{'equal_reported_subtotal':sum(abs(x['basket']-x['reported_price'])<.011 for x in rec),'all_orders':len(rec),'basis':'Sum totalPrice of positive Starter food lines before order-level discounts; excludes historical free dishes, delivery and service fee. price is sometimes line-total after POS edits, so never multiply price*quantity blindly. Parent combo represented once in saved DTO. Edited POS baskets may differ.'},'scenarios':results,'pickup_before_after':periods,'limits':['August unclosed base. Preserve all baseline costs; no final net-profit claim.','Only recipes of roll products change. Order-level promotions are modeled across Starter baskets, not solely roll lines.','Future cashback issuance 0/3/5/7 not included as immediate cash/profit saving; historical customer tier and bonus ledger required.','Best single monetary benefit used: current protected birthday/pickup/other amounts (ordinary first order reduced to15) or large-order discount or observed bonus spending with eligibility cap. If product exclusions reduce previously used bonuses on pickup, retained pickup10% is offered as fallback. Other unobserved eligible coupons/balances and full product priority engine are not reconstructed.','Referral first orders retain15 via ordinary offer; no invented1310 saving. Referrer rewards/other referral machinery not silently cancelled.','Double promotion removed as label at normalized per-unit price: same food quantity and paid amount gives no automatic12107 saving. Post-removal product prices/quantities still to design.','Product exclusions inferred from explicit observed names and pizza-line combos; exact current Starter meal IDs/categories and end-to-end mixed-basket restriction need verification before deployment.','Bonus cap replay uses historical spending, not all available balances. Deferred redemption is not permanent saving.','Monetary discount ladder can change basket size and timing; static replay does not estimate demand or customers topping up to threshold.','Pickup first observed use24Mar is not proved creation date; history API has0 rows. Disabled predecessor ID found, with no done shop3 orders returned for first/last lookup. Before/after has calendar and channel confounding, not causal proof.','Quality scenario adds100k staffing and10k equipment; existing model cost drivers and constant order mix inherited. Other workshops keep per-unit recipes, not frozen monthly sales when total orders grow.','Conditional thresholds search to6000, round10; whole kitchen capacity not validated.'], 'source_sha256':{str(f):hashlib.sha256(f.read_bytes()).hexdigest() for f in [R/'orders-aug-detail-private.jsonl',R/'gift-cost-private.json',P/'iiko-workshop-pickup.json']}}
(P/'results.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n');print('bulk',dict(hist),'excluded bonus',summary['exclusion_bonus_cap_recovery_rub'],'rollscope',summary['roll_scope'],flush=True)
