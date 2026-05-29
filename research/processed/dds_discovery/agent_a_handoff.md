# Handoff: ДДС и платежный календарь

Дата: 2026-05-20.

Режим: read-only. Google Sheets не изменялись. Персональные строки платежей, назначения, ФИО, счета и договоры не вынесены. Суммы использованы только агрегированно.

## 1. Карта листов

ДДС:

- `ДДС: месяц` — главный журнал операций. Ручные поля: дата, сумма, кошелек, направление, контрагент, назначение, статья. Формульные поля: месяц, номер месяца, тип движения, вид деятельности.
- `Тинькофф`, `Альфа`, `Сбербанк`, `Сейф`, `ТК Черникова`, `ТК Гагарина`, `ГарантФонд` — расчетные локальные ДДС по кошелькам.
- `ДДС: Сводный` — общий отчет ДДС: operating / investing / financing cashflow, остатки и проверки.
- `ДДС: статьи` — справочник статей с `Группа` и `Вид деятельности`.
- `Справочники` — направления, виды деятельности, группы движения, месяцы.
- `Контрагенты` — приватный справочник контрагентов.
- `ДДС: настройки (для ввода сальдо)` — скрытый ручной ввод стартового месяца и остатков по кошелькам.
- `Технический лист` — служебные последовательности.
- `Сводная по направлениям` — в старом файле почти не реализована.

Платежный календарь:

- `Календарь` — недельный план-факт: поступления, платежи, изменение недели, остаток на конец.
- `Плановый Реестр поступлений` — ручной план входящих платежей.
- `Плановые Реестр выбытий` — ручной план оплат.
- `Факт ДДС` — импорт фактических операций из `ДДС: месяц`.
- `Справочник` и `Контрагенты` — импорт из ДДС.
- `Диапазон недель` — даты начала/конца недель.
- `Поставщики` — вспомогательная аналитика, не ядро модели.

## 2. Как считается cashflow и остатки

В `ДДС: месяц` сумма хранится со знаком: поступление положительное, выплата отрицательная. Статья определяет `Поступление/Выбытие` и вид деятельности через `ДДС: статьи`.

Остаток кошелька:

```text
opening_balance(month,wallet)
+ sum(transactions.amount where month and wallet)
= closing_balance(month,wallet)
```

Сводный ДДС:

```text
operating_cashflow + investing_cashflow + financing_cashflow = net_cashflow_report
opening_cash + net_cashflow_report = closing_cash_report
sum(closing_balance by wallet) = closing_cash_wallets
closing_cash_report - closing_cash_wallets = reconciliation_check
```

В старых данных проверка не сходится в 2023-08, 2023-11 и 2023-12, потому что статья `Услуги ФД` не совпадает со справочником и выпадает из классификации, хотя остатки кошельков меняет.

## 3. Формулы и проверки для переноса

- Классификация операции из статьи: `movement_type`, `activity_type`.
- Monthly roll-forward по каждому кошельку.
- Сводка по operating / investing / financing.
- Отдельный учет technical transfers.
- Проверка `closing_cash_report = sum(wallet_closing)`.
- Проверка внутренних переводов: пары переводов должны давать 0.
- Контроль `unclassified = 0` перед закрытием периода.
- Платежный календарь: weekly opening, planned inflows/outflows, actual inflows/outflows, net week, closing.
- План-факт связь: planned item может быть связан с фактической транзакцией.

## 4. Риски, которые нельзя переносить без исправления

- Старый ДДС 2023 нельзя использовать как факт 2026.
- Нельзя переносить текстовые статьи как единственный ключ; нужен `dds_article_id` и aliases.
- Нельзя использовать `IMPORTRANGE` как системную интеграцию календаря с ДДС.
- Нельзя оставлять операции с неизвестной статьей в закрытом периоде.
- Нельзя раскрывать назначения платежей, номера счетов и контрагентов в открытых отчетах.
- Недельный календарь нужно генерировать заново для актуального года.

## 5. Нужные сущности базы данных

- `accounts`: банк/касса/резерв, юридический владелец, тип, валюта, активность.
- `wallets`: конкретный кошелек из старого ДДС (`Тинькофф`, `Альфа`, `Сбербанк`, `Сейф`, торговые кассы, гарантийный фонд), `account_id`, тип кошелька, location/store.
- `cashflow_transactions`: дата, signed amount, wallet_id, article_id, counterparty_id, business_direction_id, private purpose/comment, source, import id, transfer_group_id.
- `dds_articles`: name, aliases, movement_type, activity_type, description, active flag.
- `counterparties`: name, group, privacy flags, optional external ids.
- `payment_calendar_items`: direction, status, due_date, amount, article_id, counterparty_id, wallet/account, private invoice number, private comment, linked_transaction_id.
- Дополнительно полезны: `wallet_balance_snapshots`, `calendar_weeks`, `cashflow_reconciliation_checks`, `business_directions`, `internal_transfer_links`.

Сопроводительные файлы этого прохода:

- `research/processed/dds_discovery/gs_dds_workbook_architecture.md`
- `research/processed/dds_discovery/gs_dds_formula_dependencies.csv`
- `research/processed/dds_discovery/payment_calendar_architecture.md`
- `research/processed/dds_discovery/dds_sheet_quality_risks.md`
