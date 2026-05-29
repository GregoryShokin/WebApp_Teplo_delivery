# Agent B Handoff: Bank DDS Contour

Дата сборки: 2026-05-20.

Входные источники прочитаны из local docs и processed-агрегатов. Private/raw строки не выносились.

## 1. Карта банковского контура

- Sber - основной банк входящей выручки. Проверенный факт: `2026-02-01` - `2026-05-19`, 640 операций, 10.69 млн RUB поступлений, 10.79 млн RUB списаний.
- T-Bank - основной банк расходов и расчетов. Проверенный факт: `2026-02-01` - `2026-05-19`, 584 операции, 11.24 млн RUB поступлений, 11.45 млн RUB списаний.
- Денежная цепочка: `Sber -> T-Bank -> контрагенты`.
- Прямой T-Bank acquiring существует как малая доля выручки: сейчас 448 706.00 RUB / 41 операций, но owner confirmation еще нужен.
- Внутренние переводы Sber -> T-Bank зеркально сходятся по месяцам на 0.00 RUB diff.

## 2. Правила классификации операций

Текущий rule engine работает по схеме:

1. Нормализует Sber/T-Bank операции в общий формат.
2. Проверяет собственные счета/ИНН через private `own_accounts_registry`.
3. Применяет приоритетные правила: revenue acquiring, internal transfer, loan/refund/tax/fee, payroll, supplier, other.
4. Публикует агрегаты в processed; построчная классификация и full payment purpose остаются private.

Главные правила:

- Sber credit с acquiring/merchant или подтвержденным payment-contract signature -> `revenue_acquiring_sber`.
- T-Bank credit `incomePeople` от Sber и собственного счета -> `internal_transfer_sber_to_tbank`.
- Sber debit на собственный T-Bank счет -> `internal_transfer_sber_to_tbank`.
- T-Bank credit `incomePeople` с T-Bank acquiring signature -> `revenue_acquiring_tbank`.
- T-Bank debit `contragentOutcome` -> `supplier_payment`, статья через private mapping/heuristics.
- T-Bank debit `contragentPeople` -> `payroll_payment`, всегда owner review.
- `tax`/`budget`/налоговые сигнатуры -> `tax_payment`, owner review.
- `fee`/комиссии/РКО -> `bank_fee`.
- `incomeLoan`/`creditPaymentOuter`/кредит/овердрафт -> `loan_payment`.
- `refundIn`/возврат -> `refund`, затем подтвердить customer refund.

## 3. Что чем является

| Категория | Flow types | DDS/P&L handling |
| --- | --- | --- |
| Выручка | `revenue_acquiring_sber`, `revenue_acquiring_tbank`, минус подтвержденные customer `refund` | Управленческая банковская выручка = Sber acquiring + T-Bank acquiring - возвраты; внутренние переводы исключить |
| Расходы | `supplier_payment`, `payroll_payment`, `bank_fee`, часть `other_outflow` после разметки | Операционный cashflow/P&L в зависимости от статьи |
| Внутренний перевод | `internal_transfer_sber_to_tbank` | Не выручка, не расход, не operating cashflow |
| Финансирование | `loan_payment`, часть `depositFullWithdrawal`/`selfTransferOuter` после подтверждения | Отдельный financing cashflow; тело/проценты/комиссии разделить |
| Налоги | `tax_payment` | Отдельно split: налоги с ЗП, прочие налоги, штрафы/пени |
| Прочее | `other_inflow`, `other_outflow` | Не автоматизировать до owner meaning |

## 4. Сверки, которые должны быть встроены

- Sber daily statement summary vs transactions: сейчас 108/108 дней ok.
- T-Bank statement parse completeness: 0 unknown direction, 0 missing category.
- Internal transfer mirror: monthly Sber outflow = T-Bank inflow, tolerance max(1%, 10 000 RUB); сейчас все месяцы ok.
- Revenue bridge: Sber acquiring + T-Bank acquiring - customer refunds vs iiko revenue.
- Duplicate-revenue guard: T-Bank inflow from Sber never contributes to revenue.
- Owner-review coverage: count/amount of review-required groups by flow type and native category.
- Rule drift: new bank categories, new counterparties, new acquiring signatures, material amount changes.
- Privacy export control: processed exports cannot include full accounts, cards, FIO, payment purposes or tokens.

## 5. Owner questions blocking automation

1. Confirm exact T-Bank direct acquiring signature for `revenue_acquiring_tbank`.
2. Split `tax_payment`: payroll taxes vs other taxes vs penalties/fines.
3. Confirm `payroll_payment`: payroll/advances vs owner withdrawals vs other people payments.
4. Explain `depositFullWithdrawal` inflows: deposit, financing, internal movement or other.
5. Explain `selfTransferOuter` outflows and Sber other debits outside confirmed transfer chain.
6. Classify T-Bank `cardOperation` spend into DDS articles.
7. Finish supplier mapping for review groups without private override.
8. Split loan/overdraft into principal, interest and bank commissions.
9. Confirm which `refund` rows are customer refunds that reduce management revenue.

## Generated discovery files

- `research/processed/dds_discovery/bank_integration_map.md`
- `research/processed/dds_discovery/bank_flow_type_dictionary.csv`
- `research/processed/dds_discovery/bank_classification_rules_spec.md`
- `research/processed/dds_discovery/bank_reconciliation_controls.md`
- `research/processed/dds_discovery/agent_b_handoff.md`

