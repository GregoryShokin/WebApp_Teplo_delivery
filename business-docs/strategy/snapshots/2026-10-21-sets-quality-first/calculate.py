"""Paper sets: preserve a declared attractive core, then replace whole recipes.
Only frozen input prices/cards are used. No business system writes.
"""
import argparse,json,math,hashlib,re
from pathlib import Path
from collections import Counter

MAIN={'salmon_plain','salmon_smoked','shrimp','mussels','eel_joint'}
LABELS={'cream_cheese':'творожный сыр','crab_mix':'крабовая смесь','cucumber':'огурец','tamago':'тамаго','salmon_plain':'лосось','salmon_smoked':'лосось х/к','shrimp':'креветка','mussels':'мидии','eel_joint':'угорь','masago':'масаго','mozzarella':'моцарелла','spice':'Spice','yaki':'яки','cheese_sauce':'сырный соус','black_eel':'Чёрный угорь','tartar_plain':'тартар из лосося','tartar_smoked':'тартар из лосося х/к','unagi':'унаги','teriyaki':'терияки','batter':'кляр','crumbs':'сухари','oil':'масло'}

def compact(ids,recipes):
    return '; '.join(recipes[k]['name']+(' (жареный)' if k=='F05' else ' (холодный)' if k=='D02' else '')+(f' ×{q}' if q>1 else '') for k,q in Counter(ids).items())
def money(x):return f'{x:,.2f}'.replace(',',' ').replace('.',',')
def grams(d):return ', '.join(f'{LABELS[k]} {v:g}' for k,v in d.items()) or 'нет'

def recipe_cost(r,c,legacy=False):
    lines=[]
    for stage in ['base','inner','top','frying']:
        for k,q in r.get(stage,{}).items():
            ck='eel_legacy' if legacy and k=='eel_joint' else k
            p=c[ck]['cost_per_sheet'] if k=='nori' else c[ck]['cost_per_g']
            lines.append({'component':k,'stage':stage,'amount':q,'unit':'лист' if k=='nori' else 'г','cost_rub':q*p})
    return sum(x['cost_rub'] for x in lines),lines

def calculate(inp):
    comp=inp['components'];recipes={r['id']:r.copy() for r in inp['original_recipes']}
    checks=[]
    def check(s,b):
        checks.append({'check':s,'status':'PASS' if b else 'FAIL'});assert b,s
    for spec in inp['new_recipes']:
        r=spec.copy();r.update(kind='ordinary',base={'rice':110,'nori':.5},status='agent_new_paper_recipe_not_existing_recipe_shrink')
        r.setdefault('top',{});r.setdefault('frying',{})
        j,lines=recipe_cost(r,comp);p,_=recipe_cost(r,comp,True)
        r.update(joint_scenario_cost_rub=j,price_reserve_cost_rub=p,lines=lines,inner_total_g=sum(r['inner'].values()))
        r['menu_price_rub']=math.ceil((p/.35-1e-10)/5)*5
        check(r['id']+' complete100g filling andmain30%',r['inner_total_g']==100 and all(q/100>=.3 for k,q in r['inner'].items() if k in MAIN))
        check(r['id']+' containsmainprotein',any(k in MAIN for k in r['inner']))
        check(r['id']+' cheeseORcrab',not ('cream_cheese' in r['inner'] and 'crab_mix' in r['inner']))
        check(r['id']+' rice110 nori0.5',r['base']=={'rice':110,'nori':.5})
        check(r['id']+' allowed palette andpricedingredients',all(k in comp for s in ['base','inner','top','frying'] for k in r[s]))
        check(r['id']+' standalone<=399 andnominalFC<=35',r['menu_price_rub']<=399 and p/r['menu_price_rub']<=.35+1e-12)
        recipes[r['id']]=r
    for r in inp['original_recipes']:
        p,_=recipe_cost(r,comp,True);j,_=recipe_cost(r,comp)
        check(r['id']+' unchangedoriginalcost',abs(p-r['price_reserve_cost_rub'])<1e-8 and abs(j-r['joint_scenario_cost_rub'])<1e-8)
    def cost(ids,key='price_reserve_cost_rub'):return sum(recipes[k][key] for k in ids)
    sets=[]
    for spec in inp['sets']:
        r=spec.copy();seed=r['seed'];final=r['final'];price=r['price'];cap=price*inp['fc_working_target'];n=r['roll_count']
        check(r['id']+' portioncounts',len(seed)==n and len(final)==n)
        check(r['id']+' seedoverbudget',cost(seed)>cap)
        path=[];current=seed.copy();first_pass=None
        for pos,(old,new) in enumerate(zip(seed,final)):
            if old==new:continue
            before=cost(current);current[pos]=new;after=cost(current)
            check(r['id']+f' substitution{pos} lowerwholeportioncost',after<before)
            path.append({'slot':pos+1,'from':old,'to':new,'before_food_rub':before,'after_food_rub':after,'over_budget_rub':max(0,after-cap),'within_food_budget':after<=cap})
            if after<=cap and first_pass is None:first_pass=len(path)
        food=cost(final);joint=cost(final,'joint_scenario_cost_rub');boxq=inp['packaging']['quantity_by_set_roll_count'][str(n)];boxes=boxq*inp['packaging']['unit_price_rub']
        check(r['id']+' firstpasslastsubstitution',first_pass==len(path))
        check(r['id']+' rawFC<=40',food<=cap+1e-9)
        check(r['id']+' recipefoodonce',abs(sum(q*recipes[k]['price_reserve_cost_rub'] for k,q in Counter(final).items())-food)<1e-8)
        hero=[k for k in seed if k in final];counts=Counter(final)
        mussel_rolls=sum(q for k,q in counts.items() if 'mussels' in recipes[k]['inner'])
        shrimp_rolls=sum(q for k,q in counts.items() if 'shrimp' in recipes[k]['inner'] or 'shrimp' in recipes[k]['top'])
        salmon_rolls=sum(q for k,q in counts.items() if any(x in recipes[k]['inner'] or x in recipes[k]['top'] for x in ['salmon_plain','salmon_smoked','tartar_plain','tartar_smoked']))
        eel_rolls=sum(q for k,q in counts.items() if 'eel_joint' in recipes[k]['inner'] or 'eel_joint' in recipes[k]['top'])
        standalone=sum(min(recipes[k]['menu_price_rub'],399) for k in final)
        check(r['id']+' setprice<=syntheticstandalone',price<=standalone)
        r.update(raw_budget_rub=cap,seed_food_rub=cost(seed),seed_fc=cost(seed)/price,seed_over_budget_rub=cost(seed)-cap,substitutions=path,retained_original_ids=hero,counts=dict(counts),food_cost_rub=food,joint_food_cost_rub=joint,raw_fc=food/price,boxes_count=boxq,boxes_cost_rub=boxes,food_plus_boxes_rub=food+boxes,food_plus_boxes_fc=(food+boxes)/price,contribution_after_food_boxes_rub=price-food-boxes,raw_headroom_rub=cap-food,raw_cost_increase_tolerance=cap/food-1,raw_headroom_per_roll=(cap-food)/n,mussels_roll_count=mussel_rolls,shrimp_roll_count=shrimp_rolls,salmon_roll_count=salmon_rolls,eel_roll_count=eel_rolls,synthetic_standalone_sum=standalone,synthetic_saving_rub=standalone-price,synthetic_saving_fraction=1-price/standalone,effective_fc_15=food/(price*.85),effective_fc_20=food/(price*.8),extra_box5_50_fc=(food+boxes+inp['packaging']['unit_price_rub'])/price)
        r['selected_for_paper_shortlist']=r['id']!='5H-A'
        r['set_surplus_eel_stress_food_rub']=sum(recipes[k].get('surplus_sauce_stress_cost_rub',recipes[k]['price_reserve_cost_rub']) for k in final)
        r['set_surplus_eel_stress_fc']=r['set_surplus_eel_stress_food_rub']/price
        r['selection_note']='deferred: only5RUBsyntheticconsumer saving; eelcooutputfullwastestress43.55%' if r['id']=='5H-A' else 'papercandidate; taste/size notvalidated'
        sets.append(r)
    current_min=min(r['price_reserve_cost_rub'] for r in inp['original_recipes'])
    lower={'cheapest_existing_recipe_rub':current_min,'ten_existing_portions_at_minimum_rub':10*current_min,'ten_set_price':1899,'raw_budget':1899*.4,'gap_before_boxes':10*current_min-1899*.4,'meaning':'Even10repeats ofthe cheapest ofexisting18 cannot satisfy40%; newrecipesorprice/size/FCchange needed.'}
    check('10oldportionsinfeasibleevenwithrepeats',lower['gap_before_boxes']>0)
    return {'as_of':'2026-10-21','status':'paperrecipesandsets, notowneracceptedmenu','fc_basis':'recipe materials / statedsetprice, before any extra discount/bonus.40% workingprojectassumption,notpermanentownerceiling. Packaging separate.','new_recipes':[recipes[r['id']] for r in inp['new_recipes']],'sets':sets,'existing18_lower_bound':lower,'validation':checks,'source_sha256':inp['source_sha256'],'limits':inp['limits']}

def render(out,res,inp):
    recipes={r['id']:r for r in inp['original_recipes']+res['new_recipes']};sets=res['sets']
    md=['# Сеты «Тепла»: от желаемого состава к цене — 21.10.2026','',
    '**9 основных вариантов:** три по999 ₽/4 ролла, два по1299 и один по1399 ₽/5 роллов, три по1899 ₽/10 роллов. Дополнительно сохранён один отложенный вариант с угрём. Рабочая граница — **40% сырьевого FC от цены сета до дополнительных акций**, выбранная агентом для этого расчёта из примера владельца. Это не новая постоянная норма.','',
    'Сначала сохранены желаемые составы с выразительными крышками, тартаром и миксами; затем позиции заменялись целыми порциями до попадания в бюджет. Высокая цена ингредиентов не названа доказанным вкусом. В калькуляции основная консервативная стоимость угря, без неподтверждённой экономии полного сбыта соуса.','',
    '## Кандидаты','',
    '| Сет | Состав: целые роллы | Цена, ₽ | Продукты, ₽ | Сырьевой FC | Продукты+коробки, ₽ / FC |','|---|---|---:|---:|---:|---:|']
    for s in sets:
        if not s['selected_for_paper_shortlist']:continue
        md.append(f"| {s['id']} {s['name']} | {compact(s['final'],recipes)} | {s['price']} | {money(s['food_cost_rub'])} | {s['raw_fc']*100:.2f}% | {money(s['food_plus_boxes_rub'])} / {s['food_plus_boxes_fc']*100:.2f}% |")
    e=next(s for s in sets if s['id']=='5H-A')
    md += ['','**Отложенный угревый вариант5H-A,1399 ₽/5 роллов:** '+compact(e['final'],recipes)+f". В базе готового филе продукты{money(e['food_cost_rub'])} ₽, FC{e['raw_fc']*100:.2f}%. При изготовлении всего сопутствующего соуса и списании его избытка —{money(e['set_surplus_eel_stress_food_rub'])} ₽ /{e['set_surplus_eel_stress_fc']*100:.2f}%. Экономия относительно проектных штучных цен всего{money(e['synthetic_saving_rub'])} ₽. В основную девятку не включён; снижение сырьевого FC через неподтверждённый сбыт соуса не принято за факт."]
    md += ['','Коробки: **11 ₽ для4/5 роллов и22 ₽ для10**, по текущим расходам аналогичных карт при5,50 ₽/ланчбокс. Вместимость для новых крупных роллов не проверена. Дополнительный бокс+5,50 ₽; приборы/пакет/гарнирный комплект и дополнительные соусы вне карт отдельно, не объявлены нулём. Повторно прибавлять продуктовую упаковку из карты нельзя.','',
    '## Новые доступные рецептуры','',
    'Во всех новых: **готовый рис110 г и½листа нори**. Внутренняя начинка100 г; основной продукт30 г=30%. Это новые отдельные проекты в разрешённом диапазоне100–110 г, а не уменьшенные втайне старые карты105 г. Крабовая смесь80/20 по нетто; закупочная база сохранена из предыдущего расчёта. Масса готового изделия после обработки и единый визуал не измерены.','',
    '| ID | Новый ролл | Внутри, г | Сверху/жарка, г | Себестоимость, ₽ |','|---|---|---|---|---:|']
    for r in res['new_recipes']:
        ext=grams(r['top']);fry=grams(r['frying'])
        md.append(f"| {r['id']} | {r['name']} | {grams(r['inner'])} | Сверху: {ext}; жарка: {fry} | {money(r['price_reserve_cost_rub'])} |")
    md += ['','ЖареныеD05/D09 идут без наружного соуса. Их вкус/текстура и переносимость доставки ещё не проверены. УD07 только20 г яки, это не прежние35 г покрытия. Огурец40 г в части новых начинок — проектная норма; тёплые варианты требуют отдельной проверки.','',
    '## Что пришлось заменить','',
    '| Сет | Желаемый исходный состав | Исходное сырьё / превышение бюджета, ₽ | Замены по шагам |','|---|---|---:|---|']
    for s in sets:
        steps='; '.join(f"{x['from']}→{x['to']} (сырьё стало {money(x['after_food_rub'])} ₽)" for x in s['substitutions'])
        md.append(f"| {s['id']} | {compact(s['seed'],recipes)} | {money(s['seed_food_rub'])} / +{money(s['seed_over_budget_rub'])} | {steps} |")
    md += ['','Показываем состав до и после, а не выдаём выбранный набор за самый вкусный результат. Слоты не уменьшаются: если нужна новая граммовка/состав, это новая явно названная карта. Расшифровка всех исходныхC/B/F-позиций: [первая линейка Q22](../2026-10-21-new-roll-line-v1/report.md).','',
    '## Ограничение10 роллов за1899','',
    f"Бюджет продуктов759,60 ₽, то есть75,96 ₽/ролл. Самый дешёвый из прежних18 стоит{money(res['existing18_lower_bound']['cheapest_existing_recipe_rub'])} ₽: даже10 одинаковых дали бы{money(res['existing18_lower_bound']['ten_existing_portions_at_minimum_rub'])} ₽ без коробок. Поэтому новые доступные карты здесь необходимы при выбранных условиях, а не добавлены произвольно.",'',
    'Большие варианты различаются реальной уступкой:','']
    for s in sets:
        if s['roll_count']==10:
            md.append(f"- **{s['id']} {s['name']}:** мидийных{s['mussels_roll_count']}, креветочных{s['shrimp_roll_count']}, лососевых{s['salmon_roll_count']}. ВариантыA/B сохраняют выразительную крышку/тартар; вариантC распределяет больше порций между лососем и креветкой, но отдельной50-граммовой крышки/тартарной шапки в нём нет.")
    md += ['','## Запас, дополнительные выгоды и ценность','',
    '| Сет | Запас сырья до40%, ₽ | Допустимый рост сырьевой стоимости | Сырьевой FC после дополнительных15% /20% | Сумма проектных штучных цен / экономия, ₽ |','|---|---:|---:|---:|---:|']
    for s in sets:
        md.append(f"| {s['id']} | {money(s['raw_headroom_rub'])} | {s['raw_cost_increase_tolerance']*100:.2f}% | {s['effective_fc_15']*100:.2f}% / {s['effective_fc_20']*100:.2f}% | {money(s['synthetic_standalone_sum'])} / {money(s['synthetic_saving_rub'])} |")
    md += ['','Почти нулевой запас не означает устойчивую производственную калькуляцию. Особенно внимательно10A,4B и5L-A. Ещё одна коробка и прочие расходы могут увеличить материальную долю, даже если сырьевой FC формально≤40%. Полная прибыль требует прежнего бюджета ФОТ, доставки, комиссий, налогов, потерь и новой техники; остаток после продуктов/боксов не назван чистой прибылью.','',
    'Штучные цены — только расчётное сравнение: у существующих сохранён потолок399 ₽ изQ23, для новых применено округление вверх до5 ₽ отcost/35%, также≤399. Это не опубликованный прайс. Экономия некоторых маленьких наборов составляет лишь2–3%, а отложенного угревого —0,36%; не обещать большую скидку или превосходство без доказательства.40% для сырья до акций не равно40% от оплаты при бонусах/скидках сверху.','',
    '## Что проверяем дальше','',
    'Выбрать кандидатов и проверить вкус, размер/срез, доставку и сборку именно новых100-граммовых начинок; подтвердить упаковку/комплект; установить совместимость выгод и микс сетов/штучных заказов. После этого пересчитать оплаченный чек и бюджет500тыс., не переносить порогиQ23 без нового микса. Кухонная проработка/рассылка/публикация сейчас не запускались.','',
    'Вводные/пропорции/источники: [inputs.json](inputs.json). Построчные карты, все замены, упаковка и чувствительности: [results.json](results.json). Пересборка: `python3 calculate.py --inputs inputs.json --out .`. Правило в стратегии: [сборка сетов](../../../../skills/teplo-profit-strategy/references/hypothesis-evaluation.md#сборка-сетов-от-желаемого-состава-к-доступной-цене). Журнал: [Q24](../../profit-progress.md).','']
    md=[re.sub(r'(\d)(?=[А-Яа-яЁё₽])',r'\1 ',re.sub(r'(?<=[А-Яа-яЁё])(\d)',r' \1',line)) if not ('[' in line or '`' in line or line.startswith('|')) else line for line in md]
    (out/'report.md').write_text('\n'.join(md))

def main():
    p=argparse.ArgumentParser();p.add_argument('--inputs',type=Path,required=True);p.add_argument('--out',type=Path,required=True);a=p.parse_args()
    x=json.loads(a.inputs.read_text());r=calculate(x);a.out.mkdir(parents=True,exist_ok=True)
    (a.out/'results.json').write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n');render(a.out,r,x)
    print(json.dumps({'sets':[{'id':s['id'],'count':s['roll_count'],'price':s['price'],'food':s['food_cost_rub'],'raw_fc_pct':s['raw_fc']*100,'food_boxes_fc_pct':s['food_plus_boxes_fc']*100,'headroom':s['raw_headroom_rub'],'changes':len(s['substitutions'])} for s in r['sets']],'newrecipes':len(r['new_recipes']),'checks':len(r['validation'])},ensure_ascii=False,indent=2))

if __name__=='__main__':main()
