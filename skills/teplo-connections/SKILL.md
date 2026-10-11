---
name: teplo-connections
description: >-
  Connect to Teplo business systems using verified authentication and local
  credential sources. Use for подключения Тепла, программный вход в Starter,
  доступ к API, проверку сессии и поиск нужных переменных окружения. Covers the
  tested Starter login and routes to iiko/Revvy access references; customer
  extraction and RFM belong to teplo-customer-analysis.
---

# Подключения «Тепла»

Найди подтверждённый способ входа и проверь минимальный читающий запрос.
Сохраняй метод, имена переменных и дату проверки; сами доступы остаются
в разрешённом локальном хранилище. Не запрашивай их повторно, если они уже есть.

## Куда обращаться

| Сервис | Подключение | Что читать после входа |
|---|---|---|
| Starter CRM | Проверенный программный вход ниже | [Клиентские данные](../teplo-customer-analysis/references/starter-sources.md), [акции](../teplo-promotions/SKILL.md) |
| iikoCloud / iikoCard / iikoServer | [Существующий транспорт, environment-backed клиенты и различия API](../teplo-customer-analysis/references/iiko-sources.md) | [Анализ клиентов](../teplo-customer-analysis/SKILL.md), [юнит-экономика](../teplo-unit-economics/SKILL.md) |
| Revvy | [Авторизация API и чтение SignalR](../teplo-customer-research/references/revvy-access.md) | [Отзывы и исследование](../teplo-customer-research/SKILL.md) |

Инструкции iiko/Revvy остаются в указанных справках; не дублируй их контракты.
Доступ к одному сервису/организации не доказывает доступ ко всем остальным.

## Starter: источник доступа

Владелец разрешил программный вход и хранение доступов в локальном файле
`/Users/grigorii/Developer/Projects/Teplo-all-for-business/.env`.

- `STARTER_BASE_URL` — `https://crm.starterapp.ru/fmteplo`.
- `STARTER_USERNAME` — имя пользователя.
- `STARTER_PASSWORD` — пароль.

Файл проверен 11.10.2026: исключён из Git, не отслеживается, права 0600.
Это состояние на дату проверки, не гарантия будущей конфигурации. В sibling-worktree
`.env` может отсутствовать — прочитай указанный файл; не копируй весь набор доступов
в другую рабочую копию. Загружай только необходимые переменные, не печатай значения.

## Starter: проверенный вход

1. `POST {STARTER_BASE_URL}/api/v1/frontend/login`, Content-Type `application/json`:
   `{username: STARTER_USERNAME, password: STARTER_PASSWORD}`.
2. Проверь исходный ответ до автоматического перехода. Live 11.10.2026:
   **HTTP 303**, Location `https://crm.starterapp.ru/fmteplo/admin`.
   Ответ установил cookies `access_token` и `refresh_token`, HttpOnly/Secure,
   domain `.crm.starterapp.ru`, path `/fmteplo`.
3. Сохрани их обычным cookie jar HTTP-клиента **в памяти** и тем же клиентом
   читай защищённые методы. Значения cookie в skill, `.env` и логи не записывай.
   Проверенный поток не требует HTTP Basic, придуманного Bearer или подмены `user-role`.
4. Для минимальной проверки вызови
   `POST {STARTER_BASE_URL}/api/v2/frontend/customers`
   с body `{"offset":0,"limit":1,"order_by":"-created_at"}`.
   Live получен **HTTP 200**, Content-Type application/json, массив из одной записи.
   Показывай только статус, форму и число строк. Не печатай сырой профиль клиента.

В Python проверена связка `urllib.request.HTTPCookieProcessor(CookieJar())`
и `HTTPRedirectHandler`, у которого `redirect_request` возвращает None.
`HTTPError` с303 при таком обработчике — ожидаемый результат входа: cookie jar
уже получает Set-Cookie. Обработчик не должен принимать остальные HTTP-ошибки за успех.

Браузерная и программная сессии независимы. Страница входа в браузере не означает,
что API недоступен; HTTP200 с HTML этой страницы не подтверждает авторизацию.
Проверяй status, Content-Type и форму JSON. 401/403, ошибка DNS/сети и пустой
успешный список — разные состояния; не объявляй их отсутствием клиентов.
При неудачном входе разберись с причиной, не запускай перебор/бесконечные повторы.

Публичная схема: [Starter OpenAPI](https://crm.starterapp.ru/fmteplo/api/v1/openapi.json).
Открытая схема сама по себе не доказывает авторизацию. Cookie-поток и минимальное
чтение проверены 11.10.2026; обновляй дату только после новой фактической проверки.

## После подключения

Для полей, фильтров, страниц, полноты, связи клиентов и заказов переходи в
[teplo-customer-analysis](../teplo-customer-analysis/SKILL.md). Для текущих акций —
в [teplo-promotions](../teplo-promotions/SKILL.md).

Авторизация не разрешает менять настройки, начислять бонусы или писать клиентам.
POST может читать данные (`v2/customers`) либо создавать объект
(`v1/customer/segment`); проверяй конкретный маршрут перед вызовом.
