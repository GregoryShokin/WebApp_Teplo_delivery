"""Recalculate paper recipe options from frozen 21 Oct inputs; no live writes."""
import copy
import hashlib
import json
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
SNAPSHOTS = HERE.parent
SOURCE = SNAPSHOTS / '2026-10-21-new-roll-line-v1'
SETS = SNAPSHOTS / '2026-10-21-sets-quality-first/results.json'
inputs = json.loads((SOURCE / 'inputs.json').read_text())
original = json.loads((SOURCE / 'results.json').read_text())
sets = json.loads(SETS.read_text())
components = inputs['components']
recipes = {r['id']: r for r in original['recipes']}


def cost(recipe):
    total = 0.0
    for stage in ('base', 'inner', 'top', 'frying'):
        for ingredient, amount in recipe.get(stage, {}).items():
            # Eel options deliberately excluded: they require a coupled output balance.
            assert ingredient not in ('eel_joint', 'eel_legacy', 'black_eel')
            unit = 'cost_per_sheet' if ingredient == 'nori' else 'cost_per_g'
            total += amount * components[ingredient][unit]
    return total


options = []


def add(option_id, recipe_id, title, mains, inner=None, top=None, note=''):
    old = recipes[recipe_id]
    new = copy.deepcopy(old)
    if inner is not None:
        new['inner'] = inner
    if top is not None:
        new['top'] = top
    old_cost, new_cost = cost(old), cost(new)
    assert abs(old_cost - old['price_reserve_cost_rub']) < 1e-8
    inner_total = sum(new['inner'].values())
    assert inner_total == sum(old['inner'].values())
    assert not ('crab_mix' in new['inner'] and 'cream_cheese' in new['inner'])
    if old['kind'] == 'lid':
        assert sum(new['top'].get(k, 0) for k in ('salmon_plain', 'salmon_smoked', 'shrimp')) == 50
    if old['kind'] == 'tartar':
        assert 62 <= sum(v for k, v in new['top'].items() if k.startswith('tartar')) <= 80
    fractions = {}
    for ingredient in mains:
        amount = new['inner'].get(ingredient, 0)
        if ingredient == 'crab_mix':
            assert not any(new['inner'].get(k, 0) for k in ('cream_cheese', 'mozzarella'))
            amount *= 0.8  # Mayonnaise is not crab.
        fractions[ingredient] = amount / inner_total
        assert fractions[ingredient] >= 0.3 - 1e-10
    options.append({
        'option_id': option_id, 'source_recipe': recipe_id, 'title': title,
        'old_name': old['name'], 'old_cost_rub': old_cost,
        'new_cost_rub': new_cost, 'saving_rub': old_cost - new_cost,
        'old_inner': old['inner'], 'new_inner': new['inner'],
        'old_top': old['top'], 'new_top': new['top'],
        'base_unchanged': new['base'], 'frying_unchanged': new['frying'],
        'inner_total_g': inner_total, 'selected_main_fractions': fractions,
        'note': note,
    })


add('A1', 'F01', 'Лосось 35→32 г; сыр 50→53 г', ['salmon_plain'],
    inner={'salmon_plain': 32, 'cream_cheese': 53, 'cucumber': 20},
    note='До минимума 31,5 г остаётся лишь 0,5 г; производственный допуск не доказан.')
add('A2', 'F01', 'Лосось 35→33 г; сыр 50→52 г', ['salmon_plain'],
    inner={'salmon_plain': 33, 'cream_cheese': 52, 'cucumber': 20})
add('B1', 'B01', 'Лосось и креветка по 35→32 г; крабовая смесь 20→26 г',
    ['salmon_plain', 'shrimp'],
    inner={'salmon_plain': 32, 'shrimp': 32, 'crab_mix': 26, 'cucumber': 15},
    note='Каждый основной 30,48%; краб здесь остаётся наполнителем. Запас лишь по 0,5 г.')
add('B2', 'B01', 'Лосось и креветка по 35→33 г; крабовая смесь 20→24 г',
    ['salmon_plain', 'shrimp'],
    inner={'salmon_plain': 33, 'shrimp': 33, 'crab_mix': 24, 'cucumber': 15})
add('C1', 'F01', 'Творожный сыр 50 г → крабовая смесь 50 г',
    ['salmon_plain', 'crab_mix'],
    inner={'salmon_plain': 35, 'crab_mix': 50, 'cucumber': 20},
    note='40 г самого подготовленного краба; иной вкус, новый вариант карты.')
add('C2', 'C01', 'Под крышкой: сыр 65 г → крабовая смесь 65 г', ['crab_mix'],
    inner={'crab_mix': 65, 'cucumber': 20, 'tamago': 20},
    note='50 г лосося сверху сохранены. 52 г самого подготовленного краба внутри.')
for option_id, rid, ingredient in [('D1', 'C05', 'tartar_plain'), ('D2', 'C06', 'tartar_smoked')]:
    top = dict(recipes[rid]['top'])
    top[ingredient] = 62
    add(option_id, rid, 'Готовая тартарная шапка 70→62 г', [], top=top,
        note='Разрешённый диапазон 62–80 г; объём верха уменьшается, нужен визуальный контроль.')
add('E1', 'B01', 'Лосось 35 г + мидии 35 г вместо лосося и креветки',
    ['salmon_plain', 'mussels'],
    inner={'salmon_plain': 35, 'mussels': 35, 'crab_mix': 20, 'cucumber': 15},
    note='Новая рецептура с другим названием и вкусом; мидии по книге цен 21.10, спорная закупка остаётся O13.')
add('F1', 'B01', 'Лосось 35 г + крабовая смесь 50 г + огурец 20 г',
    ['salmon_plain', 'crab_mix'],
    inner={'salmon_plain': 35, 'crab_mix': 50, 'cucumber': 20},
    note='Креветка исключена; новый ролл с лососем и крабом, а не прежний микс.')
add('F2', 'B01', 'Креветка 35 г + крабовая смесь 50 г + огурец 20 г',
    ['shrimp', 'crab_mix'],
    inner={'shrimp': 35, 'crab_mix': 50, 'cucumber': 20},
    note='Лосось исключён; новый ролл с креветкой и крабом, а не прежний микс.')
add('G1', 'F01', 'Лосось 32 г + крабовая смесь 53 г + огурец 20 г',
    ['salmon_plain', 'crab_mix'],
    inner={'salmon_plain': 32, 'crab_mix': 53, 'cucumber': 20},
    note='Эффекты пересчитаны вместе, не сложены из независимых вариантов; запас лосося 0,5 г.')
add('H1', 'C01', 'Крышка 50 г: лосось без копчения → лосось х/к', [],
    top={'salmon_smoked': 50}, note='Иной вкус. Обе цены с выходами; свежий пересчёт старой книги, не новое предложение поставщика.')

crab = {'id': 'CR01-proposal', 'name': 'Краб, тамаго и огурец — предложение',
        'kind': 'ordinary', 'base': {'rice': 110, 'nori': 0.5},
        'inner': {'crab_mix': 40, 'tamago': 35, 'cucumber': 25}, 'top': {}, 'frying': {}}
crab['recipe_materials_rub'] = cost(crab)
crab['actual_prepared_crab_g'] = 32
crab['actual_crab_share_inner'] = 0.32
crab['status'] = 'costed_idea_not_approved_recipe'
assert sum(crab['inner'].values()) == 100

# Verify identity and canonical composition, so aliases cannot bypass repeats.
all_recipes = {**recipes, **{r['id']: r for r in sets['new_recipes']}}
def canonical(r):
    return json.dumps({k: r.get(k, {}) for k in ('base', 'inner', 'top', 'frying')}, sort_keys=True)

set_audit = []
for s in sets['sets']:
    counts = Counter(s['final'])
    canonical_counts = Counter(canonical(all_recipes[rid]) for rid in s['final'])
    assert max(counts.values()) <= 2
    assert max(canonical_counts.values()) <= 2
    set_audit.append({'id': s['id'], 'max_identical_id': max(counts.values()),
                      'max_identical_composition': max(canonical_counts.values()), 'pass': True})

sources = [SOURCE / 'inputs.json', SOURCE / 'results.json', SETS]
result = {
    'as_of': '2026-10-21', 'status': 'costed_ideas_not_accepted_changes',
    'cost_basis': 'recipe materials only; same frozen21Oct inputs and owner crab450/cheese+30%; before packaging, labour, discounts and other order costs; water unpriced',
    'source_sha256': {str(p.relative_to(SNAPSHOTS)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources},
    'owner_rules': {'max_identical_rolls_per_set': 2, 'crab_allowed_as_main': True,
                    'new_primary_crab_options_no_cheese_inside': True},
    'interpretation': 'Existing30% inside rule applied to actual preparedcrab, not mayonnaise;40g80/20mix contains32gcrab.',
    'options': options, 'new_crab_candidate': crab, 'previous_set_repeat_audit': set_audit,
    'limits': ['Equal grams do not prove equal volume, taste or appearance.',
               'No eel savings claimed without full fillet/sauce output and usage balance.',
               'No physical trial, video timing, iiko/price/menu changes.',
               'New recipes do not inherit old profit thresholds without salesmix and paidcheck.'],
}
# Later owner correction: gram-level savings cannot be used by this kitchen.
retired_ids = {'A1', 'A2', 'B1', 'B2', 'G1'}
for option in options:
    option['operational_status'] = ('withdrawn_gram_shaving_not_executable'
                                    if option['option_id'] in retired_ids
                                    else 'mass_only_requires_defined_physical_portion')
crab['operational_status'] = 'mass_only_requires_defined_physical_portion'
for option in options:
    option['source_recipe_must_remain_unchanged'] = True
    option['distinct_new_recipe_only_if_not_withdrawn'] = option['option_id'] not in retired_ids
result['owner_assortment_correction'] = {
    'as_of': '2026-10-21', 'journal': 'Q27',
    'current_set_balancing_keeps_approved_recipes_and_weights': True,
    'new_ingredients_are_candidates_not_approved_recipes': True,
}
result['owner_portion_correction'] = {
    'as_of': '2026-10-21', 'journal': 'Q26',
    'salmon_piece_g_range_owner_estimate': [10, 15],
    'fixed_piece_weight_or_universal_step_not_defined': True,
    'withdrawn_option_ids': sorted(retired_ids),
    'no_automatic_recipe_or_profit_recalculation': True,
}
(HERE / 'results.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
rows = ['| Вариант | Было, ₽ | Стало, ₽ | Разница продуктов, ₽ | Статус после Q26 |', '|---|---:|---:|---:|---|']
for o in options:
    status = 'Отозван: снятие2–3 г' if o['option_id'] in retired_ids else 'Бумажный расчёт; порционность не подтверждена'
    rows.append(f"| {o['option_id']}: {o['title']} | {o['old_cost_rub']:.2f} | {o['new_cost_rub']:.2f} | {o['saving_rub']:.2f} | {status} |")
report = '''# Удешевление дорогих роллов и новые правила — 21.10.2026

**Последующее уточнение владельца, Q26:** кухня не может экономить снятием1–3 г отдельного ингредиента. ВариантыA1/A2/B1/B2/G1 **отозваны как непрактичные**; цифры ниже сохранены как история арифметики, а не доступная экономия. Кусок лосося ориентировочно10–15 г, без гарантированных15 г. Остальным вариантам, включая CR01, ещё нужна определённая физическая порция/доза и её разброс. Для ложек, дозаторов, креветок и прочих продуктов единицы не назначены. Исходные нормы массы и доли основного не отменены. Готовые карты и новый бюджет из этого уточнения автоматически не получаются.

**Следующее уточнение владельца, Q27:** утверждённые позиции/граммовки сохраняются. Удешевлять сеты следует отдельными новыми роллами, в том числе с новыми ингредиентами. Неотозванные сравнения ниже допустимы только как проекты отдельных новых карт; их нельзя применять как изменения карты-источника. [Кандидаты дополнительных ингредиентов](../2026-10-21-new-budget-roll-candidates/report.md).

Владелец установил максимум **две порции одного и того же ролла в одном сете**. Новый крабовый основной продукт разрешён; по указанию «не смешивать его с сыром» новые варианты с основным крабом сделаны без любого сыра внутри. Наружный сырный соус отдельно не уточнён. Остальные прежние нормы остаются: рис 110 г, обычная начинка 100–110 г, каждый выбранный основной внутри ≥30%, крышки из основного продукта ровно 50 г. Разные названия одной карты не создают разнообразия.

Прежний минимум 30% применён к самому подготовленному крабу, без майонеза: для 100 г начинки нужно минимум 37,5 г смеси 80/20, для 105 г — 39,375 г. Малые 15–20 г смеси в старых проектах остаются наполнителем, а не самостоятельным основным продуктом. Новое разрешение не меняет перечень внешних крышек/шапок.

Все десять рассмотренных наборов Q24, включая отложенный угревый, уже соблюдают максимум ×2 по ID и фактическому составу. Девять основных сетов автоматически не переделывались.

## Калькуляции предложений

Это стоимость продуктов на **целый ролл**, не кусочек и не полная себестоимость заказа. Использована та же книга цен 21.10 с выходами п/ф, крабом 450 ₽/кг покупного сырья и сыром +30%. Упаковка, ФОТ, доставка, налоги и акции сюда не входят; вода отдельно не оценена. Действующие карты/цены не изменены.

'''+ '\n'.join(rows) + '''

Полные старые/новые закладки, сохранённые верх/жарка и доли каждого основного — в [results.json](results.json). Исходные рецептуры — [Q22](../2026-10-21-new-roll-line-v1/report.md).

## Как выбирать

- **Отозвано в Q26:** снятие2–3 г с дорогого компонента с компенсацией граммами наполнителя. Прежние6,33/9,49 ₽ остаются только арифметикой; текущая кухня такой способ закладки не обеспечивает. Оптимизация начинается с реальной физической порции, а не желаемых граммов.
- Сыр → крабовая смесь даёт 4,55–5,92 ₽, сохраняя граммы дорогого основного/50 г крышки. Вкус меняется; сыр убирается полностью, оба наполнителя не смешиваются.
- Шапка тартар 70→62 г экономит 13,17–15,13 ₽ в уже разрешённом диапазоне, но сверху физически меньше продукта. Одинаковый визуал этим расчётом не доказан.
- Замена второго дорогого продукта мидиями/крабом даёт заметно больше — 21,39–69,54 ₽ на приведённых примерах. Это отдельные названия и рецептуры. Дешевле не означает, что дегустация будет успешной. Цена мидий остаётся открытым вопросом O13.
- Х/к лосось по этой книге дешевле обычного подготовленного лосося: на 50 г крышке разница 16,05 ₽. Это отдельный вкус, а не равнозначная подмена.
- Дозирование может убрать незапланированный перевес; без измерений фактического перевеса экономия не назначается. Цена 1 лишнего грамма: лосось 2,37 ₽, креветка 1,61 ₽. Потери и обрезь в начинку/тартар считать один раз, с выходами и фактическим использованием, не объявлять бесплатными.

## Отдельный крабовый ролл для разнообразия сета

Предложение CR01: рис110 г, нори½листа; внутри крабовая смесь40 г, тамаго35 г, огурец25 г =100 г. В смеси32 г самого краба, то есть32% внутренней начинки. Сыр отсутствует. Материалы **CRAB_COST ₽** на целую порцию. Это дополнительный доступный вкус для замены повторяющихся мидийных позиций с сохранением дорогого ядра сета; новый сет не рассчитан и карта ещё не выбрана.

## Что остаётся допущением

Совпадение граммов не доказывает совпадение объёма и визуала. Вкус, внешний вид, готовый выход и доставка предложений не проверены; новая дегустация/съёмка не запускалась. Экономия на сырье не равна приросту чистой прибыли: после выбора вариантов нужны микс продаж, упаковка/льготы, оплаченный чек и пересчёт общего бюджета. По угрю никаких новых обещаний экономии без баланса филе/«Чёрного угря» нет.

Воспроизведение: `python3 calculate.py`; источники и SHA-256 записаны в `results.json`.
'''
report = report.replace('CRAB_COST', f"{crab['recipe_materials_rub']:.2f}".replace('.', ','))
(HERE / 'report.md').write_text(report)
print(json.dumps({'options': len(options), 'sets_checked': len(set_audit), 'all_repeat_checks_pass': True,
                  'crab_candidate_cost': crab['recipe_materials_rub']}, ensure_ascii=False))
