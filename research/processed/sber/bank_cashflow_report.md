# Sber API: банковская выписка и ДДС

Дата сборки: 2026-05-19.

Источник: Sber API `statement/summary` и `statement/transactions`, raw только в `research/private/sber/statement/`.
В открытые processed-файлы не перенесены названия контрагентов, полные счета и назначения платежей.

## Короткий ответ

- Период: `2026-02-01 - 2026-05-19`.
- Дней с выпиской: 108, из них без расхождений summary/transactions: 108.
- Операций: 640.
- Поступления: 10693123.54 руб.
- Списания: 10794090.00 руб.
- Net cashflow: -100966.46 руб.

## Выгруженные файлы

- `research/processed/sber/bank_cashflow_daily.csv`
- `research/processed/sber/bank_cashflow_monthly.csv`
- `research/processed/sber/bank_operation_codes.csv`
- `research/processed/sber/bank_counterparty_summary.csv`
- `research/processed/sber/bank_cashflow_articles_draft.csv`
- `research/private/sber/processed/counterparty_map_private.csv`
- `research/private/sber/processed/transactions_private.csv`

## Месячный агрегат

| Период | Операций | Поступления | Списания | Net | Остаток на конец |
| --- | ---: | ---: | ---: | ---: | ---: |
| 2026-02 | 173 | 2932164.26 | 2754377.00 | 177787.26 | 279715.95 |
| 2026-03 | 188 | 3376477.58 | 3629917.00 | -253439.42 | 26276.53 |
| 2026-04 | 169 | 2558836.39 | 2479305.00 | 79531.39 | 105807.92 |
| 2026-05 | 110 | 1825645.31 | 1930491.00 | -104845.69 | 962.23 |

## Крупнейшие коды операций

| Период | Направление | Код | Операций | Сумма |
| --- | --- | --- | ---: | ---: |
| 2026-03 | DEBIT | 01 | 8 | 3609758.00 |
| 2026-03 | CREDIT | 01 | 178 | 3376477.58 |
| 2026-02 | CREDIT | 01 | 162 | 2932164.26 |
| 2026-02 | DEBIT | 01 | 8 | 2735520.00 |
| 2026-04 | CREDIT | 01 | 157 | 2558836.39 |
| 2026-04 | DEBIT | 01 | 8 | 2457084.00 |
| 2026-05 | DEBIT | 01 | 4 | 1912032.00 |
| 2026-05 | CREDIT | 01 | 105 | 1825645.31 |

## Черновая классификация ДДС

| Группа | Статья | Направление | Операций | Сумма | Статус |
| --- | --- | --- | ---: | ---: | --- |
| unclassified_outflow | Списания, требуется разметка | DEBIT | 10 | 3629917.00 | needs_owner_review |
| operating_inflow | Поступления от эквайринга / агрегаторов | CREDIT | 177 | 3156477.58 | needs_owner_review |
| operating_inflow | Поступления от эквайринга / агрегаторов | CREDIT | 162 | 2932164.26 | needs_owner_review |
| unclassified_outflow | Списания, требуется разметка | DEBIT | 9 | 2753979.00 | needs_owner_review |
| operating_inflow | Поступления от эквайринга / агрегаторов | CREDIT | 157 | 2558836.39 | needs_owner_review |
| unclassified_outflow | Списания, требуется разметка | DEBIT | 10 | 2379106.00 | needs_owner_review |
| unclassified_outflow | Списания, требуется разметка | DEBIT | 5 | 1930491.00 | needs_owner_review |
| operating_inflow | Поступления от эквайринга / агрегаторов | CREDIT | 105 | 1825645.31 | needs_owner_review |
| operating_inflow | Поступления / выручка, требуется разметка | CREDIT | 1 | 220000.00 | needs_owner_review |
| operating_outflow | Аренда | DEBIT | 1 | 100000.00 | needs_owner_review |
| financing | Кредиты / овердрафт | DEBIT | 2 | 398.00 | needs_owner_review |
| financing | Кредиты / овердрафт | DEBIT | 1 | 199.00 | needs_owner_review |

## Крупнейшие псевдонимы контрагентов

| ID | Направление | ИНН | Операций | Сумма | Статус |
| --- | --- | --- | ---: | ---: | --- |
| CP0009 | DEBIT | ********9201 | 19 | 9396000.00 | needs_manual_mapping |
| CP0003 | CREDIT | ******3893 | 492 | 7584746.28 | needs_manual_mapping |
| CP0005 | CREDIT | ******3893 | 109 | 2888377.26 | needs_manual_mapping |
| CP0004 | DEBIT | ******3893 | 9 | 1318394.00 | needs_manual_mapping |
| CP0008 | CREDIT | ********9201 | 1 | 220000.00 | needs_manual_mapping |
| CP0006 | DEBIT | ******3893 | 4 | 73836.00 | needs_manual_mapping |
| CP0001 | DEBIT | ******9831 | 2 | 3400.00 | needs_manual_mapping |
| CP0002 | DEBIT | ******3893 | 1 | 1863.00 | needs_manual_mapping |

## Следующие действия

1. Разметить `counterparty_id` по статьям ДДС/P&L в приватной карте.
2. Отделить операционные расходы от кредитов, налогов, внутренних переводов и прочих ниже EBITDA.
3. Сверить банковские поступления с iiko-выручкой с учетом эквайринга, агрегаторов и кассовых лагов.
