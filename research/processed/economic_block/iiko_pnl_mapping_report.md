# iiko P&L account mapping draft

Дата подготовки: 2026-05-18.

Цель: собрать управленческую карту известных `Account.Type` / `Account.Name` из локальных raw snapshots iiko P&L без расчета финального P&L и без подгонки цифр.

## Источники

Новые API-запросы не выполнялись. Использованы только локальные файлы:

- `docs/business-control/10-economic-block.md`
- `research/processed/economic_block/gs_finance_report.md`
- `research/processed/economic_block/gs_expense_mapping.csv`
- `research/raw/iiko/api_discovery/pnl_by_warehouses_*.json`

Выходной CSV:

- `research/processed/economic_block/iiko_pnl_account_mapping_draft.csv`

## Raw Snapshots

| Файл | Статус | Строк `data` | Комментарий |
| --- | ---: | ---: | --- |
| `pnl_by_warehouses_2026-02.json` | ok | 16 | Валидный snapshot. |
| `pnl_by_warehouses_2026-02_iso.json` | ok | 16 | Дубликат `pnl_by_warehouses_2026-02.json` по содержимому `data`. |
| `pnl_by_warehouses_2026-03.json` | ok | 15 | Валидный snapshot. |
| `pnl_by_warehouses_2026-04.json` | ok | 19 | Валидный snapshot. |
| `pnl_by_warehouses_2026-05-01_17.json` | ok | 17 | Валидный snapshot за неполный майский период. |
| `pnl_by_warehouses_2026-02_ddmmyyyy.json` | not_json | 0 | Содержит текст ошибки `DateTimeParseException`; не использован для списка счетов. |

Итого найдено 18 уникальных пар `Account.Type` / `Account.Name` в валидных snapshots.

## Mapping Summary

| Account.Type | Account.Name | Proposed block | Confidence | Комментарий |
| --- | --- | --- | --- | --- |
| `INCOME` | `Торговая выручка без учета скидок` | `revenue` | high | Основная валовая выручка до скидок. |
| `INCOME` | `Торговая выручка` | `revenue` | medium | Небольшая отдельная положительная строка; нужна расшифровка, чтобы не задвоить выручку. |
| `INCOME` | `Предоставленные скидки` | `discounts` | high | Отрицательная contra-revenue строка. |
| `COST_OF_GOODS_SOLD` | `Расход продуктов` | `food_cost` | high | Основной food cost из iiko P&L. |
| `COST_OF_GOODS_SOLD` | `Оплата недорогих продуктов через фронт` | `food_cost` | medium | Единичная строка; нужна проверка, что это именно food cost. |
| `COST_OF_GOODS_SOLD` | `Излишки инвентаризации` | `inventory_surplus_shortage` | high | Отрицательная строка, уменьшает потери/себестоимость. |
| `COST_OF_GOODS_SOLD` | `Недостача инвентаризации` | `inventory_surplus_shortage` | high | Положительная строка потерь/недостач. |
| `COST_OF_GOODS_SOLD` | `Удаление блюд со списанием` | `writeoffs` | high | Операционные списания. |
| `EXPENSES` | `Зарплата курьеров` | `couriers` | medium | В экономическом блоке уже выделена как источник курьеров; метод учета нужно подтвердить. |
| `EXPENSES` | `Затраты на персонал` | `payroll` | medium | Не заменяет основной ФОТ из Google Sheets без сверки состава. |
| `EXPENSES` | `Затраты на поиск сотрудников` | `payroll` | medium | Похоже на персональные расходы; владелец должен подтвердить блок. |
| `EXPENSES` | `Комиссия партнерам` | `partner_commissions` | high | Отдельный блок комиссий партнеров/агрегаторов. |
| `OTHER_INCOME` | `Прочие доходы` | `other_income` | medium | Нужна расшифровка состава и операционности. |
| `OTHER_EXPENSES` | `Актуализация` | `below_ebitda_or_unclear` | requiring_owner_confirmation | Спорная строка, не включать в EBITDA до подтверждения. |
| `OTHER_EXPENSES` | `Всякое Гагарина` | `below_ebitda_or_unclear` | requiring_owner_confirmation | Спорная строка; отдельно важно не смешать историческую Гагарина с текущей Черникова. |
| `OTHER_EXPENSES` | `Всякое Черникова` | `below_ebitda_or_unclear` | requiring_owner_confirmation | Спорная строка, нужна детализация. |
| `OTHER_EXPENSES` | `Перемещения` | `below_ebitda_or_unclear` | requiring_owner_confirmation | Может быть движением запасов/межскладской операцией, а не расходом. |
| `OTHER_EXPENSES` | `Прочие расходы` | `below_ebitda_or_unclear` | requiring_owner_confirmation | Спорная агрегированная строка, нужна расшифровка. |

## Sign Logic

Базовая мера в snapshots: `Sum.ResignedSum`. Ее нужно сохранять как signed value и не превращать автоматически в модуль.

- `INCOME`: положительные значения увеличивают выручку; `Предоставленные скидки` являются contra-revenue и в snapshots идут отрицательными значениями.
- `COST_OF_GOODS_SOLD`: положительные значения увеличивают себестоимость/потери; отрицательные значения по излишкам уменьшают себестоимость/потери.
- `EXPENSES`: положительные значения являются расходами и вычитаются в управленческом P&L; отрицательные значения, если появятся, трактуются как сторно/возврат расхода.
- `OTHER_INCOME`: положительные значения добавляются как прочие доходы, но состав нужно подтвердить.
- `OTHER_EXPENSES`: строки не включаются в EBITDA автоматически. Для `Актуализация`, `Всякое`, `Перемещения`, `Прочие расходы` требуется owner confirmation.

## Owner Questions

1. Что означает отдельная строка `Торговая выручка` рядом с `Торговая выручка без учета скидок`: дополнительная выручка, корректировка или техническая строка?
2. Подтвердить, что `Оплата недорогих продуктов через фронт` управленчески относится к food cost.
3. Подтверждаем ли `Зарплата курьеров` как факт курьерских расходов для P&L, и это начисление или выплата?
4. Что входит в iiko строку `Затраты на персонал`, и как она соотносится с Google Sheets `Расчет зарплат NEW`?
5. `Затраты на поиск сотрудников` относить к payroll или к operating_expenses?
6. Что входит в `Прочие доходы`, и это операционный доход или ниже EBITDA?
7. Расшифровать `Актуализация`, `Всякое Гагарина`, `Всякое Черникова`, `Перемещения`, `Прочие расходы` до любого использования в P&L.

## Ограничения

Финальный P&L не считался. Суммы не подгонялись и не сверялись с целевым результатом. Этот файл является управленческим справочником для следующего шага: согласовать спорные строки с владельцем и только после этого применять маппинг к месячным P&L выгрузкам.
