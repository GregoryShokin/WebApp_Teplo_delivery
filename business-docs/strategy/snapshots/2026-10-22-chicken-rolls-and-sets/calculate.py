"""Frozen-input material costing of new chicken recipes and whole-roll set changes."""
import copy
import hashlib
import json
import math
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
data = json.loads((HERE/'inputs.json').read_text())
components = data['components']
old = {r['id']: r for r in data['existing_recipes']}
recipes = copy.deepcopy(old)
checks = []


def check(label, passed):
    assert passed, label
    checks.append({'check': label, 'status': 'PASS'})


def cost(r):
    lines=[]
    for stage in ('base','inner','top','frying'):
        for name,q in r.get(stage,{}).items():
            check(r['id']+' no unresolved eel '+name, name not in ('eel_joint','eel_legacy','black_eel'))
            unit='cost_per_sheet' if name=='nori' else 'cost_per_g'
            lines.append({'stage':stage,'component':name,'amount':q,
                          'unit':'лист' if name=='nori' else 'г',
                          'materials_rub':q*components[name][unit]})
    return sum(x['materials_rub'] for x in lines),lines


for r in data['new_recipes']:
    r=copy.deepcopy(r)
    food,lines=cost(r)
    r.update(materials_rub=food,lines=lines,inner_total_g=sum(r['inner'].values()))
    r['project_menu_price_rub']=math.ceil((food/.35-1e-10)/5)*5
    r['project_menu_fc']=food/r['project_menu_price_rub']
    r['status']='new_paper_candidate_not_approved_recipe_or_measured_dose'
    r['physical_portion_status']='one_prepared_chicken_portion_target40g_proposal_not_measured; other_units_not_validated'
    check(r['id']+' rice110 and halfsheet',r['base']=={'rice':110,'nori':.5})
    check(r['id']+' full100g inside',r['inner_total_g']==100)
    check(r['id']+' chicken40%',r['inner']['chicken_smoked']/r['inner_total_g']==.4)
    check(r['id']+' primary>=30',r['inner']['chicken_smoked']/r['inner_total_g']>=.3)
    if r['inner'].get('crab_mix'):
        check(r['id']+' no cheese with crab',not any(r['inner'].get(k,0) for k in ('cream_cheese','mozzarella')))
        r['actual_crab_g']=.8*r['inner']['crab_mix']
        check(r['id']+' actual crab primary>=30%',r['actual_crab_g']/r['inner_total_g']>=.3)
    check(r['id']+' menuFC<=35 and price<=399',r['project_menu_fc']<=.35 and r['project_menu_price_rub']<=399)
    check(r['id']+' allowed palette',not any(k in str(r['inner']) for k in ('squid','tuna','carrot','mushroom','radish')))
    recipes[r['id']]=r


def food(ids):
    return sum(recipes[k].get('materials_rub',recipes[k].get('price_reserve_cost_rub')) for k in ids)


def signature(r):
    return json.dumps({k:r.get(k,{}) for k in ('base','inner','top','frying')},sort_keys=True)


def counts_mains(ids):
    result={k:0 for k in ('chicken','salmon','shrimp','mussels')}
    for rid in ids:
        r=recipes[rid]
        stages={**r['inner'],**r.get('top',{})}
        result['chicken']+=int('chicken_smoked' in stages)
        result['salmon']+=int(any(k in stages for k in ('salmon_plain','salmon_smoked','tartar_plain','tartar_smoked')))
        result['shrimp']+=int('shrimp' in stages)
        result['mussels']+=int('mussels' in stages)
    return result


out=[]
previous={s['id']:s for s in data['previous_sets']}
for s in data['sets']:
    s=copy.deepcopy(s)
    final,seed=s['final'],s['seed']
    cap=s['price']*data['working_raw_fc_target']
    check(s['id']+' exact portions',len(final)==len(seed)==s['roll_count'])
    check(s['id']+' IDs max2',max(Counter(final).values())<=2)
    check(s['id']+' composition max2',max(Counter(signature(recipes[k]) for k in final).values())<=2)
    check(s['id']+' new chicken present',any(k.startswith('K') for k in final))
    check(s['id']+' seafood retained',any(not k.startswith('K') for k in final))
    check(s['id']+' costed ingredients no eel',all(not ({name for stage in ('base','inner','top','frying') for name in recipes[k].get(stage,{})} & {'eel_joint','eel_legacy','black_eel'}) for k in set(seed+final)))
    # Preserve a desired seed and show only whole-card substitutions, ascending savings.
    changes=[(i,a,b,food([a])-food([b])) for i,(a,b) in enumerate(zip(seed,final)) if a!=b]
    check(s['id']+' replacements lower cost',all(delta>0 for _,_,_,delta in changes))
    changes.sort(key=lambda x:x[3])
    current=seed.copy()
    path=[]
    for i,a,b,delta in changes:
        check(s['id']+' replace only while seed exceeds budget',food(current)>cap+1e-8)
        before=food(current)
        current[i]=b
        path.append({'slot':i+1,'from':a,'to':b,'before_rub':before,
                     'after_rub':food(current),'saving_rub':delta})
    check(s['id']+' final reached',current==final)
    amount=food(final)
    check(s['id']+' raw working target',amount<=cap+1e-8)
    pack=data['packaging']
    boxes_count=pack['quantity_by_set_roll_count'][str(s['roll_count'])]
    boxes=boxes_count*pack['unit_price_rub']
    standalone=sum(recipes[k].get('project_menu_price_rub',min(recipes[k].get('menu_price_rub',0),399)) for k in final)
    check(s['id']+' no set dearer than project singles',standalone>=s['price'])
    prev=previous[s['previous_id']]
    s.update(counts=dict(Counter(final)),seed_food_rub=food(seed),seed_fc=food(seed)/s['price'],
             substitutions=path,food_cost_rub=amount,raw_fc=amount/s['price'],
             boxes_count=boxes_count,boxes_cost_rub=boxes,food_plus_boxes_rub=amount+boxes,
             food_plus_boxes_fc=(amount+boxes)/s['price'],raw_headroom_rub=cap-amount,
             recipe_price_increase_tolerance=cap/amount-1,
             main_presence_roll_counts=counts_mains(final),
             previous_composition=prev['final'],previous_food_rub=prev['food_cost_rub'],
             food_change_vs_previous_rub=amount-prev['food_cost_rub'],
             raw_fc_change_vs_previous_pp=(amount-prev['food_cost_rub'])/s['price']*100,
             synthetic_standalone_sum_rub=standalone,
             synthetic_customer_saving_rub=standalone-s['price'],
             synthetic_customer_saving_pct=(1-s['price']/standalone)*100,
             raw_fc_if_paid15pct_less=amount/(s['price']*.85),
             raw_fc_if_paid20pct_less=amount/(s['price']*.8))
    out.append(s)

check('existing cards left byte-equivalent as JSON records',all(recipes[k]==v for k,v in old.items()))
new=[recipes[r['id']] for r in data['new_recipes']]
result={'as_of':data['as_of'],'status':'paper_chicken_recipes_and_sets_not_accepted_production_menu',
        'fc_basis':'recipe materials / menu set price before extra discounts; packaging separate; unpriced water and order costs not zero',
        'working_raw_fc_target':data['working_raw_fc_target'],
        'new_recipes':new,'sets':out,'existing_cards_unchanged':True,
        'checks':checks,'source_sha256':data['source_sha256'],
        'assumptions':data['assumptions']}
(HERE/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')

labels={k:v['name'] for k,v in components.items()}
labels.update(chicken_smoked='Копчёная курица',cream_cheese='творожный сыр',crab_mix='крабовая смесь80/20',cucumber='огурец',tamago='тамаго',mozzarella='моцарелла',yaki='яки',spice='Spice',cheese_sauce='сырный соус',unagi='унаги',teriyaki='терияки')
def comp(items):
    return ', '.join(f'{labels[k]} {v:g} г' for k,v in items.items()) or '—'
def names(ids):
    return '; '.join((f'{n}× ' if n>1 else '')+f'{k} «{recipes[k]["name"]}»' for k,n in Counter(ids).items())
def rub(x):return f'{x:.2f}'.replace('.',',')
def pct(x):return f'{100*x:.2f}%'.replace('.',',')

md=['# Новые куриные роллы и пересобранные сеты — 22.10.2026','',
    '**Шесть новых кандидатов и девять сетов.** Прежние карты, внутренние граммовки,50 г крышки и70 г тартарных шапок сохранены без изменений. В каждом сете≤2 одинаковых ролла, проверены ID и фактический состав. Курица добавлена отдельными новыми картами; это не замена ингредиента внутри утверждённой позиции.','',
    'Материалы на целый ролл/сет, не полная себестоимость заказа. Цена копчёной курицы465 ₽/кг покупной, выход74%, готовая628,38 ₽/кг; выход уже применён один раз. Остальная база21.10, сыр+30%, покупной снежный краб450 ₽/кг, смесь80/20.','',
    '## Новые роллы и закладки','',
    'У всех: **рис110 г, нори½листа, внутренняя начинка100 г**. Курица40 г — целевая заранее подготовленная порция для нового проекта, не измеренная физическая доза и не перенос диапазона10–15 г куска лосося. Ложки/дозаторы/разброс массы ещё не проверены. Номинальные граммы нужны для калькуляции, без снятия1–3 г со старых карт. В K2/K5 краба32 г без майонеза; сыр внутри отсутствует.','',
    'В жареных дополнительно условные прежние нормы: кляр25 г, сухари15 г, масло5 г. Это материальная закладка для бумаги, не измеренный вес панировки/готовой порции или полный расход фритюра.','',
    'Штучные цены ниже — **отдельное предложение** по прежнему правилу: округлитьcost/35% вверх до5 ₽, до акций. Это нужно для самостоятельного FC и сравнения с сетом; прайс не изменён.','',
    '| ID / группа / название | Внутри | Сверху | Материалы, ₽ | Проектная цена, ₽ | FC до акций |',
    '|---|---|---|---:|---:|---:|']
for r in new:
    md.append(f'| {r["id"]} / {r["group"]} / {r["name"]} | {comp(r["inner"])} | {comp(r["top"])} | {rub(r["materials_rub"])} | {r["project_menu_price_rub"]} | {pct(r["project_menu_fc"])} |')
md += ['', '## Сеты по прежним ценам','',
       'Для сопоставимости сохранён прежний **рабочий ориентир≤40% продуктов от цены сета до дополнительных выгод**. Он выбран для этой бумаги, не является новой постоянной нормой владельца. Все исходные желаемые составы и замены сохранены ниже. Вкусовое качество от высокой цены не выводится.','',
       '| Сет | Цена / роллов | Состав | Продукты, ₽ | FC продуктов | С коробками, ₽ | FC с коробками |',
       '|---|---:|---|---:|---:|---:|---:|']
for s in out:
    md.append(f'| {s["id"]} «{s["name"]}» | {s["price"]} / {s["roll_count"]} | {names(s["final"])} | {rub(s["food_cost_rub"])} | {pct(s["raw_fc"])} | {rub(s["food_plus_boxes_rub"])} | {pct(s["food_plus_boxes_fc"])} |')
md += ['', 'Коробки: прежний proxy —2×5,50=11 ₽ для4/5 роллов и4×5,50=22 ₽ для10. Вместимость для новых порций не подтверждена. Пакет, приборы, гарнирный комплект и дополнительные соусы вне карт отдельно не оценены. Вода не оценена; ни один неизвестный расход не принят за ноль.','',
       '## Отличия от прошлых сетов и запас','',
       '| Сет | Продукты раньше→теперь, ₽ | Изменение FC, п.п. | Куриных / с лососем / с креветкой / с мидиями | Запас до40%, ₽ | Условная выгода от штучных цен |',
       '|---|---:|---:|---|---:|---:|']
for s in out:
    c=s['main_presence_roll_counts']
    md.append(f'| {s["id"]} | {rub(s["previous_food_rub"])} → {rub(s["food_cost_rub"])} | {s["raw_fc_change_vs_previous_pp"]:+.2f} | {c["chicken"]}/{c["salmon"]}/{c["shrimp"]}/{c["mussels"]} | {rub(s["raw_headroom_rub"])} | {rub(s["synthetic_customer_saving_rub"])} ₽ / {s["synthetic_customer_saving_pct"]:.2f}% |')
md += ['', 'Числа присутствия основных могут пересекаться в смешанных роллах. Курица дешевле дорогих рыбных позиций, но не всегда дешевле прежних доступных мидийных: изменение стоимости отдельных сетов может быть положительным ради разнообразия. Выгода рассчитана по проектным штучным ценам, у старых сохранён потолок399 ₽; это не опубликованная скидка.\n\nУ4A и4C условная выгода всего10 ₽ (около1%), у5L-A40 ₽ (около3%): не обещать значительную скидку. В10B нет лосося — сохранена креветочная крышка. В десятках4/4/5 куриных и4/4/3 мидийных порций; не выдавать десять порций за десять разных основных продуктов. У5H-B и10A запас до40% сырья всего8,69/11,43 ₽, ещё до неоценённых расходов заказа.','',
       '## Снижение оплаты сверх цены сета','',
       'Иллюстрация одинаковой дополнительной потери оплаты15/20%, без изменения продуктов; **это не расчёт действующей механики акций или подарков**. Такие расходы нужно раскрывать отдельно.','',
       '| Сет | FC продуктов по цене сета | Если оплата−15% | Если оплата−20% |',
       '|---|---:|---:|---:|']
for s in out:md.append(f'| {s["id"]} | {pct(s["raw_fc"])} | {pct(s["raw_fc_if_paid15pct_less"])} | {pct(s["raw_fc_if_paid20pct_less"])} |')
md += ['', '## Желаемый состав и замены целых позиций','']
for s in out:
    md += [f'### {s["id"]}: {s["name"]}', '', f'Исходный желаемый состав: {names(s["seed"])}. Продукты{rub(s["seed_food_rub"])} ₽, FC{pct(s["seed_fc"])}.', '',
           '| Место | Целая позиция → новая целая позиция | Материалы сета до→после, ₽ |', '|---|---|---:|']
    for step in s['substitutions']:
        md.append(f'| {step["slot"]} | {step["from"]} → {step["to"]} | {rub(step["before_rub"])} → {rub(step["after_rub"])} |')
md += ['', '## Составы использованных прежних карт — без изменений','',
       'На каждый прежний ролл также рис110 г и нори½листа. В таблице сохранены исходные закладки, включая105 г обычной начинки или90 г под тартаром; их не округляли и не уменьшали.', '',
       '| ID / название | Внутри | Сверху | Дополнительная жарка | Продукты, ₽ |',
       '|---|---|---|---|---:|']
for rid in sorted(set(k for s in out for k in s['final'] if not k.startswith('K'))):
    r=recipes[rid]
    md.append(f'| {rid} «{r["name"]}» | {comp(r["inner"])} | {comp(r["top"])} | {comp(r["frying"])} | {rub(r["price_reserve_cost_rub"])} |')
md += ['', '## Границы и источники','',
       '- Рецептуры/новые цены/сеты предложены агентом; они не утверждены как производственные и не опубликованы. Вкус, визуал, упаковка/доставка и дозирование не проверены. Новые съёмка/замеры/пилот не запускались.',
       '- Кальмар, консервированный тунец, корейская морковь и шампиньоны исключены. Редька не включена из-за неподтверждённой текущей цены. Угорь остаётся вне основной девятки до баланса сопутствующего соуса; это не глобальный запрет этих товаров.',
       '- ФОТ, энергия, доставка, налоги, комиссии, амортизация и оборудование не включены в стоимость продуктов/коробок. Прежние пороги прибыли500 тыс. автоматически на новый ассортимент не переносятся.',
       '- [inputs.json](inputs.json):33 карты, база цен, рецептурные предложения, прежние/новые составы и SHA-256 источников. [results.json](results.json): построчные стоимости, доли, замены и проверки. [calculate.py](calculate.py) воспроизводит результаты без живых запросов.', '']
(HERE/'report.md').write_text('\n'.join(md))
print(json.dumps({'new_recipes':len(new),'sets':len(out),'checks':len(checks),
                  'new_recipe_costs':{r['id']:r['materials_rub'] for r in new},
                  'sets_food_fc':{s['id']:[s['food_cost_rub'],s['raw_fc']] for s in out}},ensure_ascii=False))
