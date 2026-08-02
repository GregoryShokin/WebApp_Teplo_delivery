# T-Bank Payment Order CLI

> **`payment_order_bot.py` выведен из работы 02.08.2026 (решение владельца).** Приёмка
> документов из Телеграма переехала в приложение: `apps/api/app/services/telegram_intake.py`,
> джоба `poll_telegram_intake`. Контур подготовки платёжных поручений T-Bank владельцу не нужен —
> платежи уходят из очереди оплат приложения.
>
> **Не запускайте старого бота с тем же токеном, что настроен в приложении.** У Телеграма
> `getUpdates` может читать только ОДИН потребитель: второй начнёт перехватывать сообщения, и
> документы будут пропадать через раз — молча, без единой ошибки. Если старый контур зачем-то
> понадобится, заведите ему отдельного бота.
>
> Остальные скрипты (`payment_order.py`, `payment_parsers.py`, выгрузка выписки) не тронуты:
> `payment_parsers.py` — предок парсеров `apps/api/app/services/utility_recognition.py`, и по
> нему сверяют правила разбора.

Private payment-order intake lives under `research/private/tbank/payment_orders/`.

Common commands:

```bash
python3 integrations/tbank/scripts/payment_order.py check-env
python3 integrations/tbank/scripts/payment_order.py parse --file invoice.txt
python3 integrations/tbank/scripts/payment_order.py upload --file invoice.pdf --source-channel telegram --sender "@user"
python3 integrations/tbank/scripts/payment_order.py prepare --parsed-id parsed_xxx
python3 integrations/tbank/scripts/payment_order.py list-candidates --status owner_review
python3 integrations/tbank/scripts/payment_order.py list-candidates --status ready
python3 integrations/tbank/scripts/payment_order.py export-candidate --id cand_xxx
python3 integrations/tbank/scripts/payment_order.py submit --input-json research/private/tbank/payment_orders/requests/payment_order_request_*.json
```

Telegram intake bot:

```bash
python3 integrations/tbank/scripts/payment_order_bot.py --check
python3 integrations/tbank/scripts/payment_order_bot.py
python3 integrations/tbank/scripts/payment_order_bot.py --once
```

Required local `.env` keys:

```text
TELEGRAM_BOT_TOKEN
TELEGRAM_BOT_USERNAME
TELEGRAM_PAYMENT_BOT_ALLOWED_CHAT_IDS
TELEGRAM_PAYMENT_BOT_ACCOUNT_NUMBER
```

`upload` saves the original file, upload metadata, parser output in
`payment_intake.sqlite3`, and a prepared request JSON. Runtime parsing is
deterministic: `payment_parsers.py` uses source-specific regex/template parsers
for invoices, UPD, waybills, receipts, payment orders, known counterparties, and
utility acts, plus a generic requisites fallback.

Some intake candidates can be built from linked source documents. Electricity
acts are deterministic-paired when one act is the factual electricity/losses
period and another act with the same supplier/date is marked as an advance for
the following period. The bank payment amount is `fact amount due + advance`,
while purpose/period come from the factual act. Missing requisites or an
unpaired side still block submit-ready payload creation.

For electricity acts the intake DB also creates a separate
`expense_accrual_candidates` row from the factual act. That row carries the
period expense for P&L/ОПиУ (`electricity + losses`) and is intentionally
separate from the cash payment candidate used for DDS/bank submission.

`payment_order_bot.py` receives Telegram documents, photos, or text messages,
saves them through the same private upload flow, and replies with `upload_id`,
`parsed_id`, `candidate_id`, parser, confidence, and status. It does not send
payments to T-Bank.

Only `submit` calls the H2H endpoint
`/api/v1/payments-orders/payments-by-requisites/submit`. Low-confidence,
duplicate, or incomplete candidates are marked `requires_owner_review=true` and
do not get a submit-ready `work_queue_payload`.
