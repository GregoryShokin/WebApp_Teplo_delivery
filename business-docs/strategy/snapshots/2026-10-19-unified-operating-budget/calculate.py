"""Offline unified forward budget. Inputs are sanitized closed-report aggregates.
Run: python3 calculate.py [snapshot-directory]. No business-system mutations.
"""
import calendar
import datetime as dt
import json
import math
import sys
from pathlib import Path

ROOT=Path(sys.argv[1]).resolve() if len(sys.argv)>1 else Path(__file__).resolve().parent
D=json.loads((ROOT/'inputs.json').read_text());MONTHS=D['months'];N=len(MONTHS)
Q=sum(m['orders'] for m in MONTHS.values());R=sum(m['enterprise']['net'] for m in MONTHS.values());FOOD=sum(m['enterprise']['food'] for m in MONTHS.values())
DAYS=sum(m['calendar_days'] for m in MONTHS.values());Q0=Q/N;CHECK=R/Q
ALLDAYS=[d for m in MONTHS.values() for d in m['days']]
TIERS=sorted(D['rates']['revenue_tiers'],key=lambda t:t['min'])
def a(m,c):
 v=m['lines'][c]['amount'];assert v is not None,(c,'unknown must remain visible');return float(v)
def total(c):return sum(a(m,c) for m in MONTHS.values())
def mean(c):return total(c)/N

def daily_pool(r):return next((r*t['rate'] for t in reversed(TIERS) if r>=t['min']),0.)
def pool(q):return sum(daily_pool(d['revenue']*q/Q0) for d in ALLDAYS)/N

def sushi_percent(q):return sum(4*math.floor(daily_pool(d['revenue']*q/Q0)*7.5/(d['other_coeff']+30)) for d in ALLDAYS)/N

# Native future line registry: fixed rub/month + variable rub/order, pool separately.
rows=[]
def add(code,title,fixed=0.,unit=0.,basis='',group='other'):
 rows.append({'code':code,'title':title,'fixed_monthly':fixed,'per_order':unit,'basis':basis,'group':group})
rate=D['rates'];premium_days=sum(dt.date.fromisoformat(d['date']).weekday() in (4,5) for d in ALLDAYS)/N
four_fixed=(4*rate['sushi_daily_category2']+rate['senior_daily'])*DAYS/N+4*rate['frisat_daily']*premium_days+(math.floor(rate['sushi_daily_category2']*.05)*DAYS/N+math.floor(rate['frisat_daily']*.05)*premium_days)
actual_percent=sum(float(m['payroll_breakdown_sum']['percent_pay']) for m in MONTHS.values())/N
ordinary_penalties=sum(float(m['payroll_breakdown_sum']['other_penalties']) for m in MONTHS.values())/N
old_sushi=sum(m['sushi_fixed_fund'] for m in MONTHS.values())/N
payroll_codes=['cook_payroll','administrator_payroll','admin_payroll','bonuses','accumulation_fund']
other_fixed_payroll=sum(mean(c) for c in payroll_codes)+ordinary_penalties-actual_percent-old_sushi
add('four_sushi_fixed','Четверо второй категории: смены, старший, пт/сб, фонд',four_fixed,basis='4 ежедневных места ×12ч; 2400/смена; 1 старший500; 1 сотрудник5%фонда. Средний календарь92/3 дня.',group='payroll')
add('other_fixed_payroll','Остальной фиксированный ФОТ, премии, фонды, отпускной резерв',other_fixed_payroll,basis='Полный ФОТ ОПиУ + отмена экономии от прочих штрафов − старые суши база/фонд − весь исторический процент; отпуска включены.',group='payroll')
roll_unit=.33*sum(m['rolls']['net_allocated'] for m in MONTHS.values())/Q
other_food_unit=sum(m['other']['food'] for m in MONTHS.values())/Q
food_unit=roll_unit+other_food_unit
add('roll_food','Продукты роллов: FC33%',unit=roll_unit,basis='33% от распределённой net-выручки только роллов; раскрытие смешанных сетов.',group='food')
add('other_food','Продукты остальных блюд: рецептуры прежние',unit=other_food_unit,basis='Исходные сырьевые рубли прочих блюд / заказы квартала.',group='food')
add('writeoff_reserve','Резерв списаний продуктов',unit=total('writeoffs')/FOOD*food_unit,basis='Квартальные списания / квартальная рецептурная стоимость × будущая стоимость продуктов.',group='losses')
add('inventory_reserve','Резерв результатов ревизий до удержаний с сотрудников',unit=sum(m['audit_before_employee_recoveries'] for m in MONTHS.values())/FOOD*food_unit,basis='Компонент inventory до payroll-компенсаций, уже с учётом излишков. Удержания не планируем.',group='losses')
box_total=total('pizza_box_inventory')
add('packaging_discrepancy','Отклонения инвентаризации упаковки и напитков сверх техкарт',unit=(total('packaging_inventory')+total('beverage_inventory')+max(0,box_total))/Q,basis='Упаковка в техкартах уже в продуктах. Здесь только отклонения; квартальный чистый излишек коробок не планируется.',group='losses')
add('courier_service','Курьерская служба',unit=total('courier_service')/Q,basis='Расход квартала / учётные заказы; бюджетный proxy при прежних долях доставки.',group='delivery')
add('consumables_maintenance','Расходники и содержание точки',fixed=.5*(mean('aux_goods')+mean('shop_maintenance')),unit=.5*(total('aux_goods')+total('shop_maintenance'))/Q,basis='50% условно постоянных,50% на заказ; прежнее явно заданное допущение.',group='operations')
electric_water=sum(D['utilities_electricity_water'].values())
fixed_waste=mean('utilities_chernikova')-electric_water/N
add('utilities','Коммунальные расходы и вывоз мусора',fixed=.5*electric_water/N+fixed_waste+D['garbage_reclassified_from_july_rent']/N,unit=.5*electric_water/Q,basis='Электричество/вода50% переменные,50% постоянные; вывоз мусора постоянный, включая3000июля из аренды.',group='operations')
add('rent','Аренда точки и склада',fixed=100000+mean('warehouse_rent'),basis='Аренда точки100000/мес, склада10000/мес.',group='operations')
add('rent_unspecified_reserve','Резерв нерасшифрованной добавки к аренде июля',fixed=(total('rent_chernikova')-100000*N-D['garbage_reclassified_from_july_rent'])/N,basis='1000₽ июля без назначения сохранены как333.33/мес; не объявлены экономией.',group='operations')
add('acquiring','Эквайринг',unit=total('acquiring')/Q,basis='Квартальная эффективная комиссия при прежнем среднем чеке.',group='fees')
starter_total=sum(m['channels']['starter_net'] for m in MONTHS.values());yandex_total=sum(m['channels']['yandex_net'] for m in MONTHS.values())
starter_residual=mean('website_app')-D['assumed_starter_rate']*starter_total/N
add('starter','Starter: сайт и приложение',fixed=starter_residual,unit=D['assumed_starter_rate']*starter_total/Q,basis='Принятые в модели3% оборота Starter +494.05/мес эмпирического остатка счета. Это калибровка бюджета, не новый тариф.',group='fees')
add('yandex','Комиссия Яндекса вне каскада ОПиУ',unit=D['assumed_yandex_rate']*yandex_total/Q,basis='Плановые25% только доли Яндекса. Историческая меньшая комиссия не переносится как будущая скидка.',group='fees')
add('partner_refunds','Комиссии прочим партнёрам и возвраты',unit=(total('partner_commission')+total('customer_refunds'))/Q,basis='Ровно один раз: модель использует iiko net до этих вычетов.',group='fees')
add('payroll_taxes','Резерв налогов с зарплаты',fixed=D['future_payroll_tax_provision'],basis='20013.93 по июлю/августу при прежней официальной схеме и конвенции ОПиУ; сентябрьский None сохранён в источнике.',group='taxes')
tax_residual=mean('taxes')-D['assumed_business_tax_rate']*R/N
add('business_taxes','Налоги бизнеса',fixed=tax_residual,unit=D['assumed_business_tax_rate']*CHECK,basis='Прежняя модель6% выручки +4782.50/мес постоянной составляющей по источнику; без новой юридической трактовки.',group='taxes')
add('fuel_reserve','Топливо: чистая сумма августовской аналитической поправки',fixed=D['fuel_net_august']/N,basis='2640₽ квартал /3; не потеряно при объединении.',group='operations')
handled=set(payroll_codes+['food_cost_chernikova','writeoffs','audit_results','pizza_box_inventory','packaging_inventory','beverage_inventory','courier_service','aux_goods','shop_maintenance','utilities_chernikova','rent_chernikova','warehouse_rent','acquiring','website_app','partner_commission','customer_refunds','payroll_taxes','taxes','revenue_net_chernikova','other_income','unclaimed_deposits_writeoff'])
unknown=[]
for code,line in next(iter(MONTHS.values()))['lines'].items():
 if code in handled:continue
 values=[m['lines'][code]['amount'] for m in MONTHS.values()]
 if any(v is None for v in values):
  unknown.append({'code':code,'title':line['title'],'observed':values,'treatment':'В числовой итог не включено; статус не заменён подтверждённым нулём. Новое финансирование отдельно.'});continue
 amt=mean(code)
 if code=='other_expenses':amt-=D['oneoff_bank_error_july']/N
 group='marketing' if code in ['context_ads','seo','aggregator_commission','targeted_ads','outdoor_ads','yandex_maps_ads','flyers','other_marketing'] else 'operations'
 add(code,line['title'],fixed=amt,basis='Средний расход трёх закрытых месяцев. Нерегулярные потребности сохраняются резервом.' if code!='other_expenses' else 'Среднее после исключения подтверждённой единичной потери1000₽ июля.',group=group)
FIXED=sum(r['fixed_monthly'] for r in rows);UNIT=sum(r['per_order'] for r in rows)
def estimate(q,extra=0,details=False):
 rev=q*CHECK;p=pool(q);profit=rev-FIXED-UNIT*q-p-extra
 if not details:return profit
 detail=[dict(r,amount=r['fixed_monthly']+q*r['per_order']) for r in rows]
 detail.append({'code':'company_percent_pool','title':'Единый процентный фонд всей кухни','amount':p,'group':'payroll','fixed_monthly':None,'per_order':None,'basis':'Действующие ступени применены к каждому из92 масштабированных дней, затем /3. Не четыре самостоятельных процента.'})
 return {'orders':q,'orders_per_average_day':q/(DAYS/N),'revenue':rev,'fixed_costs':FIXED,'variable_without_company_pool':UNIT*q,'company_pool':p,'costs_before_unknowns':FIXED+UNIT*q+p+extra,'additional_future_cost':extra,'profit_before_unknowns':profit,'food':food_unit*q,'enterprise_recipe_fc':food_unit/CHECK,'four_sushi_payroll_fixed_fund_percent':four_fixed+sushi_percent(q),'total_payroll_fixed_plus_pool_before_payrolltax':four_fixed+other_fixed_payroll+p,'budget_lines':detail}

def thresholds(extra=0):
 profits=[0]+[estimate(q,extra) for q in range(1,8001)];suffix=[math.inf]*8002
 for q in range(8000,0,-1):suffix[q]=min(suffix[q+1],profits[q])
 out={}
 for target in [0,500000]:
  first=next(q for q in range(1,8001) if profits[q]>=target);safe=next(q for q in range(1,8001) if suffix[q]>=target);plan=math.ceil(safe/50)*50
  out[str(target)]={'first_crossing':first,'safe_crossing_through8000':safe,'planning_orders':plan,'profit_at_safe':profits[safe],'profit_at_previous_order':profits[safe-1],'profit_at_planning':profits[plan],'revenue_at_planning':plan*CHECK}
 return out

# Validate original accepted app identity without inventing zero for absent components.
source_checks={}
for month,m in MONTHS.items():
 net=m['enterprise']['net'];sumcost=sum(float(x['amount']) for x in m['lines'].values() if x['sign_role']==-1 and x['amount'] is not None);income=sum(float(x['amount']) for x in m['lines'].values() if x['sign_role']==1 and x['code']!='revenue_net_chernikova' and x['amount'] is not None)
 rawfood=a(m,'food_cost_chernikova');expected=net-sumcost+income;profit=float(m['reported_profit'])
 assert abs(expected-profit)<.011,(month,expected,profit)
 assert abs(sum(d['revenue'] for d in m['days'])-net)<.011
 assert abs(m['audit_before_employee_recoveries']-m['audit_employee_recoveries']-a(m,'audit_results'))<.011
 source_checks[month]={'reported_profit':profit,'reconstructed_profit':expected,'difference':expected-profit,'food_correction':m['enterprise']['food']-rawfood}
assert abs(four_fixed-320433.3333333333)<.01
assert abs(other_fixed_payroll-719164.59)<.02
assert abs(roll_unit*Q/.33-sum(m['rolls']['net_allocated'] for m in MONTHS.values()))<.01
T=thresholds();probes=sorted(set([2000,2200,2250,2300,2500,2950,3000,3050,3100,3200]+[v['planning_orders'] for v in T.values()]))
result={'as_of':'2026-10-19','model':'One forward operating budget; actual closed reports remain unchanged.','scope':'Черникова Department.Code=2. Four daily12h sushi positions ofcategory2; otherroles preserved; onlyrollrecipesFC33%; actualobservedoffers retained.','calendar':{'months':N,'quarter_days':DAYS,'average_month_days':DAYS/N,'average_friday_saturday_days':premium_days},'baseline':{'quarter_orders':Q,'average_orders':Q0,'quarter_revenue':R,'weighted_net_check':CHECK,'actual_quarter_roll_fc':sum(m['rolls']['food'] for m in MONTHS.values())/sum(m['rolls']['net_allocated'] for m in MONTHS.values()),'future_roll_fc':.33,'future_enterprise_recipe_fc':food_unit/CHECK},'budget_coefficients':{'fixed_monthly':FIXED,'variable_per_order_without_company_pool':UNIT,'contribution_per_order_before_company_pool':CHECK-UNIT,'formula':'profit(q)=q*netcheck−fixed_monthly−q*variable_per_order−one_daily_company_pool(q)−new_unknown_costs','four_sushi_fixed_fund':four_fixed,'other_fixed_payroll':other_fixed_payroll,'vacation_reserve_already_inside_payroll':22000,'starter_fixed_residual':starter_residual,'business_tax_fixed_residual':tax_residual},'thresholds':T,'scenarios':{str(q):estimate(q,details=True) for q in probes},'additional_monthly_cost_sensitivity':{str(extra):thresholds(extra) for extra in [20000,50000,100000]},'normalization':{'excluded_true_oneoff_cost':{'quarter_amount':1000,'monthly_effect':1000/N,'purpose':'July loss from wrongbankdetails, confirmed drill. Historical report not changed.'},'excluded_favorable_otherincome_and_deposits_monthly':mean('other_income')+mean('unclaimed_deposits_writeoff'),'excluded_ordinary_employee_penalty_savings_monthly':ordinary_penalties,'excluded_inventory_employee_recoveries_monthly_at_historical_food':sum(m['audit_employee_recoveries'] for m in MONTHS.values())/N,'pizza_box_quarter_net_surplus_not_projected_as_income':box_total,'payroll_tax_future_provision':D['future_payroll_tax_provision'],'payroll_tax_increase_vs_report_mean':D['future_payroll_tax_provision']-sum(float(m['lines']['payroll_taxes']['amount'] or 0) for m in MONTHS.values())/N,'yandex_full_rate_addition_vs_old_anchored_cost_monthly':.25*yandex_total/N-sum(D['yandex_analytical_commission'].values())/N,'payroll_full_pool_difference_vs_historical_rounding':sum(daily_pool(d['revenue']) for d in ALLDAYS)/N-actual_percent,'inventory_loss_scaling':'Quarterwriteoffs andinventorybeforeemployeerecoveries / quarterrecipefoodcost ×futurefoodbudget; commonweightedrate.'},'retained_irregular_reserves':{code:mean(code) for code in ['equipment_repair','recruiting','staff_expenses','transport','aggregator_commission','bank_fees','office_maintenance']},'unknown_future_costs':unknown+[{'code':'new_equipment','title':'Новая техника: амортизация, проценты, сервис, энергия, монтаж и обучение','treatment':'Цена покупки и оборотные вложения отдельно; месячная стоимость неизвестна, см. +20/50/100тыс.'},{'code':'extra_capacity','title':'Дополнительные смены, персонал, доставка при пиках','treatment':'Финансовый объём не доказывает производственную мощность; новые ступени расходов пока не оценены.'}], 'assumptions':['Средний месяц — 92/3 дня. Средний чек и состав заказов взвешены по 5 507 заказам. Результат не гарантирует 500 тысяч в каждом календарном месяце.','Фактические акции сохраняются; экономия прежнего упрощения акций не включена.','Четыре ежедневных места по 12 часов. Один старший с доплатой 500 рублей и фонд 5% одному из остальных — сохранённые допущения состава смены. Для смен нужны подмены и выходные.','Скидки и бонусы уже учтены в чистом чеке. Подарки и упаковка в техкартах включены в сырьевую стоимость; повторного вычитания нет.','Потери до удержаний с сотрудников сохранены. Экономия от автоматизации, улучшения качества и обучения пока не заложена.','Курьеры зависят от количества заказов. Для расходников, содержания, электричества и воды принято 50% переменной части. Это допущения бюджета.','Starter: 3% своей доли выручки и 494,05 рубля постоянного остатка. Яндекс: плановые 25% своей доли. Налог бизнеса: 6% выручки и постоянная часть 4 782,50 рубля по прежней модели.','Налоги с зарплаты: резерв 20 013,93 рубля по известной схеме и конвенции ОПиУ. Сентябрьское пустое поле и статус закрытого месяца сохранены.','Нерегулярные ремонты, найм, форма и транспорт сохранены в резервах. Отсутствие подробного назначения не считается доказанной экономией.','Новые неизвестные расходы финансирования, оборудования, запуска и пиковой мощности вынесены за числовой итог. Их влияние показано отдельно.'],'validation':{'accepted_source_identity':source_checks,'quarter_daily_revenue_matches':True,'audit_recoveries_removed_once':True,'four_fixed_payroll_verified':True,'all_thresholds_suffix_safe_through8000':True,'no_business_system_mutations':True}}
for q,s in result['scenarios'].items():assert abs(sum(x['amount'] for x in s['budget_lines'])+s['additional_future_cost']-s['costs_before_unknowns'])<.01
(ROOT/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'thresholds':T,'fixed':FIXED,'unit':UNIT,'check':CHECK,'profit3000':estimate(3000),'profit3050':estimate(3050),'sensitivity':result['additional_monthly_cost_sensitivity']},ensure_ascii=False,indent=2))
