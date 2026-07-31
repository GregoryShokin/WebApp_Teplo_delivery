# Запуск тестов

## Требования

1. Postgres 16 запущен через `docker compose`.
2. Создана СВОЯ база под слот — `teplo_test_<слот>`, а не общая `teplo_test`:

   ```bash
   docker exec teplo-postgres bash -c "PGPASSWORD=teplo psql -U teplo -d postgres -c 'CREATE DATABASE teplo_test_os OWNER teplo;'"
   ```

   Имя базы разводится по слоту так же, как порты. В compose-файлах агентов так и сделано
   (`teplo_test_b`, `teplo_test_c`, `teplo_test_partial`), и `WORK-IN-PROGRESS.md` это фиксирует
   — но только для запуска ВНУТРИ контейнера. Дыра была в запуске с хоста: там
   `TEPLO_TEST_DATABASE_URL` набирают руками, а этот файл до 31.07.2026 предлагал набрать
   `teplo_test` — то есть общую.

   Почему это ломается. У каждого агента свой worktree и свой контейнер Postgres, но наружу
   порт обычно опубликован лишь у одного, а имя `teplo_test` у всех одинаковое — и все
   pytest-процессы сходятся в одну базу внутри чужого контейнера. Схема там пересоздаётся в
   начале каждой сессии, поэтому второй прогон сносит таблицы первому: тот получает сотни
   ошибок в коде, к которому не притрагивался, и уходит искать несуществующий баг. Ровно это
   случилось 31.07.2026 — 1594 «упавших» теста оказались чужим прогоном на той же базе.

3. Установлена переменная окружения — с именем СВОЕЙ базы и портом своего слота:

   ```bash
   export TEPLO_TEST_DATABASE_URL='postgresql+asyncpg://teplo:teplo@localhost:5432/teplo_test_os'
   ```

## Запуск

```bash
docker exec teplo-api bash -lc "cd /app/apps/api && pytest -q"
```

## Через bootstrap-скрипт

В окружении, где доступен `psql`, можно переопределить подключение:

```bash
PGHOST=localhost PGUSER=teplo PGPASSWORD=teplo TEPLO_TEST_DB=teplo_test_os apps/api/scripts/bootstrap_test_db.sh
```

## Что нельзя

- Запускать pytest на dev-БД `teplo`: `tests/conftest_guard.py` заблокирует это с ошибкой.
- Полагаться на `DATABASE_URL` для тестов: нужна именно `TEPLO_TEST_DATABASE_URL`.
- Делить одну тестовую базу с другим агентом — см. пункт 2 требований.
- **Ронять схему вслепую.** `DROP SCHEMA public CASCADE` — обычный приём, когда база встала в
  кривое состояние, и ровно им 31.07.2026 был убит идущий прогон соседнего агента: схему из-под
  живой сессии не вернуть, ему пришлось начинать заново. Перед любым разрушительным действием
  спроси, кто в базе:

  ```bash
  docker exec teplo-postgres psql -U teplo -d postgres -c \
    "select pid, state, query_start from pg_stat_activity where datname='teplo_test_os';"
  ```

  Чужие коннекты означают, что база не ваша, — заведите свою и уходите на неё.

Перед test session схема `public` пересоздаётся, затем Alembic накатывается до `head`. Между
тестами выполняется `TRUNCATE ... RESTART IDENTITY CASCADE` и восстанавливается миграционный
baseline seed-данных.

## Массовые ошибки на входе — сначала проверь соседа, потом код

Прогон, упавший целиком или почти целиком (сотни ошибок в setup, `type ... does not exist`,
`duplicate key ... alembic_version`), почти никогда не означает, что сломан код: свои изменения
столько не ломают. Это подпись параллельного прогона на той же базе. Порядок разбора:

```bash
pgrep -fa pytest                       # чей ещё процесс жив и из какой папки
docker exec teplo-postgres psql -U teplo -d postgres -c \
  "select datname, count(*) from pg_stat_activity where datname like 'teplo_test%' group by 1;"
```

Нашёлся чужой процесс — не трогай базу вообще: заведи свою и перезапусти прогон на ней. Чужой
прогон при этом доживёт до конца.
