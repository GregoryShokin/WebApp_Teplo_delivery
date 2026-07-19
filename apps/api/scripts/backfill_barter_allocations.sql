-- Перенос УЖЕ ПРОВЕДЁННЫХ бартерных взаимозачётов в аллокации (source_kind='barter').
--
-- Запускать ОДИН РАЗ после `alembic upgrade head` на каждой базе, где есть зачёты,
-- сделанные до миграции 0200. Идемпотентен: накладные, у которых аллокация зачёта уже
-- есть, пропускаются.
--
-- Почему не в миграции: PostgreSQL запрещает использовать значение enum, добавленное в той
-- же транзакции, а alembic здесь гоняет всю цепочку одной транзакцией.
--
-- Проверка ДО и ПОСЛЕ (числа не должны измениться):
--   SELECT count(*) FROM supplier_invoice WHERE barter_settlement_id IS NOT NULL;

INSERT INTO invoice_payment_allocation
    (id, invoice_id, source_kind, amount, barter_settlement_id, created_at)
SELECT gen_random_uuid(),
       i.id,
       'barter',
       GREATEST(i.amount - COALESCE(paid.total, 0), 0),
       i.barter_settlement_id,
       now()
FROM supplier_invoice i
LEFT JOIN (
    SELECT invoice_id, SUM(amount) AS total
    FROM invoice_payment_allocation
    GROUP BY invoice_id
) paid ON paid.invoice_id = i.id
WHERE i.barter_settlement_id IS NOT NULL
  AND GREATEST(i.amount - COALESCE(paid.total, 0), 0) > 0
  AND NOT EXISTS (
        SELECT 1 FROM invoice_payment_allocation a
        WHERE a.invoice_id = i.id AND a.barter_settlement_id IS NOT NULL
  );
