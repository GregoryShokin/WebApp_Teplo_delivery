# Исследовательский слой

Исторические снапшоты, downstream-классификаторы поверх данных и архив анализов. Контент здесь — рабочий для текущих агентов и исторически ценный, но не является источником истины для приложения или бизнес-логики.

## Структура

| Папка | Что внутри |
|---|---|
| [raw/](raw/) | Сырые выгрузки из iiko, T-Bank, Courier Service. Gitignored. |
| [processed/](processed/) | Пустые подпапки — target для билдер-скриптов |
| [private/](private/) | Локальные приватные данные и исторические артефакты. Gitignored. |
| [scripts/](scripts/) | Downstream-классификаторы и агрегаторы поверх данных |
| [archive/](archive/) | Исторические анализы и discovery-документы |

## Архив

| Документ | О чём |
|---|---|
| [Анализ Google Sheets payroll-калькулятора](archive/payroll-calculator-analysis.md) | Read-only разбор работающего Sheets-калькулятора |
| [Текущее состояние payroll Google Sheets](archive/payroll-google-sheets-current-state.md) | Структура и формулы legacy payroll |
| [ОС и баланс — read-only discovery](archive/old-os-and-balance-discovery.md) | Что было снято из старых Google Sheets |
| [Курьеры — Sheets discovery](archive/couriers-sheets-discovery/) | Структура «График курьеров»: инвентарь, схема, KPI, views |
| [Анализ расхождений таксономии](archive/staff-taxonomy-gap-analysis.md) | Исторический gap-анализ |
| [MVP smoke-test web](archive/web-mvp-smoke-test-report.md) | Отчёт о smoke-тесте раннего MVP веб-приложения |

## Скрипты

| Папка | Что внутри |
|---|---|
| [scripts/bank/](scripts/bank/) | Классификация cashflow |
| [scripts/business_control/](scripts/business_control/) | Сводные билдеры: fixed assets, labor costs |

## Не здесь

Операционные интеграции с внешними API (iiko, Sber, T-Bank, Mango, Mail.ru) и их credentials живут в [/integrations/](../integrations/00-index.md). research/ содержит только исторические снапшоты и downstream-классификаторы поверх данных.
