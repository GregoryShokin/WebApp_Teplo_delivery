# Спецификация приложения

Полное описание того, как устроено и работает приложение Teplo: архитектура, бизнес-модули, страницы UI, сущности БД, интеграции с внешними системами, архитектурные решения.

Кому здесь жить:
- Разработчику — при работе над модулем
- Архитектору — при принятии решений о расширении
- Владельцу — при проверке, что приложение реализует нужное

## Архитектура

| Документ | О чём |
|---|---|
| [Видение продукта](architecture/vision.md) | Цель системы, очерёдность модулей, контуры интеграций |
| [Архитектура БД](architecture/database.md) | Core domain, master data, finance/staff модули, audit layer, invariants |
| [Roadmap миграции](architecture/migration-roadmap.md) | Фазы, opening balances, cutover criteria |

## Архитектурные решения (ADR)

| Документ | О чём |
|---|---|
| [Стек приложения](architecture/decisions/stack-decision.md) | FastAPI + React + Postgres + APScheduler |
| [Решения по БД](architecture/decisions/database-decisions.md) | counterparty vs supplier, cashflow vs payment, source-reference, и т.д. |
| [Решения по payroll](architecture/decisions/payroll-decisions.md) | Категория 6, intern, фонд 15 января, минуты без округления |
| [Решения, которые нельзя автоматизировать](architecture/decisions/non-automatic-decisions.md) | Owner-guardrails |

## Модули

### Finance

| Модуль | Спека |
|---|---|
| ДДС и платёжный контур | [dds/spec.md](modules/finance/dds/spec.md) |
| Баланс | [balance/spec.md](modules/finance/balance/spec.md) |
| Платёжный календарь | [payment-calendar/spec.md](modules/finance/payment-calendar/spec.md) |
| Учёт ОС | [fixed-assets/spec.md](modules/finance/fixed-assets/spec.md) |
| ДЗ/КЗ поставщиков | [dz-kz/spec.md](modules/finance/dz-kz/spec.md) |
| Финансовое планирование | [financial-planning/spec.md](modules/finance/financial-planning/spec.md) |

### Staff

| Модуль | Спека |
|---|---|
| Таксономия штата | [taxonomy.md](modules/staff/taxonomy.md) |
| Payroll движок | [payroll/00-engine.md](modules/staff/payroll/00-engine.md) |
| Payroll — производственный персонал | [payroll/production/spec.md](modules/staff/payroll/production/spec.md) |
| Payroll — курьеры (TODO) | [payroll/couriers/spec.md](modules/staff/payroll/couriers/spec.md) |
| Payroll — административный персонал | [payroll/administrative/spec.md](modules/staff/payroll/administrative/spec.md) |
| График сотрудников | [shift-schedule/spec.md](modules/staff/shift-schedule/spec.md) |

### Settings

| Модуль | Спека |
|---|---|
| Настройки приложения | [settings/spec.md](modules/settings/spec.md) |

## Сущности БД

| Документ | О чём |
|---|---|
| [payroll-entities.md](entities/payroll-entities.md) | Сущности payroll-модуля |

## Страницы UI

(пока пусто; будет наполняться по мере проектирования UI)

## Интеграции с внешними системами

| Система | Документы |
|---|---|
| iiko Server | [server-api](integrations/iiko/server-api-endpoints.md), [employees](integrations/iiko/employees-api.md), [finance chart](integrations/iiko/finance-chart-of-accounts.md), [first audit](integrations/iiko/first-export-audit.md) |
| iiko Courier Service | [overview](integrations/iiko/courier-service/overview.md), [data-flow](integrations/iiko/courier-service/data-flow.md), [migration](integrations/iiko/courier-service/migration-mapping.md) |
| Sber API | [api-endpoints](integrations/sber/api-endpoints.md) |
| T-Bank API | [api-endpoints](integrations/tbank/api-endpoints.md) |
| Mango (телефония) | [telephony](integrations/mango/telephony.md) |
| СБИС ЭДО | [api-endpoints](integrations/sbis-edo/api-endpoints.md) |
| Mail.ru | [personal-mailbox](integrations/mailru/personal-mailbox.md) |
| Реестр источников | [data-inventory](integrations/data-inventory.md), [google-drive](integrations/google-drive-discovery.md) |

## AI-агенты

| Документ | О чём |
|---|---|
| [Правила работы агентов](ai-agents/working-rules.md) | Operating rules для AI-агентов в проекте |
| [Паттерны интеграции](ai-agents/integration-patterns.md) | Каталог переиспользуемых паттернов подключения |
