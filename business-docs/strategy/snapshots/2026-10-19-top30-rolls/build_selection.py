"""Rebuild selected roll cohort from sanitized historical aggregates and live cards.
Sources and full historical-allocation collectors are retained in the private archive.
"""
import json
from pathlib import Path
P=Path(__file__).resolve().parent
D=json.loads((P/'inputs.json').read_text());sales=D['sales'];cost={x['roll_guid']:x for x in D['costing']['rolls']};recipes=D['recipes']['recipes']
bycat={};selected=[]
for cat in ['cold','baked','fried']:
 population=[x for x in sales['rolls'] if x['category']==cat and x['q3']['paid_qty']>0]
 chosen=sorted(population,key=lambda x:(-x['q3']['paid_qty'],x['roll_guid']))[:10]
 assert len(chosen)==10
 bycat[cat]=[]
 for rank,r in enumerate(chosen,1):
  i=r['roll_guid'];c=cost[i];recipe=recipes[i];q=r['q3'];C=c['priced_food_cost_ex_water_rub']
  # All non-water ingredient prices must be known. Water remains explicitly unpriced.
  raw_sum=sum(x['quantity_per_portion']*x['unit_price_rub'] for x in c['leaves'] if not x['is_water']);assert abs(raw_sum-C)<.000001,(r['name'],raw_sum,C)
  assert not c['unknown_nonwater_prices']
  row={'rank':rank,'roll_guid':i,'name':r['name'].strip(),'category':cat,'original_catalog_group':r['group'],'unit':'порция','sales':q,'months':r['months'],'current_recipe':recipe,'current_costing':c,'current_recipe_repriced_period_cost_ex_water':C*q['all_qty'],'current_recipe_repriced_period_fc_with_zero_net':C*q['all_qty']/q['revenue'],'current_recipe_repriced_period_fc_paid_only':C*q['paid_qty']/q['revenue'],'historical_period_fc_with_zero_net':q['food']/q['revenue'],'share_roll_revenue':q['revenue']/sales['rollline_net'],'share_enterprise_revenue':q['revenue']/sales['enterprise_net'],'allocation_note':'Компонент сета: выручка и исторический food cost распределены аналитически.','current_publication_note':'Есть в каталоге iiko; публикация и цена клиентской витрины отдельно не подтверждены.'}
  assert abs(sum(m.get('paid_qty',0) for m in r['months'].values())-q['paid_qty'])<.0001
  assert abs(q.get('standalone_paid_qty',0)+q.get('in_set_paid_qty',0)-q['paid_qty'])<.0001
  bycat[cat].append(row);selected.append(row)
assert len({r['roll_guid'] for r in selected})==30
sumq=sum(r['sales']['paid_qty'] for r in selected);sumall=sum(r['sales']['all_qty'] for r in selected);sumR=sum(r['sales']['revenue'] for r in selected);oldC=sum(r['sales']['food'] for r in selected);newC=sum(r['current_recipe_repriced_period_cost_ex_water'] for r in selected)
allpaid=sum(r['q3']['paid_qty'] for r in sales['rolls']);allphysical=sum(r['q3']['all_qty'] for r in sales['rolls'])
summary={'selected_paid_portions':sumq,'selected_physical_portions_including_zero_net':sumall,'selected_zero_net_portions':sumall-sumq,'selected_paid_standalone':sum(r['sales'].get('standalone_paid_qty',0) for r in selected),'selected_paid_in_sets':sum(r['sales'].get('in_set_paid_qty',0) for r in selected),'all_paid_portions':allpaid,'all_physical_portions':allphysical,'selected_paid_coverage':sumq/allpaid,'selected_revenue':sumR,'all_roll_revenue':sales['rollline_net'],'enterprise_revenue':sales['enterprise_net'],'selected_roll_revenue_coverage':sumR/sales['rollline_net'],'selected_enterprise_revenue_coverage':sumR/sales['enterprise_net'],'historical_food':oldC,'historical_fc':oldC/sumR,'repriced_current_recipe_cost_ex_water':newC,'repriced_current_recipe_fc_with_zero_net':newC/sumR,'current_fc_is_conditional_repricing_not_observed_october_sales':True}
assert abs(sumq-17048)<.01 and abs(sumR-3909320.9782970194)<.01
out={'as_of':'2026-10-19','sales_period':'2026-07-01..2026-09-30','scope':'Черникова, Department.Code=2','ranking':'Оплаченные физические порции: отдельные продажи + компоненты платных сетов. Нулевые продажи отдельно; кусочки не суммируются как порции.','current_cost_definition':'Пересчёт текущей карты по последним проведённым закупкам на19Oct; FC = стоимостьпорции × всефизическиепорциииюль–сентябрь / распределённая net-выручкапериода. Это условныйпересчёт, не фактическийFCоктября.','historical_fc_definition':'Исходный ProductCostBase.ProductCost / netпериода с аналитическим распределениемвнутрисетов.','summary':summary,'groups':bycat,'source_controls':sales['controls'],'allocation_method':sales['revenue_method'],'allocation_sensitivity':D['sales_summary']['set_revenue_sensitivity'],'owner_classification':D['owner_classification'],'price_updates':D['mussels_price_check'],'quality_notes':['Рецептурные закладки проверены по raw/prepared API. Они не являются контрольным взвешиванием готового ролла.','Все неводные сырьевые цены подтверждены последней проведённой закупкой. Вода указана в литрах, отдельная закупочная цена неизвестна; её нельзя объявлять подтверждённым нулём. Коммунальные расходы отдельно в модели предприятия.','Действующая defaultSalePrice из каталога iiko не приравнена к подтверждённой цене витрины. У двух компонентов сетов цена0 и FCккаталогуnull.','Последние закупки отдельных ингредиентов могут быть старыми: все даты и возраст цены сохранены в карточках. Свежий запрос не делает старую закупку новой.','Популярность сетов существенно определяет отбор. Нулевые/подарочные порции не ранжируются как оплаченный спрос, но их выпуск и стоимость сохранены.','У трёх компонентов сетов без самостоятельной исторической цены использована явно отмеченная оценка; рейтинг количества от способа распределения выручки не зависит.','Порог33% не применён к каждомуSKU и не подтверждён для всейлинейки этимотбором. Составы, цены и акции вproductionнеизменялись.']}
(P/'selection.json').write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(summary,ensure_ascii=False,indent=2))
