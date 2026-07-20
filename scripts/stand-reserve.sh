#!/usr/bin/env bash
# Стенд «Активные платежи»: сцена для проверки СУММ при частичной выплате.
#
# Резерв заводится тем же путём, каким платежи рождаются у тебя на проде:
# окно «Новый платёж» → статья расхода → наличными с Сейфа. Почты здесь нет вовсе,
# поэтому двойной счёт из пункта 1 сцену не засоряет — в окне ровно одна строка.
#
# Ничего в БД руками не правим: только штатные эндпоинты, те же, что дёргает UI.
#
#   ./scripts/stand-reserve.sh [сумма]      (по умолчанию 44503)
set -euo pipefail

cd "$(dirname "$0")/.."
AMOUNT="${1:-44503}"
API="http://localhost:8010/api/v1"

echo "→ чищу платёжные данные стенда…"
docker exec teplo-postgres-b psql -U teplo -d teplo -q -c "
TRUNCATE safe_allocations, email_invoice_intake, supplier_invoice,
         counterparty_payment_draft, cashflow_transactions,
         invoice_payment_allocation, counterparty_payable_profile, counterparty CASCADE;" >/dev/null 2>&1

TOKEN=$(curl -s -X POST "$API/auth/login" -H "Content-Type: application/json" \
  -d '{"email":"admin1@teplo.local","password":"admin-password-for-smoke"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

SAFE=$(docker exec teplo-postgres-b psql -U teplo -d teplo -t -A -c \
  "select id from wallet where code='cash_safe';")
KASSA=$(docker exec teplo-postgres-b psql -U teplo -d teplo -t -A -c \
  "select id from wallet where code='tk_chernikova';")
ART=$(docker exec teplo-postgres-b psql -U teplo -d teplo -t -A -c \
  "select id from dds_articles where code='arenda_torgovyh_tochek';")

TOPUP=$(python3 -c "print(int(float('$AMOUNT')) * 2)")
echo "→ пополняю Сейф на $TOPUP ₽ (внутренний перевод Касса→Сейф, как в «Новом платеже»)…"
curl -s -X POST "$API/dds/internal-transfer" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"source_wallet_id\":\"$KASSA\",\"dest_wallet_id\":\"$SAFE\",\"mode\":\"plain\",\"amount\":$TOPUP,\"purpose\":\"Пополнение Сейфа (стенд)\"}" \
  -o /dev/null -w "   перевод: HTTP %{http_code}\n"

echo "→ создаю платёж наличными с Сейфа на $AMOUNT ₽ (окно «Новый платёж» → «Создать»)…"
curl -s -X POST "$API/dds/new-payment/expense-cash" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"wallet_id\":\"$SAFE\",\"lines\":[{\"article_id\":\"$ART\",\"amount\":$AMOUNT,\"purpose\":\"Аренда точки на Ленина\"}]}" \
  -o /dev/null -w "   резерв: HTTP %{http_code}\n"

echo
docker exec teplo-postgres-b psql -U teplo -d teplo -c "
select purpose as резерв, amount as сумма, amount_paid as выплачено,
       (amount - amount_paid) as осталось_доплатить, status as статус
  from safe_allocations;"

cat <<TXT
✓ Сцена готова: на Сейфе один резерв «Аренда точки на Ленина» на $AMOUNT ₽. Больше в окне ничего нет.

  Дальше — руками в UI (http://localhost:5183):

   1. Открой кошелёк справа внизу.
      Заголовок: «1 шт · $AMOUNT ₽». Пока честно.
   2. У строки «Аренда торговых точек» нажми «Выплатить».
      Впиши ЧАСТЬ суммы — например 20000 — и подтверди.
   3. Снова открой кошелёк и смотри внимательно:
        • мелким шрифтом строка честно скажет «выплачено 20 000 ₽»
        • но крупная цифра справа  — всё ещё $AMOUNT ₽
        • и заголовок окна        — всё ещё $AMOUNT ₽
        • хотя доплатить осталось  — 24 503 ₽

      Окно просит денег больше, чем нужно: выплаченную часть оно не вычитает.
TXT
