# Gap analysis: таксономия сотрудников

Источник истины: `docs/business-control/staff/taxonomy.md`, фиксация владельца 2026-05-29.

## A. Бэкенд: модели, схемы, сервисы

- `apps/api/app/models/employee.py`: есть `position`, legacy-shortcut `category/default_cooking_station`, флаги `is_senior/is_deputy_senior`, assignments с ролями `sushi/pizza/shawarma/prep/administrator/manager`; нет `pin_hash/pin_set_at`; нет check constraint по 6 каноническим должностям. Должно быть: position только из 6 должностей; payroll roles только `administrator/sushi/pizza/shawarma/prep`; category nullable только там, где роль без категорий; ПИН хранится хешем. Меняем: добавляем ПИН-поля, убираем `manager` из employee role assignments, добавляем централизованную валидацию таксономии; legacy `category/default_cooking_station` оставляем deprecated для совместимости.
- `apps/api/app/models/payroll.py`: payroll config хранит `position_group` как человекочитаемое имя роли, availability по `position_group/category`, категории включают `freelancer`. Должно быть: availability отражает категории ролей из таксономии; у курьера категорий нет. Меняем seed/data-migration availability; `freelancer` помечаем legacy deprecated, скрываем из UI, backend-совместимость оставляем до решения владельца.
- `apps/api/app/schemas/employees.py`: create уже принимает `pin_code`, но `EmployeeRead` не отдаёт `pin_set_at`; roles обязательны всегда; patch не умеет атомарно менять assignments и ПИН. Должно быть: create требует ПИН и roles только для должностей с ролями; наружу только `pin_set_at`; смена ПИН отдельным действием. Меняем схемы create/update/pin, `pin_set_at` в read.
- `apps/api/app/services/employee_status.py`: `TARGET_POSITION_ALIASES` включает роли/алиасы (`Сушист`, `Шеф-повар`, `Администратор`) и не включает `Курьер/Системный администратор`; группы `cook/staff` смешивают должность и роль; `STAFF_PAYROLL_ROLES` включает `manager`. Должно быть: 6 должностей, роли только у Кассира/Повара, неканонические iiko-должности пропускаются. Меняем на канонические position helpers и role/category rules.
- `apps/api/app/services/employee_assignments.py`: role/category validation основана на общем списке и availability; `_ensure_category_available` ошибочно мапит code роли в label через `PAYROLL_ROLE_LABELS`, допускает `manager`, не проверяет position сотрудника. Должно быть: cascade position -> payroll_role -> category. Меняем validation на таксономию, запрет роли чужой должности, сохранение одной primary.
- `apps/api/app/services/payroll_config.py`: category order включает `freelancer`; enabled categories берутся из БД, не из канона. Должно быть: канонические enabled rows; legacy скрыт из UI. Меняем labels/order для UI, оставляем backend legacy.
- `apps/api/app/services/iiko_sync.py`: sync уже пропускает не-target для новых, но existing non-target деактивирует, не удаляет; target aliases сейчас шире канона; create не проверяет отфильтрованные iiko роли в глубине. Должно быть: все не из 6, включая `Кассир-фастфуда`, не создаются; исторические неканонические удаляются миграцией. Меняем target helpers, filtering, create guard.
- `apps/api/app/api/v1/routes/employees.py`: `/iiko-roles` возвращает все активные iiko roles; create показывает/разрешает только 4 должности, нет Курьера; roles обязательны даже для должностей без ролей; patch редактирует `position` строкой и legacy `category/default_cooking_station`, без audit для role/category/premium/pin. Должно быть: `/iiko-roles` только 5 create-должностей; create/edit cascade; patch валидирует premiums; assignments/pin audit. Меняем endpoints и helpers.

## B. Seed и миграции

- Alembic seeds: `0008` содержит legacy categories `4/5/6`, `0009` нормализует `4 -> intern`, `5 -> intern/category_3`, `6 -> freelancer`; `0010` создаёт availability из `payroll_rate`; `0016_add_category_4` включает `category_4` только для Шаурмиста. Должно быть: Администратор `2/3/4/intern`, Сушист и Пиццерист `1/2/3/intern`, Шаурмист `3/4/intern`, Заготовщик только `3`, Курьер без категорий. Меняем новой ревизией, которая upsert/disable availability и добавит недостающую `Администратор/category_4`.
- `payroll_role_category_availability`: сейчас нет роли Курьер, потому что таблица ролевая, не должностная; это корректно, но UI/API не должны требовать категорию у Курьера. Меняем service validation, а не добавляем категории Курьеру.
- Legacy `freelancer` и старые коды `4/5/6`: требуют решения владельца. Не удаляем; помечаем deprecated, скрываем из UI, backend-compatible.

## C. Frontend: формы Штата

- `apps/web/src/routes/staff.tsx`: create role selector фильтрует только `Кассир/Менеджер/Повар/Управляющий`, не показывает Курьера; роли показываются всегда; роль `manager` есть в UI; категории общие; надбавки показываются всегда; edit position свободный input; ПИН есть только при создании, без `pin_set_at` и отдельной смены. Должно быть: create positions `Кассир/Менеджер/Повар/Управляющий/Курьер`; роли скрыты для должностей без ролей; role/category cascade; premiums по применимости; edit с отдельной сменой ПИН. Меняем форму create и drawer edit.
- `apps/web/src/lib/i18n/employee.ts`: labels содержат `freelancer` и `manager`. Должно быть: `freelancer` deprecated/hidden, `manager` не payroll role. Меняем labels/types usage.
- `apps/web/src/lib/api.ts`: `PayrollRole` включает `manager`; `Employee` не имеет `pin_set_at`; нет API для смены ПИН. Меняем типы и client method.

## D. ПИН-код для открытия смены

- Сейчас create принимает и отправляет ПИН в iiko, но локально не хранит; в модели/миграциях нет `pin_hash/pin_set_at`; наружу нет метаданных. Должно быть: обязательный 4-значный ПИН при создании, bcrypt hash локально, наружу только `pin_set_at`, отдельное действие смены. Меняем модель, миграцию, схемы, route, UI, audit.

## E. iiko sync: фильтрация и чистка

- Сейчас target filtering шире канона и existing non-target деактивируются, но не удаляются; `Кассир-фастфуда`, `Шеф-повар`, `Администратор` могут считаться target. Должно быть: только 6 должностей; `Кассир-фастфуда` и все прочие пропускаются; существующие non-canonical удаляются с зависимыми записями. Меняем helpers sync; добавляем отдельную data migration с delete order и audit/log count.

## F. Надбавки «Старший / Зам старшего»

- Сейчас флаги есть на Employee, но применимость не валидируется; UI показывает оба чекбокса всем; смена должности не сбрасывает невалидные флаги; audit есть только для lifecycle/dismiss. Должно быть: Кассир/Повар оба флага, Курьер только Старший, остальные без флагов; невалидное сохранение запрещено; при смене должности флаги сбрасываются и audit logs. Меняем backend validation, patch behavior, audit snapshots, UI visibility.
