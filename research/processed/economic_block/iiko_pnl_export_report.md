# iiko P&L preset export

Дата выгрузки: 2026-05-18.

Quality flag от 2026-05-18: этот отчет был построен старой версией экспортера, где `dateTo` передавался как последний день месяца. Для endpoint `/v2/reports/olap/byPresetId/...` `dateTo` является исключающей верхней границей, поэтому такие строки могут не включать последний день периода. Контрольная перепроверка live API для апреля 2026 с `dateFrom=2026-04-01`, `dateTo=2026-05-01` дала `Зарплата курьеров = 248 401`; значение `238 043` ниже является неполным старым снимком.

Режим: read-only. Выполнены только GET-запросы к `byPresetId`; авторизация через `POST /auth` возможна только при истекшем токене. Google Sheets, iiko-данные и настройки не изменялись.

Контур: активная экономика — Foodmarket Тепло Черникова. Гагарина после января 2024 считается исторической и не смешивается с текущей экономикой.

Источник: iiko preset `P&L по складам`, reportType `TRANSACTIONS`, endpoint `/resto/api/v2/reports/olap/byPresetId/8c13763a-35bf-9f27-017f-5468b1e70021`.

Поля нормализации: `Account.Type`, `Account.Name`, `Store`, `Sum.ResignedSum`.

## Файлы

- Raw JSON: `research/raw/iiko/pnl/`, файлов: 7.
- Processed CSV: `research/processed/economic_block/iiko_pnl_by_preset_rows.csv`.
- Отчет: `research/processed/economic_block/iiko_pnl_export_report.md`.

## Статус периодов

| Период | Даты | Raw rows | Stores |
| --- | --- | ---: | --- |
| 2025-11 | 2025-11-01 — 2025-11-30 | 18 | (Прочее), Основной склад Черникова |
| 2025-12 | 2025-12-01 — 2025-12-31 | 17 | (Прочее), Основной склад Черникова |
| 2026-01 | 2026-01-01 — 2026-01-31 | 15 | (Прочее), Основной склад Черникова |
| 2026-02 | 2026-02-01 — 2026-02-28 | 16 | (Прочее), Основной склад Черникова |
| 2026-03 | 2026-03-01 — 2026-03-31 | 15 | (Прочее), Основной склад Черникова |
| 2026-04 | 2026-04-01 — 2026-04-30 | 19 | (Прочее), Кухня Черникова, Основной склад Черникова |
| 2026-05-01_2026-05-17 | 2026-05-01 — 2026-05-17 | 17 | (Прочее), Основной склад Черникова |

## Зарплата курьеров

| Период | Зарплата курьеров |
| --- | ---: |
| 2025-11 | 299 909 |
| 2025-12 | 340 599 |
| 2026-01 | 307 417 |
| 2026-02 | 268 593 |
| 2026-03 | 280 132 |
| 2026-04 | 238 043 |
| 2026-05-01_2026-05-17 | 143 108 |

## Totals By Account Type

| Период | Account.Type | Sum.ResignedSum |
| --- | --- | ---: |
| 2025-11 | COST_OF_GOODS_SOLD | 1 505 820 |
| 2025-11 | EXPENSES | 386 436 |
| 2025-11 | INCOME | 3 619 700 |
| 2025-11 | OTHER_EXPENSES | 36 254 |
| 2025-11 | OTHER_INCOME | 500 |
| 2025-12 | COST_OF_GOODS_SOLD | 1 630 622 |
| 2025-12 | EXPENSES | 441 562 |
| 2025-12 | INCOME | 4 121 346 |
| 2025-12 | OTHER_EXPENSES | 40 039 |
| 2025-12 | OTHER_INCOME | 2 630 |
| 2026-01 | COST_OF_GOODS_SOLD | 1 526 551 |
| 2026-01 | EXPENSES | 395 190 |
| 2026-01 | INCOME | 3 918 437 |
| 2026-01 | OTHER_EXPENSES | 62 637 |
| 2026-02 | COST_OF_GOODS_SOLD | 1 261 649 |
| 2026-02 | EXPENSES | 351 881 |
| 2026-02 | INCOME | 3 644 785 |
| 2026-02 | OTHER_EXPENSES | 28 932 |
| 2026-02 | OTHER_INCOME | 1 101 |
| 2026-03 | COST_OF_GOODS_SOLD | 1 322 903 |
| 2026-03 | EXPENSES | 311 236 |
| 2026-03 | INCOME | 3 880 557 |
| 2026-03 | OTHER_EXPENSES | 46 407 |
| 2026-03 | OTHER_INCOME | 10 000 |
| 2026-04 | COST_OF_GOODS_SOLD | 1 136 884 |
| 2026-04 | EXPENSES | 331 348 |
| 2026-04 | INCOME | 3 184 362 |
| 2026-04 | OTHER_EXPENSES | 165 420 |
| 2026-04 | OTHER_INCOME | 9 650 |
| 2026-05-01_2026-05-17 | COST_OF_GOODS_SOLD | 699 022 |
| 2026-05-01_2026-05-17 | EXPENSES | 199 491 |
| 2026-05-01_2026-05-17 | INCOME | 1 957 487 |
| 2026-05-01_2026-05-17 | OTHER_EXPENSES | 77 440 |
| 2026-05-01_2026-05-17 | OTHER_INCOME | 1 300 |

## Контроль Гагарина

Строк со store, содержащим `Гагарин`: 0.
В нормализованной выгрузке не обнаружены строки складов Гагарина.

## Raw Files

- `research/raw/iiko/pnl/iiko_pnl_by_preset_2025-11-01_2025-11-30.json`
- `research/raw/iiko/pnl/iiko_pnl_by_preset_2025-12-01_2025-12-31.json`
- `research/raw/iiko/pnl/iiko_pnl_by_preset_2026-01-01_2026-01-31.json`
- `research/raw/iiko/pnl/iiko_pnl_by_preset_2026-02-01_2026-02-28.json`
- `research/raw/iiko/pnl/iiko_pnl_by_preset_2026-03-01_2026-03-31.json`
- `research/raw/iiko/pnl/iiko_pnl_by_preset_2026-04-01_2026-04-30.json`
- `research/raw/iiko/pnl/iiko_pnl_by_preset_2026-05-01_2026-05-17.json`
