# Python-сервис курьерской книги: связь с книгой, риски и перенос

Документ закрывает связь snapshot-сервиса `data/raw/courier_service/` с уже описанной книгой `График курьеров и показатели курьеров` и фиксирует, что именно переносить в `apps/api`.

## 6. Связь с уже изученной книгой

### Какие листы сервис трогает

| Лист книги | Сервис пишет? | Фактическое поведение | Вывод для миграции |
| --- | --- | --- | --- |
| `Курьеры` | да | `A:B` = iiko id, ФИО; header перезаписывается сервисом | заменить на `courier_profile`/employee lookup |
| `Выходы` | да | `A:F` = дата, open, close, часы, ФИО, employee id | заменить на `courier_shift` или расширенный `shift_ledger_entry` |
| `Смены` | нет | в коде нет обращения к листу `Смены` | вероятно ручной/устаревший/другой источник; не считать частью этого сервиса |
| `Технический лист` | нет | не открывается | переносить как backend calendar/enums, не как импорт |
| `График` | нет | не открывается | ручной план/факт, нужен UI |
| `Доставки` | да | `A:G` = дата, OnWay, Delivered, courier id, order number, order id, duration | заменить на delivery order + assignment facts |
| `Статистика` | нет | не открывается | пересчитать в backend/view из фактов |

Главная корректировка к первой разведке: книга заполняется не Apps Script внутри Google Sheets, а внешним Python FastAPI-сервисом через `gspread`. При этом сервис покрывает только `Курьеры`, `Выходы`, `Доставки`; остальные листы книги либо ручные, либо формульные, либо обслуживаются другим не найденным механизмом.

### Что нужно поправить в прежних выводах

| Было в первой разведке | После чтения Python-сервиса |
| --- | --- |
| Возможный источник - Apps Script/надстройка/ручной импорт | подтвержден внешний Python-сервис с webhook endpoint |
| Подтвержденная интеграция описывалась через iikoServer Resto | для этой книги используется iikoCloud `https://api-ru.iiko.services/api/1`; iikoServer остается отдельным контуром проекта |
| `Смены` предполагался как прямой iiko/import лист | найденный сервис пишет смены в `Выходы`, а лист `Смены` не трогает |
| `Доставки.B` предполагался как курьер/текст в старой схеме | найденный сервис пишет courier id в колонку D; фактический контракт листа нужно сверить с live book/snapshot |
| Частота обновления была неизвестна | courier catalog/token каждые 600 секунд; доставки/смены on webhook |

## 7. Слабые места кода

Факты по snapshot:

1. `IIKO_API_LOGIN` hard-coded в `TestAppScript.py`; это credential и его нельзя хранить в коде.
2. `service_account.json` лежит рядом со скриптом и содержит private key service account.
3. `SPREADSHEET_ID` и `ORGANIZATION_ID` hard-coded; нет env/config разделения окружений.
4. Webhook `POST /aiko-webhook` не проверяет подпись, секрет, source IP или auth header.
5. Нет idempotency: повторный `OnWay` вставит дубль доставки.
6. Нет retry/backoff для iikoCloud и Google Sheets; `raise_for_status()` + log.
7. Нет refresh-on-401: token обновляется только по таймеру.
8. Возможна гонка startup: `periodic_courier_update_async()` может сходить в iiko до успешного получения token.
9. `sheet_lock` защищает только webhook batch, но не периодическое обновление `Курьеры`.
10. Удаление deleted couriers по заранее рассчитанным индексам может сдвигать строки при нескольких delete подряд.
11. Сервис не обновляет ФИО уже существующего courier id.
12. Нет backfill/reconciliation: пропущенный webhook не восстанавливается историческим запросом.
13. `Delivered` без ранее обработанного `OnWay` не создает строку, а только логирует ошибку.
14. Закрытие смены без найденной открытой строки фактически теряется без явного error.
15. Парсинг `eventTime` хрупкий: ожидает `YYYY-MM-DD HH:MM:SS`, считает строку UTC.
16. Длительность считается только по времени суток; кейс больше 24 часов невозможен.
17. Логи содержат order id, courier id и ФИО; это PII/operational data.
18. `requirements.txt` раздут и содержит неиспользуемые зависимости.
19. Название `TestAppScript.py` и backup указывают на ad-hoc service, а не production package.
20. Нет тестов, healthcheck, metrics и import run ledger.

## 8. План переноса в приложение

### Что переносить 1-к-1

| Логика старого сервиса | Куда переносить | Комментарий |
| --- | --- | --- |
| iikoCloud access token по `apiLogin` | `apps/api/app/services/iiko_cloud/auth.py` | отдельный клиент от `iiko_sync.py` |
| `/employees/couriers/by_role` с `rolesToCheck=["courier"]` | `apps/api/app/services/iiko_cloud/couriers.py` | справочник courier/employee ids |
| `/employees/info` для `PersonalShift` | `apps/api/app/services/iiko_cloud/employees.py` | лучше кешировать и синхронизировать пакетно |
| обработка `DeliveryOrderUpdate` statuses `OnWay`, `Delivered`, `Cancelled` | `apps/api/app/services/iiko_cloud/webhooks.py` | сохранить семантику статусов, добавить idempotency |
| обработка `PersonalShift.opened` | `apps/api/app/services/iiko_cloud/webhooks.py` | open/close shift facts |
| перевод `eventTime` в Moscow business date/time | общий time utility интеграционного слоя | сделать timezone-aware и покрыть тестами |

### Что переписывать иначе

| Старый сервис | В приложении |
| --- | --- |
| Пишет напрямую в Google Sheets | писать в PostgreSQL, а UI строить поверх БД |
| Google row является источником истины | raw event/order/shift facts + import run ledger + correction layer |
| `insert_row(2)` как журнал | append/upsert по stable external id |
| `delete_rows` для отмены | хранить order status/history; отмена не удаляет raw fact |
| ФИО в открытых листах | protected employee/courier profile, display/masking по ролям |
| ручные правки в Sheets | отдельная correction table с audit |
| нет backfill | добавить периодический reconcile/backfill endpoint'ами iikoCloud или event replay, если доступно |

### Draft-таблицы из `06` и что они покрывают

| Draft-таблица из `06-issues-and-migration-plan.md` | Покрывает кусок старого сервиса |
| --- | --- |
| `employee` | `EmployeeInfo`, ФИО, iiko employee id |
| `employee_role_assignment` | роль `courier`, если курьер является сотрудником в штатном модуле |
| `courier_profile` | замена листа `Курьеры`, связь iiko courier/employee id с безопасным профилем |
| `courier_shift` | замена строк `Выходы.A:F`, открытие/закрытие смен |
| `courier_shift_correction` | ручные исправления незакрытых/ошибочных смен |
| `courier_schedule_entry` | замена ручного листа `График`, который Python-сервис не трогал |
| `delivery_order` | order id, order number, status из `DeliveryOrderUpdate` |
| `delivery_order_courier_assignment` | courier id, on-way time, delivered time, duration |
| `courier_kpi_snapshot` | замена `Статистика`, считается из delivery/shift facts |
| `courier_import_run` | мониторинг token/webhook/backfill jobs, которого в старом сервисе нет |
| `courier_payroll_rule` | отсутствующая в книге логика ставок |
| `courier_payroll_accrual` | будущая связка KPI/смен с начислениями после правил владельца |

Важно: в текущем коде `apps/api` найден существующий `shift_ledger_entry`, но роль `courier` в локальных constraints/services не видна. Перед реализацией нужно решить, добавлять отдельный `courier_shift` или расширять общий shift ledger под courier-role.

### Где разместить iikoCloud endpoint'ы

Рекомендация: не смешивать с текущим `apps/api/app/services/iiko_sync.py`, потому что он обслуживает iikoServer Resto и другие auth/URL contracts.

Предлагаемая структура:

```text
apps/api/app/services/iiko_cloud/
  __init__.py
  client.py          # base URL, timeouts, auth headers, common request/retry
  auth.py            # POST /api/1/access_token
  couriers.py        # POST /api/1/employees/couriers/by_role
  employees.py       # POST /api/1/employees/info
  webhooks.py        # parse/validate DeliveryOrderUpdate, PersonalShift
  schemas.py         # Pydantic DTO for webhook and API responses
```

Нужные env/config names для приложения:

| Переменная | Назначение |
| --- | --- |
| `IIKO_CLOUD_BASE_URL` | default `https://api-ru.iiko.services` |
| `IIKO_CLOUD_API_LOGIN` | секретный apiLogin |
| `IIKO_CLOUD_ORGANIZATION_ID` | organization id точки/организации |
| `IIKO_CLOUD_TIMEOUT_SECONDS` | timeout HTTP-клиента |
| `IIKO_CLOUD_WEBHOOK_SECRET` | секрет/подпись для входящего webhook, если iiko поддерживает или через reverse proxy |
| `IIKO_CLOUD_TOKEN_REFRESH_SECONDS` | интервал proactive refresh |

### Какие endpoint'ы переезжают в интеграционный слой

Минимальный набор из snapshot:

| Метод | Path | Новый слой |
| --- | --- | --- |
| `POST` | `/api/1/access_token` | `iiko_cloud.auth` |
| `POST` | `/api/1/employees/couriers/by_role` | `iiko_cloud.couriers` |
| `POST` | `/api/1/employees/info` | `iiko_cloud.employees` |
| inbound `POST` | `/aiko-webhook` или новый route | API route -> `iiko_cloud.webhooks` |

Для production-миграции, вероятно, понадобится отдельный backfill исторических доставок/смен. В snapshot таких endpoint'ов нет, поэтому их нельзя считать подтвержденными; следующий агент должен подобрать их по официальной iikoCloud документации или по доступу владельца.

### Что можно не переносить

| Кусок snapshot-сервиса | Почему не переносить |
| --- | --- |
| `gspread`-запись в Google Sheets | приложение должно писать в PostgreSQL |
| `service_account.json` | больше не нужен для операционного контура; если нужен read-only архив, хранить отдельно в secrets |
| `requirements.txt` целиком | содержит много лишних зависимостей |
| `TestAppScript.py.bak` | backup без новой логики |
| `insert_row(2)`/`delete_rows` row-based подход | заменить idempotent upsert/status history |
| логирование ФИО/order id в plain logs | заменить структурным безопасным логированием |
| Google header rewrite `A1:B1` | это spreadsheet-техническая деталь |

### Приоритеты для реализатора

1. Сделать read-only/append-only прием iikoCloud webhook в БД с idempotency по event/order/shift identity.
2. Вынести iikoCloud auth/client отдельно от iikoServer Resto.
3. Создать защищенный courier profile и mapping по iiko id, не по ФИО.
4. Перенести `Доставки` и `Выходы` как факты, а не как редактируемые raw-таблицы.
5. Сделать reconciliation/backfill план, потому что старый сервис теряет пропущенные webhook-события.
6. Оставить `График` ручным UI-модулем, так как Python-сервис его не пишет.
7. Считать `Статистика` заново из delivery/shift facts.
