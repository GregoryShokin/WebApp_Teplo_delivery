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

echo "=== 3. БАНК-ЧЕРНОВИКИ ВЫДАЧИ ДЕПОЗИТА (полный цикл) ==="
# created/updated — ждёт подписи в банке (Активные платежи «Отправлен в банк»);
# paid — оплачен, деньги резервом на Сейфе («На Сейфе»); disbursed — выдан сотруднику;
# failed/deleted/cancelled — история. Депозит-счёт списывается ТОЛЬКО при disbursed.
docker exec teplo-postgres-b psql -U teplo -d teplo -c "
select coalesce(e.full_name, 'курьер') as получатель, d.status as статус,
       d.bank_provider as банк, d.amount as сумма,
       (d.provider_ref is not null) as есть_ref,
       (d.safe_allocation_id is not null) as есть_резерв,
       (d.deposit_transaction_id is not null) as списан_депозит
from deposit_bank_draft d
  left join employee e on e.id = d.employee_id
order by d.created_at;"

echo "=== 4. ДЕПОЗИТ-РЕЗЕРВЫ НА СЕЙФЕ/В КАССЕ (обязательство перед сотрудником) ==="
# R1: employee_id ОБЯЗАН быть NULL — иначе pay_allocation срежет ЗП. Ссылка на получателя —
# в deposit_bank_draft. status reserved → ждёт «Выплатить депозит»; paid → уже выдан.
docker exec teplo-postgres-b psql -U teplo -d teplo -c "
select s.purpose as назначение, s.status as статус, s.location as где,
       s.amount as сумма, s.amount_paid as выплачено,
       (s.employee_id is null) as employee_id_null_R1
from safe_allocations s
where s.id in (select safe_allocation_id from deposit_bank_draft where safe_allocation_id is not null)
order by s.created_at;"

echo "=== 5. КЕЙСЫ НА РАЗБОР (банк не ответил при отправке черновика) ==="
docker exec teplo-postgres-b psql -U teplo -d teplo -c "
select count(*) filter (where kind='deposit_bank_draft_failed') as депозитные_кейсы,
       count(*) as всего_кейсов
from reconciliation_cases where status='pending';"
