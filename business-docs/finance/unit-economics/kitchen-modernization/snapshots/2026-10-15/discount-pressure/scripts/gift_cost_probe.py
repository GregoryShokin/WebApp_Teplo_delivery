import json,collections,re,sys
from pathlib import Path
RAW=Path(sys.argv[1])
D=[json.loads(l) for l in open(RAW/'orders-aug-detail-private.jsonl')]
L={x['id']:x for x in json.load(open(RAW/'orders-aug-list-private.json'))}
I=json.load(open(RAW/'iiko-orders-private.json'))
num=collections.defaultdict(list);skus=collections.defaultdict(list)
for x in I['orders_aug']['data']:
 if x['DishSumInt'] or x['DishDiscountSumInt']:num[str(x['OrderNum'])].append(x)
for x in I['order_sku_aug']['data']:skus[x['UniqOrderId.Id']].append(x)
def norm(s):return re.sub(r'[^a-zа-я0-9]','',s.lower().replace('ё','е').replace('пепперони','пеперони'))
agg=collections.defaultdict(lambda:{'orders':set(),'qty':0,'matched_qty':0,'cost':0.,'retail':0.,'unknown':collections.Counter()})
matchcnt=0;exacts=0;missing=[];rows=[]
for s in D:
 cand=num[L[s['id']]['posId']];i=cand[0] if len(cand)==1 else None
 if i:matchcnt+=1;exacts+=abs(s['totalPrice']-s['bonuses']-i['DishDiscountSumInt'])<.011
 else:missing.append(s['id'])
 gifts=[x for x in s['discounts'] if x['type']!='increase' and not x.get('sum')]
 key=' + '.join(sorted(x['name'] for x in gifts)) or '(no rule)'
 for it in s['orderItems']:
  if it.get('totalPrice') or it['quantity']<=0:continue
  a=agg[key];a['orders'].add(s['id']);a['qty']+=it['quantity'];a['retail']+=it['price']*it['quantity']
  candidates=[x for x in skus[i['UniqOrderId.Id']] if norm(x['DishName'])==norm(it['menuItem']['name']) and not x.get('SoldWithDish.Id')] if i else []
  cost=None
  if candidates and sum(x['DishAmountInt'] for x in candidates)>=it['quantity']:
   cost=sum(x['ProductCostBase.ProductCost'] for x in candidates)/sum(x['DishAmountInt'] for x in candidates)*it['quantity']
   a['cost']+=cost;a['matched_qty']+=it['quantity']
  else:a['unknown'][it['menuItem']['name']]+=it['quantity']
  rows.append({'starter_order_id':s['id'],'rule_names':key,'item_name':it['menuItem']['name'],'qty':it['quantity'],'cost':cost})
print('matched orders',matchcnt,'cash exact',exacts,'missing',len(missing))
for k,a in sorted(agg.items(),key=lambda kv:kv[1]['cost'],reverse=True):print(k,{'orders':len(a['orders']),**{x:v for x,v in a.items() if x!='orders'}})
json.dump(rows,open(RAW/'gift-cost-private.json','w'),ensure_ascii=False,indent=2)
