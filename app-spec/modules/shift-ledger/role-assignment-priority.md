# Учёт смен: приоритет автоматического проставления роли/категории

Зафиксированная логика того, как для записи в журнале смен (`shift_ledger_entry`) выбирается значение `payroll_role` и `category` при автоматическом построении (build_ledger_for_date) и какие источники не перетираются.

Источник истины по коду: `apps/api/app/services/shift_ledger.py`, функции `build_ledger_for_date`, `resolve_default_assignment`, `manually_correct`.

Связанные документы: `app-spec/modules/staff/taxonomy.md` (роли/категории), `app-spec/modules/finance/payroll.md` (расчёт ЗП).

## Четыре источника, разный приоритет

Каждая запись `shift_ledger_entry` хранит поле `source` — откуда пришли её `payroll_role` и `category`. Значения и их приоритет (сверху — выше):

| # | source | Что это | Когда возникает | Перезаписывается ли при повторном build? |
|---|---|---|---|---|
| 1 | `manual_correction` | Менеджер вручную в UI «Учёт смен» поставил роль | Через PATCH `/shifts/ledger/{id}` или вызов `manually_correct()` | **Нет** — защищена от автологики |
| 2 | `schedule` | Из графика сотрудников на эту дату | Запись в таблице графика (`scheduled_shift` / `shift_schedule_entry` / `employee_schedule_shift`) содержит роль, и эта роль есть среди текущих активных ролей сотрудника | Да |
| 3 | `fallback_primary` | Основная или единственная активная роль сотрудника | График пуст ИЛИ роль из графика не закреплена за сотрудником | Да |
| 4 | `fallback_primary` (с пустыми `payroll_role` / `category`) | Не из чего взять | У сотрудника вообще нет активных ролей | Да |

Случаи 3 и 4 различаются содержимым полей, не значением `source`. Это семантическая неточность в кодировке источника, не функциональная.

## Алгоритм `resolve_default_assignment`

Псевдокод (см. `shift_ledger.py:560`):

```
если у сотрудника нет ни одной активной роли:
    вернуть пусто, source="fallback_primary"

если в графике есть назначение на эту дату И его роль есть в активных ролях сотрудника:
    вернуть эту роль (с её категорией из активных ролей), source="schedule"

если у сотрудника ровно одна активная роль:
    вернуть её, source="fallback_primary"

если среди активных ролей есть отмеченная is_primary=true:
    вернуть её, source="fallback_primary"

иначе:
    вернуть пусто, source="fallback_primary"
```

## Защита ручной правки

В `build_ledger_for_date` (см. `shift_ledger.py:118`):

```python
if entry.source != "manual_correction":
    entry.payroll_role = default_assignment.payroll_role
    entry.category = default_assignment.category
    entry.source = source
```

Если у существующей записи `source == "manual_correction"` — её роль/категория НЕ перезатираются. То есть как только менеджер явно поставил роль через UI, эта запись становится «sticky» — последующие синки/перестроения её не трогают.

Это означает: чтобы «передовать» график → manual → новый график, нужно либо явно очистить ручную правку, либо менеджер должен заново выбрать роль в UI (тогда `manually_correct()` обновит запись, оставив `source="manual_correction"`).

## Активные роли сотрудника — что считается «активной»

Используется `load_currently_active_role_assignments(session, employee_ids)` (в матрице «Учёт смен») и `load_available_role_assignments(session, work_date, ids)` (в payroll-расчёте — исторически корректно).

- В матрице UI «Учёт смен»: активные роли = текущие, т.е. `effective_to IS NULL OR effective_to > today()`. Применяются ОДИНАКОВО ко всем 7 дням недели — менеджер может проставить роль задним числом для любой даты в незакрытом payroll-периоде.
- В payroll-расчёте: активные роли = на дату смены, т.е. `effective_from <= work_date AND (effective_to IS NULL OR effective_to > work_date)`. Это нужно, чтобы ЗП за прошлый месяц считалась по той роли, которая была у сотрудника тогда, а не сейчас.

## Заморозка по закрытой ЗП

После закрытия payroll-периода (`payroll_run.status IN ('completed', 'closed')`) все `shift_ledger_entry` с `work_date <= period_end` становятся read-only:
- PATCH `/shifts/ledger/{id}` для такой записи → 409 Conflict.
- В UI: Select заблокирован, иконка-замок, tooltip «ЗП за эту неделю закрыта».

Это не отменяет приоритет источников выше — просто блокирует любую правку (ручную или из перестроения).

## Текущее состояние интеграции графика

На момент 2026-05-30 модуль «График сотрудников» ещё не реализован — соответствующая таблица в БД отсутствует. `load_schedule_assignments` через `find_schedule_shape` обнаруживает отсутствие таблицы и возвращает пустой dict. **Приоритет 2 (schedule) фактически не активен** — алгоритм всегда падает на приоритет 3 (primary role).

Когда модуль «График сотрудников» будет реализован и создаст таблицу с одним из ожидаемых имён (`scheduled_shift`, `shift_schedule_entry`, `employee_schedule_shift`) с колонками `employee_id`, `work_date` (или `shift_date`/`date`), `payroll_role`/`primary_role`/`role`/`station` — приоритет 2 включится без правок в `shift_ledger.py`.

## Что НЕ делает текущий алгоритм

- Не различает «единственная роль» и «основная из нескольких» — оба попадают в `source = "fallback_primary"`. Семантически точнее было бы `fallback_only_role` vs `fallback_primary`, но функционально работает.
- Не разрешает менеджеру в UI «отменить» ручную правку и вернуть auto-default. Workaround: менеджер может явно выбрать ту же роль, что предложила бы автологика — `source` останется `manual_correction`, но значения совпадут.
- Не пушит изменение `payroll_role` обратно в `EmployeeRoleAssignment`. Журнал смен — read-side артефакт расчёта; основная роль/категория правится только на странице «Штат».
