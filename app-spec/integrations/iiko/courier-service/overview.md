# Python-сервис курьерской книги: обзор и iikoCloud

Документ основан на reference snapshot `research/raw/courier_service/` и описывает только найденный Python-сервис. Значения токенов, ключей, spreadsheet id и service account не приводятся.

## 1. Общая карта сервиса

### Что лежит в snapshot

| Файл | Назначение | Комментарий |
| --- | --- | --- |
| `TestAppScript.py` | единственный рабочий FastAPI-скрипт сервиса | 301 строка, UTF-8/CRLF |
| `TestAppScript.py.bak` | backup того же скрипта | отличается только `logging.DEBUG` и `uvicorn(..., reload=True)` |
| `requirements.txt` | зависимости Python | UTF-16 LE/CRLF, сильно шире фактических import'ов |
| `service_account.json` | Google service account key | содержит приватный ключ; значение не переносить в документы |

В snapshot не найдено `main.py`, `app.py`, `pyproject.toml`, Dockerfile, systemd unit, supervisor config, cron config или `.env`.

### Точка входа и режим жизни

Точка входа: `TestAppScript.py`.

Скрипт создает `FastAPI()` и объявляет входящий route:

```text
POST /aiko-webhook
```

При прямом запуске используется:

```text
uvicorn.run("TestAppScript:app", host="0.0.0.0", port=8000)
```

По поведению это long-running демон:

- принимает webhook-события от iikoCloud/iiko Transport;
- на startup запускает background task обновления iiko token;
- на startup запускает background task обновления листа `Курьеры`;
- пишет в Google Sheets сразу при обработке webhook-событий.

Подтвержденного внешнего способа запуска в snapshot нет. На сервере владельца это может быть systemd/supervisor/ручной `uvicorn`, но конфиг не попал в snapshot.

### Фактически используемые зависимости

Фактические import'ы в `TestAppScript.py`:

| Пакет / модуль | Для чего |
| --- | --- |
| `fastapi` | HTTP webhook endpoint |
| `uvicorn` | локальный запуск ASGI-приложения |
| `httpx` | исходящие HTTP-запросы в iikoCloud |
| `gspread` | запись в Google Sheets |
| `google.oauth2.service_account.Credentials` | авторизация service account |
| `asyncio` | background tasks и `to_thread` для sync Google API |
| `pytz` | перевод времени в `Europe/Moscow` |
| `datetime`, `logging` | парсинг времени и логи |

`requirements.txt` дополнительно содержит много неиспользуемых в скрипте пакетов: `Flask`, `Quart`, `schedule`, `requests`, `pyiikocloudapi`, MySQL/VK/SMS-библиотеки и др. Для переноса в приложение это не является списком нужных зависимостей.

### Конфигурация

Все найденные настройки заданы константами в коде или файлом рядом со скриптом. `os.environ`, CLI-аргументы и config-файлы, кроме service account JSON, не используются.

| Имя в коде | Источник | Назначение | Секретность / перенос |
| --- | --- | --- | --- |
| `SERVICE_ACCOUNT_FILE` | hard-coded relative path | путь к Google service account JSON, сейчас `service_account.json` рядом со скриптом | путь можно документировать, содержимое ключа нельзя |
| `SPREADSHEET_ID` | hard-coded string | id книги `График курьеров и показатели курьеров` | не переносить значение; в приложении заменить env/config |
| `SHEET_NAME_COURIERS` | hard-coded string | лист `Курьеры` | не секрет |
| `SHEET_NAME_SHIFTS` | hard-coded string | фактически лист `Выходы`; переменная названа `SHIFTS` | не секрет |
| `SHEET_NAME_DELIVERIES` | hard-coded string | лист `Доставки` | не секрет |
| `ORGANIZATION_ID` | hard-coded string | iikoCloud organization id | не публиковать без необходимости; в приложении env/config |
| `IIKO_API_LOGIN` | hard-coded string | apiLogin для получения iikoCloud token | секрет/credential; срочно вынести из кода |
| `IIKO_TOKEN_URL` | hard-coded URL | endpoint авторизации iikoCloud | не секрет |
| `IIKO_EMPLOYEE_INFO_URL` | hard-coded URL | endpoint карточки сотрудника | не секрет |
| `IIKO_COURIERS_URL` | hard-coded URL | endpoint списка курьеров по роли | не секрет |
| `API_TOKEN` | global runtime variable | cached `Bearer <token>` | runtime secret; не логировать |
| `SCOPES` | hard-coded list | Google Sheets scope | не секрет |

Google scope:

```text
https://www.googleapis.com/auth/spreadsheets
```

## 2. Интеграция iikoCloud / iiko Transport

### API и базовый URL

Сервис использует iikoCloud на хосте:

```text
https://api-ru.iiko.services
```

Версия API в найденных endpoint'ах:

```text
/api/1
```

Это не `iikoServer Resto` (`/resto/api`, `foodmarket-teplo-co.iiko.it`), который уже используется в основном проекте. Для переноса нужен отдельный интеграционный слой.

### Авторизация

Схема:

1. `POST https://api-ru.iiko.services/api/1/access_token`
2. JSON body: `{"apiLogin": IIKO_API_LOGIN}`
3. Из ответа берется поле `token`.
4. В памяти сохраняется строка `API_TOKEN = "Bearer <token>"`.
5. Все следующие iikoCloud-запросы отправляют header `Authorization: Bearer <token>`.

Кеширование и refresh:

- token хранится только в памяти процесса;
- token обновляется на startup;
- затем обновляется каждые 600 секунд;
- на `401/403` нет отдельной логики refresh-and-retry;
- при падении запроса token updater только пишет ошибку в лог и продолжает следующий цикл.

### Исходящие iikoCloud endpoint'ы

В коде найдены только эти исходящие вызовы:

| Метод | Полный path | Body / параметры | Когда вызывается | Назначение |
| --- | --- | --- | --- | --- |
| `POST` | `/api/1/access_token` | JSON `apiLogin` | startup и каждые 600 секунд | получить bearer token |
| `POST` | `/api/1/employees/couriers/by_role` | JSON `organizationIds: [ORGANIZATION_ID]`, `rolesToCheck: ["courier"]` | startup и каждые 600 секунд | получить справочник курьеров с признаком роли |
| `POST` | `/api/1/employees/info` | JSON `organizationId`, `id` | на каждое событие `PersonalShift` | получить `firstName`/`lastName` сотрудника по employee id |

Endpoint'ы вроде `/api/1/organizations`, `/api/1/deliveries/by_delivery_date_and_status`, `/api/1/employees/couriers/active_locations` в snapshot не вызываются.

### Входящий webhook

Сервис сам предоставляет endpoint:

```text
POST /aiko-webhook
```

Ожидаемый payload: JSON-массив событий. Если пришел не список, сервис возвращает `{"error": "Invalid event format"}`.

Обрабатываются только два `eventType`:

| `eventType` | Назначение | Основные поля payload |
| --- | --- | --- |
| `DeliveryOrderUpdate` | изменение статуса заказа доставки | `eventTime`, `eventInfo.id`, `eventInfo.order.number`, `eventInfo.order.status`, `eventInfo.order.courierInfo.courier.id` |
| `PersonalShift` | открытие/закрытие личной смены сотрудника | `eventTime`, `organizationId`, `eventInfo.id`, `eventInfo.opened` |

Все остальные event type игнорируются с debug-логом.

### Фильтры

| Поток | Фильтр | Где задан |
| --- | --- | --- |
| Справочник курьеров | `organizationIds = [ORGANIZATION_ID]` | `fetch_courier_data_async` |
| Справочник курьеров | `rolesToCheck = ["courier"]` | `fetch_courier_data_async` |
| Employee info | `organizationId` из события webhook | `get_employee_info` |
| Employee info | `id` из `eventInfo.id` | `get_employee_info` |
| Delivery events | фильтра по организации/terminal group в коде нет | webhook доверяет входящему событию |
| Delivery statuses | обрабатываются только `OnWay`, `Delivered`, `Cancelled` | ветка `DeliveryOrderUpdate` |

`terminal_group`, периодические date filters и status filters для исторической выгрузки в коде отсутствуют.

### Частота вызовов

| Действие | Частота |
| --- | --- |
| Получение access token | startup, затем каждые 600 секунд |
| Обновление листа `Курьеры` через `/employees/couriers/by_role` | startup, затем каждые 600 секунд |
| `/employees/info` | на каждое событие `PersonalShift` |
| Запись `DeliveryOrderUpdate` | на каждое webhook-событие |
| Запись `PersonalShift` | на каждое webhook-событие |

Для исходящих iikoCloud-запросов используется `httpx.AsyncClient(timeout=10)`.

### Retry и error handling

Повторных попыток нет.

Фактическое поведение:

- `raise_for_status()` используется для всех исходящих iikoCloud-запросов;
- ошибки token request логируются, но не пробрасываются дальше;
- ошибка списка курьеров логируется, функция возвращает `[]`;
- ошибка employee info логируется, функция возвращает `{}`;
- общий exception в webhook ловится и возвращается как `{"error": str(ex)}`;
- Google Sheets-записи не имеют retry/backoff;
- нет idempotency key и защиты от повторной доставки webhook-события.

### Важное отличие от прежней разведки

Первая разведка книги предполагала Apps Script или другой spreadsheet-loader. Snapshot подтверждает внешний Python FastAPI-сервис, который пишет в Google Sheets через service account и получает данные из iikoCloud webhook + трех iikoCloud API endpoint'ов. Apps Script внутри книги для этого потока не нужен и в snapshot не участвует.
