# Python-сервис курьерской книги: модели, Sheets и расчеты

Документ продолжает разбор `research/raw/courier_service/TestAppScript.py` и фиксирует фактический поток данных: iikoCloud/webhook -> Python dict'и -> Google Sheets. PII и секреты не приводятся.

## 3. Модель данных в памяти и на выходе

В сервисе нет Pydantic-моделей, dataclass'ов или typed DTO. Вся модель данных держится в dict/list из JSON-ответов iikoCloud и webhook payload.

### Runtime-сущности

| Сущность | Где появляется | Ключевые поля из кода | Назначение |
| --- | --- | --- | --- |
| `CourierCatalogItem` | ответ `/api/1/employees/couriers/by_role` | `id`, `firstName`, `lastName`, `isDeleted` | обновление листа `Курьеры` |
| `EmployeeInfo` | ответ `/api/1/employees/info` | `employeeInfo.firstName`, `employeeInfo.lastName` | ФИО для строки смены и auto-add в `Курьеры` |
| `DeliveryOrderUpdateEvent` | inbound webhook | `eventType`, `eventTime`, `eventInfo.id`, `eventInfo.order.number`, `eventInfo.order.status`, `eventInfo.order.courierInfo.courier.id` | создание/закрытие/удаление строки в `Доставки` |
| `PersonalShiftEvent` | inbound webhook | `eventType`, `eventTime`, `organizationId`, `eventInfo.id`, `eventInfo.opened` | создание/закрытие строки в `Выходы` |
| `SheetDeliveryRow` | лист `Доставки` | `A:G` | материализованный статус доставки |
| `SheetShiftRow` | лист `Выходы` | `A:F` | материализованный факт смены |
| `SheetCourierRow` | лист `Курьеры` | `A:B` | справочник iiko id -> имя |

### Маппинг iiko/webhook -> внутренняя модель

| Источник | Поле | Внутреннее значение | Преобразование |
| --- | --- | --- | --- |
| webhook | `eventTime` | `date_str`, `time_str` | строка парсится как UTC `YYYY-MM-DD HH:MM:SS`, затем переводится в `Europe/Moscow`; date = `%d.%m.%Y`, time = `%H:%M:%S` |
| `DeliveryOrderUpdate` | `eventInfo.id` | `order_id` | `str(...).strip()` |
| `DeliveryOrderUpdate` | `eventInfo.order.number` | `order_num` | `str(...).strip()` |
| `DeliveryOrderUpdate` | `eventInfo.order.status` | `status` | `str(...).strip()` |
| `DeliveryOrderUpdate` | `eventInfo.order.courierInfo.courier.id` | `courier_id` | `str(...).strip()` |
| `PersonalShift` | `organizationId` | `org_id` | передается в `/employees/info` |
| `PersonalShift` | `eventInfo.id` | `emp_id` | `str(...)`; затем используется как employee/courier id |
| `PersonalShift` | `eventInfo.opened` | `opened` | truthy/falsy bool для открытия/закрытия |
| `/employees/info` | `employeeInfo.firstName`, `lastName` | `full_name` | join через пробел, `.strip()` |
| `/employees/couriers/by_role` | `items[].firstName`, `lastName` | `name` | join через пробел, `.strip()` |
| `/employees/couriers/by_role` | `items[].isDeleted` | `is_del` | deleted courier удаляется из листа, active добавляется |

### Выходные row-модели

`Доставки`:

```text
[date_str, on_way_time, delivered_time, courier_id, order_num, order_id, duration_hours]
```

`Выходы`:

```text
[date_str, opened_time, closed_time, duration_hours, full_name, emp_id]
```

`Курьеры`:

```text
[courier_id_or_employee_id, full_name]
```

## 4. Интеграция с Google Sheets

### Клиент, scope и авторизация

Клиент: `gspread`.

Авторизация: Google service account через:

```text
Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
gspread.authorize(credentials)
```

Scope:

```text
https://www.googleapis.com/auth/spreadsheets
```

Файл ключа: `research/raw/courier_service/service_account.json` в snapshot. Внутри есть `private_key`, `client_email`, `private_key_id`, `project_id` и другие поля service account. Значения не документировать и не переносить в приложение.

### Какие листы сервис пишет

| Лист | Пишет? | Что делает |
| --- | --- | --- |
| `Курьеры` | да | ставит header `A1:B1`, добавляет активных курьеров, удаляет deleted, auto-add сотрудника из shift event |
| `Выходы` | да | пишет открытие/закрытие смены и длительность |
| `Доставки` | да | пишет выход заказа в путь, доставку, удаление отмены и длительность |
| `Смены` | нет | в коде не открывается; переменная `shifts_sheet` указывает на лист `Выходы` |
| `Технический лист` | нет | не используется |
| `График` | нет | не используется |
| `Статистика` | нет | не используется |

### Операции Google Sheets

| Лист | Метод | Диапазон / строка | Поведение |
| --- | --- | --- | --- |
| `Курьеры` | `update` | `A1:B1` | перезаписывает заголовок `ID`, `Имя и Фамилия` |
| `Курьеры` | `get_all_values` | все строки | строит index существующих id из колонки A |
| `Курьеры` | `append_row` | новая строка | добавляет отсутствующего активного курьера |
| `Курьеры` | `delete_rows` | найденная строка | удаляет courier, если `isDeleted=true` |
| `Курьеры` | `col_values(1)` | колонка A | список known courier/employee ids при webhook |
| `Доставки` | `get_all_values` | все строки | ищет order_id для update/delete |
| `Доставки` | `insert_row(row, 2)` | строка 2 | новая доставка всегда вставляется сверху |
| `Доставки` | `update_cell` | `C`, `G` | закрывает доставку временем и длительностью |
| `Доставки` | `delete_rows` | найденная строка | удаляет отмененный заказ |
| `Выходы` | `get_all_values` | все строки | ищет открытую смену |
| `Выходы` | `insert_row(row, 2)` | строка 2 | новая смена всегда вставляется сверху |
| `Выходы` | `update_cell` | `C`, `D` | закрывает смену временем и длительностью |

### Маппинг в лист `Курьеры`

| Колонка | Заголовок из сервиса | Источник | Комментарий |
| --- | --- | --- | --- |
| A | `ID` | `items[].id` или `PersonalShift.eventInfo.id` | iiko employee/courier id |
| B | `Имя и Фамилия` | `firstName + lastName` | PII; сервис пишет открытым текстом |

Сервис не обновляет имя у уже существующего id, если ФИО поменялось в iiko. Он только добавляет отсутствующих и удаляет `isDeleted`.

### Маппинг в лист `Доставки`

| Колонка | Значение | Источник / расчет | Когда пишется |
| --- | --- | --- | --- |
| A | Дата события | `eventTime` -> Moscow date | `OnWay` insert |
| B | Время выхода в путь | `eventTime` -> Moscow time | `OnWay` insert |
| C | Время доставки | `eventTime` -> Moscow time | `Delivered` update |
| D | Courier id | `order.courierInfo.courier.id` | `OnWay` insert |
| E | Номер заказа | `order.number` | `OnWay` insert |
| F | Order id | `eventInfo.id` | `OnWay` insert; ключ поиска |
| G | Длительность, часы | `C - B`, округление до 2 знаков | `Delivered` update |

Для `Cancelled` сервис ищет строку по колонке F и удаляет ее.

### Маппинг в лист `Выходы`

| Колонка | Значение | Источник / расчет | Когда пишется |
| --- | --- | --- | --- |
| A | Дата события | `eventTime` -> Moscow date | open insert |
| B | Время открытия | `eventTime` -> Moscow time | `opened=true` |
| C | Время закрытия | `eventTime` -> Moscow time | `opened=false` |
| D | Длительность смены, часы | `C - B`, округление до 2 знаков | `opened=false` |
| E | Имя и фамилия | `/employees/info` | open insert |
| F | Employee id | `PersonalShift.eventInfo.id` | open insert; ключ поиска |

Для закрытия сервис ищет первую строку после header, где колонка F равна `emp_id`, а колонка C пустая. Если строка не найдена, явного warning/error нет.

### PII

PII в сервисе:

- ФИО курьера пишется в `Курьеры.B`;
- ФИО сотрудника пишется в `Выходы.E`;
- ФИО пишется в info-логи при открытии/закрытии смены;
- телефоны в коде не читаются и в Sheets не пишутся;
- courier/employee id и order id пишутся открыто в Sheets и логи.

При переносе в приложение ФИО должны жить в защищенном employee/courier profile, а аналитические витрины должны ссылаться на internal id и маскировать PII там, где это не нужно операционной роли.

## 5. Бизнес-расчеты внутри сервиса

### Что сервис считает сам

| Расчет | Формула | Где пишется |
| --- | --- | --- |
| Длительность доставки | `delivered_time - on_way_time` в часах, если отрицательно +24 часа | `Доставки.G` |
| Длительность смены | `closed_time - opened_time` в часах, если отрицательно +24 часа | `Выходы.D` |
| ФИО | `firstName + " " + lastName` | `Курьеры.B`, `Выходы.E` |
| Active courier delta | active missing -> append, deleted existing -> delete | `Курьеры` |

Сервис не считает KPI листа `Статистика`, payroll, стоимость доставки, количество уникальных заказов за период, no-show, план/факт графика или агрегаты по курьерам.

### Что перекладывается 1-к-1

| Данные | Поведение |
| --- | --- |
| `order.number` | пишется как номер заказа |
| `eventInfo.id` для доставки | пишется как order id/key |
| `courier.id` | пишется в `Доставки.D` без матчинга по ФИО |
| `PersonalShift.eventInfo.id` | пишется как employee id в `Выходы.F` |
| `eventTime` | используется как источник даты/времени события |
| `status` | используется только для branch logic, в лист не пишется |

### Алгоритм матчинга курьера

Явного матчинга по фамилии/имени нет.

Фактический алгоритм:

1. Справочник `Курьеры` строится по iiko id из `/employees/couriers/by_role`.
2. Для доставок в `Доставки.D` пишется `courier.id`, а не ФИО.
3. Для смен в `Выходы.F` пишется `emp_id`, а в `Выходы.E` - ФИО из `/employees/info`.
4. Если `emp_id` из shift event отсутствует в `Курьеры.A`, сервис добавляет `[emp_id, full_name]`.

Поэтому расхождение фамилий между iiko и книгой сервис не решает. Он опирается на id. Если старые формулы книги ожидали имя курьера, а не id, этот сервис фактически изменяет контракт листов на id-based lookup.

### Частично закрытые смены

Открытая смена вставляется в `Выходы` с пустыми `C` и `D`. При закрытии сервис заполняет `C` и `D`.

Если закрытие не пришло:

- строка остается незакрытой;
- длительность остается пустой;
- отдельного reconciliation/backfill нет.

Если закрытие пришло, но открытая строка не найдена:

- сервис не пишет явную ошибку;
- событие фактически теряется для Sheets.

### Незавершенные и отмененные доставки

`OnWay` создает строку с пустыми `C` и `G`.

`Delivered` ищет строку по `order_id`, заполняет время доставки и длительность.

`Cancelled` ищет строку по `order_id` и удаляет ее.

Если `Delivered` пришел без ранее обработанного `OnWay`, сервис логирует ошибку `order_id not found` и не создает строку. Если `OnWay` пришел повторно, сервис вставит дубль, потому что idempotency check перед insert отсутствует.

### Время и timezone

Сервис ожидает `eventTime` в формате:

```text
YYYY-MM-DD HH:MM:SS[.fraction]
```

Он отрезает дробную часть после точки, считает время UTC и переводит в `Europe/Moscow`. Если iiko пришлет ISO-строку с `T`/timezone или уже локальное время, парсер/интерпретация сломаются.
