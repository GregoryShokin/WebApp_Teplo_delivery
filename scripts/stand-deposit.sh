#!/usr/bin/env bash
# Стенд: депозиты сотрудников — сцена для проверки выдач.
#
# Заводит 14 поваров/кассиров с разными накоплениями (как на проде), чтобы можно было
# руками пройти любой путь выдачи:
#   «Зарплата → Депозиты → Операция → Выдать депозит»
#     • «В ближайшей ведомости» — выдача уедет в столбец ведомости
#     • «Выдать сразу» → «Торговая касса Черникова» / «Сейф» / «Банк-черновик (через
#       Сейф)» / «Сбербанк → Сейф (черновик)»
#
# Банк имитируется (TEPLO_BANK_CLIENT_MODE=mock в docker-compose.agent-b.yml) —
# в настоящий Т-Банк ничего не уходит, но код проходит ровно тот же путь, что на проде.
#
# Цели накопления берутся из payroll.category_rules по категории сотрудника:
#   1-я → 20 000 ₽, 2-я → 15 000 ₽, 3-я → 10 000 ₽, 4-я и стажёр → 7 000 ₽.
#
#   ./scripts/stand-deposit.sh
set -euo pipefail

cd "$(dirname "$0")/.."

echo "→ чищу депозитные данные стенда…"
docker exec teplo-postgres-b psql -U teplo -d teplo -q -c "
TRUNCATE deposit_transaction, deposit_account, deposit_payout_schedule,
         employee_position_assignment, cashflow_transactions, reconciliation_cases CASCADE;
DELETE FROM employee WHERE iiko_id LIKE 'stand-%';" >/dev/null 2>&1

echo "→ завожу 14 сотрудников с депозитами…"
docker exec teplo-postgres-b psql -U teplo -d teplo -q -c "
DO \$\$
DECLARE
  emp uuid;
  r record;
BEGIN
  FOR r IN
    SELECT * FROM (VALUES
      -- ФИО, должность, категория, накоплено (цель определится категорией)
      ('София Колесникова',   'Кассир', 'category_2',  9000),
      ('Валерий Кудря',       'Повар',  'category_2', 15000),  -- цель достигнута
      ('Георгий Фёдоров',     'Повар',  'category_1', 20000),  -- цель достигнута
      ('Артём Остапенко',     'Повар',  'category_3',  4000),
      ('Светлана Молоканова', 'Повар',  'category_3',   720),  -- почти пусто
      ('Александр Чмыхов',    'Повар',  'category_1', 18000),
      ('Ирина Лаптева',       'Кассир', 'category_2', 11500),
      ('Дмитрий Сомов',       'Повар',  'category_3',  9800),
      ('Наталья Крылова',     'Кассир', 'category_4',  7000),  -- цель достигнута
      ('Павел Гущин',         'Повар',  'category_4',  3500),
      ('Олеся Тимофеева',     'Кассир', 'category_2',  6200),
      ('Роман Бабичев',       'Повар',  'category_1',  2500),
      ('Юлия Нестерова',      'Кассир', 'category_3', 10000),  -- цель достигнута
      ('Егор Панкратов',      'Повар',  'intern',      1500)   -- стажёр: правило «4», цель 7 000
    ) AS t(full_name, position, category, balance)
  LOOP
    emp := gen_random_uuid();
    INSERT INTO employee(id, full_name, iiko_id, category, status, hire_date, created_at,
                         updated_at, is_senior, is_deputy_senior, pin_assumed_from_iiko)
      VALUES (emp, r.full_name, 'stand-' || replace(lower(r.full_name), ' ', '-'), r.category,
              'active', current_date - 200, now(), now(), false, false, false);
    INSERT INTO employee_position_assignment(id, employee_id, position, effective_from,
                                             created_at, updated_at)
      VALUES (gen_random_uuid(), emp, r.position, current_date - 200, now(), now());
    INSERT INTO deposit_account(id, employee_id, balance, last_updated, initial_balance)
      VALUES (gen_random_uuid(), emp, r.balance, now(), 0);
  END LOOP;
END \$\$;" >/dev/null

# Прод-паритет: без этих строк стенд соврёт (не будет переключателя способа и статьи расхода).
docker exec teplo-postgres-b psql -U teplo -d teplo -q -c "
INSERT INTO app_setting(id, key, value, value_type, category, display_name, widget_type, updated_at)
VALUES (gen_random_uuid(), 'payroll.deposit_scheduled_payout_enabled', 'true', 'bool', 'payroll',
        'Отложенная выдача депозита', 'switch', now())
ON CONFLICT (key) DO UPDATE SET value='true';
INSERT INTO dds_articles(id, code, name, movement_type, activity_type, is_active, kassa_enabled, created_at, updated_at)
VALUES (gen_random_uuid(),'vydacha_depozita_sotrudniku','Выдача депозита','outflow','operating',true,false,now(),now())
ON CONFLICT (code) DO NOTHING;" >/dev/null

echo
docker exec teplo-postgres-b psql -U teplo -d teplo -c "
select count(*) as сотрудников, sum(d.balance) as суммарный_баланс
from deposit_account d;"

cat <<'TXT'
✓ Сцена готова: 14 сотрудников с депозитами.

  UI: http://localhost:5183 → «Зарплата → Депозиты»
      (admin1@teplo.local / admin-password-for-smoke)

  Что можно прощупать:
   • «Операция → Выдать депозит» → «Выдать сразу» → любой из 4 каналов
   • «В ближайшей ведомости» — отложенная выдача через ЗП
   • Кошелёк «Активные платежи» — банк-черновика депозита там нет by design
   • «ДДС → Требуют разбора» — сюда падает кейс, если банк не ответил

  Состояние после выдачи: ./scripts/stand-deposit-state.sh
  Начать заново:          ./scripts/stand-deposit.sh
TXT
