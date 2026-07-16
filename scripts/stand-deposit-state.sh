#!/usr/bin/env bash
# Стенд: что система сделала после выдачи депозита — все следы в одном месте.
set -euo pipefail

cd "$(dirname "$0")/.."

echo "=== 1. ДЕПОЗИТ СОТРУДНИКА ==="
docker exec teplo-postgres-b psql -U teplo -d teplo -c "
select e.full_name as сотрудник, d.balance as остаток_депозита,
       coalesce((select string_agg(t.transaction_type||' '||t.amount, ', ')
                 from deposit_transaction t where t.employee_id = e.id), 'операций нет') as операции
from employee e join deposit_account d on d.employee_id = e.id;"

echo "=== 2. ДВИЖЕНИЕ ДЕНЕГ (ДДС) ==="
docker exec teplo-postgres-b psql -U teplo -d teplo -c "
select coalesce(w.code,'—') as кошелёк, c.direction as направление, c.amount as сумма,
       coalesce(a.name,'—') as статья, c.source_kind as тип
from cashflow_transactions c
  left join wallet w on w.id = c.wallet_id
  left join dds_articles a on a.id = c.article_id
order by c.created_at;"

echo "=== 3. СЛЕДЫ БАНК-ЧЕРНОВИКА (который ждёт подписи в банке) ==="
docker exec teplo-postgres-b psql -U teplo -d teplo -c "
select 'запись о черновике депозита' as где_искали,
       (select count(*) from counterparty_payment_draft) as черновики_контрагентов,
       (select count(*) from payroll_bank_draft) as черновики_зп,
       (select count(*) from reconciliation_cases) as кейсы_на_разбор;"

echo "=== 4. ЛОГ (по коду — единственный след успешной отправки) ==="
FOUND=$(docker logs teplo-api-b 2>&1 | grep -c "Банк-черновик выдачи депозита" || true)
if [ "$FOUND" = "0" ]; then
  echo "  Записей нет. Успех логируется через logger.info, а уровень логов — WARNING,"
  echo "  поэтому info не пишется. При УСПЕХЕ следа не остаётся вообще нигде."
else
  docker logs teplo-api-b 2>&1 | grep "Банк-черновик выдачи депозита" | tail -3
fi
