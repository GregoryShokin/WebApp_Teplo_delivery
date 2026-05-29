# Wave 2 cut plan

Temporary owner-review plan for Wave 2B. Do not cut `16`, `17`, `19` or `30` before owner review.

## 16-fixed-assets-and-balance.md

| секция | заголовок | тип | целевой файл | коммент |
|---|---|---|---|---|
| 0 | Основные средства и баланс | research | `research/archive/old-os-and-balance-discovery.md` | Wrapper for the original read-only discovery snapshot. |
| 1 | Назначение | research | `research/archive/old-os-and-balance-discovery.md` | States that this is not a current balance, but a first read-only slice and recovery plan. |
| 2 | Статус источников | research | `research/archive/old-os-and-balance-discovery.md` | Historical/source reliability notes for old Google Sheets and bank CSV. |
| 3 | Что Снято Read-Only | research | `research/archive/old-os-and-balance-discovery.md` | Discovery inventory of exported tabs. |
| 3.1 | Учёт ОС | research | `research/archive/old-os-and-balance-discovery.md` | Read-only structure of the old fixed-assets workbook. |
| 3.2 | Баланс | research | `research/archive/old-os-and-balance-discovery.md` | Read-only structure of the old balance workbook. |
| 4 | Историческое Состояние ОС | research | `research/archive/old-os-and-balance-discovery.md` | Snapshot aggregates from processed historical register, not current truth. |
| 5 | Кандидаты В ОС Из Банковской Выписки | research | `research/archive/old-os-and-balance-discovery.md` | Discovery result from bank screening; useful as historical evidence only. |
| 6 | Связь С P&L | business-docs | `business-docs/finance/fixed-assets-rules.md` | Business rule: do not recognize depreciation or mix DDS payments into P&L before confirmed asset register. |
| 7 | Открытые Вопросы Владельцу | research | `research/archive/old-os-and-balance-discovery.md` | Historical owner-question list; before Wave 2B cross-check with later fixed-assets decisions. |
| 8 | План Восстановления Реестра ОС | business-docs | `business-docs/finance/fixed-assets-rules.md` | Operational methodology for inventory and asset-register recovery, independent of app UI. |
| 9 | План Восстановления Баланса | business-docs | `business-docs/finance/balance-methodology.md` | Balance recovery methodology: collect control-date balances instead of stretching stale formulas. |
| 10 | Processed-Артефакты | research | `research/archive/old-os-and-balance-discovery.md` | Pointers to processed evidence files. |

## 17-unified-management-app.md

| секция | заголовок | тип | целевой файл | коммент |
|---|---|---|---|---|
| 0 | Единое управленческое веб-приложение | app-spec | `app-spec/architecture/vision.md` | Document title and date for the target product vision. |
| 1 | Назначение | app-spec | `app-spec/architecture/vision.md` | Explains why current research should become future module specs. |
| 2 | Цель Системы | app-spec | `app-spec/architecture/vision.md` | High-level scope of the unified management app. |
| 3 | Принцип Построения | app-spec | `app-spec/architecture/vision.md` | Product architecture principle: rules, sources and responsibility before dashboards. |
| 4 | Очерёдность Модулей | app-spec | `app-spec/architecture/vision.md` | Module rollout order. Keep subsections together unless owner wants separate roadmap. |
| 4.1 | Зарплата И Кадры | app-spec | `app-spec/architecture/vision.md` | First module vision, not detailed payroll formulas. |
| 4.2 | Финансы И Управленческая Отчётность | app-spec | `app-spec/architecture/vision.md` | Finance module vision. |
| 4.3 | Интеграции Операционной Модели | app-spec | `app-spec/architecture/vision.md` | Integration layer vision. |
| 4.4 | Технологические Карты, Склад И Производство | app-spec | `app-spec/architecture/vision.md` | Future production and inventory layer. |
| 4.5 | Маркетинг И Рост | app-spec | `app-spec/architecture/vision.md` | Future marketing layer after finance and operations. |
| 5 | Контуры Интеграций | app-spec | `app-spec/architecture/vision.md` | Integration contour table; can cross-link later integration specs. |
| 6 | Правила Для Агентов | app-spec | `app-spec/ai-agents/working-rules.md` | Agent operating rules should live separately from product vision. |
| 7 | Ближайший Практический Вывод | app-spec | `app-spec/architecture/vision.md` | Practical priority statement for product sequencing. |

## 19-payroll-module-spec.md

| секция | заголовок | тип | целевой файл | коммент |
|---|---|---|---|---|
| 0 | Спецификация payroll-модуля `Зарплата и кадры` | app-spec | `app-spec/modules/staff/payroll/00-engine.md` | Intro, privacy boundary and module purpose for the payroll engine. |
| A | Источники и статусы правил | mixed | manual split: `research/archive/payroll-google-sheets-current-state.md` + `app-spec/modules/staff/payroll/00-engine.md` | Source list is research; status vocabulary is useful for app calculation quality. |
| B | Решения Владельца 2026-05-20 | decision | `app-spec/architecture/decisions/payroll-decisions.md` | Owner decisions: Apps Script not copied, category 6, interns, fund date, deposit visibility. |
| C | Решения Владельца 2026-05-24 | decision | `app-spec/architecture/decisions/payroll-decisions.md` | Decision 11.7 on `payroll_payment` as obligation, not cash fact. |
| 1 | Текущее устройство расчета зарплаты в Google Sheets | research | `research/archive/payroll-google-sheets-current-state.md` | Current Sheets architecture and legacy flow. |
| 2 | Карта листов и назначение | research | `research/archive/payroll-google-sheets-current-state.md` | Sheet-by-sheet discovery map. |
| 3 | Роли, должности, категории и коэффициенты | mixed | manual split: `app-spec/modules/staff/payroll/00-engine.md` + `app-spec/modules/staff/payroll/production/spec.md` + `app-spec/modules/staff/payroll/administrative/spec.md` | Contains the shared role model, production categories and separate administrative model. |
| 3.1 | Сменные роли и категории | app-spec | `app-spec/modules/staff/payroll/production/spec.md` | Production roles: shift administrator, prep, pizza, sushi, shawarma. |
| 3.2 | Категория и коэффициент процента | mixed | manual split: `app-spec/modules/staff/payroll/production/spec.md` + `business-docs/staff/payroll-policy.md` | Production coefficient table plus business defaults for deposit rules. |
| 3.3 | Ставки смены по роли и категории | app-spec | `app-spec/modules/staff/payroll/production/spec.md` | Role/category shift rates for production payroll formulas. |
| 3.4 | Надбавки | mixed | manual split: `app-spec/modules/staff/payroll/00-engine.md` + `app-spec/modules/staff/payroll/production/spec.md` | Event mechanism is common; Старший, Зам and extra-hour values are production-specific until owner says otherwise. |
| 4 | Полная логика начислений и удержаний | mixed | manual split by subsections below | Parent section only. Wave 2B should not move it as a block. |
| 4.1 | Оклад | app-spec | `app-spec/modules/staff/payroll/production/spec.md` | Production formula: role/category 12-hour shift rate, prorated by hours, allowances. Common engine only executes it. |
| 4.2 | Процент от выручки | app-spec | `app-spec/modules/staff/payroll/production/spec.md` | Production revenue-share pool and tier table. No courier or administrative formula here. |
| 4.3 | Премия | app-spec | `app-spec/modules/staff/payroll/00-engine.md` | Common manual payroll event that can create ledger lines. |
| 4.4 | Накопительный фонд | mixed | manual split: `app-spec/modules/staff/payroll/00-engine.md` + `business-docs/staff/payroll-policy.md` + `app-spec/architecture/decisions/payroll-decisions.md` | Common account mechanics plus hard business rule: payout strictly on 15 January and forfeiture before eligibility. |
| 4.5 | Больничные, отпуска и пособия | app-spec | `app-spec/modules/staff/payroll/00-engine.md` | Common line type/subtype model; may later link to HR policy. |
| 4.6 | Штрафы и удержания | app-spec | `app-spec/modules/staff/payroll/00-engine.md` | Common manual event and normalized line-type handling. |
| 4.7 | Штрафы по ревизиям | mixed | manual split: `app-spec/modules/staff/payroll/production/spec.md` + `business-docs/staff/payroll-policy.md` | Production-specific rates and exclusions, but revision penalty policy needs owner-confirmed business wording. |
| 4.8 | НДФЛ | mixed | manual split: `app-spec/modules/staff/payroll/00-engine.md` + `business-docs/staff/payroll-policy.md` | Payroll withholding mechanics plus business rule that full P&L payroll-tax source is DDS, not payroll alone. |
| 4.9 | Депозиты | mixed | manual split: `app-spec/modules/staff/payroll/00-engine.md` + `business-docs/staff/payroll-policy.md` | Common deposit account mechanics and policy. Despite the future courier bucket, this section does not contain delivery/courier-specific formulas. |
| 4.10 | Ручные корректировки | app-spec | `app-spec/modules/staff/payroll/00-engine.md` | Common `manual_adjustment` operation with audit fields. |
| 5 | Правила распределения процента от выручки между сменой | app-spec | `app-spec/modules/staff/payroll/production/spec.md` | Production revenue-share denominator, coefficients and attendance interaction. Owner decisions later changed minute rounding, so verify before copying formula text. |
| 6 | Персональный отчет сотрудника | app-spec | `app-spec/modules/staff/payroll/00-engine.md` | Payroll UI/report surface and ledger/payment/fund/deposit drill-down. |
| 7 | Разделение P&L-начислений и cash-flow выплат | mixed | manual split: `app-spec/modules/staff/payroll/00-engine.md` + `app-spec/architecture/decisions/payroll-decisions.md` | Core engine invariant plus repeated decision 11.7 about DDS cash fact and privacy. |
| 8 | Жизненный цикл сотрудника | mixed | manual split: `app-spec/modules/staff/payroll/00-engine.md` + `business-docs/staff/payroll-policy.md` | Lifecycle operations are app-spec; termination/fund/deposit consequences include HR policy. |
| 8.1 | Найм | app-spec | `app-spec/modules/staff/payroll/00-engine.md` | Common lifecycle operation and blocking validation for unknown employee. |
| 8.2 | Изменение категории | app-spec | `app-spec/modules/staff/payroll/00-engine.md` | Effective-dated category event and closed-run protection. |
| 8.3 | Смена роли | mixed | `app-spec/modules/staff/payroll/00-engine.md` | App model is clear, but deposit-category rule for multi-role employees remains an owner question. |
| 8.4 | Назначение и снятие `Старший`/`Зам` | mixed | manual split: `app-spec/modules/staff/payroll/00-engine.md` + `app-spec/modules/staff/payroll/production/spec.md` | Event model is common; current amounts and role/station application are production-specific and partly unresolved. |
| 8.5 | Увольнение | mixed | manual split: `app-spec/modules/staff/payroll/00-engine.md` + `business-docs/staff/payroll-policy.md` | App lifecycle operation plus business decisions on deposit, fund and iiko deactivation. |
| 8.6 | Потеря накопительного фонда | business-docs | `business-docs/staff/payroll-policy.md` | Hard policy: forfeiture before 15 January; engine implementation should reference it. |
| 8.7 | Удержание, возврат и списание депозита | mixed | manual split: `app-spec/modules/staff/payroll/00-engine.md` + `business-docs/staff/payroll-policy.md` | Account transactions are engine; return/writeoff reasons are policy. |
| 9 | Предлагаемая модель данных веб-приложения | app-spec | `app-spec/entities/payroll-entities.md` | Entity list should become the canonical payroll entity reference. |
| 10 | Вопросы владельцу | mixed | manual split: `app-spec/architecture/decisions/payroll-decisions.md` + owner-review section | Closed crossed-out questions become decisions; still-open items stay in owner review and should not become app spec. |
| 11 | Риски переноса | app-spec | `app-spec/modules/staff/payroll/00-engine.md` | Implementation and migration risks for the payroll engine. |
| 12 | Минимальный MVP payroll-модуля | app-spec | `app-spec/modules/staff/payroll/00-engine.md` | MVP scope for payroll module. |
| 12.1 | Экраны | app-spec | `app-spec/modules/staff/payroll/00-engine.md` | UI surfaces for MVP. |
| 12.2 | Сущности первого релиза | app-spec | `app-spec/entities/payroll-entities.md` | Short MVP entity subset; merge with §9 entity reference. |
| 12.3 | Операции первого релиза | app-spec | `app-spec/modules/staff/payroll/00-engine.md` | MVP operation list. |

## 30-app-database-architecture.md

| секция | заголовок | тип | целевой файл | коммент |
|---|---|---|---|---|
| 0 | Архитектура БД единого управленческого приложения | app-spec | `app-spec/architecture/database.md` | Title, date and status for canonical database architecture. |
| 1 | Назначение | app-spec | `app-spec/architecture/database.md` | Scope of the logical domain model. |
| 2 | Источники и обозначения | app-spec | `app-spec/architecture/database.md` | Source notation. Paths should be updated during Wave 2B after moves. |
| 3 | Полная карта сущностей из модульных спецификаций | app-spec | `app-spec/architecture/database.md` | Entity inventory across modules. |
| 4 | Принципы модели | app-spec | `app-spec/architecture/database.md` | Cross-module modeling principles. |
| 4.1 | Ключи и опциональность | app-spec | `app-spec/architecture/database.md` | Field optionality and source-reference rules. |
| 5 | Core domain и master data | app-spec | `app-spec/architecture/database.md` | Main master-data section. Contains an `app_setting` row that should cross-link to Settings in Wave 2B. |
| 6 | Financial modules | app-spec | `app-spec/architecture/database.md` | Parent section for finance modules. |
| 6.1 | Payroll | app-spec | `app-spec/architecture/database.md` | Database architecture view of payroll entities, not payroll module spec. |
| 6.2 | DDS and payment contour | app-spec | `app-spec/architecture/database.md` | Database architecture view of DDS/payment entities. |
| 6.3 | P&L / ОПиУ | app-spec | `app-spec/architecture/database.md` | Database architecture view of P&L entities. |
| 6.4 | Balance | app-spec | `app-spec/architecture/database.md` | Database architecture view of balance entities. |
| 6.5 | Supplier AR/AP / УДКЗ | app-spec | `app-spec/architecture/database.md` | Database architecture view of supplier AR/AP entities. |
| 6.6 | Fixed assets | app-spec | `app-spec/architecture/database.md` | Database architecture view of depreciation schedule and fixed asset relation. |
| 6.7 | Financial activity | app-spec | `app-spec/architecture/database.md` | Asymmetric MVP entity model for financial activity. Decisions stay in §11/§15. |
| 6.8 | Taxes | app-spec | `app-spec/architecture/database.md` | Asymmetric MVP entity model for taxes. |
| 7 | Audit & integration layer | app-spec | `app-spec/architecture/database.md` | Common audit and source-reference architecture. |
| 8 | Quality & reconciliation | app-spec | `app-spec/architecture/database.md` | Global quality status and reconciliation model. |
| 9 | High-level module diagram | app-spec | `app-spec/architecture/database.md` | Architecture diagram for database.md. |
| 10 | Cross-module invariants | app-spec | `app-spec/architecture/database.md` | Core invariants for database architecture. |
| 11 | Архитектурные вопросы и зафиксированные решения | decision | `app-spec/architecture/decisions/database-decisions.md` | Parent section for decision traceability; do not leave in main database.md except as link. |
| 11.1 | `counterparty` DDS vs `supplier_counterparty` УДКЗ | decision | `app-spec/architecture/decisions/database-decisions.md` | Decision 11.1 with variants. |
| 11.2 | `cashflow_transaction` DDS vs `supplier_document.payment` / оплата в УДКЗ | decision | `app-spec/architecture/decisions/database-decisions.md` | Decision 11.2 with variants. |
| 11.3 | `balance_source_reference` vs общий `source_reference` + `source_snapshot` + `agent_run` | decision | `app-spec/architecture/decisions/database-decisions.md` | Decision 11.3 with variants. |
| 11.4 | `wallet` DDS vs `data_source` AI/integration | decision | `app-spec/architecture/decisions/database-decisions.md` | Decision 11.4 with variants. |
| 11.5 | `prepaid_expenses` vs баланс «Выданные авансы поставщикам» vs `supplier_document` с типом аванса | decision | `app-spec/architecture/decisions/database-decisions.md` | Decision 11.5 with variants and 2026-05-25 clarification. |
| 11.6 | `dds_article` vs `pnl_line` vs `balance_line` | decision | `app-spec/architecture/decisions/database-decisions.md` | Decision 11.6 with variants. |
| 11.7 | Payroll `payments` vs DDS `cashflow_transaction` | decision | `app-spec/architecture/decisions/database-decisions.md` | Decision 11.7 with privacy implications. |
| 11.8 | `source_document` DDS vs `parsed_document` integration | decision | `app-spec/architecture/decisions/database-decisions.md` | Decision 11.8 with variants. |
| 11.9 | Учёт ФД и налоги: недостаток спецификаций | decision | `app-spec/architecture/decisions/database-decisions.md` | Decision 11.9 with asymmetric MVP. |
| 12 | Стратегия миграции исторических данных | app-spec | `app-spec/architecture/migration-roadmap.md` | Existing migration roadmap already contains date X, hybrid depth, opening balances and source snapshots. Merge only missing traceability/options; avoid duplicating §31 content. |
| 13 | Решения, которые нельзя принимать автоматически | decision | `app-spec/architecture/decisions/non-automatic-decisions.md` | Owner-decision guardrail list. Many items are now closed, but the list is useful as non-automatic-decision policy. |
| 14 | Первый технический вывод | app-spec | `app-spec/architecture/database.md` | Next design steps after owner review. |
| 14.1 | Settings layer | app-spec | `app-spec/modules/settings/spec.md` | Create new Settings module in Wave 2B and move the setting entity, categories, permissions and audit rules here. |
| 15 | Принятые архитектурные решения 2026-05-24/25/27 | decision | `app-spec/architecture/decisions/database-decisions.md` | Canonical accepted database decisions table. Consider also cross-linking payroll/payment-calendar/fixed-assets decisions to module decision docs. |

## Открытые вопросы владельцу

- `16-fixed-assets-and-balance.md` §7 may contain historical questions already closed later by fixed-assets decisions in `30` §15.4/15.5 and the fixed-assets module spec. Before Wave 2B, confirm which questions should remain historical archive and which should become active policy.
- `19-payroll-module-spec.md` "Источники и статусы правил" is mixed: source list belongs in research, but status vocabulary may be needed by the payroll engine.
- `19` §3 should be manually split: production roles and rates go to production spec, administrative roles go to administrative spec, while the shared role/category mechanism belongs in `00-engine`.
- `19` §4.4, §4.8, §4.9 and §8.5-8.7 combine executable app behavior with business policy. Owner should confirm that `business-docs/staff/payroll-policy.md` is the right home for the hard policy text.
- `19` §4.7 revision penalties are production-specific, but the business policy for revision timing, exclusions and P&L treatment still needs confirmation before being treated as stable.
- `19` contains no real courier payroll formula, delivery formula or courier-specific deposit rule. The future `app-spec/modules/staff/payroll/couriers/spec.md` should probably be created from another source or left as a placeholder until owner/source review.
- `19` §5 still mentions legacy minute rounding `>= 40`, while later owner decisions say to calculate exact minutes without that rounding. Wave 2B should avoid copying the old formula as target behavior without annotating the decision.
- `19` §10 has both closed and open questions. Closed crossed-out questions should become decisions; open questions should remain owner-review items, not app spec.
- `30` §12 overlaps heavily with the already moved `app-spec/architecture/migration-roadmap.md`. Prefer a careful merge of missing decision traceability over a separate duplicate file, unless owner wants `app-spec/architecture/data-migration-from-30.md` as a temporary appendix.
- `30` §5 contains an `app_setting` row and §14.1 contains the full Settings layer. In Wave 2B, decide whether `database.md` keeps only the entity reference while `app-spec/modules/settings/spec.md` owns the module behavior.
