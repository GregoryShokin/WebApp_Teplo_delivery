"""Rebuild the dated complaint ranking from anonymized manual coding, stdlib only."""
import json
from pathlib import Path

BASE = Path(__file__).resolve().parent
public = json.loads((BASE / 'public-unified-coded.json').read_text())
internal = json.loads((BASE / 'internal-revvy-coded.json').read_text())
rows = [
    {'id': f"R{r['row_id']:03d}", 'source': r['platform'], 'codes': r['issue_codes']}
    for r in public['review_rows_from_revvy']
] + [
    {'id': r['public_id'], 'source': 'Яндекс Еда', 'codes': r['issue_codes']}
    for r in public['new_yandex_eda_reviews']
] + [
    {'id': r['event_id'], 'source': 'Revvy: внутренние обращения', 'codes': r['themes']}
    for r in internal['all_history_specific_cases']
]
assert len({r['id'] for r in rows}) == len(rows)
assert sum(bool(r['codes']) for r in rows if r['source'] != 'Revvy: внутренние обращения') == 67
assert len(internal['all_history_specific_cases']) == 12

groups = [
    ('food', 'Вкус, качество и состав блюда', ['food_quality_taste_texture_or_recipe', 'food_taste_quality_composition']),
    ('waiting', 'Ожидание, нарушение сроков, заказ ещё не получен', ['delivery_delay_or_time_mismatch', 'order_not_received_by_review_time', 'delivery_wait']),
    ('communication', 'Общение, информирование и решение проблемы', ['service_communication_or_recovery', 'communication_order_taking', 'communication_problem_resolution']),
    ('temperature', 'Остывшая еда / температура', ['cold_or_temperature_problem', 'temperature']),
    ('completeness', 'Ошибки в заказе и дополнениях', ['order_accuracy_or_paid_item_missing', 'order_completeness_paid_extras']),
    ('value', 'Размер порции, начинка и соотношение с ценой', ['portion_or_value', 'portion_filling', 'price_value']),
    ('ordering', 'Оформление заказа, доступность и оплата', ['ordering_access_or_payment', 'ordering_payment_loyalty']),
    ('presentation', 'Внешний вид, повреждение и упаковка', ['packaging_or_presentation']),
    ('menu_options', 'Нет нужного варианта дополнения', ['menu_option_unavailable']),
]
platforms = ['Яндекс Карты', 'Google Карты', 'Яндекс Еда', 'Revvy: внутренние обращения']
known_codes = {c for _, _, codes in groups for c in codes}
assert {c for r in rows for c in r['codes']} <= known_codes
ranking = []
for code, label, codes in groups:
    matched = [r for r in rows if set(r['codes']) & set(codes)]
    ids = {source: [r['id'] for r in matched if r['source'] == source] for source in platforms}
    counts = {source: len(source_ids) for source, source_ids in ids.items()}
    ranking.append({'code': code, 'label': label, 'atomic_codes': codes, 'counts_by_source': counts, 'N_observations': len(matched), 'source_ids': ids})
ranking.sort(key=lambda x: -x['N_observations'])
output = {
    'as_of': '2026-10-13',
    'unit': 'One public review or one related internal complaint episode, once per topic; not unique people or orders.',
    'cross_channel_event_identity': 'Not established between internal and public sources; totals describe observations, not unique incidents.',
    'coverage': {
        'revvy_public_export_rows': 259, 'public_nonempty_texts_read': 230,
        'yandex_live_texts_loaded': 143, 'yandex_maps_live': 140, 'yandex_eda_live': 3,
        'public_specific_issue_reviews': 67,
        'revvy_internal_selected_histories': 16, 'revvy_internal_successful_histories': 16,
        'internal_specific_episodes_all_history': 12, 'internal_specific_episodes_window': 10,
        'internal_selection': 'Previously selected 30-day NegativeReviews category snapshot, not every Revvy chat.',
        'internal_images_unopened': 4,
        'excluded': 'Blank ratings, vague no-detail dissatisfaction, operator templates, positive qualified caveats, prompted survey answers.',
    },
    'maps_ui_dedup_note': '104 supported exact matches, 23 probable variants, 13 unresolved candidates; manual inspection found no additional issue in unresolved candidates. Total public-corpus size uncertain; no complaint percentages calculated.',
    'ranking': ranking,
}
(BASE / 'complaint-ranking.json').write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n')
for r in ranking:
    print(r['N_observations'], r['label'], r['counts_by_source'])
