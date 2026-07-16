#!/usr/bin/env bash
# Стенд: сцена «выдача депозита банк-черновиком».
#
# Готовит сотрудника с депозитом, чтобы можно было руками пройти путь
# «Депозиты → Операция → Выдать депозит → Выдать сразу → Банк-черновик (через Сейф)»
# и посмотреть, что система делает и что показывает.
#
# Стенд в mock-режиме (TEPLO_BANK_CLIENT_MODE=mock в docker-compose.agent-b.yml):
# в настоящий Т-Банк ничего не уходит, но код проходит ровно тот же путь, что на проде.
#
#   ./scripts/stand-deposit.sh [баланс]      (по умолчанию 9000)
set -euo pipefail

cd "$(dirname "$0")/.."
BALANCE="${1:-9000}"

echo "→ чищу депозитные данные стенда…"
docker exec teplo-postgres-b psql -U teplo -d teplo -q -c "
TRUNCATE deposit_transaction, deposit_account, deposit_payout_schedule,
         employee_position_assignment, cashflow_transactions CASCADE;
DELETE FROM employee WHERE iiko_id = 'stand-iiko-001';" >/dev/null 2>&1

echo "→ создаю кассира с депозитом $BALANCE ₽…"
docker exec teplo-postgres-b psql -U teplo -d teplo -q -c "
DO \$\$
DECLARE emp uuid := gen_random_uuid();
BEGIN
  INSERT INTO employee(id, full_name, iiko_id, category, status, hire_date, created_at, updated_at,
                       is_senior, is_deputy_senior, pin_assumed_from_iiko)
    VALUES (emp, 'София Колесникова (стенд)', 'stand-iiko-001', 'category_2', 'active',
            current_date - 200, now(), now(), false, false, false);
  INSERT INTO employee_position_assignment(id, employee_id, position, effective_from, created_at, updated_at)
    VALUES (gen_random_uuid(), emp, 'Кассир', current_date - 200, now(), now());
  INSERT INTO deposit_account(id, employee_id, balance, last_updated, initial_balance)
    VALUES (gen_random_uuid(), emp, $BALANCE, now(), 0);
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
select e.full_name as сотрудник, a.position as должность, d.balance as депозит
from employee e
  join employee_position_assignment a on a.employee_id = e.id
  join deposit_account d on d.employee_id = e.id;"

cat <<'TXT'
✓ Сцена готова. Дальше — руками в UI (http://localhost:5183):

   1. «Зарплата → Депозиты» → у Софии кнопка «Операция» → «Выдать депозит»
   2. «Способ выдачи» → «Выдать сразу»
   3. «Счёт выдачи» → «Банк-черновик (через Сейф)»
   4. «Выдать депозит» → «Подтвердить»

   Потом смотри, что система сделала и что показала:
   • баланс депозита обнулился — сотрудник считается рассчитанным
   • ДДС: перевод р/с → Сейф и расход «Выдача депозита» с Сейфа
   • кошелёк «Активные платежи» — пусто: черновика, который ждёт
     твоей подписи в банке, там нет ни в активных, ни в истории

   Проверить состояние: ./scripts/stand-deposit-state.sh
   Начать заново:       ./scripts/stand-deposit.sh
TXT
