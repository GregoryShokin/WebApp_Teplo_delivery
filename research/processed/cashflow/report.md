# Bank Cashflow Classification Report

Дата сборки: 2026-05-19.

Источник: локальные raw-выписки Sber и T-Bank из `research/private/`; новых API-вызовов не выполнялось.
Построчная классификация хранится только в `research/private/`; processed-файлы содержат только агрегаты.

## Период

- Разметка операций: `2026-02-01` - `2026-05-19`.
- Сверка iiko vs банк за май ограничена `2026-05-01` - `2026-05-17`, потому что iiko processed-снимок доступен только за этот период.

## Классификация

- Операций всего: 1224.
- Операций с не-`unclassified` flow_type: 1224.
- Операций с `requires_owner_review=yes` или `unclassified`: 409.

| Flow type | Операций | Оборот abs | Доля оборота |
| --- | ---: | ---: | ---: |
| `internal_transfer_sber_to_tbank` | 38 | 18792000.00 | 42.53% |
| `revenue_acquiring_sber` | 601 | 10473123.54 | 23.70% |
| `supplier_payment` | 208 | 5429979.40 | 12.29% |
| `payroll_payment` | 21 | 3363645.00 | 7.61% |
| `other_outflow` | 200 | 3162703.77 | 7.16% |
| `other_inflow` | 14 | 1565819.62 | 3.54% |
| `tax_payment` | 12 | 738728.79 | 1.67% |
| `revenue_acquiring_tbank` | 41 | 448706.00 | 1.02% |
| `bank_fee` | 68 | 105810.69 | 0.24% |
| `loan_payment` | 18 | 99899.36 | 0.23% |
| `refund` | 3 | 2936.89 | 0.01% |

## Выручка

- Sber-эквайринг и договорные платежи: 10262387.50 руб.
- T-Bank-эквайринг, гипотеза требует проверки: 448706.00 руб.
- Среднее покрытие iiko банком за февраль-апрель после добавления T-Bank: 81.03%.

| Месяц | Sber | T-Bank | Банк всего | iiko | Покрытие |
| --- | ---: | ---: | ---: | ---: | ---: |
| 2026-02 | 2932164.26 | 198413.00 | 3130577.26 | 3833597.82 | 81.66% |
| 2026-03 | 3156477.58 | 174692.00 | 3331169.58 | 4006534.78 | 83.14% |
| 2026-04 | 2558836.39 | 45362.00 | 2604198.39 | 3326144.31 | 78.29% |
| 2026-05 | 1614909.27 | 30239.00 | 1645148.27 | 2082482.51 | 79.00% |

## Внутренние Переводы

- Контроль Sber -> T-Bank: ok.

| Месяц | Sber outflow | T-Bank inflow | Diff | Status |
| --- | ---: | ---: | ---: | --- |
| 2026-02 | 2375000.00 | 2375000.00 | 0.00 | ok |
| 2026-03 | 3311000.00 | 3311000.00 | 0.00 | ok |
| 2026-04 | 1991000.00 | 1991000.00 | 0.00 | ok |
| 2026-05 | 1719000.00 | 1719000.00 | 0.00 | ok |

## Открытые Вопросы

1. `tbank` / `tax_payment` / `tax`: 5 операций, 736428.79 руб. - Разделить налоги с З/п и прочие налоги.
2. `tbank` / `payroll_payment` / `contragentPeople`: 1 операций, 367491.00 руб. - Подтвердить классификацию.
3. `tbank` / `payroll_payment` / `contragentPeople`: 2 операций, 333255.00 руб. - Подтвердить классификацию.
4. `tbank` / `other_inflow` / `depositFullWithdrawal`: 1 операций, 300000.00 руб. - Определить экономический смысл группы операций.
5. `tbank` / `other_inflow` / `depositFullWithdrawal`: 1 операций, 300000.00 руб. - Определить экономический смысл группы операций.

## Файлы

- `research/private/bank_own_accounts_registry.csv`
- `research/private/bank_operation_rules.csv`
- `research/private/sber/statement_classified.csv`
- `research/private/tbank/statement_classified.csv`
- `research/processed/cashflow/dds_by_article_2026.csv`
- `research/processed/cashflow/revenue_split.csv`
- `research/processed/cashflow/iiko_vs_bank_reconciliation.csv`
- `research/processed/cashflow/internal_transfer_check.csv`
- `research/processed/cashflow/unclassified_operations_summary.csv`
