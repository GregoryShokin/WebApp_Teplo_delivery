# Payroll entities

Дата фиксации: 2026-05-29.
Источник: entity sections бывшего гибридного payroll-документа.

Минимальные сущности:

| Сущность | Назначение |
| --- | --- |
| `employees` | карточка сотрудника без расчета зарплаты внутри карточки |
| `employee_status_events` | история статусов: найм, trial, увольнение, архив |
| `roles` | справочник ролей |
| `employee_role_assignments` | история назначений сотрудника на роли |
| `employee_category_events` | история категорий с датой действия |
| `role_category_rates` | ставка смены по роли и категории |
| `category_rules` | коэффициент процента, депозит, удержание |
| `allowance_events` | назначение/снятие Старший/Зам, доплаты |
| `revenue_percent_tiers` | пороги выручки и процент |
| `shift_schedules` | плановые смены |
| `attendance_entries` | фактические интервалы явки |
| `shift_duration_results` | рассчитанная длительность и статус качества |
| `daily_revenue` | выручка по дню и подразделению |
| `shift_coefficients` | фактические коэффициенты распределения процента |
| `payroll_runs` | расчет за период/подразделение/версию правил |
| `payroll_ledger_lines` | все начисления, удержания, фонд, депозит, налоговые строки |
| `manual_payroll_events` | ручные премии, штрафы, пособия, НДФЛ, корректировки |
| `deposit_accounts` | депозитный счет сотрудника |
| `deposit_transactions` | удержание, возврат, списание депозита |
| `accumulation_fund_accounts` | счет накопительного фонда по сотруднику/году |
| `accumulation_fund_transactions` | начисление, списание, право на выплату, выплата фонда |
| `payroll_payments` | обязательства/ведомости выплат; cash-fact закрывается через 1:1 `cashflow_transaction` или `payroll_payment_batch` в DDS |
| `admin_payroll_profiles` | фиксированные административные оклады и должности |
| `pnl_mapping_rules` | связь payroll-типов со строками P&L |
| `audit_log` | кто, когда и почему изменил правило, событие или расчет |
| `import_batches` | пачки миграции/импорта из Google Sheets |

См. также `research/processed/payroll_discovery/payroll_module_entities.csv`.

## Сущности первого релиза

- сотрудники и статусы;
- роли, категории, ставки и коэффициенты;
- смены, явки, выручка;
- payroll runs;
- payroll ledger lines;
- payroll payments;
- manual payroll events;
- deposit accounts/transactions;
- accumulation fund accounts/transactions;
- audit log.
