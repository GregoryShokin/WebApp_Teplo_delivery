# Запуск тестов

## Требования

1. Postgres 16 запущен через `docker compose`.
2. Создана БД `teplo_test`:

   ```bash
   docker exec teplo-postgres bash -c "PGPASSWORD=teplo psql -U teplo -d postgres -c 'CREATE DATABASE teplo_test OWNER teplo;'"
   ```

3. Установлена переменная окружения:

   ```bash
   export TEPLO_TEST_DATABASE_URL='postgresql+asyncpg://teplo:teplo@localhost:5432/teplo_test'
   ```

## Запуск

```bash
docker exec teplo-api bash -lc "cd /app/apps/api && pytest -q"
```

## Через bootstrap-скрипт

В окружении, где доступен `psql`, можно переопределить подключение:

```bash
PGHOST=localhost PGUSER=teplo PGPASSWORD=teplo TEPLO_TEST_DB=teplo_test apps/api/scripts/bootstrap_test_db.sh
```

## Что нельзя

- Запускать pytest на dev-БД `teplo`: `tests/conftest_guard.py` заблокирует это с ошибкой.
- Полагаться на `DATABASE_URL` для тестов: нужна именно `TEPLO_TEST_DATABASE_URL`.

Перед test session схема `public` в `teplo_test` пересоздаётся, затем Alembic накатывается до
`head`. Между тестами выполняется `TRUNCATE ... RESTART IDENTITY CASCADE` и восстанавливается
миграционный baseline seed-данных. Не запускайте один и тот же `teplo_test` параллельно из
нескольких pytest-процессов.
