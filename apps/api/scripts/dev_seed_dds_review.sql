-- DEV-СЕВ: банковские операции журнала ДДС, требующие разбора (needs_review).
-- Только для изолированного dev-стека (teplo-dds). НЕ запускать на проде.
-- Счёт списания резолвится по коду кошелька tbank_main (без хардкода UUID).
-- Идемпотентно: ON CONFLICT (provider, provider_operation_id) DO NOTHING.
--
-- Применение:
--   docker exec -i teplo-postgres-dds psql -U teplo -d teplo < apps/api/scripts/dev_seed_dds_review.sql

WITH acc AS (
  SELECT account_id AS id FROM wallet WHERE code = 'tbank_main'
)
INSERT INTO bank_operations (
  id, provider, provider_operation_id, account_id, operation_date, posted_at,
  direction, amount, currency, counterparty_name_raw, counterparty_inn_raw,
  counterparty_account_raw, payment_purpose, document_number, raw_payload,
  classification_status, imported_at
)
SELECT gen_random_uuid(), v.provider, v.op_id, acc.id, v.op_date, v.op_date + time '10:30',
       v.direction, v.amount, 'RUB', v.cp_name, v.cp_inn,
       v.cp_acc, v.purpose, v.doc_no, '{"seed": true, "source": "dev_seed_dds_review"}'::jsonb,
       'needs_review', now()
FROM acc, (VALUES
  -- КЛЮЧЕВОЙ ПРИМЕР: 100 000 ₽, 03.07.2026, выплата административному персоналу
  ('tbank', 'seed-review-001', DATE '2026-07-03', 'out', 100000.00,
   'Менеджер Виктория Сергеевна', '616712345678', '40817810400001112223',
   'Заработная плата за июнь 2026 г. по реестру. НДС не облагается.', 'ПП-4471'),

  -- Вторая зарплатная выплата (другой админ) — 04.07.2026, 60 000 ₽
  ('tbank', 'seed-review-002', DATE '2026-07-04', 'out', 60000.00,
   'Супрун Вероника Андреевна', '616798765432', '40817810400004445556',
   'Выплата заработной платы за июнь 2026 г. НДС не облагается.', 'ПП-4472'),

  -- Нераспознанный расход поставщику (нужен разбор статьи + контрагент)
  ('tbank', 'seed-review-003', DATE '2026-07-02', 'out', 45500.00,
   'ООО "ФУДЛАЙН"', '6165098765', '40702810100000012345',
   'Оплата по счёту № 1204 от 30.06.2026 за продукты питания. В т.ч. НДС 20% 7583.33', 'ПП-4468'),

  -- Входящее поступление (возврат/возмещение) — нужен разбор статьи
  ('tbank', 'seed-review-004', DATE '2026-07-03', 'in', 12840.50,
   'ООО "ОФД-ПЛАТФОРМА"', '7710000001', '40702810900000098765',
   'Возврат излишне уплаченных средств по договору № ОФД-22', 'ВЗ-0091'),

  -- Перевод на другой наш счёт (кандидат во внутренний перевод)
  ('tbank', 'seed-review-005', DATE '2026-07-01', 'out', 200000.00,
   'ИП Шокина Наталья Владимировна', '890307589201', '40802810252090056194',
   'Перевод собственных средств между счетами', 'ПП-4460')
) AS v(provider, op_id, op_date, direction, amount, cp_name, cp_inn, cp_acc, purpose, doc_no)
ON CONFLICT (provider, provider_operation_id) DO NOTHING;
