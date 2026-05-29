# Bank Reconciliation Controls

Дата сборки: 2026-05-20.

## 1. Контроль покрытия источников

| Control | Current status | Source |
| --- | --- | --- |
| Sber daily statement completeness | `2026-02-01` - `2026-05-19`: 108 days, all days have summary/transactions match | `research/processed/sber/bank_cashflow_report.md`, `bank_cashflow_daily.csv` |
| T-Bank statement parsing completeness | `2026-02-01` - `2026-05-19`: 584 operations, 0 unknown direction, 0 missing category | `research/processed/tbank/report.md`, `operation_categories.csv` |
| Combined classification coverage | 1224 operations; 1224 have non-`unclassified` `flow_type`; 409 require owner review | `research/processed/cashflow/report.md` |
| iiko comparison scope | Feb-Apr full months; May only `2026-05-01` - `2026-05-17` because current iiko snapshot is limited | `research/processed/cashflow/iiko_vs_bank_reconciliation.csv` |

## 2. Internal transfer control

Rule: monthly Sber outflow classified as `internal_transfer_sber_to_tbank` must equal T-Bank inflow classified as `internal_transfer_sber_to_tbank`.

Tolerance from current script: `ok` if absolute difference is within max of 1% of transfer amount or 10 000 RUB.

Current result:

| Month | Sber outflow | T-Bank inflow | Diff | Status |
| --- | ---: | ---: | ---: | --- |
| 2026-02 | 2 375 000.00 | 2 375 000.00 | 0.00 | ok |
| 2026-03 | 3 311 000.00 | 3 311 000.00 | 0.00 | ok |
| 2026-04 | 1 991 000.00 | 1 991 000.00 | 0.00 | ok |
| 2026-05 | 1 719 000.00 | 1 719 000.00 | 0.00 | ok |

Application requirement:

- Run this control after classification for every closed month.
- Show both sides of mismatch by operation group, but keep full payment purposes private.
- Block revenue reporting if a T-Bank inflow from Sber is not paired to Sber outflow and the amount is material.

## 3. Management bank revenue control

Target formula for DDS module:

```text
management_bank_revenue =
  revenue_acquiring_sber
  + revenue_acquiring_tbank
  - customer_refunds
```

Important current nuance:

- `revenue_split.csv` currently sums Sber acquiring + T-Bank acquiring for reconciliation scope.
- `refund` is classified separately and must be deducted only after owner confirms it is a customer refund.
- Internal transfers Sber -> T-Bank are never included in revenue.

Current acquiring reconciliation:

| Month | Sber acquiring | T-Bank acquiring | Bank acquiring total | iiko revenue | Bank / iiko |
| --- | ---: | ---: | ---: | ---: | ---: |
| 2026-02 | 2 932 164.26 | 198 413.00 | 3 130 577.26 | 3 833 597.82 | 81.66% |
| 2026-03 | 3 156 477.58 | 174 692.00 | 3 331 169.58 | 4 006 534.78 | 83.14% |
| 2026-04 | 2 558 836.39 | 45 362.00 | 2 604 198.39 | 3 326 144.31 | 78.29% |
| 2026-05 | 1 614 909.27 | 30 239.00 | 1 645 148.27 | 2 082 482.51 | 79.00% |

Average coverage for full months Feb-Apr after adding T-Bank acquiring: 81.03%.

## 4. Why bank cashflow is not equal to iiko revenue

Bank cashflow and iiko revenue are different facts:

- iiko records operational sales; bank records cash settlement.
- Acquiring has settlement lags and sometimes net-of-fee presentation.
- Some payment channels can settle through aggregators or different banks.
- Cash, refunds, cancellations, technical corrections and commissions can shift timing and amount.
- May comparison is intentionally partial because iiko processed data currently ends on `2026-05-17`.
- T-Bank inflows include internal transfers from Sber; including them as revenue would double count.

Therefore bank coverage does not have to be 100%. The control should track trend, unexplained changes and missing channels, not force equality.

## 5. Expense and outflow controls

| Control | Rule | Current signal |
| --- | --- | --- |
| Supplier mapping completeness | T-Bank `contragentOutcome` should resolve to DDS article by private override or owner mapping | 208 supplier payments; review groups remain for 100 operations / 2 627 159.36 RUB |
| Payroll review | T-Bank `contragentPeople` should be confirmed as payroll, owner withdrawal or other people payment | 21 operations / 3 363 645.00 RUB require owner confirmation |
| Tax split | `tax_payment` must split payroll taxes, other taxes, penalties/fines | 12 operations / 738 728.79 RUB |
| Bank fees | Fees should split acquiring fee, RKO, other bank fees, overdraft commission | 68 operations / 105 810.69 RUB |
| Loan/overdraft | Financing movements should split principal, interest and fees | 18 operations; current net close to zero but owner split is needed |
| Business-card review | T-Bank `cardOperation` cannot stay as one article in final DDS | 184 operations / 439 923.94 RUB |
| Other inflow/outflow | Any `other_*` group above material threshold requires owner meaning | `depositFullWithdrawal`, `selfTransferOuter`, Sber other debit groups are material |

Recommended materiality:

- Always review any `other_inflow`, `other_outflow`, `loan_payment`, `tax_payment`, `payroll_payment`.
- For supplier/card groups, review all groups above 10 000 RUB or recurring monthly groups.
- Keep a separate owner-review state: `new`, `asked_owner`, `answered`, `rule_created`, `excluded`.

## 6. Controls to build into the app

1. Source freshness: last successful Sber/T-Bank run, period covered, operation count delta.
2. Sber daily statement control: summary turnovers and transaction totals must match.
3. T-Bank parse control: no unknown direction/category; pagination complete.
4. Internal transfer mirror: Sber outflow equals T-Bank inflow by month and, later, by date/amount nearest match.
5. Revenue bridge: Sber acquiring + T-Bank acquiring - confirmed customer refunds vs iiko revenue, with coverage trend.
6. Duplicate revenue guard: T-Bank inflows from Sber excluded from revenue.
7. Owner review queue: material unknowns, payroll, taxes, deposit movements, business-card spend, self-transfer-like operations.
8. Rule drift control: new native categories, new counterparties, new acquiring signatures, and large amount deviations.
9. Privacy export control: processed exports must not include full payment purpose, full accounts, cards, FIO or tokens.

