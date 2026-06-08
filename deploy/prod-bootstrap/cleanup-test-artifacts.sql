-- ========================================================================
-- ПРОД-ЧИСТКА от тестовых артефактов (Способ 2: dump dev -> restore prod -> этот скрипт)
-- ========================================================================
-- ВНИМАНИЕ: запускать ТОЛЬКО на ПРОД-копии после restore дампа dev.
-- НЕ запускать на dev/локальной БД — там тестовые данные нужны.
-- Перед запуском убедись, что есть свежий дамп (откат возможен только из него).
--
-- Что делает (всё в одной транзакции):
--  1-2. Откатывает балансы депозита/фонда от тестовых (не-legacy) расчётов
--       (инверсия finalize), иначе фейковые суммы останутся на счетах.
--  3.   Удаляет тестовые явки (legacy-импорт явок не создаёт).
--  4-5. Удаляет тестовые движения и сами тестовые расчёты (каскадом строки).
--  6.   Удаляет тестовые ревизии (каскадом позиции/исключения).
--  7.   Оставляет единственного админа admin@teplo.local (admin1 переименовывается,
--       т.к. держит настройки; остальные юзеры удаляются вместе с их ролями).
--  Legacy-история (is_imported_legacy) и весь реальный конфиг СОХРАНЯЮТСЯ.
--
-- Для предпросмотра без изменений: замени COMMIT в конце на ROLLBACK.
-- ========================================================================

BEGIN;

-- 1. Откат депозитных балансов тестовых (не-legacy) расчётов.
UPDATE deposit_account da
SET balance = balance - d.delta, last_updated = now()
FROM (
  SELECT employee_id,
         SUM(CASE WHEN transaction_type = 'accrual' THEN amount ELSE -amount END) AS delta
  FROM deposit_transaction
  WHERE run_id IN (SELECT id FROM payroll_run WHERE NOT is_imported_legacy)
  GROUP BY employee_id
) d
WHERE da.employee_id = d.employee_id;

-- 2. Откат фондовых балансов тестовых расчётов.
UPDATE accumulation_fund_account fa
SET accumulated_amount = accumulated_amount - f.delta
FROM (
  SELECT account_id,
         SUM(CASE WHEN transaction_type = 'accrual' THEN amount ELSE 0 END) AS delta
  FROM accumulation_fund_transaction
  WHERE run_id IN (SELECT id FROM payroll_run WHERE NOT is_imported_legacy)
  GROUP BY account_id
) f
WHERE fa.id = f.account_id;

-- 3. Удаление тестовых явок (до удаления расчётов — берём периоды не-legacy прогонов).
DELETE FROM attendance_entry
WHERE period_id IN (SELECT period_id FROM payroll_run WHERE NOT is_imported_legacy);

-- 4. Удаление тестовых движений (до удаления run — run_id у них SET NULL).
DELETE FROM deposit_transaction
WHERE run_id IN (SELECT id FROM payroll_run WHERE NOT is_imported_legacy);
DELETE FROM accumulation_fund_transaction
WHERE run_id IN (SELECT id FROM payroll_run WHERE NOT is_imported_legacy);

-- 5. Удаление тестовых расчётов (каскадом payroll_line).
DELETE FROM payroll_run WHERE NOT is_imported_legacy;

-- 6. Удаление тестовых ревизий (каскадом позиции/исключения).
DELETE FROM inventory_audit;

-- 7. Юзеры: оставить только admin@teplo.local.
UPDATE "user" SET email = 'admin@teplo.local' WHERE email = 'admin1@teplo.local';
DELETE FROM user_role_event WHERE user_id IN (SELECT id FROM "user" WHERE email <> 'admin@teplo.local');
DELETE FROM user_role       WHERE user_id IN (SELECT id FROM "user" WHERE email <> 'admin@teplo.local');
DELETE FROM "user"          WHERE email <> 'admin@teplo.local';

-- ПРОВЕРКА (ожидаем: 0 / 0 / 0 / 1 / 1 / 103).
SELECT 'non_legacy_runs'  AS check, count(*)::text AS value FROM payroll_run WHERE NOT is_imported_legacy
UNION ALL SELECT 'attendance_entries', count(*)::text FROM attendance_entry
UNION ALL SELECT 'inventory_audits',   count(*)::text FROM inventory_audit
UNION ALL SELECT 'users_total',        count(*)::text FROM "user"
UNION ALL SELECT 'admin_email_ok',    (SELECT count(*) FROM "user" WHERE email='admin@teplo.local')::text
UNION ALL SELECT 'legacy_runs_kept',   count(*)::text FROM payroll_run WHERE is_imported_legacy;

COMMIT;
