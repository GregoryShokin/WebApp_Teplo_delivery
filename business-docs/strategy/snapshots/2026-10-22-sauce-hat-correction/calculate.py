"""Explicit sauce application roles and 70/75/80g sensitivity; no source-card writes."""
import copy
import hashlib
import json
import math
from collections import Counter
from pathlib import Path

HERE=Path(__file__).resolve().parent
config=json.loads((HERE/'inputs.json').read_text())
for relative,h in config['source_sha256'].items():
    assert hashlib.sha256((HERE.parent/relative).read_bytes()).hexdigest()==h,relative
baseline_inputs=json.loads((HERE.parent/config['baseline_inputs']).read_text())
baseline=json.loads((HERE.parent/config['baseline_results']).read_text())
components=baseline_inputs['components']
recipes={r['id']:r for r in baseline_inputs['existing_recipes']+baseline['new_recipes']}
sets=baseline['sets']
used=sorted(set(k for s in sets for k in s['final']))
checks=[]


def check(name,value):
    assert value,name
    checks.append({'check':name,'status':'PASS'})


def cost(r):
    return sum(q*components[k]['cost_per_sheet' if k=='nori' else 'cost_per_g']
               for stage in ('base','inner','top','frying') for k,q in r.get(stage,{}).items())


variants={}
for rid in used:
    src=recipes[rid]
    original_cost=src.get('materials_rub',src.get('price_reserve_cost_rub'))
    check(rid+' baseline cost',abs(cost(src)-original_cost)<1e-8)
    classified=config['application_roles'][rid]
    check(rid+' classification covers every original top component',
          set().union(*(set(v) for v in classified.values()))==set(src['top']))
    check(rid+' roles are disjoint',sum(len(v) for v in classified.values())==len(src['top']))
    rows={}
    for g in config['hat_mass_scenarios_g']:
        r=copy.deepcopy(src)
        hat=classified['sauce_hat']
        original_hat_mass=sum(hat.values())
        if hat:
            for k,q in hat.items():r['top'][k]=g*q/original_hat_mass
            check(rid+f' total ready hat {g}g',abs(sum(r['top'][k] for k in hat)-g)<1e-8)
        check(rid+f' inner/base/frying unchanged {g}',all(r.get(k,{})==src.get(k,{}) for k in ('inner','base','frying')))
        check(rid+f' no new top ingredient {g}',set(r['top'])==set(src['top']))
        check(rid+f' drizzle unchanged {g}',all(r['top'][k]==q for k,q in classified['drizzle'].items()))
        check(rid+f' fish/tartar/other unchanged {g}',all(r['top'][k]==q for role in ('fish_cover','tartar_top','other') for k,q in classified[role].items()))
        rows[str(g)]={'materials_subtotal_ex_unknown_sesame_rub':cost(r),
                      'sauce_hat':{k:r['top'][k] for k in hat},
                      'drizzle':classified['drizzle'],'fish_cover':classified['fish_cover'],
                      'tartar_top':classified['tartar_top'],'other':classified['other']}
    price=src.get('project_menu_price_rub',min(src.get('menu_price_rub',0),399))
    middle=rows['75']['materials_subtotal_ex_unknown_sesame_rub']
    variants[rid]={'id':rid,'name':src['name'],'group':src['group'],
                   'old_materials_rub':original_cost,'source_top':src['top'],
                   'application_roles':classified,'interpretation_status':'explicit_paper_scenario_not_verified_production_application',
                   'scenarios':rows,'previous_project_single_price_rub':price,
                   'fc_at_old_project_price75':middle/price,
                   'minimum_price_for35_at75_ex_unknown_sesame_rub':math.ceil((middle/.35-1e-10)/5)*5,
                   'sesame_g':None,'sesame_cost_rub':None}

set_rows=[]
for s in sets:
    row={'id':s['id'],'name':s['name'],'price_rub':s['price'],'composition':s['final'],
         'old_food_rub':s['food_cost_rub'],'old_food_fc':s['raw_fc'],
         'boxes_rub':s['boxes_cost_rub'],'scenarios':{}}
    check(s['id']+' max2 same recipe',max(Counter(s['final']).values())<=2)
    for g in config['hat_mass_scenarios_g']:
        amount=sum(variants[k]['scenarios'][str(g)]['materials_subtotal_ex_unknown_sesame_rub'] for k in s['final'])
        row['scenarios'][str(g)]={'food_rub':amount,'raw_fc':amount/s['price'],
             'food_plus_boxes_rub':amount+s['boxes_cost_rub'],
             'food_plus_boxes_fc':(amount+s['boxes_cost_rub'])/s['price'],
             'raw_headroom_to_previous40_rub':s['price']*.4-amount,
             'within_previous_raw40_before_sesame':amount<=s['price']*.4+1e-8}
    row['increase_rub_at75']=row['scenarios']['75']['food_rub']-s['food_cost_rub']
    set_rows.append(row)

result={'as_of':'2026-10-22','status':'correction_of_paper_hat_assumptions_not_production_recipe_change',
        'owner_hat_range_g':[70,80],'central75_is_analytical_assumption_not_fixed_recipe':True,
        'application_roles':config['application_roles'],
        'recipe_scenarios':variants,'new_chicken_recipe_ids':['K1','K2','K3','K4','K5','K6'],
        'sets_same_composition_and_prices':set_rows,
        'sesame':config['sesame'],'source_sha256':config['source_sha256'],'checks':checks,
        'limits':config['limits']}
(HERE/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')

def money(v):return f'{v:.2f}'.replace('.',',')
def pct(v):return f'{v*100:.2f}%'.replace('.',',')
names={'spice':'Spice','yaki':'яки','cheese_sauce':'сырный соус','black_eel':'«Чёрный угорь»',
       'teriyaki':'терияки','unagi':'унаги','salmon_plain':'лосось','shrimp':'креветка',
       'salmon_smoked':'копчёный лосось','tartar_plain':'лососевый тартар',
       'tartar_smoked':'тартар из копчёного лосося','masago':'масаго'}
def gram(items):return ', '.join(f'{names.get(k,k)} {q:g} г' for k,q in items.items()) or '—'

md=['# Соусная шапка, поливка и исправление калькуляций — 22.10.2026','',
    'Владелец уточнил: **соусная шапка — яки, сырный, Spice или «Чёрный угорь», ориентир 70–80 г всей готовой смеси на целый ролл**. Терияки — только тонкая поливка. Прежняя колонка «сверху» смешивала разные способы оформления, а 20–35 г для соусных шапок были ошибочным допущением агента.','',
    '| Отдельная колонка | Значение | Масса / статус |','|---|---|---|',
    '| Начинка | Внутренние компоненты | Прежние закладки без изменений; 30% считают только внутри |',
    '| Соусная шапка | Яки / сырный / Spice / «Чёрный угорь» | Ориентир 70–80 г **всей** смеси; 75 г только центр расчёта, не утверждённая доза |',
    '| Рыбная крышка | Прежнее покрытие лососем/креветкой/угрём | Прежние 50 г; не соусная шапка |',
    '| Тартарный верх | Прежняя готовая лососевая смесь | Прежний отдельный диапазон 62–80 г; в исходных вариантах 70 г. Новым словарём не отменён |',
    '| Поливка | Тонкая струя терияки; унаги по прежнему решению | Старые 5 г оставлены только расчётным proxy, не подтверждённой нормой владельца |',
    '| Посыпка | Кунжут; где предусмотрено — иное оформление вроде масаго | Кунжут 285 ₽/кг=0,285 ₽/г; масса не задана, затраты не нулевые, а неизвестные |','',
    '## Что именно пересчитано','',
    '**Составы девяти сетов, цены и внутренние закладки фиксированы.** Исходные карты/файлы Q22/Q24/Q29 и iiko не менялись. Выпущена отдельная корректировка бумажных допущений о наружном слое, без скрытых замен дорогих роллов на дешёвые.','',
    'Для этой чувствительности наружные яки/сырный/Spice/«Чёрный угорь» в используемых бумажных картах явно отнесены к соусным шапкам. Для K5/K6 и других жареных с таким соусом это **указанная рабочая классификация**, а не доказанный способ подачи. Вариант без шапки был бы другой конструкцией. К пустому верху шапка не добавлялась, терияки не увеличивался до 75 г, рыба/тартар/масаго не увеличивались. При смеси двух соусов диапазон относится к общей массе, не к каждому отдельно.','',
    '## Новые куриные роллы','',
    'Ниже стоимость продуктов без пока неизвестной массы кунжута и прочих прежних неоценённых расходов. Диапазон — чувствительность нормы шапки, не производственный допуск.','',
    '| Ролл | Соусная шапка | Поливка | Раньше, ₽ | При 70 / 75 / 80 г шапки, ₽ | FC при 75 г и прежней проектной штучной цене |',
    '|---|---|---|---:|---:|---:|']
for rid in result['new_chicken_recipe_ids']:
    v=variants[rid]
    h=', '.join(names[k] for k in v['application_roles']['sauce_hat']) or 'нет'
    costs=' / '.join(money(v['scenarios'][str(g)]['materials_subtotal_ex_unknown_sesame_rub']) for g in (70,75,80))
    md.append(f'| {rid} «{v["name"]}» | {h} | {gram(v["application_roles"]["drizzle"])} | {money(v["old_materials_rub"])} | {costs} | {pct(v["fc_at_old_project_price75"])} при {v["previous_project_single_price_rub"]} ₽ |')
md += ['', 'При 75 г нижний ориентир проектной цены для FC≤35% без ещё неизвестного кунжута: '+', '.join(f'{k} — {variants[k]["minimum_price_for35_at75_ex_unknown_sesame_rub"]} ₽' for k in result['new_chicken_recipe_ids'])+'. Это справочный расчёт, не переназначение цен.','',
       '## Те же сеты и цены','',
       '| Сет | Цена, ₽ | Раньше продукты, ₽ | При 70 / 75 / 80 г шапок, ₽ | FC продуктов при 75 г | С коробками при 75 г, ₽ /FC |',
       '|---|---:|---:|---:|---:|---:|']
for s in set_rows:
    m=s['scenarios']['75']
    costs=' / '.join(money(s['scenarios'][str(g)]['food_rub']) for g in (70,75,80))
    md.append(f'| {s["id"]} | {s["price_rub"]} | {money(s["old_food_rub"])} | {costs} | {pct(m["raw_fc"])} | {money(m["food_plus_boxes_rub"])} /{pct(m["food_plus_boxes_fc"])} |')
failed=[s['id'] for s in set_rows if not s['scenarios']['75']['within_previous_raw40_before_sesame']]
md += ['', f'При 75 г **{len(failed)} из 9** наборов выше прежнего рабочего ориентира 40% сырья: '+', '.join(failed)+'. 4A проходит с запасом всего около 0,33 ₽ до кунжута и при 80 г уже не проходит. 5L-A при 75 г проходит по продуктам, но после коробок также выше 40%. Сам ориентир 40% не принят здесь как новая норма владельца.','',
       'Прежние выводы Q29 о FC 38–39% нельзя использовать как актуальную экономику при таких шапках. Все суммы до дополнительных скидок/бонусов, без неоценённых воды, пакета и гарниров, ФОТ/доставки/налогов/энергии/оборудования и других расходов заказа. Кунжут не признан бесплатным: в каждой нужной карте его расход ещё должен быть задан физической порцией.','',
       '## Явная классификация оформления в использованных картах','',
       '| ID | Соусная шапка: прежняя масса → сценарий | Поливка | Рыбная крышка | Тартарный верх | Прочее |',
       '|---|---|---|---|---|---|']
for rid in used:
    a=config['application_roles'][rid]
    h=gram(a['sauce_hat'])+' → 70 / 75 / 80 г общей смеси' if a['sauce_hat'] else '—'
    md.append(f'| {rid} | {h} | {gram(a["drizzle"])} | {gram(a["fish_cover"])} | {gram(a["tartar_top"])} | {gram(a["other"])} |')
md += ['', '## Источники и статус','',
       '- Новое уточнение владельца 22.10: названия/вид шапки, ориентир 70–80 г, терияки как тонкая струя, кунжут как обычная посыпка. 75 г, прежние 5 г поливки и классификация неуточнённых способов подачи — явно обозначенные допущения для сравнения.',
       '- [Входы/карта ролей/хеши](inputs.json), [калькуляции](results.json), [проверка кунжута](sesame-audit.json), [воспроизведение](calculate.py). Книга цен 21.10 сохранена, кунжут — проведённая закупка 01.10.2026.',
       '- [Предыдущая версия Q29](../2026-10-22-chicken-rolls-and-sets/report.md) остаётся историей ошибочного допущения. Новая версия исправляет расчёт; не утверждает производственные карты, не запускает кухонный замер и не пересобирает сеты автоматически.', '']
(HERE/'report.md').write_text('\n'.join(md))
print(json.dumps({'recipes_classified':len(used),'checks':len(checks),'sets_raw_fc75_range':[min(s['scenarios']['75']['raw_fc'] for s in set_rows),max(s['scenarios']['75']['raw_fc'] for s in set_rows)],'sets_above40_at75':failed},ensure_ascii=False))
