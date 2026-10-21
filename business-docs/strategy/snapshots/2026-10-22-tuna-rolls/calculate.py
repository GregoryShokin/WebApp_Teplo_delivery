"""Dated METRO tuna scenario; ready filling/yield and application roles explicit."""
import copy, hashlib, json, math
from pathlib import Path
P=Path(__file__).resolve().parent
inp=json.loads((P/'inputs.json').read_text())
for rel,sha in inp['source_sha256'].items():
 assert hashlib.sha256((P.parent/rel).read_bytes()).hexdigest()==sha,rel
comp=json.loads((P.parent/inp['components_source']).read_text())['components']
checks=[]
def check(label,ok):
 assert ok,label
 checks.append({'check':label,'status':'PASS'})
def calc(recipe,price,yield_ratio,hat_g):
 lines=[]
 for stage in ['base','inner','sauce_hat','drizzle','frying']:
  for k,qty in recipe[stage].items():
   q=hat_g*qty/sum(recipe[stage].values()) if stage=='sauce_hat' else qty
   unitcost=price/(1000*yield_ratio) if k=='tuna_plain' else comp[k]['cost_per_sheet' if k=='nori' else 'cost_per_g']
   lines.append({'stage':stage,'component':k,'amount':q,'unit':'sheet' if k=='nori' else 'g_ready','unit_cost_rub':unitcost,'cost_rub':q*unitcost})
 return sum(x['cost_rub'] for x in lines),lines
rows=[]
for r in inp['new_recipes']:
 check(r['id']+' inner100',sum(r['inner'].values())==100)
 check(r['id']+' tuna40',r['inner']['tuna_plain']==40)
 check(r['id']+' minimum primary30%',r['inner']['tuna_plain']/sum(r['inner'].values())>=.3)
 if 'crab_mix' in r['inner']:
  check(r['id']+' crab itself>=30%',r['inner']['crab_mix']*.8/sum(r['inner'].values())>=.3)
  check(r['id']+' no cheese with crab',not {'cream_cheese','mozzarella'}&set(r['inner']))
 check(r['id']+' rice110/nori0.5',r['base']=={'rice':110,'nori':.5})
 check(r['id']+' allowed hats only',set(r['sauce_hat'])<={'yaki','spice','cheese_sauce','black_eel'})
 check(r['id']+' drizzles separate',set(r['drizzle'])<={'teriyaki','unagi'})
 check(r['id']+' no unauthorized tuna covers or tartars',set(r['sauce_hat'])&{'tuna_plain'}==set())
 main,lines=calc(r,inp['tuna_price_base_rub_kg'],inp['yield_base_assumed'],75)
 rawmin=main/.35
 rounded=math.ceil((rawmin-1e-10)/5)*5
 suggested=min(rounded,399) if rawmin<=399 else None
 if suggested:check(r['id']+' price<=399 and FC<=35',suggested<=399 and main/suggested<=.35+1e-10)
 variants=[]
 for label,price in inp['tuna_price_scenarios_rub_kg'].items():
  for y in inp['yield_scenarios_assumed']:
   for hat in inp['hat_scenarios_g']:
    amount,_=calc(r,price,y,hat)
    variants.append({'tuna_price_scenario':label,'tuna_price_rub_kg':price,'assumed_yield':y,'hat_g_if_present':hat,'food_subtotal_rub':amount,'fc_at399':amount/399,'fc_at_base_proposed_price':amount/suggested if suggested else None,'unrounded_price_for35':amount/.35})
 check(r['id']+' sensitivity monotonic yield',calc(r,1499,.8,75)[0]>=main>=calc(r,1499,.95,75)[0])
 check(r['id']+' sensitivity monotonic hat',calc(r,1499,.9,70)[0]<=main<=calc(r,1499,.9,80)[0])
 rows.append({'recipe':r,'food_subtotal_rub':main,'lines':lines,'unrounded_price_for35':rawmin,'proposed_menu_price_rub':suggested,'fc_at_proposed_price':main/suggested if suggested else None,'fc_at399':main/399,'sensitivity':variants,'unpriced':['sesame_if_used','water_in_rice','packaging_and_order_sidekit','process_specific_energy_labour_equipment'],'status':'new_paper_candidate_not_production_recipe'})
out={'as_of':'2026-10-22','status':'conditional_ingredient_costing_not_confirmed_procurement_or_final_menu','new_recipes':rows,'source_sha256':inp['source_sha256'],'limits':inp['limits'],'checks':checks}
(P/'results.json').write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n')
M=lambda x:f'{x:.2f}'.replace('.',',')
F=lambda x:f'{x*100:.2f}%'.replace('.',',')
names={'tuna_plain':'обычный тунец','cream_cheese':'творожный сыр','crab_mix':'крабовая смесь80/20','cucumber':'огурец','tamago':'тамаго','mozzarella':'моцарелла','yaki':'яки','cheese_sauce':'сырный','spice':'спайси','teriyaki':'терияки','unagi':'унаги','batter':'кляр','crumbs':'сухари','oil':'масло'}
def grams(xs):return ', '.join(f'{names.get(k,k)} {v:g} г' for k,v in xs.items()) or 'нет'
md=['# Обычный тунец: новая ветка роллов — 22.10.2026','','Обычный некопчёный тунец включён владельцем в основные ингредиенты. Консервированный тунец остаётся исключённым. Ни прежние карты, ни сеты, ни цены не изменены. Ниже шесть новых проектов, не утверждённые техкарты.','','## Цена METRO и границы источника','',
'Для базового сценария — **1 499 ₽/кг покупного обычного замороженного филе без кожи/костей**, арт.271602, около1,5 кг. Это цена доступной поисковой копии официальной карточки (индексирована примерно за3 недели до чтения), а не подтверждённая котировка22.10 для Волгодонска. Прямой запрос карточки дал цикл перенаправлений. Акционную цену1 049 ₽ из старой категории не использовали. Регион, фактическая доступность и доставка не подтверждены.',
'', '[Официальная карточка METRO](https://online.metro-cc.ru/products/file-tuntsa-svezhemorozhenoe-bez-kozhi-bez-kosti-zamorozhennoe-1-2kg). Единица — килограмм, не вся упаковка1,5 кг. В карточке отдельно показаны условия ТЦ1 329 ₽/кг и1 199 ₽/кг от2 кг; они не подставлены как базовая цена онлайн.',
'', 'Сравнения из официальных карточек: Вкусарт500 г —789 ₽/упаковку=1 578 ₽/кг; Taishan СакуААА —2 789 ₽/кг. Форматы/качество не объявлены равнозначными; дорогой Саку — отдельный ценовой сценарий. [Полный аудит METRO](metro-price-audit.json).',
'', '## Что заложено в расчёт','',
'- Рис110 г и нори½листа; **100 г внутренней начинки, в том числе40 г подготовленного тунца**. Это проект целой подготовленной порции, не количество измеренных кусочков и не утверждённая доза.',
'- Фактический выход обычного тунца неизвестен. **90% — только условие базового расчёта**, дополнительно80% и95%. Стоимость40 г =1 499 ×0,04 /0,90 =66,62 ₽. Цена подготовленного тунца1 665,56 ₽/кг лишь при таком выходе. Потери лосося или копчёного тунца не переносились.',
'- Остальные продукты — та же зафиксированная база21.10: сыр+30%, покупной краб450 ₽/кг, готовая смесь80/20, прежние выходы заготовок применены один раз.',
'- Шапка — **75 г всей готовой смеси** для сравнения, диапазон70–80 г. Поливка отдельно: прежний условный расход5 г. Кунжут отдельно: цена285 ₽/кг известна, порция/применимость не заданы; затраты неизвестны, не нулевые.',
'- Жарка — прежние бумажные нормы: кляр25 г, сухари15 г, масло5 г; это не измеренный расход фритюра или вес готового блюда. Холодный/запечённый/жареный формат не утверждает конкретную обработку тунца; её выход, процесс и пригодность продукта должны быть определены до производственной карты.',
'- Тунец внутри; новая тунцовая крышка или тартар не добавлялись. У крабовых T2/T5 самого краба32 г, внутри нет сыра. В T6 новая конструкция без шапки; это самостоятельный кандидат, не уменьшение утверждённой позиции.',
'', '## Шесть новых проектов','',
'Материалы ниже без воды/кунжута/упаковки и прочих неоценённых расходов. Цена — предложение из FC≤35% **до акций**, округление вверх до5 ₽ с соблюдением потолка399 при условииFC≤35%.',
'', '| ID / группа / название | Внутренняя начинка, г | Шапка | Поливка | Продукты, ₽ | Предложенная цена, ₽ | FC до акций |',
'|---|---|---|---|---:|---:|---:|']
for x in rows:
 r=x['recipe'];md.append(f'| {r["id"]} / {r["group"]} / {r["name"]} | {grams(r["inner"])} | {grams(r["sauce_hat"])} | {grams(r["drizzle"])} | {M(x["food_subtotal_rub"])} | {x["proposed_menu_price_rub"]} | {F(x["fc_at_proposed_price"])} |')
md += ['', 'При цене399 ₽ эти шесть дают **24,65–32,22% сырьевого FC** только в базовом сценарии. Это не результат по сетовым ценам или после акций.',
'', '## Чувствительность к выходу и цене','',
'Те же составы и шапки75 г; меняется только неизвестный выход. При снижении выхода базовые предложенные цены могут уже не держать35%.',
'', '| Ролл | Продукты при выходе95 /90 /80%, ₽ | FC при399 и выходе80% | При Саку2 789 ₽/кг и выходе90%, ₽ /FC при399 |','|---|---:|---:|---:|']
for x in rows:
 r=x['recipe'];values=[calc(r,1499,y,75)[0] for y in [.95,.9,.8]];premium=calc(r,2789,.9,75)[0]
 md.append(f'| {r["id"]} | '+ ' / '.join(M(v) for v in values)+f' | {F(values[-1]/399)} | {M(premium)} /{F(premium/399)} |')
md += ['', 'При Саку2 789 ₽/кг все шесть в этой модели превышают35% даже по цене399 ₽. Поэтому выбор конкретного продукта/ценового уровня важнее косметической подгонки граммов. При базовом филе и условном выходе80% даже со шапкой80 г все шесть остаются≤35% по399 ₽, **до неоценённых компонентов**. Сеты не пересобирались.',
'', 'Тунец здесь — дополнительная вкусовая ветка средней стоимости; с базовым условием он дороже курицы и дешевле используемой сёмги за готовый грамм. Это не доказательство вкуса, спроса или взаимозаменяемости продуктов.',
'', '## Воспроизведение и неизвестные','',
'[Входы и хеши](inputs.json), [построчные калькуляции и сценарии](results.json), [расчёт](calculate.py), [локальный аудит тунца](local-tuna-audit.json), [проверки](validation.json). Неизвестны реальный выход/обработка, наличие/цена выбранного магазина, порционность и вкус, расход посыпки/жарки/упаковки, оплаченный микс/акции. Не выполнялись закупка, записьiiko, публикация меню или новый кухонный замер.','']
(P/'report.md').write_text('\n'.join(md))
print(json.dumps({'recipes':len(rows),'checks':len(checks),'food_range':[min(r['food_subtotal_rub'] for r in rows),max(r['food_subtotal_rub'] for r in rows)],'price_range':[min(r['proposed_menu_price_rub'] for r in rows),max(r['proposed_menu_price_rub'] for r in rows)]},ensure_ascii=False))
