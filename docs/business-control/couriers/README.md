# Курьеры: разведка Google Sheets и план миграции

Пакет документов фиксирует устройство книги `График курьеров и показатели курьеров`, подтвержденный внешний Python-сервис загрузки из iikoCloud, KPI и план переноса в приложение.

## Файлы

| Файл | Что внутри | Статус |
| --- | --- | --- |
| [01-sheets-inventory.md](01-sheets-inventory.md) | список 7 вкладок, назначение, служебные листы | готово с ограничением по live connector |
| [02-sheets-schema.md](02-sheets-schema.md) | восстановленная схема листов, источники колонок, ручные/формульные зоны | частично: точные формулы/валидации живой книги не сняты; колонки `Курьеры`/`Выходы`/`Доставки` уточнены в 08 |
| [03-iiko-transport-integration.md](03-iiko-transport-integration.md) | подтвержденная iikoCloud-интеграция внешнего Python-сервиса, webhook, endpoint'ы, маппинг Sheets | обновлено после snapshot |
| [04-kpi-definitions.md](04-kpi-definitions.md) | KPI курьеров, формулы, периоды, payroll-связь | готово; payroll rules отсутствуют |
| [05-views-and-manual-inputs.md](05-views-and-manual-inputs.md) | пользовательские виды и ручные места ввода | готово |
| [06-issues-and-migration-plan.md](06-issues-and-migration-plan.md) | слабые места, целевые сущности БД, сервисы, UI, очередность | обновлено с учетом Python-сервиса |
| [07-courier-service-overview.md](07-courier-service-overview.md) | карта snapshot-сервиса, запуск, зависимости, конфигурация, iikoCloud endpoint'ы | готово |
| [08-courier-service-data-flow.md](08-courier-service-data-flow.md) | модели dict'ов, webhook payload, Google Sheets операции, маппинг колонок, бизнес-расчеты | готово |
| [09-courier-service-migration-mapping.md](09-courier-service-migration-mapping.md) | какие листы сервис пишет, слабые места кода, перенос в `apps/api` | готово |

## Статус разведки

Подтверждено:

- в книге 7 вкладок: `Курьеры`, `Выходы`, `Смены`, `Технический лист`, `График`, `Доставки`, `Статистика`;
- `Технический лист` является служебным/скрытым в XLSX-снимке;
- `Доставки` и `Выходы` дают операционные факты, но не выплаты;
- книга заполняется внешним Python FastAPI-сервисом из `data/raw/courier_service/`, а не Apps Script внутри книги;
- сервис использует iikoCloud / iiko Transport `https://api-ru.iiko.services/api/1`, а не iikoServer Resto;
- исходящие endpoint'ы сервиса: `POST /api/1/access_token`, `POST /api/1/employees/couriers/by_role`, `POST /api/1/employees/info`;
- доставки и смены приходят через inbound webhook `POST /aiko-webhook`;
- сервис пишет только `Курьеры`, `Выходы`, `Доставки`;
- iiko delivery data в основном проекте также выгружается через iikoServer Resto API, включая `/reports/delivery/couriers`, но это отдельный контур для сверки/операционных выгрузок;
- в книге не найден источник ставок/сумм выплат курьеров;
- snapshot книги обрывается по доставкам/выходам 2026-04-21, а май есть только в iiko.

Не подтверждено:

- точные Google formulas на `Статистика` и `Технический лист`;
- data validations, named ranges, conditional formatting;
- systemd/supervisor/container config запуска Python-сервиса на сервере владельца;
- настройки iikoCloud webhook target и его auth/signature;
- endpoint'ы исторического backfill доставок/смен в iikoCloud: найденный сервис их не вызывает.

## Рекомендация

Не копировать старую spreadsheet-интеграцию буквально. Для приложения нужен отдельный слой `iiko_cloud`, прием webhook с idempotency, запись delivery/shift facts в БД, ручной график и correction layer с audit trail. Payroll курьеров включать только после подтверждения ставок/правил владельцем.
