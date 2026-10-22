#!/usr/bin/env python3
"""Q35: add an explicit METRO radish reference to frozen Q34 set costs."""
from pathlib import Path
import hashlib,html,json
R=Path(__file__).resolve().parent
oldpath=R.parent/'2026-10-22-wasabist-new-model/results.json'
old=json.loads(oldpath.read_text())
audit=json.loads((R/'metro-price-audit.json').read_text())
price=audit['working_reference']['purchase_price_rub_per_label_kg']
assert price==189/0.6==315
sets=[]
for s in old['sets']:
 out={'id':s['id'],'name':s['name'],'price_rub':s['price_rub'],'scenarios':{}}
 for inside in ['100','110']:
  v=s['scenarios'][inside]
  q=v['unpriced_takuan_g']
  assert q==(20 if s['id']=='kalifila' else 0)
  scenarios=[]
  for y in [1,0.9,0.8]:
   add=q*price/(1000*y)
   cost=v['priced_food_subtotal_rub']+add
   scenarios.append({'assumed_ready_yield':y,'yield_measured':False,
    'yield_basis':'1.0 matches old iiko amountIn=amountOut accounting usage, not proven drain yield' if y==1 else 'sensitivity only, not a measured or owner-approved yield',
    'takuan_ready_g':q,'takuan_reference_cost_rub':add,
    'priced_ingredient_subtotal_with_metro_reference_rub':cost,
    'priced_ingredients_ratio_at_competitor_price':cost/s['price_rub'],
    'with_old_box11_proxy_rub':cost+11,
    'full_actual_food_cost_rub':None})
  out['scenarios'][inside]=scenarios
 sets.append(out)
result={'as_of':'2026-10-22','status':'conditional_external_price_reference_not_iiko_purchase',
 'price_type':'metro_reference','radish_price_rub_per_label_kg':price,
 'source_status':audit['status'],'source_sha256':{
  '../2026-10-22-wasabist-new-model/results.json':hashlib.sha256(oldpath.read_bytes()).hexdigest(),
  'metro-price-audit.json':hashlib.sha256((R/'metro-price-audit.json').read_bytes()).hexdigest()},
 'sets':sets,'unknowns':['drained/main-product mass of Hansey600g pack',
  'exact match to yellow sushi takuan','live price and availability in Teplo region',
  'sesame dose, water, part of fryer oil; packaging11/set remains a proxy'],
 'limits':['All recipes, covers, hats, prices of sets and other ingredients unchanged fromQ34.',
  'Ready radish20g costs6.30/y; accounting center y=1 is not a measured drainage yield.',
  'Historical2023 price360 is preserved inS28, not silently overwritten.',
  'No historical P&L, iiko price or production recipe changed.']}
(R/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
def rub(x): return f'{x:,.2f}'.replace(',',' ').replace('.',',')
def pct(x): return f'{100*x:.2f}'.replace('.',',')+'%'
rows=''
for s in sets:
 a,b=[s['scenarios'][k][0] for k in ['100','110']]
 rows+=f'<tr><td>{html.escape(s["name"])}</td><td>{s["price_rub"]} ₽</td><td>{rub(a["priced_ingredient_subtotal_with_metro_reference_rub"])} ₽ / {pct(a["priced_ingredients_ratio_at_competitor_price"])}</td><td>{rub(b["priced_ingredient_subtotal_with_metro_reference_rub"])} ₽ / {pct(b["priced_ingredients_ratio_at_competitor_price"])}</td></tr>'
s=next(x for x in sets if x['id']=='kalifila')
sens=''
for i,y in enumerate([1,.9,.8]):
 a,b=[s['scenarios'][k][i] for k in ['100','110']]
 sens+=f'<tr><td>{int(y*100)}% (допущение)</td><td>{rub(a["takuan_reference_cost_rub"])} ₽</td><td>{rub(a["priced_ingredient_subtotal_with_metro_reference_rub"])} / {rub(b["priced_ingredient_subtotal_with_metro_reference_rub"])} ₽</td><td>{pct(a["priced_ingredients_ratio_at_competitor_price"])} / {pct(b["priced_ingredients_ratio_at_competitor_price"])}</td></tr>'
page='''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>METRO: маринованная редька и обновление сетов · 22.10.2026</title><style>body{font:16px/1.5 system-ui;max-width:1080px;margin:30px auto;padding:0 20px;color:#18282b;background:#faf9f5}table{border-collapse:collapse;width:100%;background:white}td,th{padding:12px;border:1px solid #d0d8d7;text-align:left}th{background:#e7efeb}.note{padding:16px;background:#fff1ce}a{color:#12625b}h1{line-height:1.2}h2{margin-top:32px}.scroll{overflow-x:auto}</style>
<h1>Маринованная редька: ориентир METRO</h1><p>Проверка22.10.2026. По прямому правилу владельца METRO используется для предварительных продуктовых калькуляций, когда актуальной цены iiko нет.</p>
<p><b>Редька Hansey маринованная,600 г —189 ₽/упаковка;315 ₽/кг указанной фасовки.</b> Источник: <a href="https://online.metro-cc.ru/category/bakaleya/kuhni-mira">официальный каталог METRO</a>. Это обычная не перечёркнутая цена в поисковой копии прошлого месяца с московским заголовком. Живая страница вернула ошибку переадресации; актуальные цена и наличие для нашего региона не подтверждены. Кэшированные акции179/159 ₽ отдельно, в базу не взяты.</p>
<div class="note">Масса редьки без рассола и точное соответствие жёлтому суши-такуану не установлены.315 ₽ — за килограмм указанной фасовки. Это внешний ориентир, не подтверждённая закупка Тепла и не цена килограмма слитой готовой начинки.</div>
<h2>Что меняется в расчёте сетов</h2><p>В «Калифиле»20 г редьки. При учётном списании как в прежней карте(amountIn=amountOut), без отдельного увеличения на слив, закладка стоит<b>6,30 ₽</b>. Для планирования это явно условный выход100%, а не физический замер. Остальные продукты/раскладки/цены сетов сохранены изQ34:110 г риса,100/110 г внутри,50 г рыбной крышки,75 г яки на роллах с соусной шапкой; сыр+30%, покупной краб450 ₽/кг.</p>
<div class="scroll"><table><thead><tr><th>Сет</th><th>Цена конкурента</th><th>Продукты / FC при100 г внутри</th><th>Продукты / FC при110 г внутри</th></tr></thead><tbody>ROWS</tbody></table></div>
<p>Это оценённые продукты с условной ценой METRO. Вода, кунжут и часть фритюрного масла по-прежнему не оценены. Коробка11 ₽/сет отдельно как старый ориентир без проверки вместимости; гарниры/пакеты/труд/доставка/налоги/оборудование сюда не включены.</p>
<h2>Как влияет слив: только чувствительность</h2><p>Стоимость20 г готовой редьки =6,30 ₽÷доля выхода. Ни один выход ниже не измерен;100% — только учётный центр,90/80% — сценарии чувствительности.</p><div class="scroll"><table><thead><tr><th>Выход после слива</th><th>Редька20 г</th><th>Калифила: продукты100 /110</th><th>FC100 /110</th></tr></thead><tbody>SENS</tbody></table></div>
<h2>Правило для следующих расчётов</h2><p>Сначала актуальная закупка iiko нужного товара/склада. Если её нет или цена явно устарела — официальный METRO как датированный ориентир сопоставимого продукта с фасовкой, регионом, единицей и статусом проверки. Ориентир участвует в предварительной калькуляции, но не переписывает историческую себестоимость или iiko. Новая подтверждённая закупка заменит его в новом расчёте.</p>
<p><a href="../../../../skills/teplo-unit-economics/references/ingredient-price-fallback.md">Правило METRO в навыке</a> · <a href="metro-price-audit.json">Источники и ограничения</a> · <a href="results.json">Числа и чувствительность</a> · <a href="calculate.py">Расчёт</a> · <a href="../2026-10-22-wasabist-new-model/report.html">СоставыQ34</a> · <a href="../../profit-progress.md">ЖурналQ35</a></p></html>'''
(R/'report.html').write_text(page.replace('ROWS',rows).replace('SENS',sens))
for x in sets:
 print(x['name'],[(k,round(x['scenarios'][k][0]['priced_ingredient_subtotal_with_metro_reference_rub'],2),round(100*x['scenarios'][k][0]['priced_ingredients_ratio_at_competitor_price'],2)) for k in ['100','110']])
