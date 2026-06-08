# Bootstrap боевой БД (go-live)

Подход: **скопировать dev → восстановить на проде → удалить тестовые артефакты**
(Способ 2). Сохраняет всю реальную историю (103 недели ЗП), весь ручной конфиг
(ставки, ФОТ%, надбавки, настройки) и реальных сотрудников. Удаляется только
тестовый слой.

## Что считается тестовым артефактом (удаляется)
- Тестовые расчёты ЗП (`payroll_run` с `is_imported_legacy = false`) и их строки.
- Балансовые движения этих расчётов (откатываются с депозит/фонд счетов).
- Тестовые явки (`attendance_entry`) — legacy-импорт явок не создаёт.
- Тестовые ревизии (`inventory_audit`).
- Все пользователи, кроме `admin@teplo.local` (smoke-админ `admin1` переименовывается).

## Что сохраняется
- Legacy-история ЗП (`is_imported_legacy = true`) — 103 недели.
- Весь app-конфиг: ставки, `target_payroll_revenue_ratio` (ФОТ%), `weekday_premium`,
  category_rules, revenue tiers, deposit-настройки и т.д.
- Реальные сотрудники, депозит/фонд балансы (после отката тестовых сумм).

## Процедура

0. **Подготовить prod secrets**:
   ```bash
   cd /opt/teplo/deploy
   TEPLO_DOMAIN=<боевой-домен> TEPLO_ADMIN_EMAIL=<боевой-email> ./init-prod-env.sh
   nano .env.integrations
   ./check-prod-secrets.sh
   ```
   Детали по `.env.prod`, `.env.integrations`, Sber/T-Bank credentials и mTLS файлам — в
   `deploy/SECRETS.md`.

1. **Свежий дамп dev** (источник правды):
   ```bash
   docker exec teplo-postgres pg_dump -U teplo -d teplo -Fc > teplo_golive.dump
   ```

2. **На проде — восстановить дамп** в чистую БД `teplo`
   (останови api/scheduler/web на время restore):
   ```bash
   # при необходимости пересоздать БД:
   #   docker exec teplo-postgres psql -U teplo -d postgres -c "DROP DATABASE IF EXISTS teplo;"
   #   docker exec teplo-postgres psql -U teplo -d postgres -c "CREATE DATABASE teplo OWNER teplo;"
   docker exec -i teplo-postgres pg_restore -U teplo -d teplo --clean --if-exists < teplo_golive.dump
   ```

3. **Чистка тестовых артефактов** (на проде):
   ```bash
   docker exec -i teplo-postgres psql -U teplo -d teplo -v ON_ERROR_STOP=1 \
     < deploy/prod-bootstrap/cleanup-test-artifacts.sql
   ```
   Ожидаемая сводка в конце: `non_legacy_runs=0, attendance=0, inventory_audits=0,
   users_total=1, admin_email_ok=1, legacy_runs_kept=103`.
   Для предпросмотра без изменений — заменить `COMMIT` на `ROLLBACK` в скрипте.

4. **Сменить пароль админа** (КРИТИЧНО — не уходить в прод со smoke-паролем):
   ```bash
   docker exec teplo-api python -c "from app.core.security import hash_password; print(hash_password('<НОВЫЙ_ПАРОЛЬ>'))"
   docker exec teplo-postgres psql -U teplo -d teplo -c \
     "UPDATE \"user\" SET hashed_password='<хеш>' WHERE email='admin@teplo.local';"
   ```

5. **Проверка** перед открытием доступа:
   - `SELECT version_num FROM alembic_version;` — совпадает с dev (`0076...` или новее).
   - `SELECT email FROM "user";` — только `admin@teplo.local`.
   - Зайти в приложение, открыть `/payroll` — 103 недели истории на месте, ставки/настройки корректны.

6. **Включить бэкапы** на проде (см. `deploy/backup/README.md`) — таймер pg_dump.

## Заметки
- `payroll.mock_daily_revenue` — мок-настройка для тестов. На проде выручка берётся из
  iiko; если режим боевой, мок игнорируется. При желании очистить значение вручную.
- Env `TEPLO_BANK_CLIENT_MODE` должен быть `live` на проде (для боевого банка) —
  это переменная окружения, не БД.
- Скрипт идемпотентен по смыслу: повторный запуск на уже чистой БД ничего не сломает
  (не-legacy прогонов нет, лишних юзеров нет).
