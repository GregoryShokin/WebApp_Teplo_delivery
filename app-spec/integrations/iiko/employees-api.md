# iiko employees API: явки, смены и payroll

Дата фиксации: 2026-05-20.

## Назначение

Этот документ фиксирует, какие endpoint'ы iikoServer API можно использовать для модуля `Зарплата и кадры` как источник журнала явки, графика смен, рабочих интервалов и справочных payroll-настроек.

Главный вывод: **`GET /resto/api/employees/attendance` является рабочим источником фактического журнала явки**. Старый вывод “iiko дал 0 часов” был ошибкой парсинга, а не отсутствием данных.

## Связь с другими модулями

- [08-iiko-server-api-endpoints.md](/app-spec/integrations/iiko/server-api-endpoints.md) — общая карта iiko API, авторизация, даты, ограничения.
- [09-iiko-first-export-audit.md](/app-spec/integrations/iiko/first-export-audit.md) — первый аудит, где персонал был ошибочно признан незакрытым по iiko.
- [17-unified-management-app.md](/docs/business-control/17-unified-management-app.md) — целевой модуль `Зарплата и кадры`.
- [19-payroll-module-spec.md](/docs/business-control/19-payroll-module-spec.md) — правила расчета payroll из Google Sheets.
- `research/processed/payroll_discovery/` — сущности, формулы и handoff по зарплатному модулю.
- `research/processed/iiko/employees/` — обезличенные результаты текущей разведки.

## Статус первого прохода и переоткрытие

В первом проходе raw-файлы `/employees/attendance` были сохранены, но processed-слой показал 0 часов. Причина: старый универсальный XML-парсер искал `row`, `item`, `entry`, а фактический ответ iiko содержит элементы `<attendance>`.

Проверка 2026-05-20:

| Период | Активные строки Черникова | Факт часов | Сотрудников |
| --- | ---: | ---: | ---: |
| 2026-02 | 335 | 3 725.7 | 37 |
| 2026-03 | 330 | 3 846.9 | 34 |
| 2026-04 | 287 | 3 402.6 | 31 |
| 2026-05-01..2026-05-20 | 203 | 2 340.2 | 25 |

Итого за февраль-май 2026: **1 155 строк явки** и **13 315.4 часа** по активному контуру Черникова.

## Авторизация и безопасность

Авторизация такая же, как в [08-iiko-server-api-endpoints.md](/app-spec/integrations/iiko/server-api-endpoints.md): токен передается query-параметром `key`, секреты берутся из `.env`/`ENV`. Если токен истек, общий `IikoClient` обновляет его через `POST /resto/api/auth`; остальные вызовы в этом исследовании — только `GET`.

Raw-ответы лежат в `research/raw/iiko/employees_research/` и игнорируются git. В них есть ФИО и другие персональные поля. В `processed` и Markdown перенесены только схемы, статусы, агрегаты и `employee_id_hash`.

## Найденные и проверенные endpoint'ы

WADL `GET /resto/api/application.wadl` содержит 69 method/path matches по словам `employee`, `schedule`, `attendance`, `shift`, `labour`, `worktime`, `timesheet`, `salary`, `payroll`. Полный список сохранен в `research/raw/iiko/employees_research/wadl_employees_matches.txt`.

| Endpoint | Метод | Статус | Что возвращает | Даты | Обязательные параметры | Замечания |
| --- | --- | ---: | --- | --- | --- | --- |
| `/employees/attendance` | GET | 200 | журнал фактической явки | `yyyy-MM-dd` | `from`, `to` | основной источник `attendance_entries`; `dd.MM.yyyy` дал 400 |
| `/employees/attendance/department/{departmentId}` | GET | 200 | явки по подразделению | `yyyy-MM-dd` | `departmentId`, `from`, `to` | рабочий department-specific путь |
| `/employees/attendance/byDepartment/{departmentId}` | GET | 409 | не использовать на этом сервере | `yyyy-MM-dd` | `departmentId`, `from`, `to` | WADL есть, фактически конфликт |
| `/employees/attendance/byEmployee/{employeeId}` | GET | 200 | явки одного сотрудника | `yyyy-MM-dd` | `employeeId`, `from`, `to` | пригоден для точечной сверки |
| `/employees/attendance/types` | GET | 200 | типы явок | — | нет | коды: `Р`, `Б`, `О`, `П`, `Н`, `В` |
| `/employees/schedule` | GET | 200, 0 строк | плановые смены | `yyyy-MM-dd` | `from`, `to` | не считать нулем плана; требуется owner review |
| `/employees/schedule/byEmployee/{employeeId}` | GET | 200, 0 строк | план одного сотрудника | `yyyy-MM-dd` | `employeeId`, `from`, `to` | данных нет |
| `/employees/schedule/byDepartment/{departmentId}` | GET | 409 | не использовать | `yyyy-MM-dd` | `departmentId`, `from`, `to` | WADL есть, фактически конфликт |
| `/employees/schedule/types` | GET | 200 | типы/шаблоны смен | — | нет | есть `startTime`, `lengthMinutes`, `tariff`, `overtime` |
| `/employees/salary` | GET | 200 | текущие payment-настройки сотрудников | — | нет | 39 строк; не факт выплат |
| `/employees/salary/byId/{employeeId}` | GET | 200 | payment-настройка одного сотрудника | — | `employeeId` | точечная сверка |
| `/employees/salary/byId/{employeeId}/{date}` | GET | 200 | payment-настройка на дату | path `yyyy-MM-dd` | `employeeId`, `date` | точечная сверка |
| `/employees/roles` | GET | 200 | справочник ролей | — | нет | 14 ролей; маппинг по `roleId` |
| `/employees` | GET | 200 | справочник сотрудников | — | опц. `includeDeleted` | raw содержит PII; в processed только схема |
| `/employees/byDepartment/{departmentId}` | GET | 200 | сотрудники подразделения | — | `departmentId` | raw содержит PII |
| `/v2/payrolls/list` | GET | 200, 0 строк | payroll-документы/списки | `yyyy-MM-dd` | `dateFrom`, `dateTo`, `department` | за пилотный период пусто |
| `/v2/entities/periodSchedules` | GET | 200 | периодические расписания | — | нет | 1 запись, не факт явки |
| `/v2/entities/scheduleTypes` | GET | 200 | v2-справочник типов расписания | — | нет | 72 записи |
| `/v2/reports/olap/presets` | GET | 200 | сохраненные OLAP-отчеты | — | нет | найден preset `Выручка по официантам` |
| `/v2/reports/olap/columns?reportType=LABOUR` | GET | 404 | нет такого reportType | — | `reportType` | LABOUR не поддержан этим сервером |
| `/v2/reports/olap/columns?reportType=ATTENDANCE` | GET | 404 | нет такого reportType | — | `reportType` | ATTENDANCE не поддержан этим сервером |
| `/v2/reports/olap/byPresetId/{id}` (`Выручка по официантам`) | GET | 200 | выручка по `WaiterName` | `yyyy-MM-dd` | `dateFrom`, `dateTo`, `summary` | за 2026-04-01..2026-04-07 вернул 5 строк; raw содержит имена |
| `/employees/availability/list` | GET | 400 | не проверен | — | неизвестно | требует параметры |
| `/rmsSettings/getEmployees` | GET | 500 | legacy lookup | — | неизвестно | не использовать |

## Схема журнала явки

Основной endpoint:

```text
GET /resto/api/employees/attendance
params:
  withPaymentDetails=false
  from=YYYY-MM-DD
  to=YYYY-MM-DD
```

Фактический ответ сервера приходит XML, логическая структура:

```json
{
  "attendances": [
    {
      "id": "<attendance_id>",
      "employeeId": "<employee_id>",
      "roleId": "<role_id>",
      "dateFrom": "2026-04-01T09:00:00+03:00",
      "dateTo": "2026-04-01T21:00:00+03:00",
      "attendanceTypeId": "<attendance_type_id>",
      "attendanceType": "Р",
      "comment": "",
      "departmentId": "<department_id>",
      "departmentName": "active_chernikova",
      "personalDateFrom": "2026-04-01T09:00:00+03:00",
      "personalDateTo": "2026-04-01T21:00:00+03:00",
      "created": "2026-04-01T09:00:00.000+03:00",
      "modified": "2026-04-01T21:00:00.000+03:00",
      "userModified": "<redacted user>"
    }
  ]
}
```

| Поле | Тип | Nullable | Смысл для payroll | Пример в docs |
| --- | --- | --- | --- | --- |
| `id` | uuid | нет | технический id строки явки | `<id>` |
| `employeeId` | uuid | нет | ключ сотрудника; в processed только hash | `<employee_id_hash>` |
| `roleId` | uuid | нет | роль/категория iiko через `/employees/roles` | `<id>` |
| `dateFrom` | datetime | нет | начало фактической явки | `2026-04-01T09:00:00+03:00` |
| `dateTo` | datetime | да | конец фактической явки; пусто = незакрытая явка | `2026-04-01T21:00:00+03:00` |
| `attendanceTypeId` | uuid | нет | ссылка на тип явки | `<id>` |
| `attendanceType` | string | нет | короткий код типа явки | `Р` |
| `comment` | string | да | свободный комментарий; не переносить в processed | `<redacted comment>` |
| `departmentId` | uuid | нет | точка/подразделение | `<id>` |
| `departmentName` | string | нет | маппится в `active_chernikova` / `historical_gagarina` | `active_chernikova` |
| `personalDateFrom` | datetime | нет | персональное начало, обычно совпадает с `dateFrom` | timestamp |
| `personalDateTo` | datetime | да | персональный конец, обычно совпадает с `dateTo` | timestamp |
| `created` | datetime | нет | когда строка создана в iiko | timestamp |
| `modified` | datetime | да | когда строка изменена | timestamp |
| `userModified` | uuid/string | да | кто изменил; PII/служебное, не публиковать | `<redacted user>` |

Типы явок из `/employees/attendance/types`:

| Код | Тип | Pay rate | Status |
| --- | --- | ---: | --- |
| `Р` | Отработано | 1.0 | true |
| `Б` | Больничный | 0.8 | false |
| `О` | Отпуск | 1.0 | false |
| `П` | Прогул | 0.0 | false |
| `Н` | Ночной | 1.2 | true |
| `В` | Праздничный | 2.0 | true |

`status` в справочнике типов явок — не статус утверждения смены и не признак оплаты.

## Маппинг iiko -> payroll-модель

| Payroll entity | Поля из iiko | Что делать |
| --- | --- | --- |
| `attendance_entries` | `employeeId`, `roleId`, `dateFrom`, `dateTo`, `attendanceType`, `departmentId` | основной импорт фактических интервалов; employeeId хранить в защищенном lookup, в processed — hash |
| `shift_schedule` | `/employees/schedule`, `/employees/schedule/types` | факт расписания endpoint вернул 0; план пока не заменяет Google Sheets |
| `employees` | `/employees.id`, `hireDate`, `deleted`, `mainRoleId`, `roleCodes` | raw содержит PII; использовать только в закрытом HR-слое |
| `employee_role_assignments` | `/employees.rolesIds`, `/employees/roles.id/code/name` | можно построить защищенную историю ролей как контроль, но owner decision 2026-05-25: iiko-должности не маппятся автоматически в payroll-роли; payroll-роль/станция берётся из `Смены и выручка` / `Учет смен` |
| `daily_revenue` | `/reports/olap` и preset `Выручка по дням` | источник выручки для процента уже подтвержден в sales/P&L исследованиях |
| `payroll_run` | нет прямого факта расчета | payroll run должен создаваться приложением по правилам из 19 |
| `payroll_ledger_lines` | нет прямых строк начислений/удержаний | оклад, процент, депозит, фонд, штрафы и НДФЛ рассчитываются приложением; iiko дает входы |
| `payments` | нет в employees API | `Выплаты` в payroll - ведомость/обязательство; cash-fact закрывается через DDS `cashflow_transaction` или `payroll_payment_batch` |

## Что снято в processed

Каталог: `research/processed/iiko/employees/`.

| Файл | Содержимое |
| --- | --- |
| `attendance_monthly.csv` | длинный обезличенный агрегат: `month`, `employee_id_hash`, `role_code`, `department`, `planned_minutes`, `fact_minutes`, `attendance_type_count_breakdown`, `status`, `source_endpoint` |
| `attendance_field_schema.csv` | каталог полей endpoint'ов с типом, nullable, redacted example и business meaning |
| `endpoints_inventory.csv` | все опробованные endpoint'ы, статусы, записи, формат дат, обязательные параметры, notes |
| `report.md` | короткий отчет с итогами и рисками |

`planned_minutes` оставлен пустым, когда `/employees/schedule` вернул 0 строк. Это не подтвержденный ноль плана, а статус `schedule_endpoint_returned_zero`.

## Расхождения и риски данных

1. Старый processed по персоналу был ложным нулем из-за парсера `<attendance>`.
2. `/employees/attendance` принимает `yyyy-MM-dd`; `dd.MM.yyyy` дает 400.
3. `/employees/schedule` возвращает 0 строк за все месяцы 2025-11..2026-05. Нельзя считать, что в iiko план равен нулю.
4. В каждом месячном ответе встречалась историческая незакрытая строка Гагарина от 2024-01-05 без `dateTo`; processed фильтрует по `dateFrom` внутри запрошенного месяца.
5. За 2025-11..2026-05 найдено 9 строк без `dateTo`; owner decision 2026-05-25: для расчета такая явка закрывается в 22:00, но смена capped at 12 часов; при увольнении во время активной смены закрывать временем увольнения.
6. `attendanceType.status` не равен “утверждено/оплачено”.
7. `byDepartment`-варианты из WADL на этом сервере дают 409; использовать `department/{departmentId}`.
8. `Выручка по официантам` содержит `WaiterName`; это PII и не является сменным revenue ledger. Использовать только через закрытый lookup и сверку с ролью.
9. Если сотрудник есть в iiko-явке, но отсутствует в приложении, payroll-run должен блокироваться до внесения обязательных HR/payroll-данных.
10. Если увольнение внесено во время активной смены, приложение закрывает смену временем увольнения и затем деактивирует/удаляет сотрудника в iiko; локальная карточка остается в архиве для audit.
11. Несколько закрытых интервалов одного сотрудника за день суммируются. Незакрытые явки без `dateTo` закрываются расчетно в 22:00 с ограничением смены 12 часов; увольнение во время активной смены закрывает явку временем увольнения.

## Связь с правилами расчета зарплат

Теперь из iiko можно брать:

- фактическую явку и длительность смен (`dateFrom`, `dateTo`);
- роль iiko на момент явки (`roleId` -> `/employees/roles`);
- тип явки: рабочая, больничный, отпуск, прогул, ночная, праздничная;
- подразделение/точку (`departmentName` -> `active_chernikova`);
- дневную выручку для процента из уже подтвержденного iiko SALES OLAP;
- справочные salary/schedule settings как дополнительный контроль.

Остается ручным или в логике приложения:

- payroll-категории 1-6, стажер/внештат и коэффициенты процента;
- надбавки `Старший`, `Зам`, дополнительный час и внеурочный коэффициент;
- премии, штрафы, НДФЛ, больничные/отпуска/пособия как денежные события;
- депозитные удержания/возвраты/списания;
- накопительный фонд;
- утверждение payroll run и факт выплат.
- lifecycle сотрудника в приложении как source of truth для увольнения с последующей синхронизацией деактивации/удаления в iiko.

## Открытые вопросы владельцу

1. Что в iiko считается утвержденной/проверенной явкой, если в `/employees/attendance` нет явного статуса утверждения?
2. Больничные, отпуска и прогулы реально ведутся в iiko или в Google Sheets/ручных событиях?
3. ✅ закрыто 2026-05-25 для payroll-ролей/станций: текущий источник `Смены и выручка` / `Учет смен` остается главным; iiko schedule не заменяет его.
4. ✅ закрыто 2026-05-25: iiko-роли `Повар`, `Кассир`, `Курьер` не маппятся автоматически в payroll-роли; payroll-роль берется из `Смены и выручка` / `Учет смен`.
5. Где фиксируется категория 1-6, стажер и внештатная подмена: в iiko, Google Sheets или отдельном HR-справочнике?
6. ✅ закрыто 2026-05-25: несколько закрытых интервалов за день суммируются; если `dateTo` пустой, расчетно закрывать явку в 22:00 с cap 12 часов; если `dateTo` отсутствует из-за увольнения во время активной смены, закрывать временем увольнения.
7. Можно ли использовать `Выручка по официантам` для каких-либо бонусов, или процент должен считаться только от дневной общей выручки смены?

## Следующее действие

1. Подтвердить у владельца вопросы выше.
2. Добавить защищенный lookup `employeeId -> employee` внутри будущего HR-слоя; в аналитике оставлять только hash.
3. Построить сверку `iiko attendance -> Google Sheets Загрузка явок` за одну неделю февраля и одну неделю апреля 2026.
4. После сверки заменить источник фактических явок в payroll-модуле на iiko, оставив Google Sheets как временный контроль и источник ручных payroll-событий.
