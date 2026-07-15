#!/usr/bin/env bash
# Стенд «Активные платежи»: подготовить сцену для проверки двойного счёта.
#
# Чистит платёжные данные и заводит ОДИН счёт от неофициального поставщика —
# ровно так, как он попадает на прод: письмо → process_attachment → распознавание.
# Счёт остаётся НЕотправленным: «Отправить в банк» нажимаешь ты, руками, в UI.
#
#   ./scripts/stand-reset.sh [сумма]     (по умолчанию 44503.00)
set -euo pipefail

AMOUNT="${1:-44503.00}"
NUM="С-$(date +%H%M%S)"

echo "→ чищу платёжные данные стенда…"
docker exec teplo-postgres-b psql -U teplo -d teplo -q -c "
TRUNCATE safe_allocations, email_invoice_intake, supplier_invoice,
         counterparty_payment_draft, cashflow_transactions,
         invoice_payment_allocation, counterparty_payable_profile, counterparty CASCADE;" >/dev/null

echo "→ имитирую письмо со счётом (на проде это делает почтовый робот)…"
docker exec -w /app/apps/api teplo-api-b python /app/apps/api/scripts/stand-mail.py "$NUM" "$AMOUNT"

echo "→ помечаю поставщика неофициальным (то же, что выбрать «Неофициальный» в карточке)…"
docker exec teplo-postgres-b psql -U teplo -d teplo -q -c "
update counterparty_payable_profile set relationship='informal', relationship_manual=true;" >/dev/null

echo
echo "✓ Сцена готова: счёт $NUM на $AMOUNT ₽ от неофициального поставщика «ООО ОвощБаза»."
echo
echo "  Дальше — руками в UI (http://localhost:5183):"
echo "   1. «Финансы → Страница на оплату» → у счёта нажми «Отправить в банк»"
echo "   2. Открой кошелёк справа внизу → в окне ОДНА строка на $AMOUNT ₽ — всё верно"
echo "   3. Запусти ./scripts/stand-bank-paid.sh   (это имитация: на проде так звонит T-Bank)"
echo "   4. Обнови окно кошелька → строк ДВЕ, сумма удвоилась"
