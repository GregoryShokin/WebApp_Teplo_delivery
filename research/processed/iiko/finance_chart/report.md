# iiko finance chart research report

Дата разведки: 2026-05-20.

Канонический источник операций Главной кассы: `GET /reports/olap` с `report=TRANSACTIONS`, `groupRow=DateTime.DateTyped, Account.Name, Contr-Account.Name, TransactionSide, TransactionType, Document, CashFlowCategory, Comment` и агрегатами `Sum.ResignedSum, Sum.Incoming, Sum.Outgoing`.

Период: 2026-02-01..2026-05-20. Операций Главной кассы: 279. Уникальных корсчетов: 13.

Обороты: дебет 1 294 745 руб.; кредит 1 227 138 руб.

## Артефакты

- Raw: `research/raw/iiko/finance_chart_research/` (может содержать комментарии iiko и PII).
- Processed: `research/processed/iiko/finance_chart/`.
- Документ API: `app-spec/integrations/iiko/finance-chart-of-accounts.md`.

## Top-10 корсчетов по обороту

| Корсчет | Дебет | Кредит | Статус |
| --- | ---: | ---: | --- |
| Торговые кассы | 830 260 | 0 | rule_candidate_sales_cash |
| Текущие расчеты с сотрудниками | 0 | 828 282 | rule_candidate_owner_review |
| Алиса наличные | 417 023 | 0 | partner_receivable |
| Прочие расходы | 0 | 156 938 | owner_review |
| Перемещения | 0 | 84 000 | legacy_internal_transfer_review |
| Задолженность перед поставщиками | 0 | 66 648 | iiko_auto_classified |
| Всякое Черникова | 0 | 53 568 | requires_owner_review |
| Депозиты сотрудников | 24 100 | 24 500 | deposit_control_source |
| Прочие доходы | 22 851 | 0 | owner_review |
| Основные средства | 0 | 9 977 | owner_review |

## Автоклассификация iiko

iiko не заполняет `CashFlowCategory` универсально для операций Главной кассы. Надежно выделенный бизнес-кейс: корсчет `Задолженность перед поставщиками` -> `Оплата поставщикам`; остальные корсчета требуют rule engine приложения.
За период по этому корсчету: дебет 0.00, кредит 66648.28, строк 19.

## Риски

- `Comment` может содержать ФИО сотрудников/курьеров; в processed и Markdown он не выгружается.
- `Депозиты сотрудников` / `Депозиты курьеров` являются DDS/control source депозитов курьеров, но не самостоятельным payroll-subledger без сверки с зарплатной ведомостью.
- `GET /v2/cashshifts/*` полезен для сверки наличной выручки, но не содержит полный журнал корсчетов Главной кассы.
- Май 2026 выгружен частично: до 2026-05-20 включительно.

## Owner decisions 2026-05-25

- `Алиса наличные` - партнерская дебиторка: клиент партнера оплатил напрямую партнеру, cash в DDS появляется только при фактическом получении денег.
- `Перемещения` - legacy-счет времен точки Гагарина.
- `Всякое Черникова` - отдельный review-слой мелких расходов по private-комментарию/чеку.
- `Всякое Гагарина` - исторический хвост старой точки.
- `ГарантФонд` - депозиты курьеров в Т-Банке; iiko `Депозиты сотрудников` используется для контроля движения депозитов.

## Проверенные endpoint'ы

| Endpoint | Статус | Записей | Примечание |
| --- | ---: | ---: | --- |
| `/application.wadl` | 200 | 0 | WADL discovery; 276 method/path combinations observed in project docs |
| `/v2/reports/olap/columns` | 200 | 1 | column catalog for TRANSACTIONS |
| `/v2/reports/olap/presets` | 200 | 40 | saved OLAP presets discovery |
| `/v2/reports/olap/byPresetId/8c13763a-35bf-9f27-017f-5468b1e70022` | 200 | 174 | saved preset pilot; preset_label=dds |
| `/v2/reports/olap/byPresetId/8c13763a-35bf-9f27-017f-5468b1e70023` | 200 | 94 | saved preset pilot; preset_label=dds_by_departments |
| `/v2/entities/accounts/list` | 200 | 77 | chart of accounts dictionary; source for account_id/corr_account_id |
| `/v2/cashshifts/list` | 200 | 7 | cash shift summary; useful for cash-sales reconciliation, not full chart-of-accounts ledger |
| `/v2/cashshifts/payments/list/{sessionId}` | 200 | 1 | cash shift payments detail; contains payment records, not all main-cash postings |
| `/v2/reports/balance/counteragents` | 200 | 31 | balance by counteragent for Главная касса; useful for balance snapshots, not operation ledger |
| `/v2/reports/balance/stores` | 200 | 553 | store balance endpoint; unrelated to Main Cash operations |
| `/v2/documents/internalTransfer` | 200 | 1 | direct document endpoint pilot; returned no useful Main Cash rows in pilot |
| `/reports/olap` | 200 | 76 | canonical Главная касса export; period=2026-02-01..2026-02-28 |
| `/reports/olap` | 200 | 88 | canonical Главная касса export; period=2026-03-01..2026-03-31 |
| `/reports/olap` | 200 | 69 | canonical Главная касса export; period=2026-04-01..2026-04-30 |
| `/reports/olap` | 200 | 46 | canonical Главная касса export; period=2026-05-01..2026-05-20 |
