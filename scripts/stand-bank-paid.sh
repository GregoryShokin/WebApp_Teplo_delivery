#!/usr/bin/env bash
# Стенд: «банк оплатил черновик».
#
# Шлёт настоящий вебхук T-Bank (POST /webhooks/tbank/payment-status) — тот же самый,
# которым банк на проде сообщает об оплате. Ничего в базе руками не правим: дальше
# всё делает apply_payment_status — транзит р/с→Сейф и резерв на Сейфе.
set -euo pipefail

REF=$(docker exec teplo-postgres-b psql -U teplo -d teplo -t -A -c \
  "select provider_ref from counterparty_payment_draft where status in ('created','updated') order by created_at desc limit 1;")

if [ -z "$REF" ]; then
  echo "✗ Нет черновика, ожидающего оплаты."
  echo "  Сначала нажми «Отправить в банк» на «Странице на оплату»."
  exit 1
fi

echo "→ банк сообщает об оплате черновика $REF …"
curl -sS -X POST "http://localhost:8010/api/v1/webhooks/tbank/payment-status" \
  -H "Content-Type: application/json" \
  -d "{\"paymentId\":\"$REF\",\"status\":\"executed\"}"
echo
echo
echo "✓ Оплата проведена. Открой кошелёк «Активные платежи» и сравни с тем, что было."
echo
docker exec teplo-postgres-b psql -U teplo -d teplo -c "
select 'черновик' as что, status::text as состояние, 'ушёл из активных' as примечание
  from counterparty_payment_draft
union all
select 'накладная', payment_status::text,
       case when draft_id is null then 'draft_id снят'
            else 'draft_id ОСТАЛСЯ → счёт всё ещё числится «в банке»' end
  from supplier_invoice
union all
select 'резерв на Сейфе', status::text, amount::text || ' руб' from safe_allocations;"
