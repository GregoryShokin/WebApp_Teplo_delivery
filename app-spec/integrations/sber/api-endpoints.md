# Sber API endpoint map

Дата фиксации: 2026-05-19.

## Роль Sber в финансовом контуре

Подтверждено владельцем 2026-05-19: расчётный счёт в Сбере — это **основная точка приёма выручки от коммерческой деятельности**. Сюда приходит большая часть денег от торгового эквайринга, интернет-эквайринга и других каналов приёма платежей. Это главный, но **не единственный** источник по выручке для управленческого ДДС: небольшая доля выручки приходит непосредственно на T-Bank через собственный эквайринг T-Bank (см. [14-tbank-api-endpoints.md](/app-spec/integrations/tbank/api-endpoints.md)). Поэтому управленческая выручка = эквайринг Sber + эквайринг T-Bank.

**Денежная цепочка проекта:** `Sber (основной приём выручки) → перевод на T-Bank → распределение по контрагентам в T-Bank`, плюс прямая малая ветка `T-Bank эквайринг → T-Bank → контрагенты`. Поэтому исходящие движения из Sber в значительной части — это переводы на собственный расчётный счёт в Т-Банке, а не платежи поставщикам. Расходная сторона (98% расчётов с поставщиками, выплаты сотрудникам, налоги, выводы) живёт в Т-Банке и описана в [14-tbank-api-endpoints.md](/app-spec/integrations/tbank/api-endpoints.md).

Важная поправка владельца 2026-05-19: **поступление в Sber не считается выручкой автоматически**. Выручку нужно идентифицировать по конкретным признакам в описании операции, контрагенте и банковских полях: например `эквайринг`, `перевод средств по договору`, агрегаторы и другие согласованные паттерны. Тот же принцип действует для T-Bank.

Следствие для аналитики:

- Из Sber API первично берём входящие операции, остатки и сверяем выручку iiko vs Sber (а не vs T-Bank, чтобы не задвоить за счёт внутренних переводов).
- Исходящие из Sber, направленные на собственный счёт в Т-Банке, классифицируются как `внутренний перевод между своими счетами`, не как расход поставщику.
- Выручку, прочие поступления, внутренние переводы, возвраты, кредиты и технические движения разделяем через реестр правил по описаниям/контрагентам. Безопасный шаблон: `research/processed/bank_operation_rules_template.csv`; заполненный реестр с реальными фрагментами назначений платежей хранить только в private-артефактах.
- Расходные статьи ДДС 2026 не строим по Sber API: их первичный источник — T-API.
- Если в Sber-выписке появляются заметные исходящие движения не на счёт T-Bank, они получают статус `требует проверки` и сверяются с T-API/iiko, а не автоматически разносятся в расходные статьи.

## Назначение

Этот документ - локальная карта Sber API для проекта "Тепло". Его задача: чтобы следующие агенты не начинали заново с общей витрины Сбера, а сразу понимали:

- где лежит официальная документация и OpenAPI-спецификации;
- как устроены контуры и авторизация;
- какие read-only endpoint'ы полезны для управленческой отчетности, ДДС и P&L;
- какие форматы дат, пагинация, лимиты и ошибки описаны в документации;
- какие вопросы нужно закрыть у владельца до первого боевого запроса.

## Статус изучения

Источник истины на дату фиксации:

- общая документация: https://developers.sber.ru/docs/ru/sber-api/overview
- справочник API: https://developers.sber.ru/docs/ru/sber-api/specifications/overview
- OAuth: https://developers.sber.ru/docs/ru/sber-api/specifications/oauth
- выписки: https://developers.sber.ru/docs/ru/sber-api/specifications/statement/statement-overview
- информация о клиенте: https://developers.sber.ru/docs/ru/sber-api/specifications/client-info/get-client-info
- контрагенты: https://developers.sber.ru/docs/ru/sber-api/specifications/correspondents/overview
- справочники: https://developers.sber.ru/docs/ru/sber-api/specifications/dicts/dicts-overview

Дополнительно были прочитаны официальные OpenAPI-спецификации:

- `https://developers.sber.ru/docs/files/openapi/sbapi/oauth.yaml`
- `https://developers.sber.ru/docs/files/openapi/sbapi/statement.yaml`
- `https://developers.sber.ru/docs/files/openapi/sbapi/client-info.yaml`
- `https://developers.sber.ru/docs/files/openapi/sbapi/correspondents.yaml`
- `https://developers.sber.ru/docs/files/openapi/sbapi/dicts.yaml`

Практические read-only запросы к Sber API выполнены 2026-05-19 на промышленном контуре: `GET /v1/client-info`, `GET /v2/statement/summary` и `GET /v2/statement/transactions` работают с `Bearer`-токеном и клиентским TLS-сертификатом. Сырые ответы сохранены только локально в `research/private/sber/`.

## Безопасность

- Никогда не писать `access_token`, `refresh_token`, `client_secret`, коды авторизации, JWS/JWE, персональные данные и номера реальных счетов в Markdown.
- Секреты брать только из `ENV` или локального `.env`.
- До отдельного разрешения владельца использовать только read-only сценарии.
- Не вызывать платежные, зарплатные, кредитные, карточные и любые create/update/revoke endpoint'ы, если задача явно не просит изменить данные.
- Банковские выписки содержат персональные и коммерчески чувствительные данные. Сырые ответы хранить только в `research/raw/` или `research/private/`, если потребуется локальная выгрузка.
- В рабочие документы переносить агрегаты, статусы, поля и методику, а не полные выписки и реквизиты.
- В логах и ошибках маскировать `Authorization`, `accountNumber`, ИНН физлиц, ФИО и назначение платежа, если оно содержит персональные данные.

## Рабочий контур и ENV-переменные

Официальные контуры из документации:

| Контур | Базовый URL API | Комментарий |
| --- | --- | --- |
| Песочница | `https://fintech-test.sberbank.ru:9443/fintech/api` | Есть в OpenAPI для части REST-методов |
| Тестовый | `https://iftfintech.testsbi.sberbank.ru:9443/fintech/api` | Основной тестовый контур в спеках REST API |
| Промышленный | `https://fintech.sberbank.ru:9443/fintech/api` | Боевой контур REST API |
| SSO тестовый | `https://efs-sbbol-ift-web.testsbi.sberbank.ru:9443` | Используется для `GET /ic/sso/api/v2/oauth/authorize` |
| SSO промышленный | `https://sbi.sberbank.ru:9443` | Используется для `GET /ic/sso/api/v2/oauth/authorize` |

В локальном `.env` на 2026-05-19 заполнены параметры промышленного сервиса, token-пара, расчетный счет, CA bundle и клиентский TLS-сертификат. В `.env.example` добавлен пустой блок `SBER_API_*` без секретов.

Рекомендуемые переменные для проекта:

```text
SBER_API_BASE_URL
SBER_API_AUTH_BASE_URL
SBER_API_AUTHORIZE_URL
SBER_API_TOKEN_URL
SBER_API_SERVICE_NAME
SBER_API_CLIENT_ID
SBER_API_CLIENT_SECRET
SBER_API_REDIRECT_URI
SBER_API_SCOPE
SBER_API_SCOPE_V1
SBER_API_SCOPE_V2
SBER_API_ACCESS_TOKEN
SBER_API_REFRESH_TOKEN
SBER_API_ACCOUNT_NUMBER
SBER_API_TIMEOUT_SECONDS
SBER_API_CA_BUNDLE_PATH
SBER_API_TLS_P12_PATH
SBER_API_TLS_P12_PASSWORD
SBER_API_TLS_CERT_PATH
SBER_API_TLS_KEY_PATH
```

Локальный файл `Sber API data.rtf` прочитан 2026-05-19. Из него в локальный `.env` перенесены служебные параметры подключения: базовые URL, имя сервиса, `client_id` и scope. `client_secret`, `access_token`, `refresh_token`, TLS-контейнер и пароль к нему отдельно сохранены пользователем в локальный `.env` / `research/private/sber/certs/`. `redirect_uri` в документе указан неполно, поэтому не считается рабочим значением для будущего OAuth refresh/callback flow.

## Где получить данные для доступа

Официальная точка входа: Личный кабинет Sber API внутри СберБизнес. Доступ появляется после подписания заявления/договора на подключение Sber API. По документации Сбера, в Личном кабинете можно подключить Sber API, получить и изменить интеграционные настройки, настроить сервис, выпустить сертификат безопасности и отключить сервис.

Минимальный путь подключения:

1. Войти в СберБизнес под пользователем организации.
2. В меню слева выбрать `Все продукты и услуги`.
3. На открывшейся странице найти вкладку `Сбербанк API`.
4. Выбрать продукт `Sber API` - это вход в Личный кабинет Sber API / карточку подключения.
5. Если Sber API еще не подключен, ознакомиться с информацией о продукте и нажать `Подключить`.
6. Выбрать нужный набор Sber API. Для текущей задачи нужны read-only возможности: информация о клиенте/счетах, выписки, контрагенты и справочники.
7. Создать и подписать заявление на подключение. Подписать заявление может пользователь с полномочиями ЕИО.
8. Активировать сервис в карточке сервиса.
9. Передать разработчику настройки промышленного сервиса или назначить роль `Разработчик Sber API`.

Прямой вход в СберБизнес из официальной документации:

```text
https://sbi.sberbank.ru:9443/ic/ufs/login.html
```

Ссылка пользователя на главный экран после входа:

```text
https://sbi.sberbank.ru:9443/ic/ufs/host/index.html#/main
```

Если пункт `Сбербанк API` / `Sber API` не виден, вероятные причины:

- Sber API еще не подключен или заявление не подписано;
- у пользователя нет подходящей роли в СберБизнес;
- нужен пользователь с ролью `Разработчик Sber API`, `Руководитель`, `Бухгалтер`, `Главный бухгалтер` или `Клиент Банка`;
- организация не имеет действующего договора ДБО или подписанного заявления о присоединении к Sber API.

Роль `Разработчик Sber API` нужна, если технический специалист должен сам заходить в Личный кабинет Sber API. По документации эта роль дает доступ только к Личному кабинету Sber API, а не ко всем разделам СберБизнес. Для разработчика доступны, в частности, обновление `client_secret` и создание новых `client_id`; стандартно пользователям Личного кабинета доступны выпуск/перевыпуск access token, TLS-сертификатов и настройки тестового полигона.

### Что запросить у владельца / банка

| Что нужно | Где получить | Кто обычно выдает / делает | Для какой ENV |
| --- | --- | --- | --- |
| Доступ в СберБизнес | У владельца/администратора организации в СберБизнес | Владелец, ЕИО или администратор СберБизнес | не хранить в проекте |
| Роль `Разработчик Sber API` | СберБизнес -> добавление/изменение пользователя -> роль `Разработчик Sber API` | Владелец/уполномоченный пользователь, подписывает документ | не хранить в проекте |
| Подключенный набор Sber API | Личный кабинет Sber API -> подключение набора | ЕИО подписывает заявление | влияет на `SBER_API_SCOPE` |
| `client_id` | Личный кабинет Sber API -> сервис -> параметры промышленного сервиса | Владелец, ЕИО, пользователь ЛК или разработчик с ролью | `SBER_API_CLIENT_ID` |
| `client_secret` | Личный кабинет Sber API -> сервис -> параметры промышленного сервиса / обновление секрета | Пользователь с правом обновления секрета; после генерации значение показывается один раз | `SBER_API_CLIENT_SECRET` |
| `redirect_uri` | Личный кабинет Sber API -> параметры промышленного сервиса; можно изменить через ЛК или поддержку | Владелец/разработчик с доступом к ЛК; изменение также через поддержку | `SBER_API_REDIRECT_URI` |
| Имя сервиса | Личный кабинет Sber API -> карточка сервиса | ЛК Sber API | `SBER_API_SERVICE_NAME` |
| Scope для OAuth v1/v2 | Личный кабинет Sber API -> параметры промышленного сервиса -> `Scope для авторизации v1/v2` | ЛК Sber API; состав зависит от подключенного набора | `SBER_API_SCOPE`, `SBER_API_SCOPE_V1`, `SBER_API_SCOPE_V2` |
| Промышленный API URL | Официальная документация / промышленный стенд | Не секрет, фиксируется в документации | `SBER_API_BASE_URL=https://fintech.sberbank.ru:9443/fintech/api` |
| Промышленный SSO URL | Официальная документация / промышленный стенд | Не секрет, фиксируется в документации | `SBER_API_AUTH_BASE_URL=https://sbi.sberbank.ru:9443` |
| URL авторизации | Собирается из SSO URL и path `/ic/sso/api/v2/oauth/authorize` | Разработчик | `SBER_API_AUTHORIZE_URL` |
| URL токенов | Собирается из SSO URL и path `/ic/sso/api/v2/oauth/token` | Разработчик | `SBER_API_TOKEN_URL` |
| Тестовые настройки | По документации: запрос в `supportdbo2@sberbank.ru` с указанием промышленного `client_id` | Ответственное лицо / разработчик по согласованию с владельцем | отдельный тестовый `.env`, не смешивать с боевым |
| Access/refresh token | OAuth flow через `authorize` -> `token` или генерация пары в Личном кабинете Sber API | Пользователь/разработчик с доступом к ЛК или backend OAuth flow | `SBER_API_ACCESS_TOKEN`, `SBER_API_REFRESH_TOKEN` |
| Номер счета | `GET /v1/client-info` после авторизации, либо СберБизнес/владелец | Владелец или API после consent пользователя | `SBER_API_ACCOUNT_NUMBER` |
| CA bundle для проверки сервера | Цепочка из TLS-handshake с `fintech.sberbank.ru` или доверенное хранилище ОС | Не секрет; нужен локальному `curl`, если системное хранилище не доверяет SberCA | `SBER_API_CA_BUNDLE_PATH` |
| TLS-сертификат клиента | Личный кабинет Sber API -> сертификаты шифрования / TLS-сертификат | Пользователь ЛК; пароль к контейнеру не писать в Markdown | `SBER_API_TLS_P12_PATH`, `SBER_API_TLS_P12_PASSWORD` или `SBER_API_TLS_CERT_PATH`, `SBER_API_TLS_KEY_PATH` |

Важно:

- `client_secret` имеет ограниченный срок действия; документация указывает 40 дней. После генерации секрет показывается один раз, поэтому его нужно сразу сохранить в защищенное хранилище или локальный `.env`.
- Промышленные настройки после заключения договора появляются в Личном кабинете Sber API и могут быть направлены техподдержкой на почту по запросу ответственного лица.
- Тестовые настройки не брать из догадок: по официальной инструкции их нужно запрашивать у поддержки с указанием промышленного `client_id`.
- `access_token` и `refresh_token`, созданные прямо в Личном кабинете, имеют сроки жизни, отличающиеся от токена после обновления через OAuth endpoint. Для рабочего сборщика лучше реализовать нормальный refresh flow.
- Если в организации несколько счетов, пользователь при авторизации/согласии должен дать доступ именно к тем счетам, которые нужны для ДДС/P&L. Иначе выписка может вернуть `403 ACCESS_EXCEPTION`.
- Для боевых REST-запросов Sber API gateway требует клиентский TLS-сертификат. Проверка 2026-05-19 без mTLS дошла до gateway и вернула HTML-ошибку `400 No required SSL certificate was sent`.
- Для Python/OpenSSL нужен локальный CA bundle с промежуточным `SberCA Ext` и корневым `SberCA Root Ext`, потому что сервер `fintech.sberbank.ru` отдает не всю цепочку. Bundle сохранен локально в `research/private/sber/certs/sberca_server_bundle.pem` и указан в `.env` как `SBER_API_CA_BUNDLE_PATH`.
- `SBER_API_ACCOUNT_NUMBER` должен быть записан строго как 20 цифр без пробелов и разделителей. При наличии пробелов `/v2/statement/*` возвращает `400` с проверкой регулярного выражения `^[0-9]{20}$`.

## Авторизация

Sber API использует OAuth 2.0 / OpenID Connect через СберБизнес ID.

### Получение authorization code

```text
GET /ic/sso/api/v2/oauth/authorize
```

Основные параметры:

| Параметр | Обязателен | Что передавать |
| --- | --- | --- |
| `response_type` | Да | `code` |
| `client_id` | Да | идентификатор приложения / платформы |
| `redirect_uri` | Да | зарегистрированный redirect URI |
| `scope` | Да | должен содержать `openid` и нужные операции |
| `state` | Да | случайная строка для CSRF-защиты, минимум 36 символов по рекомендации спеки |
| `nonce` | Нет | случайная строка для связывания сессии и `id_token` |
| `code_challenge`, `code_challenge_method` | Нет | PKCE, если согласован при регистрации платформы; метод только `S256` |
| `prompt` | Нет | `none`, `login`, `consent`, `select_account` |

При успешной авторизации браузер возвращается на `redirect_uri` с `code` и `state`. При ошибке возвращаются `error` и `error_description`.

### Обмен кода на токены

```text
POST /ic/sso/api/v2/oauth/token
Content-Type: application/x-www-form-urlencoded
```

Для первичного обмена:

```text
grant_type=authorization_code
client_id=<client_id>
client_secret=<client_secret>
code=<authorization_code>
redirect_uri=<redirect_uri>
code_verifier=<code_verifier_if_pkce_enabled>
```

Для обновления:

```text
grant_type=refresh_token
client_id=<client_id>
client_secret=<client_secret>
refresh_token=<refresh_token>
```

Документация указывает срок жизни `access_token` 60 минут. После обновления нужно использовать новый `refresh_token`; старый временно переводится в резервный статус.

Для REST-запросов токен передается в заголовке `Authorization`. В спеках встречаются два варианта примеров: сырой токен и `Bearer <token>`. Так как ответ OAuth содержит `token_type=Bearer`, первым пробовать стандартный заголовок:

```text
Authorization: Bearer <access_token>
```

Если тестовый контур вернет `401`, проверить по конкретному методу, не ожидает ли он токен без префикса `Bearer`.

Нужные scope для read-only сценариев:

| Scope | Для чего |
| --- | --- |
| `GET_CLIENT_ACCOUNTS` | `GET /v1/client-info`, организация и доступные счета |
| `GET_STATEMENT_ACCOUNT` | выписки, сводки, операции по счетам |
| `GET_CORRESPONDENTS` | список контрагентов по рублевым операциям |
| `DICT` | банковские справочники |

## Форматы дат

| Зона API | Параметр / поле | Формат | Комментарий |
| --- | --- | --- | --- |
| Выписка за день | `statementDate` | `yyyy-MM-dd` | Например `2026-05-19` |
| Инкрементальная выписка | `lastModifyDate`, `lastModifyDateTo` | `YYYY-MM-DDThh:mm:ss[.SSS]` | В примерах есть `2025-12-01T13:40:48.780` |
| Ответы выписки | `operationDate`, `composedDateTime`, `lastModifiedTime`, `reloadTime` | `date-time` | В спеках примеры без явного timezone |
| Ответы выписки | `documentDate`, `receiptDate`, `valueDate`, `lastMovementDate`, `prevOperationDate` | `date` | ISO-дата |
| Информация о клиенте | `orgRegDateINN`, `orgRegDateOGRN` | `date` | ISO-дата |

Рабочее правило проекта до практической проверки: считать даты банковского API датами банковского/московского операционного дня и явно фиксировать период в выгрузках.

## Пагинация и лимиты

- Для выписок и контрагентов используется параметр `page`.
- Страницы запрашивать с `page=1`.
- В ответе искать `_links[]` с `rel=next` и `rel=prev`.
- Если ссылки с `rel=next` больше нет, выгрузка страницы закончена.
- `pageSize` в изученных endpoint'ах не найден.
- Для endpoint'ов выписки в документации указан лимит 5 TPS.
- Выписка через Sber API доступна за предыдущие 5 лет плюс текущий год.
- Для управленческой выгрузки не параллелить счета и дни до отдельной проверки лимитов; безопасный старт - последовательно, один счет, один день, одна страница.

Endpoint'ы выписки с лимитом 5 TPS:

```text
GET /v2/statement/summary
GET /v2/statement/transactions
GET /v2/statement/transactionId
GET /v1/statement/print
GET /v2/statement/increment
GET /v1/statement/files
GET /v1/statement/tasks-for-download/{taskId}
GET /v1/statement/download/{fileId}
GET /v2/statement/transactionId/print
```

## Структура ошибок

OAuth-ошибка:

| Поле | Смысл |
| --- | --- |
| `error` | код, например `invalid_request`, `invalid_grant`, `invalid_client`, `invalid_token` |
| `error_description` | человекочитаемое описание |
| `error_uri` | ссылка на описание ошибки, если есть |

REST API / Fintech error:

| Поле | Смысл |
| --- | --- |
| `cause` | тип ошибки: `UNAUTHORIZED`, `ACTION_ACCESS_EXCEPTION`, `TOO_MANY_REQUESTS`, `WORKFLOW_FAULT`, `VALIDATION_FAULT`, `DATA_NOT_FOUND`, `UNKNOWN_EXCEPTION` и др. |
| `referenceId` | UUID ошибки для поддержки |
| `message` | сообщение банка |
| `checks[]` | результаты валидации |
| `checks[].fields[]` | поля, на которых упала проверка |
| `internalErrorCode` | внутренний код, если возвращен |

Типовые HTTP-статусы:

| Статус | Значение |
| --- | --- |
| `400` | неверный формат запроса, workflow/validation fault |
| `401` | access token некорректен или истек |
| `403` | у токена нет scope или пользователь не дал доступ к счету |
| `404` | данные/справочник/операция не найдены |
| `415` | неподдерживаемый формат тела, встречается в части методов |
| `429` | превышен лимит запросов |
| `500` | внутренняя ошибка |
| `503` | сервис временно недоступен |

## Практически проверенные endpoint'ы

Боевые read-only endpoint'ы Sber API проверены 2026-05-19 на промышленном контуре с mTLS.

| Что проверено | Статус | Источник |
| --- | --- | --- |
| Официальная витрина Sber API | доступна | `https://developers.sber.ru/docs/ru/sber-api/overview` |
| OpenAPI OAuth | скачана | `oauth.yaml` |
| OpenAPI выписок | скачана | `statement.yaml` |
| OpenAPI информации о клиенте | скачана | `client-info.yaml` |
| OpenAPI контрагентов | скачана | `correspondents.yaml` |
| OpenAPI справочников | скачана | `dicts.yaml` |
| Локальные данные подключения | готовы для read-only проверки | `.env`; есть `client_id`, `client_secret`, token-пара, расчетный счет, CA bundle и TLS cert/key; нет рабочего `redirect_uri` для полноценного OAuth callback |
| Запрос `/v1/client-info` без mTLS | дошел до gateway | HTTP `400 No required SSL certificate was sent`; подтверждает требование клиентского TLS-сертификата |
| Запрос `/v1/client-info` с mTLS | успешно | HTTP `200`; в ответе 2 счета: расчетный и ссудный; для ДДС выбран расчетный |
| Запрос `/v2/statement/summary` с mTLS | успешно | HTTP `200`; сводка за `2026-05-19` по расчетному счету |
| Запрос `/v2/statement/transactions` с mTLS | успешно | HTTP `200`; первая страница за `2026-05-19`, 7 операций |

После практической проверки добавлены локальные скрипты: `research/scripts/sber/export_statement.py` для raw-выписок и `research/scripts/sber/build_cashflow.py` для безопасных агрегатов ДДС. Период `2026-02-01`–`2026-05-19` выгружен и сверен.
Для диагностики выручки добавлен `research/scripts/sber/reconcile_iiko_revenue.py`: он сравнивает банковские поступления с iiko-выручкой по фокусным периодам.
Для операционной расшифровки добавлен `research/scripts/sber/build_operations_table.py`: он строит таблицу операций за период и извлекает комиссии эквайринга/приема платежей из `paymentPurpose`.

## Endpoint'ы из документации

### Авторизация и учетная запись

| Данные | Endpoint | Метод | Риск | Scope / условие |
| --- | --- | --- | --- | --- |
| Код авторизации | `/ic/sso/api/v2/oauth/authorize` | GET | безопасный browser redirect | `openid` + нужные scope |
| Получение/обновление токенов | `/ic/sso/api/v2/oauth/token` | POST | служебный, секретный | `client_id`, `client_secret`, `code` или `refresh_token` |
| Информация об учетной записи | `/ic/sso/api/v2/oauth/user-info` | GET | read-only | access token |
| Отзыв токена | `/ic/sso/api/v2/oauth/revoke` | POST | меняет состояние токена | не вызывать без явной задачи |
| Обновление client secret | `/ic/sso/api/v1/change-client-secret` | POST | меняет секрет | не вызывать без явной задачи |

### Информация о клиенте и счетах

| Данные | Endpoint | Метод | Что брать | Scope |
| --- | --- | --- | --- | --- |
| Организация и доступные счета | `/v1/client-info` | GET | `shortName`, `fullName`, `inn`, `kpps`, `accounts[]`, `dboContracts[]` | `GET_CLIENT_ACCOUNTS` |

### Выписки

| Данные | Endpoint | Метод | Параметры | Scope |
| --- | --- | --- | --- | --- |
| Сводка за день | `/v2/statement/summary` | GET | `accountNumber`, `statementDate` | `GET_STATEMENT_ACCOUNT` |
| Операции за день | `/v2/statement/transactions` | GET | `accountNumber`, `statementDate`, `page`, опционально `curFormat` | `GET_STATEMENT_ACCOUNT` |
| Одна операция | `/v2/statement/transactionId` | GET | `accountNumber`, `statementDate`, `operationId` | `GET_STATEMENT_ACCOUNT` |
| Инкрементальная выписка | `/v2/statement/increment` | GET | `accountNumber`, `statementDate` или `lastModifyDate`, `lastModifyDateTo`, `page` | `GET_STATEMENT_ACCOUNT` |
| Печатная форма операции | `/v2/statement/transactionId/print` | GET | `accountNumber`, `statementDate`, `operationId` | `GET_STATEMENT_ACCOUNT` |
| Файл выписки для экспорта | `/v1/statement/files` | GET | `accountNumber`, `statementDate`, формат | `GET_STATEMENT_ACCOUNT` |
| Файл выписки в печатном формате | `/v1/statement/print` | GET | `accountNumber`, `statementDate`, формат | `GET_STATEMENT_ACCOUNT` |
| Ссылка на скачивание | `/v1/statement/tasks-for-download/{taskId}` | GET | `taskId` | `GET_STATEMENT_ACCOUNT` |
| Скачивание файла | `/v1/statement/download/{fileId}` | GET | `fileId` | `GET_STATEMENT_ACCOUNT` |

`/v1/statement/files` и `/v1/statement/print` выглядят как `GET`, но могут создавать асинхронную задачу выгрузки. Для обычного P&L сначала использовать JSON-операции через `/v2/statement/transactions`, а файловые методы оставить для сверок.

### Контрагенты и справочники

| Данные | Endpoint | Метод | Параметры | Scope |
| --- | --- | --- | --- | --- |
| Контрагенты по рублевым операциям | `/v1/correspondents/rur` | GET | `page` | `GET_CORRESPONDENTS` |
| Банковский справочник | `/v1/dicts` | GET | `name` | `DICT` / доступ по подключению |

Доступные значения `name` для `/v1/dicts` из документации:

```text
BIC
ClearingStructure
Country
CurDict
GenericLetterType
MzpCardType
SalType
SwiftBic
VOCodes
```

## Рецепты запросов

Получить информацию о клиенте и доступных счетах:

```bash
curl -sS "$SBER_API_BASE_URL/v1/client-info" \
  --cacert "$SBER_API_CA_BUNDLE_PATH" \
  --cert "$SBER_API_TLS_CERT_PATH" \
  --key "$SBER_API_TLS_KEY_PATH" \
  -H "Authorization: Bearer $SBER_API_ACCESS_TOKEN" \
  -H "Accept: application/json"
```

Сводка по счету за день:

```bash
curl -sS -G "$SBER_API_BASE_URL/v2/statement/summary" \
  --cacert "$SBER_API_CA_BUNDLE_PATH" \
  --cert "$SBER_API_TLS_CERT_PATH" \
  --key "$SBER_API_TLS_KEY_PATH" \
  -H "Authorization: Bearer $SBER_API_ACCESS_TOKEN" \
  -H "Accept: application/json" \
  --data-urlencode "accountNumber=$SBER_API_ACCOUNT_NUMBER" \
  --data-urlencode "statementDate=2026-05-19"
```

Операции по счету за день, первая страница:

```bash
curl -sS -G "$SBER_API_BASE_URL/v2/statement/transactions" \
  --cacert "$SBER_API_CA_BUNDLE_PATH" \
  --cert "$SBER_API_TLS_CERT_PATH" \
  --key "$SBER_API_TLS_KEY_PATH" \
  -H "Authorization: Bearer $SBER_API_ACCESS_TOKEN" \
  -H "Accept: application/json" \
  --data-urlencode "accountNumber=$SBER_API_ACCOUNT_NUMBER" \
  --data-urlencode "statementDate=2026-05-19" \
  --data-urlencode "page=1"
```

Инкрементальная выписка за интервал внутри операционного дня:

```bash
curl -sS -G "$SBER_API_BASE_URL/v2/statement/increment" \
  --cacert "$SBER_API_CA_BUNDLE_PATH" \
  --cert "$SBER_API_TLS_CERT_PATH" \
  --key "$SBER_API_TLS_KEY_PATH" \
  -H "Authorization: Bearer $SBER_API_ACCESS_TOKEN" \
  -H "Accept: application/json" \
  --data-urlencode "accountNumber=$SBER_API_ACCOUNT_NUMBER" \
  --data-urlencode "lastModifyDate=2026-05-19T10:00:00.000" \
  --data-urlencode "lastModifyDateTo=2026-05-19T11:00:00.000" \
  --data-urlencode "page=1"
```

Контрагенты по рублевым операциям:

```bash
curl -sS -G "$SBER_API_BASE_URL/v1/correspondents/rur" \
  --cacert "$SBER_API_CA_BUNDLE_PATH" \
  --cert "$SBER_API_TLS_CERT_PATH" \
  --key "$SBER_API_TLS_KEY_PATH" \
  -H "Authorization: Bearer $SBER_API_ACCESS_TOKEN" \
  -H "Accept: application/json" \
  --data-urlencode "page=1"
```

Справочник валют:

```bash
curl -sS -G "$SBER_API_BASE_URL/v1/dicts" \
  --cacert "$SBER_API_CA_BUNDLE_PATH" \
  --cert "$SBER_API_TLS_CERT_PATH" \
  --key "$SBER_API_TLS_KEY_PATH" \
  -H "Authorization: Bearer $SBER_API_ACCESS_TOKEN" \
  -H "Accept: application/json" \
  --data-urlencode "name=CurDict"
```

Локальный сборщик для выписки за один день или период:

```bash
python3 research/scripts/sber/export_statement.py --date 2026-05-19
python3 research/scripts/sber/export_statement.py --start-date 2026-05-01 --end-date 2026-05-19
python3 research/scripts/sber/export_statement.py --start-date 2026-02-01 --end-date 2026-05-19 --sleep-seconds 0.5
```

Сборщик печатает только маску счета, количество операций и путь к manifest. Сырые `summary.json` / `transactions_page_*.json` сохраняются в `research/private/sber/statement/`.

Построить безопасные агрегаты ДДС из raw-выписок:

```bash
python3 research/scripts/sber/build_cashflow.py --start-date 2026-05-01 --end-date 2026-05-19
python3 research/scripts/sber/build_cashflow.py --start-date 2026-02-01 --end-date 2026-05-19
```

Агрегаты сохраняются в `research/processed/sber/`. Приватная расшифровка контрагентов и назначений платежей сохраняется отдельно в `research/private/sber/processed/`.

Сверить банковские поступления с iiko-выручкой:

```bash
python3 research/scripts/sber/reconcile_iiko_revenue.py
```

Собрать таблицу операций и комиссий за период:

```bash
python3 research/scripts/sber/build_operations_table.py --start-date 2026-05-13 --end-date 2026-05-18
```

Если endpoint возвращает `401`, повторить с заголовком без `Bearer` только после фиксации этого факта в рабочем логе:

```text
Authorization: <access_token>
```

## Поля, полезные для управленческой отчетности / P&L

| Блок | Поля | Для чего |
| --- | --- | --- |
| Организация | `shortName`, `fullName`, `inn`, `kpps`, `resident` | идентифицировать юрлицо и договоры |
| Счета | `accounts[].number`, `accounts[].name`, `accounts[].currencyCode`, `accounts[].bic`, `accounts[].type` | выбрать расчетные счета для ДДС/P&L |
| Договоры ДБО | `dboContracts[]` | понять доступные банковские договоры |
| Сводка выписки | `openingBalance`, `closingBalance`, `debitTurnover`, `creditTurnover`, `debitTransactionsNumber`, `creditTransactionsNumber` | сверка ДДС, остатков и оборотов |
| Операция | `operationId`, `uuid`, `hashAbc`, `operationDate`, `documentDate`, `direction`, `amount`, `amountRub`, `operationCode`, `paymentPurpose` | факт поступлений/списаний, назначение платежа, дедупликация |
| Рублевый перевод | `rurTransfer.payerName`, `payerInn`, `payerAccount`, `payeeName`, `payeeInn`, `payeeAccount`, `payeeBankBic`, `payerBankBic` | маппинг контрагента на статью P&L/ДДС |
| Валютный перевод | `curTransfer.*`, `swiftTransfer.*` | валютные операции, если появятся |
| Контрагенты | `name`, `inn`, `kpp`, `accountNumber`, `bankBic`, `bankName`, `signed`, `remark` | справочник подрядчиков и поставщиков |
| Справочники | `BIC`, `CurDict`, `Country`, `VOCodes` | расшифровка банков, валют и валютных операций |

Для P&L полезнее всего начинать с выписок:

```text
ДДС / банк = statement transactions
Остатки и обороты = statement summary
Справочник счетов = client-info.accounts
Маппинг подрядчиков = correspondents + paymentPurpose + ИНН
```

## Ограничения и риски

- Документация Sber API широкая: в ней есть платежи, зарплаты, кредиты, карты, СБП и другие write-сценарии. Для текущей аналитики использовать только read-only блоки.
- На промышленном контуре 2026-05-19 практически проверены `Authorization: Bearer ...`, mTLS, доступ к счетам и scope `GET_CLIENT_ACCOUNTS` / `GET_STATEMENT_ACCOUNT`.
- Пользователь в СберБизнес ID должен дать согласие и отметить счета, доступные платформе. Иначе возможен `403 ACCESS_EXCEPTION`.
- Примеры `Authorization` в разных спеках отличаются: где-то указан bare token, где-то `Bearer`. Для проверенных промышленных read-only endpoint'ов работает `Bearer`.
- Даты в ответах имеют `date-time`, но в примерах нет явного timezone. До сверки считать банковским операционным днем и фиксировать локальную дату выгрузки.
- Выписки выдаются постранично и за один день; для месяца нужно идти по дням и счетам.
- Файловые методы могут создавать задачу выгрузки, а ссылка на скачивание может иметь ограниченный срок жизни.
- Ошибки банка могут содержать `referenceId`; его можно сохранять для поддержки, но без токенов и персональных данных.
- Сырые банковские данные нельзя коммитить в репозиторий.

## Вопросы к владельцу

1. Кто в организации имеет полномочия ЕИО и может подписать заявление/корректирующее заявление Sber API?
2. Есть ли у нас пользователь с ролью `Разработчик Sber API`, или его нужно добавить?
3. Нужен только read-only доступ к выпискам или также планируются платежные операции?
4. Какое юрлицо/ИП подключаем и какие расчетные счета нужны для управленческой отчетности?
5. Есть ли уже зарегистрированный сервис Sber API: `client_id`, `redirect_uri`, список scope?
6. Где будет проходить OAuth redirect: локальный callback, внешний backend или ручной обмен кода?
7. Какие статьи P&L нужно закрывать банковской выпиской в первую очередь?
8. Нужна ли выгрузка только по текущему году или исторически за 2024-2026?

## Следующие действия

1. Расширить `research/scripts/sber/export_statement.py` на полный нужный период 2026, сохраняя последовательный режим без параллельных запросов.
2. Разметить `research/private/sber/processed/transactions_private.csv` и `counterparty_map_private.csv` по статьям ДДС/P&L.
3. Отделить операционные расходы от кредитов, налогов, внутренних переводов и прочих ниже EBITDA.
4. Разложить банковские поступления по источникам оплаты и сверить с iiko-выручкой с учетом эквайринга, агрегаторов, комиссий, наличных и кассовых лагов.
5. В Личном кабинете Sber API задать рабочий `redirect_uri`, потому что в локальном документе он неполный.
6. Реализовать refresh flow для долгоживущего сборщика, чтобы не зависеть от ручной token-пары из ЛК.
7. Запросить тестовые настройки у `supportdbo2@sberbank.ru` с промышленным `client_id`, если нужен тестовый контур.
