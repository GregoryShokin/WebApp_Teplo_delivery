# Bank Classification Rules Spec

Дата сборки: 2026-05-20.

Эта спецификация описывает текущую post-line классификацию банковских операций по безопасным источникам. Полные назначения платежей и приватные counterparty patterns остаются только в `research/private`.

## 1. Rule engine contract

Классификация должна быть воспроизводимой и двухслойной:

1. Нормализовать банковскую операцию в общий формат: банк, дата, направление, сумма, native category/code, payer/receiver block, counterparty hash, masked-INN, account alias/hash, acquiring-сигналы, private reference на raw/назначение.
2. Определить, является ли контрагент/счет собственным через `own_accounts_registry`.
3. Применить правила в порядке приоритета: выручка и внутренние переводы до generic inflow/outflow; специальные банковские категории до supplier fallback.
4. Записать `rule_id_matched`, `flow_type`, `dds_article_candidate`, `pnl_line_candidate`, `confidence`, `requires_owner_review`.
5. Для строк `requires_owner_review=yes` публиковать наружу только агрегированную группу: bank, direction, native category, flow_type, amount/count, date range, `description_signature`, suggested question.

## 2. Нормализованные поля для match

| Поле | Sber mapping | T-Bank mapping |
| --- | --- | --- |
| `bank` | constant `sber` | constant `tbank` |
| `operation_date` | `operationDate` / `documentDate` | `operationDate` / `docDate` / `chargeDate` |
| `direction` | `direction`: `CREDIT` -> `credit`, `DEBIT` -> `debit` | `typeOfOperation`: `Credit` -> `credit`, otherwise `debit` |
| `amount_abs` | `amountRub` / `amount` | `rubleAmount` / `accountAmount` / `operationAmount` |
| `counterparty_name` | payer for credit, payee for debit | payer for credit, receiver for debit |
| `counterparty_inn` | payer/payee INN, cleaned digits | payer/receiver `inn`, cleaned digits |
| `counterparty_account` | payer/payee account or corresponding account | payer/receiver `acct` |
| `counterparty_bank_name` | payer/payee bank name | payer/receiver `bankName` |
| `counterparty_bic` | payer/payee bank BIC | payer/receiver `bicRu` or operation `bic` |
| `description` | `paymentPurpose` private-only raw text | `payPurpose` / `description` private-only raw text |
| `category_native_bank` | `operationCode` | `category` |

## 3. Current rule priority

| Priority | Rule family | Match | Output | Quality |
| ---: | --- | --- | --- | --- |
| 1 | Sber own-account reverse inflow review | `bank=sber`, `credit`, own counterparty and T-Bank bank signature | `other_inflow` | low, owner review |
| 10 | Sber acquiring | `bank=sber`, `credit`, text/acquiring signature such as acquiring/merchant | `revenue_acquiring_sber` | high |
| 11 | Sber payment-acceptance contract | `bank=sber`, `credit`, owner-confirmed payment-acceptance contract signature | `revenue_acquiring_sber` | high |
| 1-2 | Sber -> T-Bank internal transfer | Sber debit to own T-Bank account, or T-Bank credit from own Sber account | `internal_transfer_sber_to_tbank` | high |
| 30 | Loan/overdraft | category or text indicates loan/overdraft | `loan_payment` | medium, owner review |
| 35 | Refund | `refundIn` or refund-like text | `refund` | medium, owner review |
| 40 | Tax/budget | `tax`, `budget`, or tax/contribution signature | `tax_payment` | medium, owner review |
| 50 | Bank fee | `fee`, Sber operation code `02`/`17`, or commission/RKO signature | `bank_fee` | high for fee detection |
| 55 | T-Bank people payments | T-Bank debit `contragentPeople` | `payroll_payment` | medium, owner review |
| 59-60 | T-Bank supplier | T-Bank debit `contragentOutcome`; private counterparty override first, safe text heuristic second | `supplier_payment` | high with override, otherwise medium/low |
| 65 | Sber debit review | Sber debit outside confirmed own-transfer/fee/loan patterns | `other_outflow` | low, owner review |
| fallback | T-Bank card/self-transfer/other | `cardOperation`, `selfTransferOuter`, or unmatched debit/credit | `other_outflow` / `other_inflow` / `unclassified` | low, owner review |

## 4. Bank-specific rules

### Sber

- `credit` with acquiring/merchant signature -> `revenue_acquiring_sber`, DDS `Выручка эквайринг Sber`, P&L `Выручка с учетом скидок Черникова`.
- `credit` with owner-confirmed payment-acceptance contract signature -> `revenue_acquiring_sber`, DDS `Выручка интернет-эквайринг Sber / StarterApp`.
- `credit` from own/T-Bank-like counterparty is not part of the confirmed `Sber -> T-Bank` chain; it is routed to `other_inflow` with owner review.
- `debit` to own T-Bank account -> `internal_transfer_sber_to_tbank`, not supplier expense.
- `debit` with loan/overdraft signature -> `loan_payment`.
- `debit` with fee code/category/signature -> `bank_fee`; acquiring fee goes to DDS article `Эквайринг`, otherwise `Прочие банковские комиссии`.
- Other Sber debit -> `other_outflow`, owner review.

### T-Bank

- `credit`, category `incomePeople`, payer bank is Sber, payer account/INN is own -> `internal_transfer_sber_to_tbank`.
- `credit`, category `incomePeople`, payer bank/acquirer signature is T-Bank and acquiring text/signals are present -> `revenue_acquiring_tbank`.
- `credit`, category `incomeLoan` or loan/overdraft signature -> `loan_payment`.
- `credit`, category `refundIn` or refund signature -> `refund`.
- Other `credit` -> `other_inflow`, owner review. This includes current `depositFullWithdrawal` groups until the owner gives economic meaning.
- `debit`, category `fee` -> `bank_fee`.
- `debit`, category `creditPaymentOuter` or loan/overdraft signature -> `loan_payment`.
- `debit`, category `tax`/`budget` or tax/contribution signature -> `tax_payment`.
- `debit`, category `contragentPeople` -> `payroll_payment`.
- `debit`, category `contragentOutcome` -> `supplier_payment`, then article by private counterparty override or safe keyword heuristic.
- `debit`, category `cardOperation` -> `other_outflow`, DDS `Бизнес-карта / требуется разметка`.
- `debit`, category `selfTransferOuter` -> `other_outflow`, owner review.

## 5. Supplier article subrules

Supplier classification is intentionally conservative:

- Private counterparty overrides may assign exact DDS/P&L articles and turn `requires_owner_review` to `no`.
- Without override, safe keyword heuristics can suggest article groups: transport/logistics, staff meals, automation systems, marketing/context/SEO, leaflets/printing, hiring/training, telecom/hosting, rent, fines/penalties.
- If neither override nor safe heuristic matches, article stays `Поставщики / требуется разметка` and owner review is required.
- Full legal names and payment-purpose examples for overrides must remain private.

## 6. Owner review queue from current aggregates

| Review area | Current signal | Amount / operations | Blocking question |
| --- | --- | ---: | --- |
| T-Bank direct acquiring | `revenue_acquiring_tbank`, category `incomePeople` | 448 706.00 / 41 | Confirm this is real T-Bank acquiring and not internal transfer |
| Taxes | `tax_payment`, categories `tax`, `budget`, some `contragentOutcome` | 738 728.79 / 12 | Split payroll taxes, other taxes, penalties/fines |
| Payroll | `payroll_payment`, category `contragentPeople` | 3 363 645.00 / 21 | Confirm payroll vs owner withdrawals/other people payments |
| `depositFullWithdrawal` | `other_inflow` | 1 327 801.83 / 5 | Define economic meaning: financing, internal transfer, deposit movement or other inflow |
| Supplier unmapped | `supplier_payment` with review groups | 2 627 159.36 / 100 in review groups | Assign DDS/P&L articles and working counterparty aliases |
| Business card | `other_outflow`, category `cardOperation` | 439 923.94 / 184 | Split by article: supplies, meals, delivery, owner spend, etc. |
| Self-transfer-like outflows | `other_outflow`, category `selfTransferOuter` | 1 404 385.83 / 7 | Identify internal transfer, owner withdrawal, financing, or expense |
| Sber other debit | `other_outflow`, operation code `01` | 1 318 394.00 / 9 | Determine whether these are internal transfers, expenses, financing or owner withdrawals |
| Loan/overdraft | `loan_payment` | inflow 49 651.18; outflow 50 248.18 | Split principal, interest and bank commission |
| Refund | `refund` | 2 936.89 / 3 | Confirm customer refunds vs bank/supplier refunds |

## 7. Privacy requirements for implementation

- The rule engine may read full `paymentPurpose`/`payPurpose` only inside private processing.
- Processed outputs must store `description_signature` or safe category labels, not full raw text.
- Full account numbers, card numbers, auth codes, rrn, full INN of individuals, FIO and contract identifiers stay private.
- Public rule specs can contain generic patterns, not full sensitive strings.
- Owner review UI may show full strings only to authorized users; exports for agents/docs should use aggregates and pseudonyms.

