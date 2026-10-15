# Заказы, продажи и источники базовой экономики

Все пути исходников относительны корню репозитория. Короткое `services/...` означает `apps/api/app/services/...`.
Это карта методов: текущие значения, полнота периода и фильтры iiko должны подтверждаться при каждом расчёте.
Не запускать sync/recompute/export main ради чтения: они могут писать БД или research/raw/processed.

## Период и канонический месячный источник

- Последний календарно завершившийся месяц не обязательно закрыт в учёте.
- GET `/api/v1/accounting/suppliers/periods` возвращает закрытые месяцы; выбрать последний полностью прошедший (`apps/api/app/api/v1/routes/accounting_suppliers.py:2610`).
- GET `/api/v1/accounting/suppliers/periods/readiness?month=YYYY-MM-01` проверяет зависшие начисления услуг, включая просроченные других месяцев; это не полная проверка месяца. GET `/api/v1/reports/pnl?month=YYYY-MM` читает текущую проекцию.
- AccountingPeriodClose — блокировка изменений, не снимок всех чисел (`apps/api/app/models/counterparty_payable.py:1126`). Проверять warnings/NO_DATA и покрытие источников даже у закрытого месяца.
- Канонический источник gross/net/foodcost P&L — сохранённый SALES preset, ID из `services/pnl/iiko_sync.py::REVENUE_PRESET_ID`.
- Чтение: GET `/resto/api/v2/reports/olap/byPresetId/{id}?summary=false&dateFrom=MONTH_START&dateTo=NEXT_MONTH_START`.
- Даты `[from,to)`: конец — первое число следующего месяца (`services/pnl/iiko_sync.py:92 month_bounds_exclusive`, `:238 _fetch_preset`).
- Получить актуальное определение preset через GET `/v2/reports/olap/presets`; сохранить всю группировку, фильтры, агрегаты и дату чтения.
- Код research-экспортёра фиксирует OrderDeleted/DeletedWithWriteoff=NOT_DELETED, но комментарий не доказывает текущие серверные настройки (`integrations/iiko/scripts/export_revenue_by_direction.py:372`).

## Подразделение: существенное ограничение реализации

- `services/pnl/iiko_sync.py:198 aggregate_revenue_rows` **не читает и не фильтрует Department, Department.Id или Department.Code**.
- Он суммирует все строки ответа, у которых CookingPlace удалось сопоставить направлению; unknown CookingPlace пропускаются, включая total.
- Наличие Department.Code в группировке preset не является фильтром нужной точки. Его значения и область preset необходимо проверить явно.
- Mapping: Суши+Специи→rolls; Пицца→pizza; Шаурма→hot_shop; Бар→bar (`:99 map_direction`).
- Старый research-метод `row_is_active_chernikova` дополнительно фильтрует имя/код, но эта функция **не используется** production-агрегатором (`export_revenue_by_direction.py:246`).
- Поэтому независимое ограничение по Department.Code может создать разницу с P&L. Показать обе области и сверку; не объявлять их одинаковыми по имени строки «Черникова».

## Метрики и свежесть зеркала

| iiko | P&L-метрика | Определение |
|---|---|---|
| DishSumInt | revenue_gross | Сумма до скидки; не прибыль |
| DishDiscountSumInt | revenue_net | Сумма после скидки; не выплата банка после комиссий |
| ProductCostBase.ProductCost | food_cost | Учётная себестоимость позиций выбранного отчёта |

- Скидки=gross−net только для одинаковой выборки. Не складывать gross и net и не вычитать скидки из net ещё раз.
- Fallback `sumAfterDiscountWithoutVAT` — поле без НДС; его наличие в коде не доказывает одинаковую VAT-базу с DishDiscountSumInt. Фиксировать реально выбранное поле.
- Чистые читатели: `services/pnl/sources/iiko.py:216 month_facts`, `:229 month_by_direction`; таблица `pnl_iiko_fact` содержит amount/source_ref/previous_amount/rows_count.
- **rows_count — число строк источника, не заказов** (`services/pnl/iiko_sync.py:347`).
- **synced_at в pnl_iiko_fact — timestamp INSERT, а не последнего успешного refresh.** Модель задаёт только `server_default=now()`, без onupdate (`apps/api/app/models/pnl.py:382`).
- `sync_month` обновляет amount/rows_count/source_ref, но не synced_at (`services/pnl/iiko_sync.py:345–380`).
- Helper `sources/iiko.py:245 synced_at` возвращает max этого поля; несмотря на docstring, он не подтверждает свежесть месяца.
- Свежесть подтверждать отдельно временем успешного прогона в доступных логах/журнале и датой сохранения фактического ответа, с проверкой покрытия и ошибок; не выводить её из synced_at.

## Безопасное проектирование дополнительного OLAP

1. Прочитать существующий preset и metadata GET `/v2/reports/olap/columns?reportType=SALES` (`integrations/iiko/scripts/export_sales_day_hour.py:444`).
2. Использовать только наблюдённые поля, разрешённые для grouping/aggregation, и точные типы/значения фильтров. Не угадывать идентификаторы и enum.
3. Сохранить исходные фильтры preset; заменить только период и добавить нужные разрезы в отдельный reporting-запрос. Не редактировать saved preset на сервере.
4. Для v2 POST `/v2/reports/olap` построить read-only отчёт из наблюдённой metadata; документировать отличия от canonical. Не копировать legacy from/to без проверки границ.
5. Проверить малый диапазон/сверку с тем же preset; затем целый месяц. Отсутствующий день/поле или ошибка парсинга — missing, не автоматический0.
6. Для canonical дневной выручки можно читать тот же preset на `[day,day+1)`, не меняя его группировку и фильтры.

- Payroll-запрос `services/iiko_revenue.py:54` фильтрует дату+Department, но не OrderDeleted/DeletedWithWriteoff. Его выручка не доказанно равна P&L-популяции.
- Старые day/hour/orders/foodcost exporters используют raw `/reports/olap` без canonical фильтров. Их нельзя просто объединить с месячным P&L.
- Эти scripts пишут файлы и могут выгружать персональные поля; использовать их как рецепты, а сбор сохранять только в разрешённое приватное место с минимальными полями.

## Заказы, завершение и возвраты

- Экономическая популяция — учтённые SALES canonical preset по его временному фильтру (например SessionID.OperDay); операционная — завершённые доставки по подтверждённому статусу/времени. Не считать их тождественными. OpenDate.Typed может отличаться от учётного дня.
- Предпочитай наблюдённый агрегат `UniqOrderId` на недробящем разрезе день+точка или distinct стабильного GUID `UniqOrderId.Id`. Первый — количество, второй — идентификатор группировки; проверить metadata и фактический ответ.
- Проверенный дневной saved preset `8c13763a-35bf-9f27-017f-5468b1e7001b` «Выручка по дням» использует OpenDate.Typed и агрегат UniqOrderId. При каждом чтении заново проверить фильтры и сверить сумму gross/net с основным месячным пресетом.
- Legacy-экспортёры трактуют агрегированный OrderNum как количество, но metadata называет поле «Номер чека». Не переносить эту трактовку на произвольный отчёт без контроля GUID. Суммы заказов по SKU/цехам/PayTypes могут задвоить заказ. DishAmountInt — штуки блюд, GuestNum — гости.
- Средний чек=Σnet/N для одной популяции; не среднее средних. При N=0 показатель не определён.
- Возвраты/сторно хранить со знаком и датой источника; отдельно диагностировать OrderDeleted,DeletedWithWriteoff,Storned,Delivery.CancelCause,RemovalType,DishReturnSum.withoutVAT.
- Не исключать все refund-строки и называть результат net; не вычитать возврат повторно из уже уменьшенной выручки.
- Legacy cancellation loss estimate с abs/max — диагностическая оценка, не готовый дополнительный P&L-расход (`export_orders_delivery.py:1347`).
- Контроли: sale/return/cancel/unknown, нулевые/отрицательные суммы, отсутствующие GUID, дубли, покрытие дней/каналов.

## Доставки, SKU и нагрузка

- GET `/api/v1/couriers/iiko-deliveries?from=...&to=...&limit=1000&offset=0` читает сохранённые строки; доступны service_type и include_cancelled; окно≤92дней (`routes/couriers.py:325`).
- `include_cancelled=false` оставляет NULL/Open/OnWay/Unconfirmed; это не completed-only. Items требуют пагинации; summary сверять независимо.
- `delivery_order` имеет два загрузчика: `services/courier_sync.py:344` ограничивает COURIER/точку; `services/couriers/iiko_olap_sync.py:179` читает DELIVERIES без Department/ServiceType-фильтра. Полнота продаж и единство точки не гарантированы.
- Проверять provenance, тип услуги, GUID, timestamp и status. Department отдельным столбцом не хранится; вторая выгрузка его не запрашивает.
- SKU-рецепт: `integrations/iiko/scripts/export_ops.py:1642` — DishName/категория/группа, DishAmountInt, суммы, ProductCostBase.ProductCost. Добавлять DishId/CookingPlace/SoldWithDish только при подтверждённой metadata.
- Для одной выборки SKU: цена=Σnet/units; cost/шт=Σcost/units; вклад=Σnet−Σcost; foodcost%=Σcost/Σnet. Это не текущая цена меню и не полная прибыль.
- Сет может иметь revenue на родителе и cost на дочерних SoldWithDish-строках. Переносить cost для анализа контейнера один раз, не увеличивая общий foodcost.
- Phantom units из техкарты полезны для нагрузки, но не новые продажи/выручка/повторная себестоимость; старые processed меню не заменяют текущий месяц.
- HourOpen — приход спроса, не час приготовления; fallback CloseTime меняет смысл оси. Фиксировать используемое поле (`export_sales_day_hour.py:479`).
- Сопоставлять заказы/блюда/mix по часу с закрытыми фактическими интервалами работников по цехам; план и открытая явка не равны факту.
- Готовой доказанной мощности кухни из выручки нет: нужны время операций по SKU, доступные люди/оборудование, партии и SLA. Средний ФОТ/выручка не порог мощности.

## Тарифы и настройки: текущие и на дату

| Чистый GET `/api/v1/...` | Использование |
|---|---|
| payroll/config/rates?history=true | Все версии ставок; выбрать по effective date |
| payroll/config/availability | Текущая доступность роль×категория, отдельно от history rates |
| payroll/config/revenue-tiers?history=true | Общий процентный пул |
| payroll/config/category-coefficients?history=true | Веса распределения пула |
| payroll/config/seniority-premium?history=true | Версии Старший/Зам |
| settings/{key}, settings/{key}/history | Текущее value/widget_options и история изменений узкого ключа |

- Источник маршрутов: `apps/api/app/api/v1/payroll_config.py:63/105/176/210/278`, `routes/settings.py:97/138`; чтение без commit.
- Нет as_of: дляD применять effective_from≤D<effective_to; NULL конец=без конца. Current GET использует date.today(), не дату закрытого месяца.
- History rates не присоединяет availability; default is_enabled в response не доказывает доступность. AppSetting.changed_at — время редактирования, не бизнес-effective date.
- Узкие ключи: payroll.weekday_premium (+widget_options), payroll.fund_rates_by_tenure, payroll.category_rules, payroll.ndfl; по нужде daily_revenue_by_date, mock_daily_revenue, deposit_auto_withholding_enabled, deposit_fund_payment_date, deposit_write_offs; отдельно vacation.daily_amount.
- Legacy revenue-share не заменяет revenue-tiers/category-coefficients. Для факта месяца использовать finalized components/календарный P&L, не сегодняшние ставки.

## Чистое чтение смен и запреты на скрытые записи

- GET `/api/v1/schedule?status=...&date_from=...&date_to=...` и `/schedule/{id}` — SELECT планов; `/schedule/forecast?...` — сохранённый прогноз.
- Факт — заранее полученный snapshot либо разрешённый SELECT attendance_entry/shift_ledger_entry; отдельного чистого production GET явок в проверенной зоне не найдено.
- GET `/shifts/ledger` и `/shifts/ledger/matrix` могут resolve+commit; GET `/schedule/living` может создавать/расширять график. Исключить из read-only (`services/shift_ledger.py:194/251`, `shift_schedule_service.py:200`).
- GET `/shifts/ledger/bonuses` делает live iiko-запрос; это preview, не accrual. Не вызывать sync/recompute/finalize/ensure ради сценария.
- Итоговая сверка: одинаковые период/точка/filters, Σдни/ΣSKU↔canonical месячная сумма, N без дублей, refunds один раз, ФОТ календарными начислениями, все missing видимы.
