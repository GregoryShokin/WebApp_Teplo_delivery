# Разведка: связь «График сотрудников ↔ Учёт смен» и роль Шевченко Любы

**Статус:** разведка (без правок кода). **Изолированная среда:** worktree `../Teplo-agent-schedrole`, ветка `agent/schedrole-shift-role-recon`.
**Дата:** 2026-07-10. **Проверка:** состязательная (4 независимых скептика-агента), **4/4 CONFIRMED, high confidence**.

---

## TL;DR

1. **Задача 1 — ПОДТВЕРЖДЁННЫЙ БАГ.** Учёт смен действительно игнорирует график и всегда ставит **главную роль**. Правильная логика («сначала график, иначе главная роль») **уже написана** в резолвере — но график до него не доходит: функция угадывания таблицы графика ищет колонку даты среди `("work_date","shift_date","date")`, а реальная колонка называется **`business_date`**. Из-за этого график «не находится», загрузка возвращает пусто, и роль всегда падает в главную. Тесты баг не ловят, потому что мокают загрузчик.

2. **Задача 2 — НЕ баг данных и НЕ баг ролей, а UI-соглашение отображения.** Главная роль Любы — **Пиццерист** (это гарантируют инварианты модели). У неё есть доп-квалификация «подменный Сушист», и на июльские смены её поставили подменным сушистом — **осознанно** (авто такую роль система не проставляет). Подпись под именем «Повар · Сушист [подмена]» берётся **не из главной роли**, а из роли **первой смены** в видимом периоде (`firstVisibleShiftRole`). Поэтому и выглядит, будто главная роль стала Сушист.

3. **Связь двух задач:** из-за бага №1 даже правильно поставленная в графике подмена (Люба = Сушист 13.07) в учёте смен начислится как её **главная** роль (Пиццерист). Починка №1 доводит роль из графика (включая подмены) до леджера — тогда учёт смен и график становятся согласованы.

---

## Как устроен контур (карта)

- **«Учёт смен»** = таблица `shift_ledger_entry`. Строится функцией `build_ledger_for_date()` — [apps/api/app/services/shift_ledger.py:72](apps/api/app/services/shift_ledger.py#L72). Точки входа — роутер [apps/api/app/api/v1/routes/shifts.py:63](apps/api/app/api/v1/routes/shifts.py#L63) (`POST /ledger/build`, `/ledger/build-week`).
- **«График сотрудников»** = таблицы `shift_schedule` (версия графика: draft/published/superseded) + `scheduled_shift` (строка «сотрудник × день × роль»). Модель — [apps/api/app/models/shift_schedule.py](apps/api/app/models/shift_schedule.py). Каждая строка графика хранит **явную роль** `payroll_role` ([shift_schedule.py:93](apps/api/app/models/shift_schedule.py#L93)) — то есть управляющий уже назначает конкретную роль на день (сушист/пиццерист), как и требуется.
- **Словарь ролей согласован.** И `scheduled_shift.payroll_role`, и `EmployeeRoleAssignment.payroll_role` хранят **коды** (`sushi/pizza/shawarma/prep/administrator`; русские названия — только ярлыки, `PAYROLL_ROLE_LABELS` в [staff_taxonomy.py:21](apps/api/app/services/staff_taxonomy.py#L21)). Валидация upsert графика (`_resolve_role`, [shift_schedule_service.py:577](apps/api/app/services/shift_schedule_service.py#L577)) это гарантирует. → После починки колонки роль сматчится «код-в-код», доп-маппинг не нужен.

---

## Задача 1. Учёт смен всегда ставит главную роль вместо роли из графика

### Что происходит (пошагово)

При построении леджера для каждой отработанной смены роль выбирает `resolve_default_assignment()` — [shift_ledger.py:635](apps/api/app/services/shift_ledger.py#L635):

```python
if schedule_assignment is not None and schedule_assignment.payroll_role is not None:
    scheduled_role = assignment_by_role(available_roles, schedule_assignment.payroll_role)
    if scheduled_role is not None:
        return scheduled_role, "schedule"        # ← роль из ГРАФИКА (нужное поведение)
if len(available_roles) == 1:
    return available_roles[0], "fallback_primary"
primary_assignment = next((a for a in available_roles if a.is_primary), None)
if primary_assignment is not None:
    return primary_assignment, "fallback_primary" # ← ГЛАВНАЯ роль (запасной вариант)
```

**Логика правильная и переписывать её не нужно** — она в точности реализует правило владельца: «сначала роль из графика; если графика нет — главная роль». Проблема **выше по стеку**: аргумент `schedule_assignment` всегда приходит пустым.

### Корневая причина

`build_ledger_for_date` берёт роли из графика через `load_schedule_assignments()` → `find_schedule_shape()` — [shift_ledger.py:592](apps/api/app/services/shift_ledger.py#L592). Это «угадыватель схемы»: он перебирает список таблиц-кандидатов и колонок по именам:

```python
SCHEDULE_TABLE_CANDIDATES = ("scheduled_shift", "shift_schedule_entry", "employee_schedule_shift")  # :61
SCHEDULE_DATE_COLUMNS     = ("work_date", "shift_date", "date")                                     # :66
```

- Реальная колонка даты в `scheduled_shift` — **`business_date`** ([модель shift_schedule.py:89](apps/api/app/models/shift_schedule.py#L89); [миграция 0032_shift_schedule_base.py:64](apps/api/alembic/versions/0032_shift_schedule_base.py#L64)). Её **нет** в `SCHEDULE_DATE_COLUMNS`.
- `find_schedule_shape` для `scheduled_shift` не находит колонку даты → `date_column is None` → `continue` ([shift_ledger.py:599](apps/api/app/services/shift_ledger.py#L599)), таблица пропущена.
- Две другие таблицы-кандидата (`shift_schedule_entry`, `employee_schedule_shift`) **не существуют** нигде в коде/миграциях — проверено grep'ом.
- Итог: `find_schedule_shape` возвращает `None` → `load_schedule_assignments` возвращает `{}` ([shift_ledger.py:546](apps/api/app/services/shift_ledger.py#L546)) → `schedule_assignment` всегда `None` → резолвер всегда падает в `fallback_primary` (главную роль).

**Симптом ровно тот, что описал владелец:** роль всегда главная, вне зависимости от графика.

### Почему баг не поймали автотесты

В `test_payroll.py` загрузчик графика **замокан**: `monkeypatch.setattr(shift_ledger_service, "load_schedule_assignments", fake_schedule)` — [test_payroll.py:2218](apps/api/tests/test_payroll.py#L2218) (и :2442). Реальные `find_schedule_shape`/`load_schedule_assignments` против реальной таблицы `scheduled_shift` не вызываются никогда, поэтому рассинхрон имени колонки для suite невидим.

### Вторичная находка (ПОДТВЕРЖДЕНА) — нет фильтра по статусу графика

Даже после починки колонки запрос в `load_schedule_assignments` ([shift_ledger.py:566-575](apps/api/app/services/shift_ledger.py#L566)) выбирает `from scheduled_shift where <дата> = :work_date` **без фильтра/джойна по `shift_schedule.status`**. При этом:
- версии графика **не удаляются**: при публикации предыдущий график помечается `superseded` ([shift_schedule_service.py:176](apps/api/app/services/shift_schedule_service.py#L176)), черновики тоже живут;
- уникальность `scheduled_shift` — только внутри одной версии: `(shift_schedule_id, business_date, employee_id)` ([shift_schedule.py:75](apps/api/app/models/shift_schedule.py#L75)).

→ Одна и та же `(business_date, employee_id)` может иметь строки под draft + published + superseded одновременно. Без фильтра статуса и без `ORDER BY` цикл `assignments[employee_id] = ...` ([shift_ledger.py:585](apps/api/app/services/shift_ledger.py#L585)) оставляет **последнюю** строку в произвольном порядке БД → можно подтянуть устаревшую (superseded) или неопубликованную (draft) роль вместо действующей published. Чинить надо вместе с основным багом.

### План исправления (Задача 1)

**Рекомендуемый вариант — переписать загрузчик на прямой ORM-запрос** (устраняет обе проблемы разом и убирает хрупкое «угадывание схемы»):

1. В `load_schedule_assignments` заменить `find_schedule_shape` + raw-SQL на ORM-запрос к `ScheduledShift` c джойном на `ShiftSchedule`:
   - `WHERE ShiftSchedule.status = 'published'` (и, если действующих версий несколько, брать самую свежую по `published_at`);
   - `WHERE ScheduledShift.business_date = :work_date AND ScheduledShift.employee_id IN (...)`;
   - вернуть `{employee_id: LedgerAssignment(payroll_role=..., category=None)}`.
   Так же, как это уже делают другие сервисы: `payroll_forecast_run_service` ([:249](apps/api/app/services/payroll_forecast_run_service.py#L249)) и `inventory_audit_service` ([:1975](apps/api/app/services/inventory_audit_service.py#L1975)) — они читают `ScheduledShift.business_date` через ORM корректно. Категорию не тащим из графика — резолвер и так восстановит её из `available_roles` (роль-в-роль).
2. Удалить мёртвую машинерию `find_schedule_shape` / `SCHEDULE_TABLE_CANDIDATES` / `SCHEDULE_DATE_COLUMNS` / `table_columns` (или оставить как приватный фолбэк, но она больше не нужна).
3. **Тесты:** добавить интеграционный тест `build_ledger_for_date` **без мока** загрузчика — реальные `scheduled_shift` строки (published) + двуролевой сотрудник → леджер получает роль из графика (`source="schedule"`), а без строки графика → главную (`source="fallback_primary"`). Отдельный кейс: draft/superseded строки на ту же дату **не** влияют на результат.

**Хотфикс-минимум (если нужно срочно):** добавить `"business_date"` в `SCHEDULE_DATE_COLUMNS` ([shift_ledger.py:66](apps/api/app/services/shift_ledger.py#L66)). Снимет основной симптом, **но оставит** неоднозначность по статусу графика (draft/superseded). Как самостоятельное решение не рекомендую.

**После выкатки** нужно **пересобрать леджер** за уже затронутые периоды (`POST /ledger/build-week`), т.к. существующие строки со `source != "manual_correction"` перезапишутся корректной ролью; ручные корректировки (`source="manual_correction"`) не трогаются ([shift_ledger.py:122](apps/api/app/services/shift_ledger.py#L122)) — это правильно.

> ⚠️ Зона внимания: параллельный агент `agent/b-shift-ledger` (worktree `Teplo-agent-b`) уже работает по shift-ledger. Перед реализацией — согласовать, чтобы не разъехались правки в `shift_ledger.py`.

---

## Задача 2. Почему Люба в графике «Сушист» при главной роли Пиццерист

### Ответ: это не ошибка данных, а два наложившихся факта

**(а) Главная роль Любы действительно Пиццерист.** Это гарантируют инварианты модели:
- значок «подмена/зам» = флаг `is_substitute = true` на роли;
- `is_substitute` и `is_primary` **взаимоисключающи** — три независимых гарда: Pydantic Create ([employees.py:49](apps/api/app/schemas/employees.py#L49) «Подменная роль не может быть основной»), Patch (:64), сервис записи ([employee_assignments.py:186](apps/api/app/services/employee_assignments.py#L186) `is_primary=False if is_substitute else ...`);
- плюс партиал-уникальный индекс «один открытый primary на сотрудника» ([employee.py:331](apps/api/app/models/employee.py#L331)).

→ Раз у её Сушиста стоит «подмена» (`is_substitute=true`), то её `is_primary` **заведомо** на другой роли — Пиццеристе. Модель не позволяет иного.

**(б) Роль «Сушист» на смене поставлена осознанно, автоматом её не подставить.** При быстром создании смены с пустой ячейки фронт шлёт только `{employee_id, business_date}` ([schedule.tsx:930](apps/web/src/routes/schedule.tsx#L930)), а бэкенд резолвит роль **строго по `is_primary`** ([shift_schedule_service.py:590](apps/api/app/services/shift_schedule_service.py#L590)) и даёт 422, если primary нет. Подменную роль можно проставить **только явным выбором** — в диалоге роли или кликом по конкретной станции (Роллы). То есть Люба поставлена подменным сушистом менеджером намеренно.

**(в) Причина визуальной путаницы — подпись строит роль из ПЕРВОЙ смены периода, а не из главной роли.** Подпись под именем рендерит `EmployeeRoleSubtitle` ([schedule.tsx:3785](apps/web/src/routes/schedule.tsx#L3785)), которому передаётся `firstVisibleShiftRole(...)` ([schedule.tsx:3483](apps/web/src/routes/schedule.tsx#L3483)):

```js
function firstVisibleShiftRole(employee, days, shifts) {
  for (const day of days) {                         // :5615
    const shift = shifts.get(`${employee.id}:${day}`);
    if (shift?.payroll_role) return shift.payroll_role; // ← роль ПЕРВОЙ смены в периоде
  }
  return primaryRoleLabelSource(employee);          // ← главная роль — только если смен нет
}
```

Значок «подмена» рядом с ролью зажигается по `is_substitute` этой роли ([schedule.tsx:3798](apps/web/src/routes/schedule.tsx#L3798)). Т.к. первая июльская смена Любы — подменный Сушист, подпись показывает «Повар · Сушист [подмена]», хотя её постоянная роль — Пиццерист. Бэкенд при этом отдаёт настоящую главную роль в `primary_payroll_role` ([shift_schedule_service.py:466](apps/api/app/services/shift_schedule_service.py#L466)) — она просто не используется в подписи.

### Баг это или дизайн?

Строго говоря — **осознанное UI-соглашение**, но **сбивающее с толку** именно для двуролевых сотрудников на подмене: подпись читается как «постоянная роль», а показывает «роль этого периода». Это **продуктовое решение владельца**, а не однозначная ошибка. Варианты:

- **Вариант A (рекомендую) — подпись = постоянная (главная) роль.** Передавать в `EmployeeRoleSubtitle` не `firstVisibleShiftRole`, а `primaryRoleLabelSource(employee)`. Тогда под именем всегда «Повар · Пиццерист», а фактическая роль на каждый день и так видна в ячейках (с бейджем «зам»). Минимальная правка одной строки ([schedule.tsx:3483](apps/web/src/routes/schedule.tsx#L3483)), меньше всего путаницы.
- **Вариант B — показывать обе роли:** «Повар · Пиццерист · сегодня: Сушист (подмена)». Информативнее, но подпись длиннее.
- **Вариант C — оставить как есть** (подпись = роль периода). Если владелец считает полезным видеть в шапке, кем человек по факту работает этот период.

### Обязательная финальная проверка данных (чтобы исключить мисконфигурацию в Штате)

Инварианты модели делают состояние «primary=Пиццерист, substitute=Сушист» единственно возможным при бейдже «подмена», но **на 100%** это подтверждается только запросом к БД. Проверить строки `employee_role_assignment` (модель [employee.py:304](apps/api/app/models/employee.py#L304)) для Шевченко Любы, активные на сегодня:

```sql
SELECT r.payroll_role, r.is_primary, r.is_substitute, r.effective_from, r.effective_to
FROM employee_role_assignment r
JOIN employee e ON e.id = r.employee_id
WHERE e.full_name ILIKE '%Шевченко Люб%'
  AND r.effective_from <= CURRENT_DATE
  AND (r.effective_to IS NULL OR r.effective_to >= CURRENT_DATE);
```

Ожидаем: строка `is_primary=true` → `payroll_role='pizza'`, `is_substitute=false`; строка `payroll_role='sushi'` → `is_substitute=true`, `is_primary=false`. **Если** `is_primary=true` оказалось на `'sushi'` — тогда это настоящая мисконфигурация Штата (и она же «подкормила» баг №1: главная роль стала бы Сушист), исправлять надо в Штате.

---

## Приоритеты

1. **Задача 1 (баг, деньги)** — высокий приоритет: искажает начисление ЗП двуролевым (подмена начисляется как главная роль). Чинить загрузчик графика + фильтр по `published` + интеграционный тест. Согласовать с `agent/b-shift-ledger`.
2. **Проверка данных Любы (SQL)** — 5 минут, снимает последнюю неопределённость по задаче 2 и заодно страхует задачу 1.
3. **Задача 2 (UX подписи)** — низкий приоритет и требует решения владельца (варианты A/B/C). Рекомендация — Вариант A.

---

## Приложение. Проверка выводов

Состязательная верификация 4 независимыми агентами (каждый пытался **опровергнуть** тезис), все **CONFIRMED, high**:

| Тезис | Вердикт |
|---|---|
| T1-datecol: график никогда не читается из-за `business_date` не в списке колонок | ✅ CONFIRMED (перебрал 5 путей опровержения) |
| T1-logic: резолвер уже реализует нужное правило; тесты слепы из-за мока; +нет фильтра статуса | ✅ CONFIRMED |
| T2-subtitle: подпись = роль первой смены, не главная; бейдж «подмена» строго по `is_substitute` | ✅ CONFIRMED |
| T2-datamodel: `is_substitute`⇒не primary (3 гарда+индекс); подмену нельзя проставить авто | ✅ CONFIRMED |

Единственное, что не проверяется из кода — фактические строки `employee_role_assignment` Любы (см. SQL выше).
