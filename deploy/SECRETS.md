# Продовые секреты Teplo

Первый go-live использует только IIKO и T-Bank. Sber, mTLS, Mango, Mail.ru и
Telegram не входят в обязательный prod-набор и не должны блокировать старт.

Это по-прежнему верно про **старт**: без них `api` и `scheduler` поднимаются. Но с
момента, когда список писался, на почте выросли два работающих контура («Страница на
оплату» и модуль «Налоги»), а к ним добавились Claude и СБИС. Пустое значение у них —
законный выбор, только принимать его надо осознанно: см. «Необязательные контуры»
ниже, там сказано, что именно выключается вместе с каждым.

Все интеграционные значения вводятся только на сервере в файле:

```bash
/opt/teplo/deploy/.env.integrations
```

Не передавай банковские токены аргументами команд: они могут попасть в shell
history, audit log или список процессов.

## Что где хранится

- `/opt/teplo/deploy/.env.prod` - домен, Postgres password, пароль первого
  админа, JWT, токен входящего вебхука T-Банка и prod-режимы приложения.
  Права `600`.
- `TBANK_WEBHOOK_TOKEN` - НАШ shared secret, а не выданный банком: банк присылает
  его в `Authorization: Bearer` на вебхук «Статус платежа». Вебхук мутирует финучёт
  (гасит и откатывает накладные), а фильтр по IP за прокси ненадёжен, поэтому токен —
  основная защита. В production он обязателен: без него валидация `Settings` падает
  на импорте, и `api` со `scheduler` не стартуют вовсе. `init-prod-env.sh` генерирует
  значение сам; его нужно сообщить банку при подключении вебхука.
- `/opt/teplo/deploy/.env.integrations` - IIKO и T-Bank runtime-секреты для
  `api` и `scheduler`. Права `600`.
- Таблица `source_credential` - активный T-Bank credential
  `tbank/bearer_token`, синхронизированный из `TBANK_API_ACCESS_TOKEN`.
- IIKO credentials не сохраняются в БД. IIKO-клиент читает `IIKO_SERVER_*` из
  env и сам получает токен по логину и паролю; Cloud-контур накладных (пуш,
  `add_payment`, Cloud-синк) читает оттуда же `IIKO_CLOUD_*`.
- Внешний password manager - источник правды для значений, которые нужно знать
  людям: пароль админа, JWT recovery-процедуры, банковский bearer token.

Важно: `source_credential.value_encrypted` сейчас хранит значение в БД без
прикладного envelope encryption. До его добавления доступ к БД, дампам и backup
remote считается доступом к T-Bank token.

## Первый запуск

На сервере:

```bash
cd /opt/teplo/deploy
TEPLO_DOMAIN=app.company.ru TEPLO_ADMIN_EMAIL=admin@company.ru ./init-prod-env.sh
nano .env.prod
[[ -f .env.integrations ]] || install -m 600 env.integrations.example .env.integrations
nano .env.integrations
./check-prod-secrets.sh
```

Минимальный `.env.integrations`:

```dotenv
IIKO_SERVER_BASE_URL=
IIKO_SERVER_LOGIN=
IIKO_SERVER_PASSWORD=
IIKO_SERVER_TIMEOUT_SECONDS=90

IIKO_CLOUD_APP_ID=
IIKO_CLOUD_API_LOGIN=
IIKO_CLOUD_CLIENT_SECRET=

TBANK_API_BASE_URL=https://business.tbank.ru/openapi
TBANK_PAYMENT_BASE_URL=https://secured-openapi.tbank.ru
TBANK_API_ACCESS_TOKEN=
TBANK_API_ACCOUNT_NUMBER=
TBANK_API_TIMEOUT_SECONDS=90
```

`TBANK_API_ACCESS_TOKEN` - общий bearer token для T-Bank API: выписки, движение
по счету и создание платежных черновиков. Не переименовывай его в
`TBANK_API_PAYMENT_DRAFT_TOKEN`.

`TBANK_API_ACCOUNT_NUMBER` нужен для создания T-Bank payment drafts как счет
плательщика. Для импорта выписок счет также может прийти из справочника счетов
или metadata, поэтому отсутствие этой переменной не должно ломать startup.

После проверки перечитай переменные — пересоздания контейнеров достаточно,
пересобирать образ ради секретов не нужно (они приходят из `.env`, а не из кода):

```bash
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d api scheduler
```

Миграции к смене секретов отношения не имеют: если вместе с этим катится и новый
код, иди по порядку из `deploy/README.md` («Updating after a git pull») — там
миграция применяется ДО пересоздания контейнеров.

Если БД восстанавливается из dev-дампа для go-live, следуй
`deploy/prod-bootstrap/README.md`: там есть отдельный шаг смены пароля админа.

## Синхронизация T-Bank token

После заполнения `.env.integrations` запусти внутри контейнера:

```bash
cd /opt/teplo/deploy
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d api scheduler
docker compose -f docker-compose.prod.yml --env-file .env.prod exec -T api \
  python -m app.scripts.sync_integration_secrets
docker compose -f docker-compose.prod.yml --env-file .env.prod exec -T api \
  python -m app.scripts.sync_integration_secrets --check
```

`sync_integration_secrets` читает `TBANK_API_ACCESS_TOKEN` из env контейнера,
деактивирует прежний активный `tbank/bearer_token` и создает новое активное
значение. IIKO значения этот скрипт только проверяет как env-настройку и в БД не
кладет.

`--check` печатает только статусы:

```text
iiko_env=set
tbank_bearer_token=env:set db:set
```

Секретные значения не выводятся.

## Sber API OAuth

Если в `.env.prod` задано `BANK_SYNC_PROVIDERS=sber,...`, добавь в
`.env.integrations` все значения Sber: `SBER_API_CLIENT_ID`,
`SBER_API_CLIENT_SECRET`, `SBER_API_ACCESS_TOKEN`, `SBER_API_REFRESH_TOKEN`,
`SBER_API_ACCOUNT_NUMBER`, `SBER_API_TLS_CERT_PATH` и `SBER_API_TLS_KEY_PATH`.
`SBER_API_TOKEN_URL` оставь значением из шаблона. После перезапуска выполни
`sync_integration_secrets`; access/refresh пара сохранится в БД. Сбер выдаёт
новый refresh token при каждом обновлении, поэтому не заменяй его вручную после
успешного запуска — приложение ротирует пару само.

Планировщик каждый день в 03:15 МСК обслуживает Sber credentials: за 5 дней до
окончания 40-дневного срока меняет `client_secret` через API Сбера и сохраняет
новое значение в БД. Значения OAuth в `.env.integrations` служат только для
первичного заполнения: последующий `sync_integration_secrets` не перезаписывает
ими автоматически обновлённые access/refresh/client secret.

mTLS-сертификат Сбер не перевыпускает через API. Планировщик проверяет его
`notAfter` ежедневно и за 30 дней создаёт кейс
`sber_tls_certificate_expiring`; новый сертификат нужно выпустить в кабинете
Сбера или по CSR через поддержку и установить в `secrets/sber/` обычным
деплоем.

## Необязательные контуры (`.env.prod`)

Ни одно значение из этого раздела не мешает `api` и `scheduler` подняться — все они
включают и выключают функции. Тем и опасны: стенд выглядит исправным, а часть работы
просто не делается. Заполняются они в `.env.prod`, а НЕ в `.env.integrations`:
`build_integrations_env.py` владеет только префиксами `IIKO_`, `TBANK_` и `SBER_`, а
отрендеренный `.env.integrations` грузится последним — ключ без локального значения
уедет туда пустым и затрёт то, что стоит в `.env.prod`.

- **`ANTHROPIC_API_KEY`** — распознавание счетов, «ИИ-разбор»/«ИИ-ревизия» в «Налогах»,
  переоценка ОС. Без ключа счёт разбирает только детерминированный слой (регексы), и всё,
  что ниже порога уверенности, уходит оператору в «требует проверки»; ИИ-кнопки отвечают
  422, джоба переоценки ОС помечает записи ошибкой.
- **`ANTHROPIC_BASE_URL`** — адрес релея. С российского IP Anthropic отвечает 403 «Request
  not allowed» даже на верный ключ (проверено на проде 27.07.2026), поэтому одного ключа
  мало: запросы идут через воркер из `deploy/anthropic-relay-worker.js`. Переменную читает
  сам SDK — в `Settings` её нет, — так что она должна быть именно в окружении контейнера.
  Альтернатива релею — `HTTPS_PROXY` с выходом в разрешённый регион.
- **`ANTHROPIC_RELAY_SECRET`** — общий пароль релея (уходит заголовком `x-relay-secret`).
  Воркер отвечает 403 всем, у кого заголовок не совпал, поэтому вместе с адресом релея
  секрет обязателен: без него падает каждый вызов модели, а выглядит это как плохой ключ.
- **`MAILRU_EMAIL` / `MAILRU_APP_PASSWORD` / `MAILRU_WORKMAIL` / `MAILRU_WORKMAIL_PASSWORD`** —
  оба ящика. Ящик берётся в работу, только если заданы и логин, и пароль (пароль —
  внешний пароль приложения, не пароль от ящика). Если не задан ни один, оба прохода
  возвращают `not_configured`, причём планировщик не пишет об этом даже строчки в лог:
  «Страница на оплату» и налоговый staging просто остаются пустыми.
- **`TAX_DOCUMENT_SENDERS`** — отправители, чьи вложения считаются налоговыми. Пусто →
  дефолт методологии из кода, так что переменная нужна при смене бухгалтера. Письма с
  других адресов отбрасываются молча.
- **`SBIS_APP_CLIENT_ID` / `SBIS_SECRET_KEY`** — сервисная авторизация ЭДО. Без пары
  ежечасный синк выходит сразу, а кнопка «Синхронизировать» отвечает 409 «СБИС не
  настроен»; зеркало входящих документов остаётся пустым.
- **`IIKO_WEBHOOK_TOKEN`** (лежит в `.env.integrations`) — единственный здесь пункт про
  безопасность, а не про функциональность. Пустое значение выключает не вебхук, а его
  ПРОВЕРКУ: `POST /api/v1/webhooks/iiko` начинает принимать события без авторизации, а
  этот эндпоинт открывает и закрывает смены курьеров, то есть двигает учёт смен и ЗП.
  Пока подписка iikoCloud не настроена, эндпоинт всё равно открыт наружу.

Выключатели `MAIL_POLL_ENABLED`, `TAX_DOCUMENT_POLL_ENABLED` и `SBIS_SYNC_ENABLED` в
шаблоне стоят в `true` (как и дефолты кода). Если контур не нужен, гаси его явным
`false` — тогда `check-prod-secrets.sh` перестанет предупреждать о пустых значениях, и
«выключено осознанно» станет отличимо от «забыли заполнить».

## Что не требуется для первого go-live

- Sber access token.
- Sber mTLS cert/key/CA bundle.
- `source_credential` записи для Sber.
- Передача токенов через `python -m app.scripts.set_credential ... <token>`.

Отсутствие Sber variables не должно ломать `check-prod-secrets.sh`,
`sync_integration_secrets` или production startup.

По умолчанию scheduler синхронизирует только T-Bank. Когда Sber будет готов,
его можно включить отдельно через `BANK_SYNC_PROVIDERS=sber,tbank` и отдельный
runbook для Sber credentials.

## Ротация

- JWT: заменить `JWT_SECRET_KEY` в `.env.prod`, перезапустить `api` и
  `scheduler`. Все текущие сессии пользователей станут недействительными.
- Postgres password: менять синхронно в Postgres и `.env.prod`, затем
  пересоздать сервисы, которые подключаются к БД.
- T-Bank bearer token: заменить `TBANK_API_ACCESS_TOKEN` в `.env.integrations`,
  перезапустить `api` и `scheduler`, затем снова выполнить
  `python -m app.scripts.sync_integration_secrets`.
- IIKO login/password: заменить `IIKO_SERVER_LOGIN` и `IIKO_SERVER_PASSWORD` в
  `.env.integrations`, затем перезапустить `api` и `scheduler`.

Перед каждой выкаткой на прод запускай:

```bash
cd /opt/teplo/deploy
./check-prod-secrets.sh
```
