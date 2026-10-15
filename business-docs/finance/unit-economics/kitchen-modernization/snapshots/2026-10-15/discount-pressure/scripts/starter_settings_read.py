from starter_read import *
OUT.mkdir(parents=True,exist_ok=True)
base,op=connect()
keys=['clientType','selfRefill','selfWithdraw','withdrawPercent','refillPercent','canUseWithPromocode','canBeUsedWithDiscounts','refillFromClean','calculateWithoutDelivery','allowUseBonusesOnWeb','levels']
d=get(base,op,'/api/v1/frontend/loyalty_program')
a={'as_of':datetime.datetime.now(datetime.timezone.utc).isoformat(),'not_historical_rule_snapshot':True,'loyalty':{k:d.get(k) for k in keys},'rules':[]}
for route,id in [('discounts','9631fb04-b855-4116-ab5a-700b9a4a6853'),('discounts','f8f244ac-69b2-44c9-bbea-5f9cd6036979'),('promocodes','ce55c07c-a0eb-4465-bcac-7cc34a64c0f0'),('promocodes','c1a150ec-a665-4d5d-a7ca-11216c79620f')]:
 d=get(base,op,'/api/v1/frontend/promotions/'+route+'/'+id)
 a['rules'].append({k:d[k] for k in ['id','name','status','type','value','minOrderPriceCommon','settings','isReferral'] if k in d})
(OUT/'current-settings-safe.json').write_text(json.dumps(a,ensure_ascii=False,indent=2))
print('Saved allowlisted current rules; no customer promo codes or loyalty credentials.')
