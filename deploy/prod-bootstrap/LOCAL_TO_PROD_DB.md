# Перенос локальной истории БД в prod без секретов

Этот runbook переносит локальную историю Postgres в production, но не переносит
данные таблицы `source_credential`. Сейчас `source_credential.value_encrypted`
не является настоящим envelope encryption, поэтому любой полный дамп БД считается
чувствительным артефактом.

## Safety

- Не передавать `.env`, `.env.prod`, `.env.integrations` и файлы из
  `deploy/secrets`.
- Не передавать реальные токены через чат, Drive, git, issue tracker или
  аргументы команд.
- Не включать данные `source_credential` в локальный дамп. Для этого в `pg_dump`
  используются оба паттерна:
  `--exclude-table-data=source_credential` и
  `--exclude-table-data='*.source_credential'`.
- Дамп хранить как sensitive artifact: права `600`, каталог `700`, доступ только
  у оператора деплоя.
- После go-live удалить локальные временные дампы и импортный дамп на сервере
  или перенести их в защищённое хранилище. На SSD secure delete не гарантируется,
  поэтому лучше полагаться на encrypted volume / protected backup storage.

## Переменные

В примерах замени только плейсхолдеры:

```bash
PROD_HOST=<ssh-host>
PROD_USER=<ssh-user>
```

Production-команды выполняются на сервере из `/opt/teplo/deploy`. Пароль
Postgres не вводится в командную строку: `pg_dump`, `pg_restore` и `psql`
запускаются внутри контейнера `postgres` и читают `POSTGRES_USER`,
`POSTGRES_DB`, `POSTGRES_PASSWORD` из окружения контейнера.

## 1. Pre-go-live backup prod-БД

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

Сохрани путь `PRE_GO_LIVE_BACKUP`: он нужен для rollback.

## 2. Локальный дамп

Дамп создаётся в custom format (`pg_dump -Fc`) и исключает только данные
`source_credential`. Схема таблицы остаётся в дампе, чтобы после restore
`python -m app.scripts.sync_integration_secrets` мог заново записать prod-токен
из `.env.integrations` на сервере.

### Вариант A: локальная БД в Docker Compose

Из корня репозитория:

```bash
DUMP="${TMPDIR:-/tmp}/teplo-local-history-$(date +%Y%m%d-%H%M%S).dump"

deploy/prod-bootstrap/create-local-db-dump.sh \
  --docker-compose \
  --output "$DUMP"
```

Ручной эквивалент без helper-скрипта:

```bash
DUMP="${TMPDIR:-/tmp}/teplo-local-history-$(date +%Y%m%d-%H%M%S).dump"

docker compose -f apps/docker-compose.yml exec -T postgres \
  pg_dump -U teplo -d teplo -Fc \
    --exclude-table-data=source_credential \
    --exclude-table-data='*.source_credential' \
  > "$DUMP"

chmod 600 "$DUMP"
```

### Вариант B: локальная БД через `LOCAL_DATABASE_URL`

Предпочтительно дать helper-скрипту запросить URL скрытым prompt-ом. Так строка
подключения с паролем не попадает в shell history и не передаётся в `pg_dump`
аргументом процесса:

```bash
DUMP="${TMPDIR:-/tmp}/teplo-local-history-$(date +%Y%m%d-%H%M%S).dump"

deploy/prod-bootstrap/create-local-db-dump.sh \
  --database-url \
  --output "$DUMP"
```

Если `LOCAL_DATABASE_URL` уже задан безопасным способом в окружении текущей
shell-сессии, команда выше использует его. Не набирай URL с паролем прямо после
команды.

### Проверка локального дампа

Проверка должна ничего не найти:

```bash
if pg_restore -l "$DUMP" | grep -E 'TABLE DATA .* source_credential( |$)'; then
  echo "ERROR: dump contains source_credential table data"
  exit 1
fi

ls -lh "$DUMP"
```

Если `pg_restore` на локальной машине недоступен, для Docker Compose-варианта
можно проверить через контейнер:

```bash
if docker compose -f apps/docker-compose.yml exec -T postgres pg_restore -l < "$DUMP" \
  | grep -E 'TABLE DATA .* source_credential( |$)'; then
  echo "ERROR: dump contains source_credential table data"
  exit 1
fi
```

## 3. Передача дампа на сервер

На локальной машине:

```bash
DUMP_BASENAME="$(basename "$DUMP")"

ssh "$PROD_USER@$PROD_HOST" \
  'mkdir -p /opt/teplo/deploy/imports && chmod 700 /opt/teplo/deploy/imports'

scp "$DUMP" "$PROD_USER@$PROD_HOST:/opt/teplo/deploy/imports/$DUMP_BASENAME"
```

Альтернатива через `rsync`:

```bash
DUMP_BASENAME="$(basename "$DUMP")"

rsync -av --chmod=F600 "$DUMP" \
  "$PROD_USER@$PROD_HOST:/opt/teplo/deploy/imports/$DUMP_BASENAME"
```

На сервере зафиксируй путь к импортному дампу. Подставь basename файла, который
получился в `DUMP_BASENAME`:

```bash
REMOTE_DUMP=/opt/teplo/deploy/imports/teplo-local-history-YYYYMMDD-HHMMSS.dump
test -s "$REMOTE_DUMP"
chmod 600 "$REMOTE_DUMP"
```

## 4. Restore на сервере

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

Сразу после restore, до синхронизации prod-секретов, в `source_credential` не
должно быть локальных строк:

```bash
docker compose -f docker-compose.prod.yml --env-file .env.prod exec -T postgres \
  sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SELECT count(*) FROM source_credential;"'
```

Ожидаемое значение: `0`.

Запусти `api`, примени миграции и подними рабочие сервисы:

```bash
docker compose -f docker-compose.prod.yml --env-file .env.prod run --rm api \
  alembic upgrade head
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d api scheduler web
```

Миграция идёт временным контейнером (`run --rm`), а рабочие сервисы поднимаются
уже на готовой схеме — тот же порядок, что и при обновлении боевого стенда
(`deploy/README.md`, «Updating after a git pull»).

## 5. Синхронизация prod integration secrets

На сервере:

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

Проверка, что активный T-Bank token есть в БД, но значение не выводится:

```bash
docker compose -f docker-compose.prod.yml --env-file .env.prod exec -T postgres \
  sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SELECT count(*) FROM source_credential WHERE provider = '\''tbank'\'' AND credential_kind = '\''bearer_token'\'' AND is_active IS TRUE AND value_encrypted IS NOT NULL AND length(value_encrypted) > 0;"'
```

Ожидаемое значение: `1`. Эта команда печатает только количество строк, не
`value_encrypted`.

## 6. Smoke-check после восстановления

На сервере:

```bash
docker compose -f docker-compose.prod.yml --env-file .env.prod ps

docker compose -f docker-compose.prod.yml --env-file .env.prod exec -T postgres \
  sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SELECT version_num FROM alembic_version;"'

docker compose -f docker-compose.prod.yml --env-file .env.prod exec -T postgres \
  sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SELECT count(*) FROM payroll_run;"'

curl -fsS https://<prod-domain>/api/v1/readiness
```

Затем открыть приложение и проверить ключевые пользовательские сценарии:
логин, список сотрудников, `/payroll`, последние расчёты/история, DDS-раздел без
вывода секретных значений.

## Rollback

Rollback возвращает БД к pre-go-live backup, созданному в шаге 1. На сервере:

```bash
cd /opt/teplo/deploy

ROLLBACK_DUMP=/opt/teplo/deploy/backups/pre-go-live-YYYYMMDD-HHMMSS.dump
test -s "$ROLLBACK_DUMP"

docker compose -f docker-compose.prod.yml --env-file .env.prod stop api scheduler web
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d postgres

docker compose -f docker-compose.prod.yml --env-file .env.prod exec -T postgres \
  sh -c 'pg_restore --exit-on-error -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner' \
  < "$ROLLBACK_DUMP"

docker compose -f docker-compose.prod.yml --env-file .env.prod up -d api scheduler web
```

Проверки после rollback:

```bash
docker compose -f docker-compose.prod.yml --env-file .env.prod ps

docker compose -f docker-compose.prod.yml --env-file .env.prod exec -T postgres \
  sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SELECT version_num FROM alembic_version;"'

curl -fsS https://<prod-domain>/api/v1/readiness
```

Если после rollback нужно оставить prod-интеграции включёнными, повтори
синхронизацию:

```bash
docker compose -f docker-compose.prod.yml --env-file .env.prod exec -T api \
  python -m app.scripts.sync_integration_secrets

docker compose -f docker-compose.prod.yml --env-file .env.prod exec -T api \
  python -m app.scripts.sync_integration_secrets --check
```

## Cleanup артефактов

После успешного go-live:

```bash
# локально
rm -i "$DUMP"

# на сервере удалить импортный дамп, если он не нужен для защищённого архива
rm -i "$REMOTE_DUMP"
```

Pre-go-live backup лучше оставить в защищённом backup storage минимум до конца
окна наблюдения после запуска.
