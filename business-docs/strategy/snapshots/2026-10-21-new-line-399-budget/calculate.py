"""Offline conditional recipe/budget scenario. No API requests or production writes."""
import argparse,json,math,hashlib,html,re
from pathlib import Path

MAXQ=20000
def ceil50(x):return math.ceil(x/50)*50
def pool_profile(days,tiers,base_orders,base_check,new_check):
    events=[];previous=0.
    for t in tiers:
        delta=t['rate']-previous;previous=t['rate']
        for d in days:
            slope=d['revenue']/base_orders*new_check/base_check
            events.append((t['min']/slope,slope*delta/3))
    events.sort();j=0;k=0.;out=[0.]
    for q in range(1,MAXQ+1):
        while j<len(events) and events[j][0]<=q+1e-12:
            k+=events[j][1];j+=1
        out.append(q*k)
    return out

def threshold(profits,target):
    suffix=[math.inf]*(MAXQ+2)
    for q in range(MAXQ,0,-1):suffix[q]=min(profits[q],suffix[q+1])
    first=next((q for q in range(1,MAXQ+1) if profits[q]>=target),None)
    safe=next((q for q in range(1,MAXQ+1) if suffix[q]>=target),None)
    plan=ceil50(safe) if safe else None
    return {'first_crossing':first,'safe_crossing_through20000':safe,'planning_orders':plan,'profit_at_safe':profits[safe] if safe else None,'profit_at_planning':profits[plan] if plan else None}

def calculate(inp):
    b=inp['budget'];menus=inp['menu'];base=b['baseline'];coef=b['coefficients'];rows=b['budget_rows'];days=b['days'];tiers=b['tiers']
    fixed=coef['fixed_monthly'];c0=base['weighted_net_check'];q0=base['average_orders'];roll0=b['roll_net_per_order'];othernet=c0-roll0;otherfood=b['other_food_per_order'];loss=b['loss_rate'];box=inp['set_box_proxy']['rub_per_physical_roll']
    rev_codes={'acquiring','starter','yandex','business_taxes'}
    k=sum(r['per_order'] for r in rows if r['code'] in rev_codes)/c0
    ordercodes={r['code'] for r in rows}-rev_codes-{'roll_food','other_food','writeoff_reserve','inventory_reserve'}
    unitorder=sum(r['per_order'] for r in rows if r['code'] in ordercodes)
    assert abs(unitorder+(323.03875066314095+otherfood)*(1+loss)+k*c0-coef['variable_per_order_without_company_pool'])<1e-7
    pools={}
    def solve(name,check,food,extra=0,more=None):
        key=round(check,9)
        if key not in pools:pools[key]=pool_profile(days,tiers,q0,c0,check)
        pp=pools[key];specific_waste=(more or {}).get('specific_surplus_sauce_waste_per_order',0.)
        unit=unitorder+(food-specific_waste)*(1+loss)+specific_waste+k*check
        profits=[-fixed-extra]+[q*(check-unit)-fixed-pp[q]-extra for q in range(1,MAXQ+1)]
        t={str(v):threshold(profits,v) for v in [0,500000]}
        return {'scenario':name,'net_check':check,'recipe_food_per_order':food,'enterprise_recipe_fc':food/check,'unit_without_company_pool':unit,'contribution_before_pool':check-unit,'fixed_monthly':fixed,'extra_monthly':extra,'thresholds':t,'profit_at_3050':profits[3050],'profit_at_old_average_orders':q0*(check-unit)-fixed-extra-sum(d['revenue']*check/c0*next((t['rate'] for t in reversed(tiers) if d['revenue']*check/c0>=t['min']),0) for d in days)/3,**(more or {})}
    baseline=solve('Old Q15: netrollFC33%',c0,roll0*.33+otherfood)
    assert baseline['thresholds']['0']['safe_crossing_through20000']==2221
    assert baseline['thresholds']['500000']['safe_crossing_through20000']==3006
    scenarios=[];menu_summary={}
    for price_mode in ['cap399','all399']:
        price_sum=sum(min(m['old_price'],399) if price_mode=='cap399' else 399 for m in menus);avgp=price_sum/len(menus)
        for costmode in ['joint','reserve','surplus_stress']:
            avgcost=sum(m['costs'][costmode] for m in menus)/len(menus)
            menu_summary[price_mode+'_'+costmode]={'average_menu_price':avgp,'average_recipe_material':avgcost,'fc_before_offers':avgcost/avgp,'fc_with_setbox_proxy_before_offers':(avgcost+box)/avgp}
            for reduction in [0,.15,.20]:
                retention=1-reduction;paidp=avgp*retention
                # A: old paid check and roll paid revenue are held fixed by assumption;
                # implicit roll portions/order adjust to paid price. This is NOT a demand forecast.
                nroll=roll0/paidp;food=nroll*(avgcost+box)+otherfood
                avgjoint=sum(m['costs']['joint'] for m in menus)/len(menus)
                specific_waste=nroll*(avgcost-avgjoint) if costmode=='surplus_stress' else 0.
                info={'basket_mode':'fixed_paid_check','price_mode':price_mode,'cost_mode':costmode,'effective_offer_reduction':reduction,'net_roll_fc_recipe':avgcost/paidp,'net_roll_fc_with_setbox_proxy':(avgcost+box)/paidp,'roll_net_per_order':roll0,'roll_physical_portions_per_order_synthetic':nroll,'roll_material_per_order':nroll*avgcost,'setbox_per_order_proxy':nroll*box,'other_net_per_order':othernet,'specific_surplus_sauce_waste_per_order':specific_waste,'food_fc_includes_specific_sauce_writeoff':costmode=='surplus_stress'}
                scenarios.append(solve(f'{price_mode}/{costmode}/benefits{reduction:.0%}/fixed_paidcheck',c0,food,more=info))
                if price_mode=='cap399' and costmode=='reserve':
                    # B: synthetic NEW basket calibrated to old paid check at zero benefits.
                    # Same quantities when benefits increase, so the paid check falls.
                    n0=roll0/avgp;rroll=roll0*retention;newcheck=othernet+rroll;food2=n0*(avgcost+box)+otherfood
                    more=dict(info,basket_mode='fixed_new_basket_quantities',roll_net_per_order=rroll,roll_physical_portions_per_order_synthetic=n0,roll_material_per_order=n0*avgcost,setbox_per_order_proxy=n0*box)
                    scenarios.append(solve(f'{price_mode}/{costmode}/benefits{reduction:.0%}/fixed_newbasket',newcheck,food2,more=more))
                if price_mode=='cap399' and costmode=='reserve' and reduction in [0,.15,.20]:
                    for extra in [20000,50000]:
                        scenarios.append(solve(f'{price_mode}/{costmode}/benefits{reduction:.0%}/fixed_paidcheck/extra{extra}',c0,food,extra,info))
    # Verify daily stepper directly independently at discontinuities and ordinary points.
    poolchecks=0
    for check in [c0,othernet+roll0*.85,othernet+roll0*.8]:
        prof=pool_profile(days,tiers,q0,c0,check)
        for q in [1,1000,1836,2221,3006,3050,4000,8000,20000]:
            expected=sum(d['revenue']*q/q0*check/c0*next((t['rate'] for t in reversed(tiers) if d['revenue']*q/q0*check/c0>=t['min']),0) for d in days)/3
            assert abs(expected-prof[q])<1e-7,(check,q,expected,prof[q]);poolchecks+=1
    return {'as_of':'2026-10-21','scope':'Chernikova; whole roll-line replaced by hypothetical new18, equal physical sales mix, other recipes unchanged. No production changes.','baseline':baseline,'coefficients':{'fixed_monthly':fixed,'other_order_driver_cost':unitorder,'revenue_cost_rate':k,'roll_net_per_order_old':roll0,'other_net_per_order_old':othernet,'other_food_per_order':otherfood,'food_loss_reserve_rate':loss,'set_box_cost_proxy_per_physical_roll':box,'formula':'profit=q*(paidcheck − perorder_driver − (food−specific_waste)*(1+loss_rate)+specific_waste − revenue_rate*paidcheck) − fixed − daily_company_pool(q,paidcheck) − new_extra_costs'},'menu_summary':menu_summary,'scenarios':scenarios,'validation':{'old_budget_exact_thresholds_reproduced':True,'old_variable_cost_reproduced':True,'daily_pool_direct_checks':poolchecks,'thresholds_safe_horizon':MAXQ,'source_hashes':inp['source_sha256']},'limits':['Equal18 is a scenario, not observed sales mix; newsetcomposition/discount/gifts unknown','Current/new lighter offers are NOT replayed;15/20% are illustrative effective reductions','Q15 historical1806 paidcheck already includes historical benefits; future capped paidcheck unknown','fixed_newbasket calibration at zero benefits is synthetic, not historical SKU basket mapping','Set box proxy restored once; sidekits/order packaging outside cards not fully estimated','Unpriced water retained; no additional equipment/finance/capacity/marketing-acquisition budget embedded','Eel reserve and joint/stress are alternatives, not costs to add together; scenario surplusstress is not general loss reserve','Rate/source dates and fixed staff budget retained from Q15, no guarantee of kitchen capacity for threshold demand']}

def report(out,res,inp):
    ss=res['scenarios'];base=res['baseline'];c=res['coefficients'];menu=res['menu_summary']
    def relevant(s):return s['price_mode']=='cap399' and s['cost_mode']=='reserve' and s['basket_mode']=='fixed_paid_check' and s['extra_monthly']==0
    main=[s for s in ss if relevant(s)]
    md=['# Новая линейка с потолком 399 ₽ — условная экономика 21.10.2026','',
    '**Основная трактовка:** цены ниже 399 ₽ не повышаем, девять верхних цен снижаем до 399 ₽. Рецептуры прежние. Условный состав продаж — равное количество всех 18, а не фактический прогноз. Средняя цена 363,39 ₽ вместо проектных 418,33 ₽ (−13,13%). Это сравнение двух новых прайсов, не доказанное снижение исторического чека июля–сентября.','',
    '## Food cost роллов','',
    '| База продуктов | До акций | Потеря 15% меню-выручки | Потеря 20% меню-выручки |','|---|---:|---:|---:|']
    for cm,title in [('joint','Полностью востребован совместный угорь'),('reserve','Консервативная база угря — основной расчёт'),('surplus_stress','Произведён и списан весь избыток угревого соуса')]:
        fc=menu['cap399_'+cm]['fc_before_offers']
        md.append(f'| {title} | {fc*100:.2f}% | {fc/.85*100:.2f}% | {fc/.8*100:.2f}% |')
    md += ['', 'Это продукты самих роллов, без неоценённой воды и комплекта к заказу. Для сопоставимости с бюджетом отдельно добавлен прежний расход упаковки и прочих добавок из карт сетов: 43 945 ₽ / 23 405 порций = '+f"{c['set_box_cost_proxy_per_physical_roll']:.4f} ₽/порцию. Это расчёт по прежнему расходу, не утверждённая упаковка новых сетов. Он добавляет около 0,52 п.п. FC до акций. Резерв инвентаризационных отклонений упаковки остался отдельной прежней статьёй; он не заменяет обычные коробки.",'',
    'При 399 ₽ бюджет сырья для 35% —139,65 ₽. Средняя консервативная себестоимость новой линейки 145,66 ₽ ещё до коробок. При полной реализации совместного угря —133,82 ₽, но дешёвые позиции тянут среднюю цену ниже 399 ₽. Тартар из лосося при 399 ₽ имеет FC45,03% до акций; жареный угорь с креветкой —51,44% в консервативной базе.','',
    '## Порог заказов при сохранении оплаченного чека','',
    'Для сравнения с Q15 сохраняем оплаченный чек **1 806,01 ₽**, распределённую оплаченную выручку роллов **978,91 ₽/заказ** и прочую выручку **827,11 ₽/заказ**. Предполагаем замену всей линии роллов этими 18 кандидатами, остальные рецептуры прежние. Это условие расчёта: новый прайс сам по себе не доказывает такой чек. При больших акционных потерях в этой версии покупатели должны брать больше порций для сохранения той же оплаты. Все числа — за средний месяц92/3 дня, с округлением вверх до50 заказов.','',
    '| Сценарий | FC роллов с proxy коробок, от оплаты | FC продуктов всего предприятия | Безубыточность, заказов/мес | Для 500 тыс., заказов/мес | Выручка для 500 тыс., млн ₽ |','|---|---:|---:|---:|---:|---:|',
    f"| Прежний бюджет Q15: FC роллов33% | 33,00% | {base['enterprise_recipe_fc']*100:.2f}% | 2250 | 3050 | {3050*base['net_check']/1e6:.2f} |"]
    for s in main:
        zero=s['thresholds']['0']['planning_orders'];goal=s['thresholds']['500000']['planning_orders']
        md.append(f"| Потолок399; эффективная акционная потеря {s['effective_offer_reduction']:.0%} | {s['net_roll_fc_with_setbox_proxy']*100:.2f}% | {s['enterprise_recipe_fc']*100:.2f}% | {zero} | {goal} | {goal*s['net_check']/1e6:.2f} |")
    md += ['','ФОТ: четыре ежедневных места второй категории, старший/фонды и весь остальной персонал сохранены. Фиксированная часть четырёх320 433,33 ₽/мес; весь процентный фонд кухни пересчитан по каждому из92 дней с действующими ступенями3,5/4,5/5,5/6,5%, а не пропущен и не умножен на четыре. Постоянные расходы '+f"{c['fixed_monthly']:,.2f} ₽/мес. Курьеры и другие расходы на заказ, комиссии с выручки, налоги, резервы потерь включены. Нерегулярные резервы и амортизация старой техники сохранены.",'',
    '## Если при акциях количество еды в новой корзине не растёт','',
    'Дополнительный стресс: калибруем условную новую корзину без акций к оплате1 806,01 ₽, затем удерживаем её состав/количество и снижаем оплату только роллов на15/20%. Это не реконструкция исторической корзины, поскольку новых SKU/сетовых составов и их спроса пока нет. Прочие блюда сохраняют выручку/сырьё; комиссии и дневной зарплатный фонд уменьшаются с выручкой.','',
    '| Акционная потеря на роллах | Оплаченный чек, ₽ | Безубыточность, заказов/мес | Для 500 тыс., заказов/мес |','|---|---:|---:|---:|']
    for s in ss:
        if s['basket_mode']=='fixed_new_basket_quantities':md.append(f"| {s['effective_offer_reduction']:.0%} | {s['net_check']:.2f} | {s['thresholds']['0']['planning_orders']} | {s['thresholds']['500000']['planning_orders']} |")
    md += ['','## Чувствительность к угрю и новым месячным расходам','',
    '| Условие при чеке1 806 ₽ | Безубыточность | Для 500 тыс. |','|---|---:|---:|']
    for s in ss:
        if s['price_mode']=='cap399' and s['basket_mode']=='fixed_paid_check' and (s['effective_offer_reduction']==0 and s['extra_monthly']==0 or s['cost_mode']=='reserve' and s['effective_offer_reduction']==.15 and s['extra_monthly'] in [20000,50000]):
            md.append(f"| {s['cost_mode']}, потеря {s['effective_offer_reduction']:.0%}, дополнительно {s['extra_monthly']:,} ₽/мес | {s['thresholds']['0']['planning_orders']} | {s['thresholds']['500000']['planning_orders']} |")
    md += ['','## Граница «все по399»','',
    'Если поднять и дешёвые позиции до399 ₽ — это другой прайс, не наш основной потолок. В таком случае FC продуктов до акций '+f"{menu['all399_joint']['fc_before_offers']*100:.2f}% при полном совместном использовании угря либо {menu['all399_reserve']['fc_before_offers']*100:.2f}% в консервативной базе. Это не утверждение спроса по повышенным ценам.",'',
    '## Что остаётся неизвестным','',
    '- Доли продаж новых18 и замена старых SKU, новая структура сетов и оплаченный чек. Равный состав продаж — стартовое допущение.','- Фактическая эффективная нагрузка акций/бонусов/подарков новой линейки.15/20% — иллюстрации; действующие и облегчённые механики не пересчитаны без новых корзин/сетовых правил. Начисления бонусов не вычтены повторно.','- Реальная упаковка/гарнирные комплекты, вода, нормирование масла/панировки и условная цена сырья/выходы из Q22 остаются на проверку.','- Месячная стоимость новой техники, финансирование, запуск и привлечение дополнительных заказов, а также новые смены/логистика при пиках сверх прежнего бюджета. В расчёт не заложена недоказанная экономия автоматизации или гарантированный рост спроса.','- Требуемые заказы не доказывают мощность четырёх сотрудников. При росте нагрузки учитываем новые ступени расходов отдельно.','',
    'Источник бюджета: [Q15](../2026-10-19-unified-operating-budget/results.json). Рецептуры: [Q22](../2026-10-21-new-roll-line-v1/report.md). Расчёт: [results.json](results.json), вводные: [inputs.json](inputs.json), независимый аудит меню: [menu-audit.json](menu-audit.json). Пересборка: `python3 calculate.py --inputs inputs.json --out .`. Единый журнал: [Q23](../../profit-progress.md).','']
    md=[re.sub(r'(\d)(?=[А-Яа-яЁё₽])',r'\1 ',re.sub(r'(?<=[А-Яа-яЁё])(\d)',r' \1',line)) if not ('[' in line or '`' in line or line.startswith('|')) else line for line in md]
    md=[line.replace('proxy коробок','поправкой на упаковку').replace('1,517,035.18','1 517 035,18').replace('20,000','20 000').replace('50,000','50 000') for line in md]
    (out/'report.md').write_text('\n'.join(md))

def main():
    p=argparse.ArgumentParser();p.add_argument('--inputs',type=Path,required=True);p.add_argument('--out',type=Path,required=True);a=p.parse_args()
    x=json.loads(a.inputs.read_text());r=calculate(x);a.out.mkdir(parents=True,exist_ok=True)
    (a.out/'results.json').write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n');report(a.out,r,x)
    main=[s for s in r['scenarios'] if s['price_mode']=='cap399' and s['cost_mode']=='reserve' and s['extra_monthly']==0]
    print(json.dumps({'coef':r['coefficients'],'menu':r['menu_summary'],'main':[{k:s[k] for k in ['basket_mode','effective_offer_reduction','net_check','net_roll_fc_with_setbox_proxy','enterprise_recipe_fc','thresholds','profit_at_3050']} for s in main]},ensure_ascii=False,indent=2))

if __name__=='__main__':main()
