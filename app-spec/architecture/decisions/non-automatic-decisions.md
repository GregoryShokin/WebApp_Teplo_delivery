# Решения, которые нельзя принимать автоматически

Дата фиксации: 2026-05-29.
Источник: бывший §13 гибридного database architecture документа.

1. ✅ Вариант 11.1/A: `counterparty` - единый master с ролями; `supplier_counterparty` - профильное view/extension для роли `supplier`.
2. ✅ Вариант 11.2/A: первичный cash fact оплат поставщикам хранится в DDS; УДКЗ хранит только `supplier_payment_match`.
3. ✅ Вариант 11.3/A: `balance_source_reference` не создаётся; audit идёт через общий `source_reference`.
4. ✅ Вариант 11.4/A: `wallet` - бизнес-сущность, `data_source` - способ доставки, связь N:M через `wallet_data_source`.
5. ✅ Вариант 11.5/B + уточнение 2026-05-25: УДКЗ владеет supplier-based авансами; остальные регулярные префиксы живут в своих источниках и агрегируются в Balance.
6. ✅ Вариант 11.6/A: три справочника статей (`dds_article`, `pnl_line`, `balance_line`) + mapping tables.
7. ✅ Вариант 11.7/A: DDS видит payroll cash-fact через 1:1 matching или `payroll_payment_batch`; индивидуальные суммы доступны только payroll-роли.
8. ✅ Вариант 11.8/A: `parsed_document` - technical extract, `source_document` - подтверждённый бизнес-документ.
9. ✅ Решения 12a/12b: дата X = 2026-02-01; глубина миграции = C гибрид.
10. ✅ Вариант 11.9/C: ФД и налоги идут в асимметричный MVP; полноценные спеки модулей отложены.
