# Слабые места и Migration plan

Документ обновлен после чтения reference snapshot `data/raw/courier_service/`. Пункты про Apps Script и выбор между iikoTransport/iikoServer закрыты: для этой книги найден внешний Python-сервис на iikoCloud `api-ru.iiko.services/api/1`; iikoServer Resto остается отдельным контуром проекта.

## Слабые места текущего контура

| # | Проблема | Доказательство | Риск | Что делать |
| ---: | --- | --- | --- | --- |
| 1 | ~~Не удалось прочитать Apps Script живой Google Sheets-книги~~ Механизм загрузки найден: внешний Python-сервис, не Apps Script | `data/raw/courier_service/TestAppScript.py` пишет через `gspread` | старый риск закрыт; новый риск - ad-hoc сервис без production-обвязки | переносить логику сервиса в backend, Apps Script не ждать |
| 2 | В книге не найдены ставки и выплаты курьеров | `labor_report.md`, `couriers_monthly.csv`: `courier_payout_source_status=not_found_in_sheet` | KPI нельзя превратить в оплату без владельца | подтвердить источник/правило оплаты курьеров |
| 3 | Майских строк в XLSX snapshot нет | `couriers_monthly.csv`: 2026-05 Sheet = 0, iiko COURIER = 909 | Google Sheet snapshot/импорт мог быть обрезан или интеграция сломана | проверить live book и логи Python-сервиса за май |
| 4 | Sheet vs iiko delivery delta большой | 2026-04: Sheet 898 vs iiko 1441 | неполный webhook/import или различие счетчиков | определить канонический order counter и backfill |
| 5 | `/reports/delivery/couriers` нельзя суммировать как факт заказов | endpoint возвращает `AVERAGE`, `MAXIMUM`, `TARGET` | неверная статистика и оплата | использовать только как KPI/SLA, факт заказов брать из webhook/order facts/OLAP |
| 6 | `regions` по delivery пустые | `data/processed/iiko/orders_delivery/quality_risks.csv` | нельзя строить районную аналитику | проверить заполнение районов в iiko |
| 7 | `Смены`/`Выходы` могут содержать ручные корректировки без audit trail | spreadsheet-логика + Python-сервис пишет прямо в `Выходы` | спорные часы и выплаты | в приложении correction layer с автором/причиной |
| 8 | PII курьеров живет в raw iiko, таблице и логах сервиса | сервис пишет ФИО в `Курьеры`/`Выходы`, логирует ФИО/order id | риск раскрытия ПДн | protected lookup + masked analytics + безопасные логи |
| 9 | iiko employees schedule вернул 0 строк в ops-снимке | `data/processed/iiko/ops/report.md` | нельзя заменить Google-график iiko schedule | план вести в приложении, iiko использовать как факт/контроль |
| 10 | ~~Терминологический риск: iikoTransport vs iikoServer~~ API для книги подтвержден | `TestAppScript.py`: `https://api-ru.iiko.services/api/1/...` | старый риск закрыт; новый риск - два разных iiko API в одном проекте | создать отдельный `iiko_cloud` слой, не смешивать с `iiko_sync.py` |
| 11 | Секреты и id hard-coded в snapshot-сервисе | `IIKO_API_LOGIN`, `SPREADSHEET_ID`, `ORGANIZATION_ID`, `service_account.json` | утечка credential и невозможность окружений | вынести в secrets/env, ротировать service account/apiLogin |
| 12 | Нет idempotency/retry/backfill | `OnWay` всегда `insert_row`, retry отсутствует | дубли/дыры при повторных или потерянных webhook | idempotent upsert + import run ledger + backfill |
| 13 | Webhook без auth/signature validation | route `POST /aiko-webhook` принимает JSON list | чужой POST может менять таблицу | webhook secret/signature/reverse proxy allowlist |

## Migration plan

### Целевые сущности БД

| Таблица / сущность | Ключевые поля | Источник | Комментарий |
| --- | --- | --- | --- |
| `employee` | `id`, `full_name`, `iiko_id`, `status` | iikoCloud `/employees/info` + текущий HR контур | ФИО read-only из защищенного слоя |
| `employee_role_assignment` | `employee_id`, `payroll_role='courier'`, `category`, `effective_from/to` | app manual | в текущем `apps/api` роль `courier` не видна в constraints/services; нужна отдельная миграция/проверка |
| `courier_profile` | `employee_id`, `display_name`, `iiko_cloud_employee_id`, `iiko_cloud_courier_id`, `is_active`, `phone_hash`, `notes` | iikoCloud/app manual | замена листа `Курьеры` |
| `courier_shift` | `courier_id`, `work_date`, `opened_at`, `closed_at`, `source`, `status`, `hours`, `quality_status` | iikoCloud webhook/manual | замена `Выходы`; можно объединять с `shift_ledger_entry`, если ledger расширяется под courier |
| `courier_shift_correction` | `shift_id`, `field`, `old_value`, `new_value`, `reason`, `created_by` | manual | audit ручных правок |
| `courier_schedule_entry` | `courier_id`, `work_date`, `planned_status`, `planned_start_at`, `planned_end_at`, `comment` | manual/app | замена `График`; Python-сервис его не пишет |
| `delivery_order` | `iiko_cloud_order_id`, `order_number`, `status`, `created_at`, `on_way_at`, `delivered_at`, `cancelled_at` | iikoCloud webhook/backfill | canonical order fact |
| `delivery_order_courier_assignment` | `order_id`, `courier_id`, `assigned_at`, `delivered_at`, `way_duration_min`, `source` | iikoCloud webhook | замена `Доставки` |
| `courier_kpi_snapshot` | `courier_id`, `period_start`, `period_end`, `orders_count`, `avg_way_time`, `shift_hours`, `metric_source` | расчет/app | материализация `Статистика` |
| `courier_import_run` | `source`, `period_start/end`, `status`, `started_at`, `finished_at`, `raw_ref`, `row_count`, `error` | app integration | мониторинг webhook/backfill/token jobs |
| `iiko_cloud_webhook_event` | `event_id/hash`, `event_type`, `event_time`, `organization_id`, `payload_ref`, `processed_at`, `status` | inbound webhook | idempotency и replay |
| `courier_payroll_rule` | `effective_from/to`, `rate_type`, `amount`, `per_order_amount`, `shift_amount`, `bonus_rules` | owner manual | сейчас не найдено; нужно утвердить |
| `courier_payroll_accrual` | `courier_id`, `period`, `base_amount`, `order_amount`, `bonus`, `deduction`, `total`, `source_kpi_snapshot_id` | app calculation | создавать только после утверждения правил |

### Интеграционные сервисы

| Сервис | Что делает | Частота | Зависимости |
| --- | --- | --- | --- |
| `iiko_cloud_client` | base URL, token, retry, timeout, structured errors | shared | `IIKO_CLOUD_BASE_URL`, `IIKO_CLOUD_API_LOGIN` |
| `iiko_cloud_token_refresh` | получает `POST /api/1/access_token` и кеширует token | proactive + on 401 | iikoCloud credential |
| `iiko_cloud_couriers_sync` | импортирует `/employees/couriers/by_role` как courier profiles | каждые 10-60 минут + manual | organization id |
| `iiko_cloud_employee_lookup` | получает `/employees/info` и кеширует employee names | по событию + batch | employee id |
| `iiko_cloud_webhook_handler` | принимает `DeliveryOrderUpdate`, `PersonalShift`, валидирует, пишет raw event | on webhook | webhook secret/signature |
| `iiko_cloud_delivery_event_processor` | upsert order/assignment status history | async после webhook | idempotency table |
| `iiko_cloud_shift_event_processor` | upsert open/close courier shifts | async после webhook | courier profile |
| `iiko_cloud_backfill` | восстанавливает пропущенные доставки/смены, если API доступен | manual/nightly | endpoint надо подтвердить отдельно |
| `courier_kpi_calculator` | считает KPI из order/shift facts | после sync + nightly | canonical facts |
| `courier_payroll_bridge` | превращает approved KPI/смены в payroll accruals | weekly/monthly | утвержденные правила оплаты |
| `courier_pnl_reconciliation` | сверяет accruals с iiko P&L `Зарплата курьеров` и DDS выплатами | monthly close | P&L, DDS, payroll |

### UI-разделы

| Страница | Показывает | Редактируется |
| --- | --- | --- |
| `Курьеры / Список` | активные курьеры, iikoCloud match, статус, категория/payroll-role | активность, category, mapping, notes |
| `Курьеры / График` | календарь курьеров, план/факт, no-show, нагрузка | плановые статусы, комментарии |
| `Курьеры / Смены` | открытия/закрытия, часы, quality issues | manual corrections с audit |
| `Курьеры / Доставки` | заказы, курьер, статус, время, выручка/номер | correction flags/notes, не raw fields |
| `Курьеры / KPI` | deliveries, avg time, SLA, sheet/iiko reconciliation, trends | фильтры/периоды; KPI read-only |
| `Курьеры / Оплата` | расчетные начисления после утверждения правил | ставки/правила, approve payroll bridge |
| `Интеграции / iikoCloud delivery` | webhook health, import runs, errors, last event, gaps | retry, replay, backfill |

### Связь с существующими модулями

| Модуль | Пересечение |
| --- | --- |
| `Штат` / `employee` | курьер должен быть сотрудником или protected courier profile; ФИО/iiko id не дублировать в открытых таблицах |
| `Учёт смен` / `shift_ledger_entry` | можно расширить общий ledger под courier-role или держать `courier_shift` и создавать ledger entries при payroll bridge |
| `Payroll` | роль `courier` и ставки нужно добавить/проверить до начислений; в книге правил оплаты нет |
| `DDS` | факт выплат курьерам закрывается через cashflow/payroll payment batch, а не через KPI |
| `P&L` | текущий источник строки `Траты на курьерскую службу` - iiko P&L preset `Зарплата курьеров`; courier payroll accrual должен сверяться с этой строкой |
| `Operations` | delivery orders/KPI идут в операционный dashboard и влияют на сервис/retention |

### Очередность реализации

| Приоритет | Шаг | Зачем | Блокеры |
| ---: | --- | --- | --- |
| P1 | Завести отдельный `apps/api/app/services/iiko_cloud/` | iikoCloud `/api/1` не смешивать с iikoServer Resto | env/secrets |
| P1 | Реализовать защищенный inbound webhook с raw event ledger и idempotency | заменить уязвимый `/aiko-webhook` и не терять события | webhook secret/signature |
| P1 | Импортировать courier profiles по `/employees/couriers/by_role` и `/employees/info` | заменить лист `Курьеры` и id/name lookup | PII-safe lookup |
| P1 | Upsert delivery facts из `DeliveryOrderUpdate` | заменить `Доставки` без дублей | stable event/order identity |
| P1 | Upsert shift facts из `PersonalShift` | заменить `Выходы` и видеть незакрытые смены | правила open/close |
| P1 | Создать `courier_schedule_entry` и UI графика | заменить ручной `График`, который сервис не пишет | роли доступа |
| P2 | Backfill/reconciliation для пропущенных webhook | закрыть майские/исторические gaps | подтвердить iikoCloud endpoint'ы |
| P2 | KPI calculator + `Статистика` в backend | перенести dashboard без spreadsheet-формул | canonical delivery/shift facts |
| P2 | Import monitor/replay | чтобы агент видел gaps и мог переобработать событие | integration runs |
| P3 | Courier payroll rules | начать расчет оплат | owner decision по ставкам |
| P3 | Payroll/DDS/P&L bridge | связать операции, начисления и деньги | утвержденные правила и parallel-run |

## Открытые вопросы владельцу / доступам

1. Как на сервере владельца запускается `TestAppScript.py`: systemd, supervisor, Docker, ручной процесс?
2. Как в iikoCloud настроен webhook target и можно ли включить подпись/секрет?
3. Есть ли в iikoCloud endpoint для исторического backfill доставок и смен, доступный с текущим `apiLogin`?
4. Почему лист `Смены` есть в книге, но найденный сервис пишет только `Выходы`?
5. Как live formulas `Статистика` ожидают видеть `Доставки`: courier id или ФИО?
6. Где находится источник ставок и фактических выплат курьеров?
7. Какое каноническое число доставок использовать для оплаты: уникальный order id из webhook, iiko OLAP `COURIER orders`, или другой счетчик?
8. Что означает статус/признак `Помог` для оплаты и KPI?
9. Должны ли no-show/опоздания/длительность доставки влиять на штрафы/премии?
10. Как сверять iiko P&L `Зарплата курьеров` с фактическими выплатами в DDS?
