# Pre-go-live checklist

Ручной командник для финального go-live. Не вставляй реальные секреты в команды,
чат, git, issue tracker или screenshots. T-Bank token называется
`TBANK_API_ACCESS_TOKEN`; не переименовывать. Sber не является обязательным
блокером go-live.

В примерах production-команды выполняются на сервере из
`/opt/teplo/deploy`. Локальные команды выполняются из корня репозитория, если не
указано иначе.

## 0. Переменные и журнал

- [ ] Зафиксировать значения без секретов.

  ```bash
  PROD_SSH=teplo-prod
  PROD_DOMAIN=<prod-domain>
  GO_LIVE_NOTES="${TMPDIR:-/tmp}/teplo-go-live-$(date +%Y%m%d-%H%M%S).log"
  ```

  Ожидаемый результат: `PROD_SSH` указывает на SSH alias сервера,
  `PROD_DOMAIN` содержит домен без протокола, `GO_LIVE_NOTES` не лежит в git.

  Если ошибка: не продолжать с placeholder-доменом. Исправить переменные в
  текущей shell-сессии, не записывать секреты в заметки.

## 1. Local

- [ ] Проверить локальное дерево git.

  ```bash
  git status --short
  ```

  Ожидаемый результат: нет неожиданных файлов. Допустимы только осознанные
  изменения кода/документации для релиза.

  Если ошибка: убрать секреты, дампы, build artifacts и cache-директории из
  индекса/рабочего дерева. Не коммитить `.env`, `.env.*`, дампы БД,
  `node_modules`, `.venv`, `dist`, `.pytest_cache`, `.ruff_cache`.

- [ ] Backend lint.

  ```bash
  cd apps
  make api-lint
  ```

  Ожидаемый результат: `ruff check` завершается без ошибок.

  Если ошибка: исправить lint-ошибки и повторить. Если Makefile не находит
  backend Python, запустить с явным интерпретатором:

  ```bash
  make API_PYTHON=/absolute/path/to/python api-lint
  ```

- [ ] Backend format check.

  ```bash
  cd apps/api
  python -m ruff format --check .
  ```

  Ожидаемый результат: форматирование уже соответствует `ruff format`.

  Если ошибка: выполнить форматирование, пересмотреть diff и повторить проверку.

  ```bash
  python -m ruff format .
  git diff --check
  ```

- [ ] Backend tests.

  ```bash
  cd apps
  make api-test
  ```

  Ожидаемый результат: все pytest-тесты проходят.

  Если ошибка: исправить причину падения. Если проблема только в пути к Python,
  запустить с явным интерпретатором:

  ```bash
  make API_PYTHON=/absolute/path/to/python api-test
  ```

- [ ] Frontend lint.

  ```bash
  cd apps
  npm --workspace web run lint
  ```

  Ожидаемый результат: eslint завершается без ошибок.

  Если ошибка: исправить lint-ошибки и повторить.

- [ ] Frontend build.

  ```bash
  cd apps
  npm --workspace web run build
  ```

  Ожидаемый результат: `tsc -b` и `vite build` завершаются без ошибок.

  Если ошибка: исправить TypeScript/build-ошибки и повторить.

- [ ] Frontend tests/smoke.

  ```bash
  cd apps
  npm --workspace web run test:e2e
  ```

  Ожидаемый результат: Playwright-тесты проходят.

  Если ошибка: исправить регрессию. Если e2e недоступны из-за локальной
  инфраструктуры, зафиксировать причину в go-live заметках и выполнить ручной
  smoke после deploy.

- [ ] Проверить, что локальные `.env` не будут отправлены.

  ```bash
  git status --short --ignored | grep -E '(^|/)\.env(\.|$)|deploy/\.env\.prod|deploy/\.env\.integrations' || true
  git ls-files | grep -E '(^|/)\.env(\.|$)|deploy/\.env\.prod|deploy/\.env\.integrations' || true
  deploy/sync-code-to-server.sh "$PROD_SSH" /opt/teplo | grep -E '(^|/)\.env(\.|$)|deploy/\.env\.prod|deploy/\.env\.integrations' || true
  ```

  Ожидаемый результат: `git ls-files` не показывает env-файлы с секретами.
  Dry-run sync не показывает отправку `.env`, `.env.*`, `deploy/.env.prod` или
  `deploy/.env.integrations`.

  Если ошибка: удалить env-файлы из git index, проверить `.gitignore` и
  `deploy/sync-code-to-server.sh`. Не запускать `--apply`, пока dry-run не чистый.

- [ ] Создать локальный dump без данных `source_credential`.

  ```bash
  DUMP="${TMPDIR:-/tmp}/teplo-local-history-$(date +%Y%m%d-%H%M%S).dump"

  deploy/prod-bootstrap/create-local-db-dump.sh \
    --docker-compose \
    --output "$DUMP"

  chmod 600 "$DUMP"
  ```

  Ожидаемый результат: скрипт печатает путь к sensitive dump и сам проверяет,
  что `source_credential` table data отсутствует.

  Если ошибка: не переносить dump. Исправить доступ к локальной БД или убрать
  `source_credential` table data из dump-команды.

- [ ] Повторно проверить dump вручную.

  ```bash
  if pg_restore -l "$DUMP" | grep -E 'TABLE DATA .* source_credential( |$)'; then
    echo "ERROR: dump contains source_credential table data"
    exit 1
  fi

  ls -lh "$DUMP"
  ```

  Ожидаемый результат: grep ничего не находит, dump существует и имеет права
  `600`.

  Если ошибка: удалить этот dump или перенести в защищённый карантин, создать
  новый dump через `deploy/prod-bootstrap/create-local-db-dump.sh`.

## 2. Server before deploy

- [ ] Подключиться к production-серверу.

  ```bash
  ssh teplo-prod
  ```

  Ожидаемый результат: открыт shell на production-сервере.

  Если ошибка: проверить SSH alias, доступ, VPN/firewall. Не использовать
  обходные копии секретов через чат или аргументы команд.

- [ ] Перейти в каталог deploy.

  ```bash
  cd /opt/teplo/deploy
  pwd
  ```

  Ожидаемый результат: `pwd` печатает `/opt/teplo/deploy`.

  Если ошибка: проверить путь установки `/opt/teplo` и права пользователя.

- [ ] Проверить наличие prod env-файлов.

  ```bash
  ls -la .env.prod .env.integrations
  ```

  Ожидаемый результат: оба файла существуют. Значения секретов не выводятся.

  Если ошибка: создать файлы из approved procedure, заполнить секреты вручную на
  сервере, не коммитить и не копировать локальные `.env`.

- [ ] Проверить права prod env-файлов.

  ```bash
  stat -c '%a %n' .env.prod .env.integrations
  ```

  Ожидаемый результат: `600 .env.prod` и `600 .env.integrations` или строже
  `400`.

  Если ошибка: исправить права.

  ```bash
  chmod 600 .env.prod .env.integrations
  ```

- [ ] Проверить production secrets.

  ```bash
  ./check-prod-secrets.sh
  ```

  Ожидаемый результат: `Production secret check passed for ...`.
  В выводе интеграций должен быть `TBANK_API_ACCESS_TOKEN=set`. Sber отсутствие
  не блокирует go-live.

  Если ошибка: исправить только серверные `.env.prod` или `.env.integrations`.
  Не печатать значения секретов в терминал.

- [ ] Проверить состояние prod compose.

  ```bash
  docker compose -f docker-compose.prod.yml --env-file .env.prod ps
  ```

  Ожидаемый результат: видны сервисы `postgres`, `api`, `scheduler`, `web`,
  `caddy`; текущее состояние понятно перед deploy.

  Если ошибка: проверить Docker daemon, compose file, `.env.prod` и свободное
  место на сервере.

- [ ] Создать backup текущей prod DB.

  ```bash
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
  printf '%s\n' "$PRE_GO_LIVE_BACKUP"
  ```

  Ожидаемый результат: создан readable custom-format dump, путь сохранён для
  rollback.

  Если ошибка: не продолжать go-live. Проверить место на диске, права каталога,
  состояние `postgres` и валидность `.env.prod`.

## 3. Code deploy

- [ ] Выбрать способ доставки кода.

  Git pull на сервере:

  ```bash
  cd /opt/teplo
  git status --short
  git pull --ff-only
  git rev-parse --short HEAD
  cd /opt/teplo/deploy
  ```

  Rsync с локальной машины из корня репозитория:

  ```bash
  deploy/sync-code-to-server.sh teplo-prod /opt/teplo
  deploy/sync-code-to-server.sh --apply teplo-prod /opt/teplo
  ```

  Ожидаемый результат: код обновлён; серверные `deploy/.env.prod`,
  `deploy/.env.integrations`, `deploy/secrets`, `deploy/backups` не изменены и
  не удалены.

  Если ошибка: для git исправить конфликт/локальные изменения на сервере без
  удаления секретов. Для rsync остановиться на dry-run, пока список изменений не
  проверен.

- [ ] Повторить проверку production secrets после доставки кода.

  ```bash
  cd /opt/teplo/deploy
  ./check-prod-secrets.sh
  ```

  Ожидаемый результат: проверка проходит, `TBANK_API_ACCESS_TOKEN=set`.

  Если ошибка: исправить server env-файлы или изменения в скрипте проверки до
  build/restore.

- [ ] Собрать production images.

  ```bash
  docker compose -f docker-compose.prod.yml --env-file .env.prod build
  ```

  Ожидаемый результат: images для `api`, `scheduler`, `web` собраны без ошибок.

  Если ошибка: проверить Dockerfile, зависимости, доступное место и логи build.
  Не начинать restore БД до успешной сборки.

## 4. DB restore

- [ ] Передать локальный dump на сервер.

  Локально:

  ```bash
  DUMP_BASENAME="$(basename "$DUMP")"

  ssh teplo-prod 'mkdir -p /opt/teplo/deploy/imports && chmod 700 /opt/teplo/deploy/imports'
  scp "$DUMP" "teplo-prod:/opt/teplo/deploy/imports/$DUMP_BASENAME"
  ```

  На сервере:

  ```bash
  REMOTE_DUMP="/opt/teplo/deploy/imports/$DUMP_BASENAME"
  test -s "$REMOTE_DUMP"
  chmod 600 "$REMOTE_DUMP"
  ```

  Ожидаемый результат: import dump существует на сервере, не пустой, права
  `600`.

  Если ошибка: повторить передачу через `scp` или `rsync --chmod=F600`. Не
  использовать публичные каналы для dump.

- [ ] Остановить `api`, `scheduler`, `web`.

  ```bash
  cd /opt/teplo/deploy
  docker compose -f docker-compose.prod.yml --env-file .env.prod stop api scheduler web
  docker compose -f docker-compose.prod.yml --env-file .env.prod up -d postgres
  ```

  Ожидаемый результат: `postgres` работает, пользовательские сервисы остановлены
  на время restore.

  Если ошибка: проверить `docker compose ps` и логи. Не запускать restore, пока
  состояние сервисов не понятно.

- [ ] Restore dump.

  ```bash
  docker compose -f docker-compose.prod.yml --env-file .env.prod exec -T postgres \
    sh -c 'pg_restore --exit-on-error -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner' \
    < "$REMOTE_DUMP"
  ```

  Ожидаемый результат: `pg_restore` завершается с exit code `0`.

  Если ошибка: не продолжать миграции. Сохранить текст ошибки, проверить
  совместимость dump/schema и при необходимости выполнить rollback из
  `PRE_GO_LIVE_BACKUP`.

- [ ] Проверить, что локальные credentials не попали в prod DB.

  ```bash
  docker compose -f docker-compose.prod.yml --env-file .env.prod exec -T postgres \
    sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SELECT count(*) FROM source_credential;"'
  ```

  Ожидаемый результат: `0` до sync prod integration secrets.

  Если ошибка: restore содержит credentials. Не запускать приложение наружу.
  Создать корректный dump без `source_credential` table data и повторить restore
  либо откатиться.

- [ ] Выполнить миграции (временным контейнером, до подъёма сервисов).

  ```bash
  docker compose -f docker-compose.prod.yml --env-file .env.prod run --rm api \
    alembic upgrade head
  ```

  Ожидаемый результат: migrations применены до `head`.

  Если ошибка: проверить логи `api`, состояние БД и revision chain. При
  нерешаемой ошибке выполнить rollback.

- [ ] Запустить `api` и `scheduler`.

  ```bash
  docker compose -f docker-compose.prod.yml --env-file .env.prod up -d api scheduler
  ```

  Ожидаемый результат: `api` и `scheduler` запущены. `web` можно поднять после
  sync secrets или вместе со smoke, если нужен UI.

  Если ошибка: проверить `docker compose logs api scheduler`.

## 5. Secrets sync

- [ ] Синхронизировать prod integration secrets из `.env.integrations`.

  ```bash
  docker compose -f docker-compose.prod.yml --env-file .env.prod exec -T api \
    python -m app.scripts.sync_integration_secrets
  ```

  Ожидаемый результат: команда завершается без вывода значений секретов. IIKO
  остаётся env-only; T-Bank bearer token записывается в БД из
  `TBANK_API_ACCESS_TOKEN`.

  Если ошибка: проверить наличие `TBANK_API_ACCESS_TOKEN` и IIKO-переменных в
  `.env.integrations` через `./check-prod-secrets.sh`, не печатая значения.

- [ ] Проверить sync secrets в безопасном режиме.

  ```bash
  docker compose -f docker-compose.prod.yml --env-file .env.prod exec -T api \
    python -m app.scripts.sync_integration_secrets --check
  ```

  Ожидаемый результат:

  ```text
  iiko_env=set
  tbank_bearer_token=env:set db:set
  ```

  Если ошибка: не переходить к банковским smoke. Повторить sync после
  исправления `.env.integrations`.

- [ ] Проверить наличие активного T-Bank token в БД без вывода значения.

  ```bash
  docker compose -f docker-compose.prod.yml --env-file .env.prod exec -T postgres \
    sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SELECT count(*) FROM source_credential WHERE provider = '\''tbank'\'' AND credential_kind = '\''bearer_token'\'' AND is_active IS TRUE AND value_encrypted IS NOT NULL AND length(value_encrypted) > 0;"'
  ```

  Ожидаемый результат: `1`.

  Если ошибка: повторить sync secrets, проверить `TBANK_API_ACCESS_TOKEN` в
  `.env.integrations`, не выводить token.

## 6. Smoke

- [ ] Поднять web и проверить compose status.

  ```bash
  cd /opt/teplo/deploy
  docker compose -f docker-compose.prod.yml --env-file .env.prod up -d api scheduler web caddy
  docker compose -f docker-compose.prod.yml --env-file .env.prod ps
  ```

  Ожидаемый результат: `api`, `scheduler`, `web`, `caddy`, `postgres` работают.

  Если ошибка: смотреть точечные логи сервиса:

  ```bash
  docker compose -f docker-compose.prod.yml --env-file .env.prod logs --tail=200 api
  docker compose -f docker-compose.prod.yml --env-file .env.prod logs --tail=200 scheduler
  docker compose -f docker-compose.prod.yml --env-file .env.prod logs --tail=200 web
  docker compose -f docker-compose.prod.yml --env-file .env.prod logs --tail=200 caddy
  ```

- [ ] Health endpoint.

  ```bash
  curl -fsS "https://${PROD_DOMAIN}/api/v1/readiness"
  ```

  Ожидаемый результат: HTTP `2xx` и readiness без ошибки.

  Если ошибка: проверить DNS/TLS/Caddy, `api` logs и DB connectivity.

- [ ] Login.

  Локально, в браузере оператора:

  ```bash
  printf 'https://%s/\n' "$PROD_DOMAIN"
  ```

  Ожидаемый результат: URL открыт в браузере, login проходит production
  admin/user credentials, session cookie secure, пользователь попадает в
  приложение.

  Если ошибка: проверить `TEPLO_ADMIN_EMAIL`, auth cookies, Caddy HTTPS и логи
  `api`. Не выводить пароль в терминал.

- [ ] Staff / payroll / settings.

  ```text
  UI: открыть staff, payroll, settings.
  ```

  Ожидаемый результат: страницы открываются без 5xx, основные таблицы
  заполнены, payroll settings доступны.

  Если ошибка: проверить browser network, `api` logs и permissions/RBAC.

- [ ] DB history present.

  ```bash
  docker compose -f docker-compose.prod.yml --env-file .env.prod exec -T postgres \
    sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SELECT count(*) FROM payroll_run;"'

  docker compose -f docker-compose.prod.yml --env-file .env.prod exec -T postgres \
    sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SELECT count(*) FROM employee;"'
  ```

  Ожидаемый результат: counts соответствуют ожидаемой импортированной истории.

  Если ошибка: проверить, что был восстановлен правильный `REMOTE_DUMP`; при
  неправильной БД выполнить rollback или повторить restore с корректным dump.

- [ ] IIKO connectivity.

  ```text
  UI/API: запустить read-only IIKO проверку или безопасную синхронизацию,
  принятую для go-live.
  ```

  Ожидаемый результат: IIKO отвечает успешно, credentials берутся из
  `.env.integrations`, значения секретов нигде не печатаются.

  Если ошибка: проверить `./check-prod-secrets.sh`, сеть сервера до IIKO и логи
  `api`/`scheduler`. Ошибка IIKO блокирует только IIKO-зависимые сценарии; решение
  о go-live фиксирует владелец.

- [ ] T-Bank read-only check.

  ```text
  UI/API: выполнить только read-only T-Bank проверку, например чтение счетов,
  баланса или справочника, без создания платежей.
  ```

  Ожидаемый результат: T-Bank live API отвечает успешно; token берётся из
  `TBANK_API_ACCESS_TOKEN` через synced DB credential; платежные draft-операции
  ещё не выполнялись.

  Если ошибка: не создавать draft. Проверить `tbank_bearer_token=env:set db:set`,
  `TBANK_API_ACCOUNT_NUMBER`, network и T-Bank API base URL.

- [ ] T-Bank draft creation только после успешного read-only check.

  ```text
  UI/API: создать минимальный согласованный draft только после подтверждения
  read-only проверки владельцем процесса.
  ```

  Ожидаемый результат: draft создан в ожидаемом статусе, без фактической отправки
  денег, если workflow это разделяет.

  Если ошибка: остановить банковские действия, сохранить request id/status без
  token, проверить логи и T-Bank кабинет. Не повторять создание draft вслепую.

## 7. Rollback

- [ ] Остановить пользовательские сервисы.

  ```bash
  cd /opt/teplo/deploy
  docker compose -f docker-compose.prod.yml --env-file .env.prod stop api scheduler web
  docker compose -f docker-compose.prod.yml --env-file .env.prod up -d postgres
  ```

  Ожидаемый результат: `postgres` работает, `api/scheduler/web` остановлены.

  Если ошибка: проверить `docker compose ps` и логи Docker.

- [ ] Восстановить pre-go-live dump.

  ```bash
  ROLLBACK_DUMP="$PRE_GO_LIVE_BACKUP"
  test -s "$ROLLBACK_DUMP"

  docker compose -f docker-compose.prod.yml --env-file .env.prod exec -T postgres \
    sh -c 'pg_restore --exit-on-error -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner' \
    < "$ROLLBACK_DUMP"
  ```

  Ожидаемый результат: restore завершается с exit code `0`.

  Если ошибка: не запускать сервисы наружу. Проверить путь к backup, читаемость
  dump через `pg_restore -l`, свободное место и состояние Postgres.

- [ ] Перезапустить сервисы.

  ```bash
  docker compose -f docker-compose.prod.yml --env-file .env.prod up -d api scheduler web caddy
  docker compose -f docker-compose.prod.yml --env-file .env.prod ps
  ```

  Ожидаемый результат: все prod-сервисы снова работают.

  Если ошибка: смотреть логи конкретного сервиса и не считать rollback
  завершённым.

- [ ] Проверить health после rollback.

  ```bash
  curl -fsS "https://${PROD_DOMAIN}/api/v1/readiness"
  ```

  Ожидаемый результат: HTTP `2xx`.

  Если ошибка: проверить Caddy/API/Postgres logs и состояние миграций. Go-live
  остаётся отменённым до зелёного health.

- [ ] Зафиксировать итог.

  ```text
  Go-live result: success | rolled back | paused
  Code revision:
  PRE_GO_LIVE_BACKUP:
  REMOTE_DUMP:
  Smoke notes:
  ```

  Ожидаемый результат: у команды есть понятная запись, что было сделано и какой
  backup использовать дальше.

  Если ошибка: записать минимум вручную в защищённые операционные заметки, без
  секретов и без dump contents.
