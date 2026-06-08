# Продовые секреты Teplo

Целевая схема для одного VDS: секреты остаются на продовом хосте и не попадают в
git. Docker Compose прокидывает runtime-env из `deploy/.env.prod`, а файловые
секреты монтируются read-only из `deploy/secrets` в `/run/secrets/teplo`.

## Что где хранится

- `deploy/.env.prod` — домен, Postgres password, первичный пароль админа, JWT,
  prod-режимы приложения. Файл должен быть только на сервере, права `600`.
- `deploy/secrets/sber/*` — Sber mTLS cert/key/CA bundle, если они нужны для live
  API. В контейнере эти файлы доступны как `/run/secrets/teplo/sber/...`.
- Таблица `source_credential` — активные банковские credentials:
  `sber/access_token`, `sber/mtls_cert_path`, `sber/mtls_key_path`,
  `tbank/bearer_token`. API не возвращает значения credentials наружу.
- Внешний password manager — источник правды для значений, которые нужно знать
  людям: пароль админа, банковские токены, mTLS key/cert, аварийные recovery-коды.

Важно: поле `source_credential.value_encrypted` сейчас хранит значение в БД без
прикладного шифрования. До добавления envelope encryption доступ к БД, дампам и
backup remote считается доступом к банковским токенам.

## Первый запуск

На сервере:

```bash
cd /opt/teplo/deploy
TEPLO_DOMAIN=app.company.ru TEPLO_ADMIN_EMAIL=admin@company.ru ./init-prod-env.sh
nano .env.prod
./check-prod-secrets.sh
```

`init-prod-env.sh` не печатает секреты в терминал. Он создает `.env.prod` с
URL-safe случайными значениями и каталог `deploy/secrets/sber`.

После проверки:

```bash
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d --build
docker compose -f docker-compose.prod.yml --env-file .env.prod exec api alembic upgrade head
```

Если БД восстанавливается из dev-дампа для go-live, следуй
`deploy/prod-bootstrap/README.md`: там есть отдельный шаг смены пароля админа.

## Банковские credentials

Sber mTLS файлы положить на хосте:

```bash
install -d -m 700 /opt/teplo/deploy/secrets/sber
install -m 600 client.crt /opt/teplo/deploy/secrets/sber/client.crt
install -m 600 client.key /opt/teplo/deploy/secrets/sber/client.key
# если нужен отдельный CA bundle:
install -m 600 ca.pem /opt/teplo/deploy/secrets/sber/ca.pem
```

Пути, которые нужно сохранить в credentials, должны быть контейнерными:

```bash
docker compose -f docker-compose.prod.yml --env-file .env.prod exec api \
  python -m app.scripts.set_credential sber mtls_cert_path /run/secrets/teplo/sber/client.crt

docker compose -f docker-compose.prod.yml --env-file .env.prod exec api \
  python -m app.scripts.set_credential sber mtls_key_path /run/secrets/teplo/sber/client.key
```

Sber access token и T-Bank bearer token вводить через stdin, чтобы значение не
попадало в shell history и argv:

```bash
docker compose -f docker-compose.prod.yml --env-file .env.prod exec -T api \
  python -m app.scripts.set_credential sber access_token --value-stdin
# paste token, then Ctrl-D

docker compose -f docker-compose.prod.yml --env-file .env.prod exec -T api \
  python -m app.scripts.set_credential tbank bearer_token --value-stdin
# paste token, then Ctrl-D
```

Если счет не заведен в справочнике приложения, можно временно указать fallback в
`.env.prod`: `SBER_API_ACCOUNT_NUMBER` или `TBANK_API_ACCOUNT_NUMBER`.

## Ротация

- JWT: заменить `JWT_SECRET_KEY` в `.env.prod`, перезапустить `api` и `scheduler`.
  Все текущие сессии пользователей станут недействительными.
- Postgres password: менять нужно синхронно в Postgres и `.env.prod`, затем
  пересоздать сервисы, которые подключаются к БД.
- Bank credentials: повторный `set_credential` деактивирует старое значение и
  делает новое активным.
- Sber cert/key: положить новые файлы в `deploy/secrets/sber`, обновить
  `mtls_cert_path`/`mtls_key_path` при смене имени файла, перезапустить sync.

Перед каждым `up -d --build` на проде запускай:

```bash
cd /opt/teplo/deploy
./check-prod-secrets.sh
```
