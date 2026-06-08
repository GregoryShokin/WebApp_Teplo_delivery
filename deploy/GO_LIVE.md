# Go-live runbook

Практический порядок переноса текущего локального приложения Teplo в production.
Команды с production-сервером выполняются из `/opt/teplo/deploy`, если не
указано иначе.

В примерах замени только плейсхолдеры:

```bash
PROD_HOST=<ssh-host>
PROD_USER=<ssh-user>
PROD_DOMAIN=<prod-domain>
```

## 0. Что нельзя делать

- Не коммитить `.env.prod`, `.env.integrations`, токены, пароли, дампы БД.
- Не отправлять секреты в чат, Drive, issue tracker или любой другой внешний
  канал.
- Не передавать токены через аргументы команд: они могут попасть в shell
  history, audit log или список процессов.
- Не переносить `node_modules`, `.venv`, `.ruff_cache`, `.pytest_cache`,
  `dist`, `test-results`.
- Не затирать серверные `deploy/.env.prod`, `deploy/.env.integrations`,
  `deploy/secrets`, `deploy/backups`.
- Не выводить значения IIKO или T-Bank credentials в терминал, логи или
  screenshots. T-Bank token называется только `TBANK_API_ACCESS_TOKEN`.

## 1. Предусловия

- Есть SSH-доступ к серверу:

  ```bash
  ssh "$PROD_USER@$PROD_HOST"
  ```

- Код на локальной машине в рабочем состоянии.
- На сервере есть каталог `/opt/teplo`.
- На сервере есть `/opt/teplo/deploy/.env.prod` с правами `600`.
- На сервере создан `/opt/teplo/deploy/.env.integrations` с IIKO + T-Bank
  значениями и правами `600`.
- IIKO credentials остаются env-only: они читаются из `.env.integrations` и не
  синхронизируются в БД.
- В `.env.integrations` задан `TBANK_API_ACCESS_TOKEN`.
- Проверка production-секретов проходит:

  ```bash
  cd /opt/teplo/deploy
  ./check-prod-secrets.sh
  ```

  Ожидаемый результат: `Production secret check passed for ...`.

- На сервере есть место для backup:

  ```bash
  df -h /opt/teplo
  mkdir -p /opt/teplo/deploy/backups
  chmod 700 /opt/teplo/deploy/backups
  ```

## 2. Локальные проверки перед отправкой

Из корня репозитория:

```bash
cd apps
make api-lint
```

Ожидаемый результат: `ruff check` завершается без ошибок.
Если Makefile не находит backend virtualenv, передай python явно:

```bash
make API_PYTHON=/absolute/path/to/python api-lint
```

Форматирование backend, если нужно проверить без изменений:

```bash
cd apps/api
python -m ruff format --check .
```

Если проектная команда форматирует код перед отправкой:

```bash
cd apps/api
python -m ruff format .
```

Backend tests:

```bash
cd apps
make api-test
```

Ожидаемый результат: все pytest-тесты проходят.
Если нужен явный backend python:

```bash
make API_PYTHON=/absolute/path/to/python api-test
```

Frontend lint и build:

```bash
cd apps
npm --workspace web run lint
npm --workspace web run build
```

Ожидаемый результат: `eslint`, `tsc` и `vite build` завершаются без ошибок.

Frontend e2e/smoke, если Playwright установлен и окружение готово:

```bash
cd apps
npm --workspace web run test:e2e
```

Ожидаемый результат: Playwright-тесты проходят. Если e2e не запускаются из-за
локальной инфраструктуры, зафиксируй причину в go-live заметках до deploy.

Проверка git status:

```bash
git status --short
git diff -- deploy/GO_LIVE.md
```

Ожидаемый результат: в `git status` нет секретов, дампов, build artifacts или
локальных cache-директорий.

## 3. Доставка кода

### Вариант A: git pull на сервере

Предпочтительный вариант - обновить код через git на сервере:

```bash
ssh "$PROD_USER@$PROD_HOST"
cd /opt/teplo
git status --short
git pull --ff-only
git rev-parse --short HEAD
cd deploy
./check-prod-secrets.sh
```

Ожидаемый результат:

- `git pull --ff-only` обновил рабочее дерево без merge commit.
- `git status --short` не показывает неожиданных локальных изменений.
- `check-prod-secrets.sh` проходит.

### Вариант B: rsync

Альтернатива, если на сервере нет git workflow. Запускать локально из корня
репозитория. Скрипт по умолчанию делает только dry-run и не меняет сервер:

```bash
deploy/sync-code-to-server.sh "$PROD_USER@$PROD_HOST" /opt/teplo
```

Проверь вывод dry-run: в списке не должно быть секретов, дампов, raw/private
данных или локальных cache/build-директорий. Для реальной синхронизации нужен
явный флаг:

```bash
deploy/sync-code-to-server.sh --apply "$PROD_USER@$PROD_HOST" /opt/teplo
```

После rsync на сервере:

```bash
cd /opt/teplo/deploy
test -f .env.prod
test -f .env.integrations
test -d backups
./check-prod-secrets.sh
```

Ожидаемый результат: серверные env-файлы, `secrets` и `backups` остались на
месте, проверка секретов проходит.

## 4. Backup текущего prod

На сервере:

```bash
cd /opt/teplo/deploy
mkdir -p /opt/teplo/deploy/backups
chmod 700 /opt/teplo/deploy/backups

PRE_GO_LIVE_BACKUP="/opt/teplo/deploy/backups/pre-go-live-$(date +%Y%m%d-%H%M%S).dump"

docker compose -f docker-compose.prod.yml --env-file .env.prod exec -T postgres \
  sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' \
  > "$PRE_GO_LIVE_BACKUP"

chmod 600 "$PRE_GO_LIVE_BACKUP"

docker compose -f docker-compose.prod.yml --env-file .env.prod exec -T postgres \
  pg_restore -l < "$PRE_GO_LIVE_BACKUP" >/dev/null

ls -lh "$PRE_GO_LIVE_BACKUP"
```

Ожидаемый результат: создан readable custom-format dump. Сохрани путь
`PRE_GO_LIVE_BACKUP` в go-live заметках: он нужен для rollback.

## 5. Перенос локальной БД

Полный runbook: [deploy/prod-bootstrap/LOCAL_TO_PROD_DB.md](prod-bootstrap/LOCAL_TO_PROD_DB.md).

Краткий порядок ниже переносит локальную историю без данных таблицы
`source_credential`, чтобы локальные credentials не попали в prod.

### 5.1. Локальный dump без `source_credential`

Из корня репозитория, если локальная БД запущена через Docker Compose:

```bash
DUMP="${TMPDIR:-/tmp}/teplo-local-history-$(date +%Y%m%d-%H%M%S).dump"

deploy/prod-bootstrap/create-local-db-dump.sh \
  --docker-compose \
  --output "$DUMP"

chmod 600 "$DUMP"
```

Ручной эквивалент:

```bash
DUMP="${TMPDIR:-/tmp}/teplo-local-history-$(date +%Y%m%d-%H%M%S).dump"

docker compose -f apps/docker-compose.yml exec -T postgres \
  pg_dump -U teplo -d teplo -Fc \
    --exclude-table-data=source_credential \
    --exclude-table-data='*.source_credential' \
  > "$DUMP"

chmod 600 "$DUMP"
```

Проверка должна ничего не найти:

```bash
if pg_restore -l "$DUMP" | grep -E 'TABLE DATA .* source_credential( |$)'; then
  echo "ERROR: dump contains source_credential table data"
  exit 1
fi
```

### 5.2. Передать dump на сервер

Локально:

```bash
DUMP_BASENAME="$(basename "$DUMP")"

ssh "$PROD_USER@$PROD_HOST" \
  'mkdir -p /opt/teplo/deploy/imports && chmod 700 /opt/teplo/deploy/imports'

scp "$DUMP" "$PROD_USER@$PROD_HOST:/opt/teplo/deploy/imports/$DUMP_BASENAME"
```

На сервере:

```bash
REMOTE_DUMP=/opt/teplo/deploy/imports/teplo-local-history-YYYYMMDD-HHMMSS.dump
test -s "$REMOTE_DUMP"
chmod 600 "$REMOTE_DUMP"
```

Подставь реальное имя файла, переданного в `/opt/teplo/deploy/imports`.

### 5.3. Stop api/scheduler/web и restore

На сервере:

```bash
cd /opt/teplo/deploy
./check-prod-secrets.sh

docker compose -f docker-compose.prod.yml --env-file .env.prod stop api scheduler web
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d postgres

docker compose -f docker-compose.prod.yml --env-file .env.prod exec -T postgres \
  sh -c 'pg_restore --exit-on-error -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner' \
  < "$REMOTE_DUMP"
```

Проверка, что локальные credentials не восстановились:

```bash
docker compose -f docker-compose.prod.yml --env-file .env.prod exec -T postgres \
  sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SELECT count(*) FROM source_credential;"'
```

Ожидаемый результат: `0`.

### 5.4. Alembic upgrade head

На сервере:

```bash
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d api

docker compose -f docker-compose.prod.yml --env-file .env.prod exec -T api \
  alembic upgrade head
```

Ожидаемый результат: миграции применены без ошибок.

## 6. Секреты интеграций

На сервере открыть и заполнить `.env.integrations`:

```bash
cd /opt/teplo/deploy
[[ -f .env.integrations ]] || install -m 600 env.integrations.example .env.integrations
nano .env.integrations
chmod 600 .env.integrations
```

Минимально нужны:

- `IIKO_SERVER_BASE_URL`
- `IIKO_SERVER_LOGIN`
- `IIKO_SERVER_PASSWORD`
- `TBANK_API_ACCESS_TOKEN`
- `TBANK_API_ACCOUNT_NUMBER`, если планируется создание черновиков платежей

Проверить секреты:

```bash
./check-prod-secrets.sh
```

Ожидаемый результат: check проходит; значения секретов не печатаются.

Поднять `api` и `scheduler` с production env:

```bash
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d api scheduler
```

Синхронизировать T-Bank token из env в БД. IIKO credentials при этом остаются
env-only:

```bash
docker compose -f docker-compose.prod.yml --env-file .env.prod exec -T api \
  python -m app.scripts.sync_integration_secrets

docker compose -f docker-compose.prod.yml --env-file .env.prod exec -T api \
  python -m app.scripts.sync_integration_secrets --check
```

Ожидаемый безопасный вывод check:

```text
iiko_env=set
tbank_bearer_token=env:set db:set
```

Проверка БД без вывода токена:

```bash
docker compose -f docker-compose.prod.yml --env-file .env.prod exec -T postgres \
  sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SELECT count(*) FROM source_credential WHERE provider = '\''tbank'\'' AND credential_kind = '\''bearer_token'\'' AND is_active IS TRUE AND value_encrypted IS NOT NULL AND length(value_encrypted) > 0;"'
```

Ожидаемый результат: `1`.

## 7. Запуск prod

На сервере:

```bash
cd /opt/teplo/deploy
./check-prod-secrets.sh

docker compose -f docker-compose.prod.yml --env-file .env.prod up -d --build

docker compose -f docker-compose.prod.yml --env-file .env.prod exec -T api \
  alembic upgrade head

docker compose -f docker-compose.prod.yml --env-file .env.prod ps
```

Ожидаемый результат: `postgres`, `api`, `scheduler`, `web`, `caddy` находятся в
состоянии `running` или `healthy`.

Посмотреть хвост логов без вывода секретов:

```bash
docker compose -f docker-compose.prod.yml --env-file .env.prod logs --tail=100 api
docker compose -f docker-compose.prod.yml --env-file .env.prod logs --tail=100 scheduler
docker compose -f docker-compose.prod.yml --env-file .env.prod logs --tail=100 caddy
```

Если нужно наблюдать live:

```bash
docker compose -f docker-compose.prod.yml --env-file .env.prod logs -f api scheduler caddy
```

## 8. Smoke checks

Health/readiness:

```bash
curl -fsS "https://$PROD_DOMAIN/api/v1/health"
curl -fsS "https://$PROD_DOMAIN/api/v1/readiness"
```

Ожидаемый результат:

```text
{"status":"ok"}
{"status":"ready"}
```

В UI:

- Зайти под admin.
- Открыть staff.
- Открыть payroll.
- Открыть settings.
- Проверить, что история БД на месте: последние сотрудники, payroll runs,
  настройки и справочники соответствуют локальной истории.

Проверить IIKO без вывода секретов:

- Убедиться, что разделы/операции, использующие IIKO, открываются без ошибок
  авторизации.
- Смотреть только статусы, даты синхронизации, количество записей и ошибки без
  credentials.

Проверить T-Bank сначала read-only endpoint/операцию:

- Сначала выполнить read-only проверку, например получение списка счетов,
  выписки или движения по счету через UI/API, если эта операция доступна в
  текущем билде.
- Убедиться, что в логах нет `TBANK_API_ACCESS_TOKEN` или bearer token.
- Только после read-only проверки пробовать создание черновика платежа.
- Не создавать реальный платеж в рамках smoke; go-live smoke ограничивается
  черновиком.

Серверная проверка состояния БД:

```bash
cd /opt/teplo/deploy

docker compose -f docker-compose.prod.yml --env-file .env.prod exec -T postgres \
  sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SELECT version_num FROM alembic_version;"'

docker compose -f docker-compose.prod.yml --env-file .env.prod exec -T postgres \
  sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SELECT count(*) FROM payroll_run;"'
```

Ожидаемый результат: миграционная версия присутствует, количество payroll runs
соответствует ожидаемой истории.

## 9. Rollback

Rollback возвращает БД к pre-go-live backup из раздела 4. Код можно откатить
отдельно через git, если проблема связана не только с данными.

На сервере:

```bash
cd /opt/teplo/deploy

ROLLBACK_DUMP=/opt/teplo/deploy/backups/pre-go-live-YYYYMMDD-HHMMSS.dump
test -s "$ROLLBACK_DUMP"

docker compose -f docker-compose.prod.yml --env-file .env.prod stop api scheduler web
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d postgres

docker compose -f docker-compose.prod.yml --env-file .env.prod exec -T postgres \
  sh -c 'pg_restore --exit-on-error -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner' \
  < "$ROLLBACK_DUMP"

docker compose -f docker-compose.prod.yml --env-file .env.prod up -d api scheduler web caddy

docker compose -f docker-compose.prod.yml --env-file .env.prod ps
curl -fsS "https://$PROD_DOMAIN/api/v1/health"
curl -fsS "https://$PROD_DOMAIN/api/v1/readiness"
```

Ожидаемый результат: health/readiness снова отвечают, сервисы запущены.

Если нужен rollback к предыдущему commit:

```bash
cd /opt/teplo
git log --oneline -5
git checkout <previous-known-good-commit>
cd deploy
./check-prod-secrets.sh
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d --build
docker compose -f docker-compose.prod.yml --env-file .env.prod exec -T api \
  alembic upgrade head
```

Что делать с `.env.integrations` при rollback:

- Не заменять `.env.integrations` локальным файлом.
- Не удалять IIKO значения.
- Не удалять `TBANK_API_ACCESS_TOKEN`, если prod-интеграции должны продолжить
  работать после rollback.
- Если rollback restore вернул БД в состояние без активного T-Bank credential,
  повторить синхронизацию:

  ```bash
  docker compose -f docker-compose.prod.yml --env-file .env.prod exec -T api \
    python -m app.scripts.sync_integration_secrets

  docker compose -f docker-compose.prod.yml --env-file .env.prod exec -T api \
    python -m app.scripts.sync_integration_secrets --check
  ```

  Ожидаемый check:

  ```text
  iiko_env=set
  tbank_bearer_token=env:set db:set
  ```

## 10. После go-live

Локально удалить временный dump:

```bash
rm -i "$DUMP"
```

На сервере удалить импортный dump, если он не нужен для защищенного архива:

```bash
rm -i "$REMOTE_DUMP"
```

Pre-go-live backup оставить в защищенном backup storage минимум до конца окна
наблюдения.

Проверить права файлов на сервере:

```bash
cd /opt/teplo/deploy
stat -c "%a %n" .env.prod .env.integrations 2>/dev/null || stat -f "%Lp %N" .env.prod .env.integrations
find secrets -maxdepth 2 -type f -exec sh -c 'for f; do stat -c "%a %n" "$f" 2>/dev/null || stat -f "%Lp %N" "$f"; done' sh {} + 2>/dev/null || true
```

Ожидаемый результат: `.env.prod` и `.env.integrations` имеют права `600` или
`400`; файлы в `deploy/secrets` не world-readable.

Проверить, что `.env.integrations` не попал в git:

```bash
cd /opt/teplo
git status --short --ignored deploy/.env.prod deploy/.env.integrations deploy/secrets deploy/backups
git ls-files deploy/.env.prod deploy/.env.integrations deploy/secrets deploy/backups
```

Ожидаемый результат: `git ls-files` ничего не выводит для секретов и backup.

Зафиксировать дату, версию и commit go-live в release notes или внутреннем
журнале:

```bash
date -Is
git -C /opt/teplo rev-parse HEAD
git -C /opt/teplo status --short
docker compose -f /opt/teplo/deploy/docker-compose.prod.yml --env-file /opt/teplo/deploy/.env.prod ps
```

Не прикладывать к заметкам значения env, токены, пароли или дампы.
