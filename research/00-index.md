# Исследовательский слой

ETL-скрипты, выгрузки из внешних систем, исторические снапшоты, архив анализов. Контент здесь — рабочий для текущих агентов и исторически ценный, но не является источником истины для приложения или бизнес-логики.

## Структура

| Папка | Что внутри |
|---|---|
| [raw/](raw/) | Сырые выгрузки из iiko, T-Bank, Courier Service. Gitignored. |
| [processed/](processed/) | Пустые подпапки — target для билдер-скриптов |
| [private/](private/) | Секреты: .pem сертификаты Sber, личные ключи. Gitignored. |
| [scripts/](scripts/) | Python-скрипты: экспортёры из API и билдеры аналитических CSV |
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
| [scripts/iiko/](scripts/iiko/) | Экспортёры данных iiko (employees, OLAP, продажи) + билдеры (economic block, P&L) |
| [scripts/sber/](scripts/sber/) | Экспорт выписки, построение cashflow, сверка с iiko |
| [scripts/tbank/](scripts/tbank/) | Экспорт выписки, парсеры платёжек, OCR |
| [scripts/bank/](scripts/bank/) | Классификация cashflow |
| [scripts/mango/](scripts/mango/) | Экспорт телекома |
| [scripts/mail/](scripts/mail/) | Mail.ru интеграция |
| [scripts/business_control/](scripts/business_control/) | Сводные билдеры: fixed assets, labor costs |
