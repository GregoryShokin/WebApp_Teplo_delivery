# Продовые секреты Teplo

Первый go-live использует только IIKO и T-Bank. Sber, mTLS, Mango, Mail.ru и
Telegram не входят в обязательный prod-набор и не должны блокировать старт.

Все интеграционные значения вводятся только на сервере в файле:

```bash
/opt/teplo/deploy/.env.integrations
```

Не передавай банковские токены аргументами команд: они могут попасть в shell
history, audit log или список процессов.

## Что где хранится

- `/opt/teplo/deploy/.env.prod` - домен, Postgres password, пароль первого
  админа, JWT и prod-режимы приложения. Права `600`.
- `/opt/teplo/deploy/.env.integrations` - IIKO и T-Bank runtime-секреты для
  `api` и `scheduler`. Права `600`.
- Таблица `source_credential` - активный T-Bank credential
  `tbank/bearer_token`, синхронизированный из `TBANK_API_ACCESS_TOKEN`.
- IIKO credentials не сохраняются в БД. IIKO-клиент читает `IIKO_SERVER_*` из
  env и сам получает токен по логину и паролю.
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

После проверки:

```bash
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d --build
docker compose -f docker-compose.prod.yml --env-file .env.prod exec api alembic upgrade head
```

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

Перед каждым `up -d --build` на проде запускай:

```bash
cd /opt/teplo/deploy
./check-prod-secrets.sh
```
