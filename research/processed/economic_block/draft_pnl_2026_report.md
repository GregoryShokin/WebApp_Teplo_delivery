# Draft P&L 2025-11 — 2026-05 MTD

Дата сборки: 2026-05-18.

Это черновой управленческий P&L, а не финальная операционная прибыль. Основная расчетная база для показателя `draft_operating_profit_before_unclear` — дневной iiko gross margin слой; iiko P&L preset показан рядом отдельным слоем для сверки выручки и food cost.

## Источники

- `research/processed/economic_block/iiko_pnl_by_preset_rows.csv`
- `research/processed/economic_block/iiko_pnl_account_mapping_draft.csv`
- `research/processed/economic_block/iiko_monthly_gross_margin.csv`
- `research/processed/economic_block/payroll_monthly.csv`
- `research/processed/economic_block/couriers_monthly.csv`
- `docs/business-control/10-economic-block.md`

## Методика

- `revenue_iiko_sales_daily`, `food_cost_sales_daily`, `gross_margin_sales_daily` взяты из `iiko_monthly_gross_margin.csv`.
- `revenue_iiko_pnl` рассчитана как `Торговая выручка без учета скидок + Торговая выручка + Предоставленные скидки` из iiko P&L preset. Строка `Торговая выручка` имеет medium-confidence, поэтому разложена отдельной audit-колонкой в CSV.
- `food_cost_iiko_pnl` — только high-confidence строка `Расход продуктов` из iiko P&L preset. Строка `Оплата недорогих продуктов через фронт` оставлена отдельной спорной колонкой до подтверждения.
- `payroll_accrual` взят из Google Sheets слоя `payroll_monthly.csv` по начислениям. iiko строки `Затраты на персонал` и `Затраты на поиск сотрудников` не задвоены с ФОТ и вынесены в отдельные `unclear_*` колонки.
- `courier_cost_iiko_pnl` — строка iiko P&L preset `Зарплата курьеров`.
- `partner_commissions` — строка iiko P&L preset `Комиссия партнерам`.
- `other_operating_expenses` содержит только прозрачные операционные COGS-корректировки из P&L: `Излишки/Недостача инвентаризации` и `Удаление блюд со списанием`.
- Все спорные строки показаны постатейно ниже и в CSV, а не спрятаны в `other_operating_expenses`.
- Формула: `draft_operating_profit_before_unclear = gross_margin_sales_daily - payroll_accrual - courier_cost_iiko_pnl - partner_commissions - other_operating_expenses`.

## Draft P&L

| Период | Выручка sales daily | Выручка P&L | Food cost sales daily | Food cost P&L | Валовая маржа sales daily | ФОТ начисл. | Курьеры P&L | Комиссии партнеров | Other operating | Unclear expenses | Draft profit before unclear |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 2025-11 | 3 815 263 | 3 619 700 | 1 458 907 | 1 436 662 | 2 356 357 | 1 233 648 | 299 909 | 60 975 | 73 138 | 57 825 | 688 686 |
| 2025-12 | 4 364 518 | 4 121 346 | 1 620 347 | 1 574 581 | 2 744 170 | 1 166 813 | 340 599 | 62 730 | 56 040 | 78 272 | 1 117 988 |
| 2026-01 | 4 095 915 | 3 918 437 | 1 438 806 | 1 466 540 | 2 657 109 | 1 123 666 | 307 417 | 56 383 | 60 011 | 94 027 | 1 109 632 |
| 2026-02 | 3 833 598 | 3 644 785 | 1 268 227 | 1 249 487 | 2 565 371 | 1 093 659 | 268 593 | 49 903 | 12 163 | 62 317 | 1 141 053 |
| 2026-03 | 4 006 535 | 3 880 557 | 1 316 670 | 1 284 468 | 2 689 865 | 1 126 341 | 280 132 | 0 | 38 435 | 77 511 | 1 244 957 |
| 2026-04 | 3 326 144 | 3 184 362 | 1 081 655 | 1 081 851 | 2 244 489 | 1 000 437 | 238 043 | 63 404 | 54 505 | 195 850 | 888 101 |
| 2026-05-01_2026-05-17 | 2 082 483 | 1 957 487 | 676 286 | 656 006 | 1 406 197 | 499 100 | 143 108 | 41 270 | 43 016 | 92 553 | 679 703 |

## Сверка sales daily vs P&L layer

Почти во всех месяцах есть существенные расхождения между дневным iiko gross margin слоем и iiko P&L preset. Поэтому food cost из двух источников не смешивался: оба значения сохранены рядом.

| Период | Revenue P&L - sales daily | Food cost P&L - sales daily | Статус |
| --- | ---: | ---: | --- |
| 2025-11 | -195 563 | -22 244 | draft_not_final; revenue_pnl_vs_sales_daily_delta=-195563.00; food_cost_pnl_vs_sales_daily_delta=-22244.37; couriers_sheet_payout_rules_missing; unclear_lines_excluded; unmapped_pnl_rows |
| 2025-12 | -243 171 | -45 766 | draft_not_final; revenue_pnl_vs_sales_daily_delta=-243171.11; food_cost_pnl_vs_sales_daily_delta=-45765.92; couriers_sheet_payout_rules_missing; unclear_lines_excluded |
| 2026-01 | -177 478 | 27 734 | draft_not_final; revenue_pnl_vs_sales_daily_delta=-177477.65; food_cost_pnl_vs_sales_daily_delta=27734.08; couriers_sheet_payout_rules_missing; unclear_lines_excluded |
| 2026-02 | -188 813 | -18 740 | draft_not_final; revenue_pnl_vs_sales_daily_delta=-188812.61; food_cost_pnl_vs_sales_daily_delta=-18740.42; couriers_sheet_payout_rules_missing; unclear_lines_excluded |
| 2026-03 | -125 978 | -32 201 | draft_not_final; revenue_pnl_vs_sales_daily_delta=-125978.00; food_cost_pnl_vs_sales_daily_delta=-32201.38; couriers_sheet_payout_rules_missing; unclear_lines_excluded; partner_commissions_zero_in_preset |
| 2026-04 | -141 782 | 196 | draft_not_final; revenue_pnl_vs_sales_daily_delta=-141782.03; food_cost_pnl_vs_sales_daily_delta=195.67; couriers_sheet_payout_rules_missing; unclear_lines_excluded |
| 2026-05-01_2026-05-17 | -124 995 | -20 280 | draft_not_final; revenue_pnl_vs_sales_daily_delta=-124995.02; food_cost_pnl_vs_sales_daily_delta=-20279.75; payroll_partial_month; couriers_sheet_no_courier_rows_for_month; unclear_lines_excluded; may_mtd_pnl_to_2026-05-17_payroll_to_2026-05-11 |

## Other Operating Expenses

| Период | Излишки инвентаризации | Недостача инвентаризации | Writeoffs | Other operating total |
| --- | ---: | ---: | ---: | ---: |
| 2025-11 | -74 442 | 144 732 | 2 848 | 73 138 |
| 2025-12 | -84 439 | 136 102 | 4 377 | 56 040 |
| 2026-01 | -118 580 | 176 245 | 2 346 | 60 011 |
| 2026-02 | -107 746 | 111 423 | 8 485 | 12 163 |
| 2026-03 | -93 418 | 126 682 | 5 171 | 38 435 |
| 2026-04 | -64 314 | 117 658 | 1 161 | 54 505 |
| 2026-05-01_2026-05-17 | -37 552 | 79 473 | 1 094 | 43 016 |

## Спорные строки постатейно

`unclear_expenses` — signed net unresolved impact of unclear expense-like rows. `Прочие доходы` показаны отдельно и не включены ни в прибыль до unclear, ни в `unclear_expenses`.

| Период | Затраты на персонал | Поиск сотрудников | Актуализация | Всякое Гагарина | Всякое Черникова | Перемещения | Прочие расходы | Недорогие продукты через фронт | Unmapped P&L | Unclear expenses total | Прочие доходы excluded |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 2025-11 | 25 552 | 0 | 9 893 | 0 | 25 284 | 1 000 | 77 | 0 | -3 980 | 57 825 | 500 |
| 2025-12 | 38 233 | 0 | -7 003 | 0 | 22 384 | 24 150 | 507 | 0 | 0 | 78 272 | 2 630 |
| 2026-01 | 31 390 | 0 | -14 | 0 | 30 802 | 31 850 | 0 | 0 | 0 | 94 027 | 0 |
| 2026-02 | 33 385 | 0 | -455 | 0 | 15 886 | 13 500 | 0 | 0 | 0 | 62 317 | 1 101 |
| 2026-03 | 31 104 | 0 | -2 | 0 | 45 409 | 1 000 | 0 | 0 | 0 | 77 511 | 10 000 |
| 2026-04 | 29 901 | 0 | -1 | 100 | 8 383 | 0 | 156 938 | 529 | 0 | 195 850 | 9 650 |
| 2026-05-01_2026-05-17 | 14 963 | 150 | 0 | 0 | 7 939 | 69 500 | 0 | 0 | 0 | 92 553 | 1 300 |

## Ключевые ограничения

1. Май — MTD: iiko P&L и gross margin покрывают 2026-05-01 — 2026-05-17, а ФОТ по `payroll_monthly.csv` имеет accrual span 2026-05-01..2026-05-11.
2. Строки `Затраты на персонал`/`Затраты на поиск сотрудников` в iiko P&L не включены в ФОТ, чтобы не задвоить Google Sheets начисления.
3. Все `OTHER_EXPENSES` из iiko P&L требуют owner confirmation: `Актуализация`, `Всякое Гагарина`, `Всякое Черникова`, `Перемещения`, `Прочие расходы`.
4. `Прочие доходы` из P&L не включены в черновую прибыль до расшифровки операционности.
5. В 2026-03 в P&L preset нет строки `Комиссия партнерам`; в CSV стоит 0 и статус отмечен как `partner_commissions_zero_in_preset`.
6. В 2025-11 есть unmapped P&L строка `Коррекция отрицательных остатков на складе`, оставлена в unclear signed value.

## Следующие вопросы владельцу

1. Подтвердить, какую выручку считать управленческой: дневной iiko слой или net revenue из P&L preset, и что означает отдельная строка `Торговая выручка`.
2. Подтвердить, включать ли `Оплата недорогих продуктов через фронт` в food cost.
3. Расшифровать `OTHER_EXPENSES` и решить, что является операционным расходом, а что ниже EBITDA/техническим движением.
4. Подтвердить, что `Зарплата курьеров` из iiko P&L является начислением для P&L, а не кассовой выплатой.
5. Дать актуальный источник аренды, коммунальных, маркетинга, эквайринга и банковских комиссий за 2025-11 — 2026-05.

CSV: `research/processed/economic_block/draft_pnl_2026.csv`.
