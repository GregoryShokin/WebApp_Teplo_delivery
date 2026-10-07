import json,math,pathlib,hashlib
p=pathlib.Path(__file__).resolve().parent/'data'
d=json.loads((p/'product-costs.json').read_text())
params={
 'starter_rate':{'value':.03,'status':'owner','source':'Сообщение владельца 08.10.2026; база и охват каналов требуют сверки'},
 'tax_rate':{'value':.06,'status':'app_model','source':'cost-drivers.json → business_tax_application_model; pnl/sources/taxes.py и taxes/engine.py; полный налоговый режим не подтверждён'},
 'acquiring_rate':{'value':.02,'status':'assumption','source':'Допущение аналитика, договорный тариф неизвестен'},
 'future_bonus_reserve_rate':{'value':.05,'status':'assumption','source':'Сценарный резерв будущих списаний; фактические уровни 5/7/10%, совместимость отдельно'},
 'kitchen_rate':{'value':.035,'status':'conditional_fact','source':'Дневная выручка от50000 до140000, без перехода порога'},
 'courier_order_rub':{'value':130,'status':'owner_approximation','source':'Верхняя граница названных120–130₽; зональные ставки не подтверждены'},
 'additional_order_reserve_rub':{'value':30,'status':'assumption','source':'Резерв прочих расходов заказа; упаковка уже в техкарте повторно не включается'},
 'water_rub_per_l':{'value':1,'status':'assumption','source':'Не найдено закупочной цены; сценарная стоимость воды'},
 'baseline_orders':{'value':100,'status':'normalization','source':'Условные100 аналогичных оплаченных заказов, не прогноз и не фактический объём'},
 'volume_growth':{'value':.20,'status':'assumption','source':'Иллюстрация чувствительности, не ожидаемый рост'},
 'extra_shift_cost_rub':{'value':0,'status':'conditional_assumption','source':'Только при свободной мощности и неизменном графике; мощность не подтверждена'},
}
rate=sum(params[k]['value'] for k in ('starter_rate','tax_rate','acquiring_rate','future_bonus_reserve_rate','kitchen_rate'))
labels={'price_minus100':'Цена −100 ₽','selected_fillings_plus20_same_price':'Выбранная начинка +20%, цена прежняя','replace_gift_with_discount99':'Вместо подарка: цена −99 ₽','replace_gift_with_selected_fillings_plus20_same_price':'Вместо подарка: выбранная начинка +20%','replace_gift_with_discount99_and_selected_fillings_plus20':'Вместо подарка: цена −99 ₽ и начинка +20%','retain_gift_and_selected_fillings_plus20_global_recipe_change':'Подарок остаётся; выбранная начинка +20%'}
def contribution(price,m):
 return price*(1-rate)-m['priced_material_cost_ex_water']-m['water_l']*params['water_rub_per_l']['value']-130-30
rows=[]
for i,b in enumerate(d['baskets'],1):
 base=b.get('baseline_materials',b.get('baseline_materials_including_gift'))
 baseprice=b['baseline_price_rub'];basecon=contribution(baseprice,base)
 variants=[{'id':'baseline','price_rub':baseprice,'materials':base}]+b['variants']
 for v in variants:
  m=v['materials'];price=v['price_rub'];con=contribution(price,m)
  threshold=basecon/con-1 if con>0 else None
  rows.append({'row_id':f'b{i}_{v["id"]}','basket_id':f'b{i}','basket_name':b['name'],'variant_id':v['id'],'variant_label':'База' if v['id']=='baseline' else labels[v['id']], 'price_rub':price,'materials_ex_water_rub':m['priced_material_cost_ex_water'],'water_l':m['water_l'],'recipe_packaging_rub':m['recipe_packaging_cost_rub'],'materials':m,'baseline_price_rub':baseprice,'baseline_contribution_rub':basecon,'contribution_rub':con,'contribution_delta_rub':con-basecon,'threshold_volume_change':threshold,'required_growth':max(0,threshold) if threshold is not None else None,'allowable_volume_decline':max(0,-threshold) if threshold is not None else None,'min_whole_orders':math.ceil(100*basecon/con) if con>0 else None,'delta_100_rub':100*(con-basecon),'delta_120_rub':120*con-100*basecon,'threshold_with_additional_3400_rub':(100*basecon+3400)/(100*con)-1 if con>0 else None,'gift_quantity':int(i==3 and (v['id']=='baseline' or v.get('gift_quantity',0)==1))})
ph=d['philadelphia'];phrows=[]
for name,price,m in [('База',685,ph['baseline']),('Цена549, прежний состав',549,ph['baseline']),('Цена399, прежний состав',399,ph['baseline']),('СёмгаПФ+10г, цена685',685,ph['plus10g_salmon_pf']),('СёмгаПФ+10г, цена549',549,ph['plus10g_salmon_pf'])]:
 con=price*(1-rate)-m['priced_material_cost_ex_water']-m['water_l']
 basecon=685*(1-rate)-ph['baseline']['priced_material_cost_ex_water']-ph['baseline']['water_l']
 phrows.append({'label':name,'price_rub':price,'materials_ex_water_rub':m['priced_material_cost_ex_water'],'water_l':m['water_l'],'contribution_before_order_costs_rub':con,'portion_volume_threshold':basecon/con-1,'warning':'Порции внутри уже существующих заказов: без доставки/заказной упаковки и без новых смен. Нельзя читать как прирост заказов.'})
model={'schema_version':1,'as_of':'2026-10-08','title':'Цена и состав: первые сценарии Тепла','scope':'Нормированные100 прямых оплаченных корзин; изменение условного вклада до новых смен и непокрытых расходов, не чистая прибыль','params':params,'rate_sum':rate,'rows':rows,'philadelphia_portion_rows':phrows,'selected_roll_variants':[{k:v for k,v in x.items() if k not in ('baseline','variant','delta_goods','target_pf_goods_per_kg')} for x in d['priority_roll_variants']],'source_hashes':{f:hashlib.sha256((p/f).read_bytes()).hexdigest() for f in ['product-costs.json','pricebook.json','candidates.json','flattened-recipes.json','cost-config-live.json']},'delivery_note':'Клиентская плата0 — условие выбранной зоны, где корзина достигает своего порога. По владельцу минимум1000₽, другиезонывыше. Курьерская зарплата независима от клиентской услуги. Точные выплаты по зонам неизвестны.','kitchen_daily_tiers':[{'min':0,'max_exclusive':50000,'rate':0},{'min':50000,'max_exclusive':140000,'rate':.035},{'min':140000,'max_exclusive':190000,'rate':.045},{'min':190000,'max_exclusive':550000,'rate':.055},{'min':550000,'max_exclusive':None,'rate':.065}],'shift_sensitivity_note':'3400₽ = один дополнительный будний12часовой повар2категории без стажа2400 + один дополнительный выход курьера >10часов1000; не включает прочие надбавки/налоги/обеды. Число смен не выведено из объёма.'}
(p/'model.json').write_text(json.dumps(model,ensure_ascii=False,indent=2)+'\n')
ind=json.loads((p/'independent-scenario-check.json').read_text());comp=[]
for ib,b in enumerate(ind['baskets'],1):
 for v in b['variants']:
  r=next(r for r in rows if r['row_id']==f'b{ib}_{v["id"]}')
  diff=abs(r['contribution_rub']-v['variant_contribution_rub']);assert diff<1e-9
  comp.append(diff)
print(json.dumps({'independent_max_diff':max(comp),'rows':[{k:r[k] for k in ['basket_name','variant_label','price_rub','contribution_rub','required_growth','allowable_volume_decline','delta_100_rub','delta_120_rub','threshold_with_additional_3400_rub']} for r in rows],'philadelphia':phrows},ensure_ascii=False,indent=2))
