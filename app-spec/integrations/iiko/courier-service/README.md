# iiko courier service integration

Дата переноса: 2026-05-29.
Источник: бывшие документы `07-09` из старого courier discovery пакета.

Этот пакет описывает внешний Python FastAPI-сервис, который синхронизировал книгу `График курьеров` с iikoCloud / iiko Transport.

## Файлы

| Файл | Что внутри |
| --- | --- |
| [overview.md](overview.md) | карта snapshot-сервиса, запуск, зависимости, конфигурация, iikoCloud endpoint'ы |
| [data-flow.md](data-flow.md) | runtime-модели, webhook payload, Google Sheets операции, маппинг колонок, бизнес-расчеты |
| [migration-mapping.md](migration-mapping.md) | какие листы сервис пишет, слабые места кода, перенос в `apps/api` |

Sheets discovery и KPI курьеров лежат в `/research/archive/couriers-sheets-discovery/`. Payroll-правила курьеров этим пакетом не закрыты; placeholder находится в `/app-spec/modules/staff/payroll/couriers/spec.md`.
