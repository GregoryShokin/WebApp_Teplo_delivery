"""Three competitor-set analogs: frozen costs, explicit recipe matching and hats."""
import copy,hashlib,html,json
from pathlib import Path
P=Path(__file__).resolve().parent
cfg=json.loads((P/'inputs.json').read_text())
for f,h in cfg['source_sha256'].items():assert hashlib.sha256((P/f).read_bytes()).hexdigest()==h,f
A=json.loads((P/'teplo-analogs.json').read_text());C={x['product_id']:x for x in A['current_components']};R={x['name']:x for x in A['reference_cards']};G=cfg['model_guids'];checks=[]
def ck(label,ok):
 assert ok,label
 checks.append({'check':label,'status':'PASS'})
def evaluate(m,basis,hat,light=False,crabscale=1):
 source=R[m['light_sensitivity_source_name']] if light else R[m['reference_card_name']]
 if light:
  q={x['product_id']:x['amount_in_g_if_kg'] if x['source_unit']=='кг' else x['amount_in'] for x in source['lines']}
  cuc='189d8c97-a386-4977-ab8f-d3a5282e356a'
  if m['id']=='P':q.pop(cuc)
  elif m['id']=='Z':q[G['takuan']]=q.pop(cuc);q['cb4ed412-ef0b-49c6-ad99-de69553b3238']=10;q['a9eb0f18-d512-4053-aeee-19c2034e42fb']=10
 else:q=copy.deepcopy(m['reference_quantities'])
 k=m['competitor_standalone_weight_g']/source['W_teplo_portion_g'] if m['competitor_standalone_weight_g'] else crabscale
 qty={g:n if g==G['rice'] else n*k for g,n in q.items()}
 if G['yaki'] in qty and hat!='scaled_source_hat':qty[G['yaki']]=hat
 lines=[]
 for g,n in qty.items():
  c=C[g];u=c['main_unit'];unitcost=None if g==G['takuan'] else c['priced_subtotal_ex_missing_per_baseunit']/(1 if u=='шт' else 1000)
  if basis=='future' and g in cfg['future_price_overrides_per_ready_g']:unitcost=cfg['future_price_overrides_per_ready_g'][g]
  lines.append({'guid':g,'component':c['name'].strip(),'amount':n,'unit':'sheet' if u=='шт' else 'g_input_ready_component','unit_cost_rub':unitcost,'priced_cost_rub':n*unitcost if unitcost is not None else None,'unpriced_leaves':c['missing']})
 total=sum(l['priced_cost_rub'] for l in lines if l['priced_cost_rub'] is not None)
 h=cfg['radish_historical_example'];historical_radish=qty.get(G['takuan'],0)*h['purchased_price_rub_kg']/(1000*h['assumed_drained_yield']) if G['takuan'] in qty else None
 return {'reference_name':source['name'],'reference_chart_id':source['chart_id'],'reference_date_from':source['chart_date_from'],'W_reference_g':source['W_teplo_portion_g'],'W_competitor_standalone_g':m['competitor_standalone_weight_g'],'k':k,'lines':lines,'priced_food_subtotal_rub':total,'unpriced_component_guids':[g for g in qty if g==G['takuan']],'takuan_ready_g':qty.get(G['takuan']),'historical_takuan_example_only_rub':historical_radish,'finished_modeled_weight_g':None,'mass_gap_g':None,'input_component_sum_g_ex_nori_not_finished_weight':sum(n for g,n in qty.items() if C[g]['main_unit']=='кг'),'yaki_g':qty.get(G['yaki'],0),'fixed_rice_g':qty[G['rice']]}
rows={}
for m in cfg['models']:
 rows[m['id']]={'name':m['name'],'adaptations':m['adaptation_log'],'notes':m['notes'],'scenarios':{}}
 for basis in ['current','future']:
  for h in cfg['hat_modes']:
   v=evaluate(m,basis,h);rows[m['id']]['scenarios'][basis+'_'+str(h)]=v
   ck(m['id']+basis+str(h)+' fixed rice',v['fixed_rice_g']==m['reference_quantities'][G['rice']])
   ck(m['id']+basis+str(h)+' no invented hat',bool(v['yaki_g'])==(G['yaki'] in m['reference_quantities']))
   if h!='scaled_source_hat' and v['yaki_g']:ck(m['id']+basis+str(h)+' whole hat mass',v['yaki_g']==h)
   ck(m['id']+basis+str(h)+' unknown finished mass',v['finished_modeled_weight_g'] is None and v['mass_gap_g'] is None)
 if m['light_sensitivity_source_name']:
  rows[m['id']]['light_reference_sensitivity_current75']=evaluate(m,'current',75,light=True)
 ck(m['id']+' price increase nonnegative',rows[m['id']]['scenarios']['future_75']['priced_food_subtotal_rub']>=rows[m['id']]['scenarios']['current_75']['priced_food_subtotal_rub']-1e-8)
sets=[]
for s in cfg['sets']:
 v=copy.deepcopy(s);v['scenarios']={}
 for basis in ['current','future']:
  for h in cfg['hat_modes']:
   key=basis+'_'+str(h);items=[rows[k]['scenarios'][key] for k in s['rolls']];total=sum(x['priced_food_subtotal_rub'] for x in items);tq=sum(x['takuan_ready_g'] or 0 for x in items);hist=sum(x['historical_takuan_example_only_rub'] or 0 for x in items)
   v['scenarios'][key]={'priced_food_subtotal_rub':total,'priced_subtotal_fc_at_competitor_price':total/s['price_rub'],'unpriced_takuan_g':tq if tq else None,'historical_takuan_example_only_rub':hist if tq else None,'food_subtotal_plus_historical_takuan_example_rub':total+hist if tq else None,'boxes_proxy_rub':cfg['box_proxy_rub_per_set'],'priced_food_plus_box_proxy_rub':total+cfg['box_proxy_rub_per_set'],'input_component_sum_g_ex_nori_not_finished_weight':sum(x['input_component_sum_g_ex_nori_not_finished_weight'] for x in items),'finished_modeled_set_weight_g':None}
 v['light_cover_sensitivity_current75']={'priced_food_subtotal_rub':sum((rows[k].get('light_reference_sensitivity_current75') or rows[k]['scenarios']['current_75'])['priced_food_subtotal_rub'] for k in s['rolls']),'status':'whole alternate reference recipe, not1–3g trimming; no source card changes'}
 if s['id']=='four':v['unknown_warm_crab_size_sensitivity_current75']={str(k):sum(rows[x]['scenarios']['current_75']['priced_food_subtotal_rub'] for x in s['rolls'] if x!='K')+evaluate(next(x for x in cfg['models'] if x['id']=='K'),'current',75,crabscale=k)['priced_food_subtotal_rub'] for k in [.8,1,1.2]}
 ck(s['id']+'4portions',len(s['rolls'])==4)
 ck(s['id']+' hat mass monotonic',v['scenarios']['current_70']['priced_food_subtotal_rub']<=v['scenarios']['current_75']['priced_food_subtotal_rub']<=v['scenarios']['current_80']['priced_food_subtotal_rub'])
 sets.append(v)
ck('no yaki on seared Philadelphia',rows['Z']['scenarios']['current_75']['yaki_g']==0)
ck('crab weight not derived',next(x for x in cfg['models'] if x['id']=='K')['competitor_standalone_weight_g'] is None)
out={'as_of':cfg['as_of'],'pricebook_as_of':cfg['pricebook_as_of'],'label':'ingredient cost of Teplo analogs, not competitor costs or exact copy weights','rolls':rows,'sets':sets,'limits':cfg['limits'],'checks':checks,'source_sha256':cfg['source_sha256']}
(P/'results.json').write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n')
E=html.escape
M=lambda n:f'{n:.0f}'
F=lambda n:f'{n*100:.1f}%'.replace('.',',')
def amount(n,unknown):return M(n)+(' + такуан' if unknown else '')
body=['<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Три сета WASABIST — оценка по ценам Тепла</title><style>body{font:16px/1.5 system-ui,sans-serif;background:#f5f4ef;color:#202923;margin:0}main{max-width:1180px;margin:auto;padding:36px 24px}h1{font-size:30px;line-height:1.2}h2{margin-top:34px}table{border-collapse:collapse;width:100%;background:white;margin:16px 0;font-size:14px}td,th{text-align:left;padding:11px 12px;border-bottom:1px solid #ddd;vertical-align:top}th{background:#e7ece6}.note{background:#fff6d9;padding:16px;border-radius:9px}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:18px}.card{background:white;border-radius:10px;overflow:hidden}.card img{width:100%;display:block;background:black}.card p,.card h3{margin:14px}small{color:#57625b}code{font-size:13px}.num{white-space:nowrap}</style><main><h1>Сколько стоили бы Теплу три аналога сетов WASABIST</h1><p>22.10.2026 · Волгодонск · закупочная база 21.10 · оценка продуктов, не полной себестоимости и не чужой прибыли.</p><div class="note"><b>Все видимые соусные шапки оценены как яки по уточнению владельца.</b> Основная таблица — наша условная подача по75 г на целый ролл с шапкой, диапазон70–80 г. Масса не измерена у конкурента. У запечённой Филадельфии сверху обожжённая рыба: яки к ней не добавлен.</div><h2>Три набора</h2><div class="cards">']
ld=json.loads((P/'live-dom.json').read_text());images={s['name']:s['url'] for s in ld['set_photos']}
for s in sets:
 key=next(k for k in images if ('ЧЕТЫРЕ' in k and s['id']=='four') or ('ОКЕАН'==k and s['id']=='ocean') or ('КАЛИФИЛА'==k and s['id']=='kalifila'))
 body.append(f'<section class="card"><img src="{E(images[key])}" alt="{E(s["name"])}"><h3>{E(s["name"])}</h3><p>{s["price_rub"]} ₽ ·32 кусочка'+(' ·850 г по карточке' if s['id']=='four' else ' ·вес сета не опубликован')+'</p><p>'+', '.join(E(rows[x]['name']) for x in s['rolls'])+'</p></section>')
body+=['</div><p>Фото и тексты текущего сайта проверены отдельно. «Мацахиро» имеет шапку на фото, хотя её нет в текстовом перечислении; она явно включена в наш сценарий. Точный состав тёплой классики с крабом неизвестен.</p><h2>Наша версия с75 г яки</h2><p>Текущая база: сыр385 ₽/кг, готовая действующая крабовая смесь220,38 ₽/кг, яки327,11 ₽/кг. Будущая база меняет только сыр на500,5 ₽/кг и смесь на409,41 ₽/кг (покупной краб450 ₽/кг, новая смесь80/20). Это две отдельные ценовые модели.</p><table><tr><th>Набор</th><th>Цена WASABIST</th><th>Продукты на текущих закупках</th><th>Доля продуктов от их цены</th><th>При будущих ценах сыра/краба</th><th>Коробка отдельно</th></tr>']
for s in sets:
 a=s['scenarios']['current_75'];b=s['scenarios']['future_75'];unknown=a['unpriced_takuan_g']
 body.append(f'<tr><td>{E(s["name"])}</td><td>{s["price_rub"]} ₽</td><td>{amount(a["priced_food_subtotal_rub"],unknown)} ₽</td><td>{F(a["priced_subtotal_fc_at_competitor_price"])}'+(' + такуан' if unknown else '')+f'</td><td>{amount(b["priced_food_subtotal_rub"],unknown)} ₽</td><td>≈11 ₽*</td></tr>')
body+=['</table><p><small>*Коробка — прежний условный аналог, вместимость/пакет/гарнир не проверены. Вода, расход фритюрного масла и прочие неоценённые статьи не признаны бесплатными. Числа — подытог оценённых продуктов. ФОТ, энергия, доставка, налоги и оборудование не входят.</small></p><h2>Разбор по роллам</h2><p>Количество ниже — модель по нашим картам. Нельзя выдавать его за измеренную рецептуру WASABIST или готовую дозу для кухни. Рис сохраняется по выбранной карте —130 г, у крабовых маки120 г; нормы новой линейки110 г/100–110 г начинки сюда автоматически не перенесены.</p><table><tr><th>Ролл / в каких сетах</th><th>Наш масштабированный состав</th><th>Шапка / поливка</th><th>Продукты при75 г</th><th>Основание и отличия</th></tr>']
for m in cfg['models']:
 v=rows[m['id']]['scenarios']['current_75'];core=[];tops=[]
 for l in v['lines']:
  desc=f'{E(l["component"])} {l["amount"]:.1f} '+('листа' if l['unit']=='sheet' else 'г')
  (tops if l['guid'] in [G['yaki'],'cb4ed412-ef0b-49c6-ad99-de69553b3238','a9eb0f18-d512-4053-aeee-19c2034e42fb'] else core).append(desc)
 loc=', '.join(E(s['name']) for s in sets if m['id'] in s['rolls'])
 body.append(f'<tr><td><b>{E(m["name"])}</b><br><small>{loc}</small></td><td>'+ '<br>'.join(core)+'</td><td>'+('<br>'.join(tops) or 'нет')+f'</td><td>{amount(v["priced_food_subtotal_rub"],v["takuan_ready_g"])} ₽</td><td>{E(v["reference_name"])}; k={v["k"]:.4f}; W={v["W_reference_g"]:.2f} г → '+(f'{v["W_competitor_standalone_g"]} г' if v['W_competitor_standalone_g'] else 'нет веса конкурента, k=1')+'</td></tr>')
body+=['</table><h2>Базовый метод до нашей шапки75 г</h2><p>По правилу сравнения от08.10: k=вес одиночной порции конкурента / каталожный вес нашей сопоставимой карты; все закладки кроме готового риса умножаются наk. Источник W — каталог, не сумма сырья. Исходные наши карты сохранены; замены под опубликованный состав перечислены в inputs.json. Для неизвестного веса крабовой классики временно взята полная порция «Крабика», без вычитания850−680.</p><p><b>Модель не гарантирует тот же готовый вес.</b> Фиксированный рис, адаптации состава и75 г шапки меняют массу; готовый выход и разница с весом конкурента неизвестны. Сумма входных масс без нори не названа готовым весом.</p><table><tr><th>Набор</th><th>Шапки по масштабированным картам</th><th>Яки70 /75 /80 г</th><th>Сумма входов при75 г без массы нори, не выход</th></tr>']
for s in sets:
 a=s['scenarios'];un=a['current_75']['unpriced_takuan_g'];body.append(f'<tr><td>{E(s["name"])}</td><td>{amount(a["current_scaled_source_hat"]["priced_food_subtotal_rub"],un)} ₽</td><td>'+' / '.join(M(a['current_'+str(h)]['priced_food_subtotal_rub']) for h in [70,75,80])+(' + такуан' if un else '')+f' ₽</td><td>≈{M(a["current_75"]["input_component_sum_g_ex_nori_not_finished_weight"])} г</td></tr>')
body+=['</table><h2>Что сильнее всего меняет оценку</h2><p>Филадельфии рассчитаны по нашей полной «классике». Для альтернативы взята целая карта «Филадельфия лайт» с её собственным исходным весом и закладками; это чувствительность выбора аналога, не предложение снять1–3 г и не изменение утверждённой карты.</p><table><tr><th>Набор</th><th>Полная классика,75 г яки</th><th>Альтернативный эталон «лайт»</th></tr>']
for s in sets:
 a=s['scenarios']['current_75'];body.append(f'<tr><td>{E(s["name"])}</td><td>{amount(a["priced_food_subtotal_rub"],a["unpriced_takuan_g"])} ₽</td><td>{amount(s["light_cover_sensitivity_current75"]["priced_food_subtotal_rub"],a["unpriced_takuan_g"])} ₽</td></tr>')
z=rows['Z']['scenarios']['current_75'];body+=['</table><p>Таким образом, фото недостаточно для узкого достоверного диапазона себестоимости: выбор рыбного покрытия меняет результат сильнее, чем5 г соусной шапки. Все диапазоны — варианты модели, не доказанные границы чужой рецептуры.</p><h2>Неоценённый такуан</h2>',f'<p>В запечённой Филадельфии модель использует≈{z["takuan_ready_g"]:.1f} г подготовленной редьки. Добавка к стоимости Калифилы: <b>{z["takuan_ready_g"]:.2f} × P / (1000 × y)</b>, где P — покупная цена за килограмм, y — выход после слива/подготовки. Оба текущих параметра неизвестны.</p>',f'<p>Только иллюстрация: исторические360 ₽/кг от23.11.2023 и условный выход90% дали бы≈{z["historical_takuan_example_only_rub"]:.1f} ₽. Это не текущая цена или измерение и <b>не включено в основной подтверждённый подытог</b>. Например, текущая версия Калифилы75 г была бы≈{sets[2]["scenarios"]["current_75"]["food_subtotal_plus_historical_takuan_example_rub"]:.0f} ₽ оценённых продуктов плюс прочие неоценённые статьи.</p>', '<h2>Источники и границы</h2><p><a href="https://volgodonsk.wasabist.ru/shop/nabory.html">Наборы WASABIST</a> · <a href="https://volgodonsk.wasabist.ru/shop/rolly-xolodnye.html">Холодные</a> · <a href="https://volgodonsk.wasabist.ru/shop/rolly-teplye.html">Тёплые</a> · <a href="https://volgodonsk.wasabist.ru/shop/tempurnye-rolly.html">Темпура</a>.</p><p><a href="inputs.json">Вводные/адаптации</a> · <a href="results.json">Калькуляции</a> · <a href="teplo-analogs.json">Наши карты/цены/версии/хеши</a> · <a href="live-dom.json">Текущий сайт</a> · <a href="web-audit.json">Независимый веб-аудит</a> · <a href="validation.json">Проверки</a> · <a href="calculate.py">Воспроизведение</a>.</p><p>Эти суммы не устанавливают себестоимость и прибыль WASABIST. Для продажи по их ценам нужны наша полная экономика, акции, оплаченный чек и спрос. Заказы/рассылки/публикации/изменения карт не выполнялись.</p></main></html>']
(P/'report.html').write_text(''.join(body))
print(json.dumps({'checks':len(checks),'current75':{s['id']:s['scenarios']['current_75']['priced_food_subtotal_rub'] for s in sets},'future75':{s['id']:s['scenarios']['future_75']['priced_food_subtotal_rub'] for s in sets}},ensure_ascii=False))
