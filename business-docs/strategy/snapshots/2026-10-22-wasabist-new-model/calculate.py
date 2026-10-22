#!/usr/bin/env python3
"""Reproduce Q34 from frozen inputs. No network and no competitor-weight scaling."""
import hashlib
import html
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
I = json.loads((ROOT / 'inputs.json').read_text())
CHECKS = {}
for rel, expected in I['source_sha256'].items():
    actual = hashlib.sha256((ROOT.parent / rel).read_bytes()).hexdigest()
    assert actual == expected, (rel, actual, expected)
CHECKS['frozen_source_hashes_match'] = True
assert I['no_competitor_weight_scaling'] is True
assert I['inside_mass_scenarios_g'] == [100, 110]
C = I['components']
assert C['cream_cheese']['cost_per_g'] == 0.5005
assert abs(C['crab_mix']['cost_per_g'] - (0.8 * (450 / 0.98) + 0.2 * 210.32) / 1000) < 1e-12
assert C['takuan']['cost_per_g'] is None
assert C['sesame']['portion_g'] is None
old = json.loads((ROOT / I['old_comparison']).read_text())
oldsets = {s['id']: s for s in old['sets']}
roles = ['base', 'inner', 'fish_cover', 'sauce_hat', 'outside_coating', 'drizzle', 'sprinkle', 'frying']
rolls = {}
for r in I['recipes']:
    scenarios = {}
    for inside in I['inside_mass_scenarios_g']:
        inner = dict(r['inner100'])
        if inside == 110:
            for key, q in r['growth_to110'].items():
                inner[key] = inner.get(key, 0) + q
        assert sum(inner.values()) == inside
        assert r['base'] == {'rice': 110, 'nori': 0.5}
        if 'crab_mix' in inner:
            assert 'cream_cheese' not in inner
        primary = []
        for key, fraction in r['inside_primary_fraction'].items():
            actual = inner[key] * fraction
            share = actual / inside
            assert share >= 0.3
            primary.append({'component': key, 'actual_primary_g': actual, 'fraction_of_inside': share})
        if r['id'] in ['P', 'Z']:
            assert r['fish_cover'] == {'salmon_plain': 50}
            assert not r['sauce_hat'] and not r['inside_primary_fraction']
            assert set(inner) == ({'cream_cheese', 'cucumber'} if r['id'] == 'P' else {'cream_cheese', 'takuan'})
        if r['sauce_hat']:
            assert r['sauce_hat'] == {'yaki': 75}
        if r['id'] == 'H':
            assert inner['shrimp_batter'] == 50 and not r['frying']
            assert abs(inner['shrimp_batter'] * 0.8 - 40) < 1e-12
        lines = []
        for role in roles:
            values = inner if role == 'inner' else r[role]
            for key, q in values.items():
                comp = C[key]
                unit_cost = comp.get('cost_per_sheet') if key == 'nori' else comp.get('cost_per_g')
                cost = None if q is None or unit_cost is None else q * unit_cost
                lines.append({'role': role, 'component': key, 'name': comp['name'], 'quantity': q,
                              'unit': 'sheet' if key == 'nori' else 'g',
                              'unit_cost_rub': unit_cost, 'priced_cost_rub': cost,
                              'unpriced_leaves': comp.get('unpriced', [])})
        subtotal = sum(x['priced_cost_rub'] for x in lines if x['priced_cost_rub'] is not None)
        scenarios[str(inside)] = {'inside_g': inside, 'inner': inner,
            'primary_checks': primary, 'lines': lines, 'priced_food_subtotal_rub': subtotal,
            'unpriced_lines': [x for x in lines if x['priced_cost_rub'] is None],
            'all_in_cost_rub': None, 'finished_weight_g': None}
    rolls[r['id']] = {'name': r['name'], 'notes': r['notes'], 'scenarios': scenarios}
sets = []
for s in I['sets']:
    counts = Counter(s['rolls'])
    assert max(counts.values()) <= 2 and len(s['rolls']) == 4
    nr_hats = sum(bool(next(r for r in I['recipes'] if r['id'] == rid)['sauce_hat']) for rid in s['rolls'])
    assert nr_hats == {'four': 2, 'ocean': 2, 'kalifila': 1}[s['id']]
    variants = {}
    for inside in I['inside_mass_scenarios_g']:
        chosen = [rolls[rid]['scenarios'][str(inside)] for rid in s['rolls']]
        total = sum(r['priced_food_subtotal_rub'] for r in chosen)
        takuan = sum(x['quantity'] for r in chosen for x in r['lines'] if x['component'] == 'takuan')
        unpriced = [{'roll_id': rid, **x} for rid, rr in zip(s['rolls'], chosen) for x in rr['unpriced_lines']]
        oldfuture = oldsets[s['id']]['scenarios']['future_75']['priced_food_subtotal_rub']
        variants[str(inside)] = {
            'inside_g_per_roll': inside, 'inside_g_per_set': 4 * inside,
            'rice_g_per_set': 440, 'yaki_g_per_set': nr_hats * 75,
            'priced_food_subtotal_rub': total,
            'priced_subtotal_fc_at_competitor_price': total / s['price_rub'],
            'unpriced_takuan_g': takuan, 'unpriced_lines': unpriced,
            'box_proxy_rub': I['box_proxy_rub_per_set'],
            'priced_food_plus_box_proxy_rub': total + I['box_proxy_rub_per_set'],
            'priced_food_plus_box_proxy_ratio': (total + I['box_proxy_rub_per_set']) / s['price_rub'],
            'old_future75_priced_subtotal_rub': oldfuture,
            'delta_to_old_future75_priced_subtotal_rub': total - oldfuture,
            'material_budget_at35pct_rub': 0.35 * s['price_rub'],
            'priced_excess_above35pct_budget_rub': total - 0.35 * s['price_rub'],
            'full_food_cost_rub': None, 'actual_competitor_food_cost': None,
            'finished_set_weight_g': None}
    sets.append({**s, 'hat_count': nr_hats, 'scenarios': variants})
CHECKS.update({'inside_endpoints_exact_100_and110': True, 'ready_rice110_all_rolls': True,
    'covers50_and_no_yaki_on_P_Z': True, 'hats75_and_set_counts2_2_1': True,
    'each_selected_inside_primary_at_least30pct_actual_product': True,
    'no_inside_cheese_with_crab': True, 'no_double_coating_or_yield_for_H': True,
    'source_cards_and_prices_not_modified': True, 'unknowns_not_replaced_with_zero': True,
    'no_finished_weight_inference': True, 'max_two_identical_rolls': True})
OUT = {'as_of': I['as_of'], 'pricebook_as_of': I['pricebook_as_of'],
       'label': 'priced ingredients of proposed new Teplo variants at competitor prices; unknowns excluded, not zero',
       'rolls': rolls, 'sets': sets, 'checks': CHECKS, 'limits': I['limits'],
       'takuan_addition_formula_rub_per_kalifila': '20 * purchase_price_rub_per_kg / (1000 * drained_yield_fraction)',
       'source_sha256': I['source_sha256']}
(ROOT / 'results.json').write_text(json.dumps(OUT, ensure_ascii=False, indent=2) + '\n')

# Compact human-readable report. All quantities and both recipes remain visible.
e = html.escape
def rub(x): return f'{x:,.2f}'.replace(',', ' ').replace('.', ',')
def signed_rub(x): return ('+' if x >= 0 else '−') + rub(abs(x))
def pct(x): return f'{100*x:.2f}'.replace('.', ',') + '%'
short = {'cream_cheese': 'сыр', 'cucumber': 'огурец', 'takuan': 'такуан', 'crab_mix': 'крабовая смесь80/20',
         'salmon_plain': 'лосось', 'shrimp': 'креветка', 'shrimp_batter': 'креветка в кляре'}
def inside_text(inn): return ' + '.join(f'{e(short[k])} {q} г' for k,q in inn.items())
setrows = ''
for s in sets:
    a,b = (s['scenarios'][str(m)] for m in [100,110])
    suffix = ' + такуан*' if s['id'] == 'kalifila' else ''
    setrows += f'<tr><td>{e(s["name"])}</td><td>{s["price_rub"]} ₽</td><td>{rub(a["priced_food_subtotal_rub"])} ₽{suffix}</td><td>{pct(a["priced_subtotal_fc_at_competitor_price"])}{(" + *" if suffix else "")}</td><td>{rub(b["priced_food_subtotal_rub"])} ₽{suffix}</td><td>{pct(b["priced_subtotal_fc_at_competitor_price"])}{(" + *" if suffix else "")}</td></tr>'
recrows = ''
for r in I['recipes']:
    rr = rolls[r['id']]
    a,b = (rr['scenarios'][str(m)] for m in [100,110])
    outer = []
    if r['fish_cover']: outer.append('лососевая крышка50 г')
    if r['sauce_hat']: outer.append('шапка яки75 г')
    if r['outside_coating']: outer.append('масаго15 г')
    if r['drizzle']: outer.append('поливка унаги5 г')
    if r['sprinkle']: outer.append('кунжут: доза неизвестна')
    if r['frying']: outer.append('жарка: кляр25 г + сухари15 г + масло5 г (допущение)')
    suffix = ' + такуан*' if r['id'] == 'Z' else ''
    recrows += f'<tr><td>{r["id"]}. {e(r["name"])}</td><td>{inside_text(a["inner"])}</td><td>{inside_text(b["inner"])}</td><td>{e("; ".join(outer) or "—")}</td><td>{rub(a["priced_food_subtotal_rub"])} / {rub(b["priced_food_subtotal_rub"])} ₽{suffix}</td></tr>'
comprows = ''
for s in sets:
    a,b = (s['scenarios'][str(m)] for m in [100,110])
    csuffix = ' + такуан*' if s['id'] == 'kalifila' else ''
    comprows += f'<tr><td>{e(s["name"])}</td><td>{rub(a["old_future75_priced_subtotal_rub"])} ₽{csuffix}</td><td>{signed_rub(a["delta_to_old_future75_priced_subtotal_rub"])} / {signed_rub(b["delta_to_old_future75_priced_subtotal_rub"])} ₽</td><td>{rub(a["priced_food_plus_box_proxy_rub"])} / {rub(b["priced_food_plus_box_proxy_rub"])} ₽{csuffix}</td></tr>'
setmembers = ''.join(f'<li><b>{e(s["name"])}</b>: '+', '.join(e(rolls[rid]['name']) for rid in s['rolls'])+f'. Шапок яки: {s["hat_count"]}.</li>' for s in sets)
primaryrows = ''.join(f'<tr><td>{rid}</td><td>'+('; '.join(f'{e(short[p["component"]])}: {p["actual_primary_g"]:g} г, {pct(p["fraction_of_inside"])}' for p in rr['scenarios']['100']['primary_checks']) or 'Основного внутри нет; крышка отдельно')+'</td><td>'+('; '.join(f'{e(short[p["component"]])}: {p["actual_primary_g"]:g} г, {pct(p["fraction_of_inside"])}' for p in rr['scenarios']['110']['primary_checks']) or 'Основного внутри нет; крышка отдельно')+'</td></tr>' for rid,rr in rolls.items())
page = '''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Три сета WASABIST по новой модели Тепла · 22.10.2026</title>
<style>body{font:16px/1.5 system-ui,sans-serif;max-width:1360px;margin:30px auto;padding:0 22px;color:#17232c;background:#faf9f6}h1{font-size:30px;line-height:1.2}h2{font-size:23px;margin-top:32px}table{border-collapse:collapse;width:100%;background:white}th,td{padding:11px;text-align:left;vertical-align:top;border:1px solid #d5dadd}th{background:#e9efed}small,.muted{color:#50616a}.notice{padding:16px;background:#fff2cf;border-left:5px solid #b17a0a}.tablewrap{overflow-x:auto}.wide{min-width:1000px}a{color:#12625b}li{margin-bottom:8px}@media print{body{max-width:none;font-size:11px}table{font-size:10px}.tablewrap{overflow:visible}thead{display:table-header-group}tr{break-inside:avoid}.wide{min-width:0}}</style>
<h1>Три сета по новой модели Тепла</h1><p>22.10.2026 · четыре ролла /32 кусочка в каждом · цены конкурента сохранены из текущего сайта22.10.</p>
<p><b>На каждый ролл:110 г готового риса + ровно100 или110 г внутренней начинки.</b> Крышка из лосося50 г, соусная шапка яки75 г, наружная масаго, панировка и поливка идут отдельно. Это закладки подготовленных компонентов до конечной обработки, не подтверждённая масса доставленного блюда.</p>
<div class="notice"><b>При новой закладке прежние27% не подтверждаются.</b> Уже оценённые продукты дают35,43–36,96% у «Четыре по цене трёх»,37,91–39,27% у «Океана»,39,60–41,70% у «Калифилы» плюс такуан. Неизвестные расходы не приравнены к нулю. Это стоимость наших предложенных аналогов, не реальная себестоимость WASABIST.</div>
<h2>Себестоимость продуктов и food cost</h2><p>Food cost = оценённые продукты / опубликованная цена сета. Дополнительные скидки/бонусы не применялись.</p><div class="tablewrap"><table><thead><tr><th>Сет</th><th>Цена конкурента</th><th>Продукты:100 г внутри</th><th>FC:100 г</th><th>Продукты:110 г внутри</th><th>FC:110 г</th></tr></thead><tbody>SETROWS</tbody></table></div>
<p>* У «Калифилы» в обеих версиях20 г готового такуана, его текущая цена и выход после слива неизвестны. Доплата к продуктам: <b>20 × цена покупки₽/кг ÷(1000 × доля выхода)</b>; прибавка к FC — эта сумма ÷1439. Старую цену2023 года не подставляли.</p>
<p>Все три сета выше35% по оценённой части. У первого100-граммового варианта превышение всего5,12 ₽, но на неоценённые расходы запаса уже нет.</p>
<h2>Какие четыре ролла входят в каждый сет</h2><ul>SETMEMBERS</ul>
<h2>Составы предложенных новых версий</h2><p><b>У всех ниже дополнительно110 г риса и½листа нори.</b> Внутренние раскладки — отдельные предложения для бумаги, не утверждённые техкарты. Пользователь выбрал сыр+огурец/такуан внутри двух «Филадельфий»; точные соотношения60/40 и80/20 предложены агентом. Лосось только снаружи. Чтобы показать110 г, добавлены10 г явно названного компонента, без скрытого масштабирования.</p><div class="tablewrap"><table class="wide"><thead><tr><th>Ролл</th><th>Внутри100 г</th><th>Внутри110 г</th><th>Отдельно снаружи</th><th>Продукты100 /110</th></tr></thead><tbody>RECROWS</tbody></table></div>
<p>«Горячая креветочка»:50 г готовой креветки в кляре по карте =40 г креветки+10 г кляра. Это входит в общие100/110 г внутри. Мука и сухари уже оценены в стоимости этой заготовки; второй панировки нет. Фритюрное масло для этой заготовки остаётся неизвестным.</p>
<p>Токио адаптирован под наш подготовленный лосось. Отдельная карта/выход предварительного гриля не подтверждены. Тёплый крабовый с100/110 г смеси — наша предложенная конструкция, точная карта конкурента неизвестна. Запечённая «Филадельфия» — рыбная крышка, без яки. Крышки/шапки не включены в долю основного внутри.</p>
<h2>Закупочная база и неизвестные</h2><p>Цены остальных компонентов из сохранённой книги21.10.2026; выходы полуфабрикатов учтены один раз. Новые условия владельца: покупной снежный краб450 ₽/кг, смесь80/20 по готовым массам≈409,41 ₽/кг; творожный сыр+30%=500,50 ₽/кг. Это сценарные цены, не новая подтверждённая поставка. Рис≈57,73 ₽/кг, лосось п/ф≈2371,79 ₽/кг, отварная креветка п/ф≈1610,92 ₽/кг, яки≈327,11 ₽/кг.</p>
<ul><li>Такуан: неизвестны текущая цена и выход после слива.</li><li>Кунжут: цена285 ₽/кг известна, доза неизвестна. Вода в рисе/кляре и масло для креветки в кляре не оценены.</li><li>½листа нори,15 г масаго,5 г унаги, а для целиком жареного Мацахиро25 г кляра/15 г сухарей/5 г масла — открытые расчётные допущения. Не замеренные порции.</li><li>Коробки11 ₽/сет — прежний ориентир; вместимость для более крупных роллов не проверена. Палочки/соевый соус/имбирь/васаби/пакеты, труд, энергия, доставка, налоги и техника в food cost этой таблицы не включены.</li><li>100/110 г внутри не гарантируют одинаковый визуальный объём или вкус. Прежние утверждённые карты и меню не менялись.</li></ul>
<h2>Сопоставление с прежним расчётом</h2><p>Здесь сравнивается одна и та же база новых цен сыра/краба и яки75 г. Прежние27,15% первого набора были на старых количествах и текущих ценах. В новой модели меняются не только граммы внутри: рис теперь110 г, рыбные крышки ровно50 г вместо прежних масштабированных≈63,72/66,62 г, а внутренние смеси заданы заново. Поэтому разницу нельзя целиком назвать доплатой за увеличение начинки. У «Калифилы» разница ниже — только оценённая часть, количество неизвестного такуана тоже изменилось.</p><div class="tablewrap"><table><thead><tr><th>Сет</th><th>Раньше:новые цены, яки75 г</th><th>Разница новой100 /110</th><th>Новые продукты+коробки11 ₽,100 /110</th></tr></thead><tbody>COMPROWS</tbody></table></div>
<h2>Проверка доли основного внутри</h2><p>Для выбранного основного продукта≥30% именно внутренней начинки. В смеси краба учитывается сам краб80%, в креветке в кляре — сама креветка80%. В «Филадельфиях» внутри только наполнители, действует отдельное правило крышки50 г.</p><table><thead><tr><th>ID</th><th>При100 г внутри</th><th>При110 г внутри</th></tr></thead><tbody>PRIMARYROWS</tbody></table>
<h2>Источники и воспроизводимость</h2><p>Официальные разделы WASABIST: <a href="https://volgodonsk.wasabist.ru/shop/nabory.html">наборы</a>, <a href="https://volgodonsk.wasabist.ru/shop/rolly-xolodnye.html">холодные</a>, <a href="https://volgodonsk.wasabist.ru/shop/rolly-teplye.html">тёплые</a>, <a href="https://volgodonsk.wasabist.ru/shop/tempurnye-rolly.html">темпурные</a>. Датированный DOM/фотографические выводы сохранены в предыдущем снимке; этот пересчёт не утверждает чужие граммы.</p><p><a href="inputs.json">Вводные и хеши</a> · <a href="results.json">Построчная калькуляция</a> · <a href="calculate.py">Воспроизводимый расчёт</a> · <a href="validation.json">Проверки</a> · <a href="../2026-10-22-wasabist-three-sets/report.html">Предыдущая модель</a> · <a href="../../profit-progress.md">Журнал Q34</a></p></html>'''
for key,val in [('SETROWS',setrows),('SETMEMBERS',setmembers),('RECROWS',recrows),('COMPROWS',comprows),('PRIMARYROWS',primaryrows)]:
    page = page.replace(key,val)
(ROOT / 'report.html').write_text(page)
(ROOT / 'validation.json').write_text(json.dumps({'status':'pass','checks':CHECKS,
    'input_sha256':hashlib.sha256((ROOT/'inputs.json').read_bytes()).hexdigest(),
    'results_sha256':hashlib.sha256((ROOT/'results.json').read_bytes()).hexdigest(),
    'static_report_only':'local file browser preview previously denied by URL policy; not attempted here',
    'independent_review':'see independent-qa.json when present'},ensure_ascii=False,indent=2)+'\n')
for s in sets:
    print(s['name'],s['price_rub'],[(m,round(s['scenarios'][m]['priced_food_subtotal_rub'],4),round(100*s['scenarios'][m]['priced_subtotal_fc_at_competitor_price'],4)) for m in ['100','110']])
