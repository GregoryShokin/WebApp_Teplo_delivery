# iiko: источники клиентского анализа

Проверено 11.10.2026. Маркируй evidence level: `fresh-live` — чтение реального Teplo API 11.10; `official-contract` — свежая официальная OpenAPI; `repo-static` — код; `dated-local` — исторический файл. Наличие поля в схеме не доказывает его заполненность у всех клиентов.

## Транспорт и безопасное чтение

- `apps/api/app/services/iiko_cloud_client.py:21–98`: база `https://api-ru.iiko.services`; `iiko_opener()` обходит банковский proxy; `iiko_auth_token()` POST `/api/v2/access_token` с `{appId,apiKey,clientSecret}` из `IIKO_CLOUD_*`; `iiko_cloud_call(path,payload,token=...,opener=...)` возвращает `(status,payload)`. Транспортный сбой возвращает status0, HTTPошибка — её код, это не пустая выборка. Один token на серию запросов.
- `integrations/iiko/scripts/export_orders_delivery.py:51–73,103–205`: `load_local_env()` и `IikoClient` для Server; local env читается без печати значений. На production `apps/api/app/services/iiko_sync.py:220` `_load_source_credential_env` гидратирует SourceCredential; при доступе БД использовать read-only transaction/rollback, не sync jobs.
- POST у iiko — также метод чтения отчётов; разрешение определяется маршрутом. Не вызывать customer/create_or_update, card/category/program add/remove, wallet hold/cancel_hold/topup/chargeoff.
- Скрипты export_* целиком запускают запись raw/processed в checkout. Для исследования импортировать клиент и делать явные запросы с отдельной папкой вывода; сохранять sanitized manifest без credentials/телефонов/индивидуальных балансов.
- Справочник 17.06 `business-docs/marketing/data-channels.md:12–20,164` устарел: нынешний transport OAuth v2, а «Cloud не отдаёт балансы» неправда для loyalty API (верно лишь про структуру delivery order).

## iikoCard: профиль, бонусы и связь идентичностей

### Профиль — fresh-live + official-contract

POST `/api/1/loyalty/iiko/customer/info`.

```json
{"organizationId":"<org>","type":"phone","phone":"<known-customer-phone>"}
```

После разрешения личности можно `{organizationId,type:"id",id:<loyalty_customer_id>}`. Также контракт поддерживает type cardTrack/cardNumber/email; каждый имеет свой именованный критерий. Restriction group `Guests: info`.

Ответ: `id`, `referrerId`, `phone`, `name/surname`, `cards[]`, `categories[]`, `walletBalances[]`, `whenRegistered`, `firstOrderDate`, `lastProcessedOrderDate`, `lastVisitedOrganizationId`, `registrationOrganizationId`, `isDeleted`, `anonymized`; отдельно `consentStatus`, `shouldReceivePromoActionsInfo`, `shouldReceiveLoyaltyInfo`, `shouldReceiveOrderStatusInfo`, сроки personal-data consent/processing. Поля name/comment/email/birthday/userData брать только при необходимости, не переносить в общий skill. Согласие на обработку и информационные флаги не считать одним универсальным согласием на любой канал.

`walletBalances[]`: `id` (wallet), `name`, `type`, `balance`. Wallet types по контракту: 0 deposit/corporate nutrition, 1 bonus, 2 products, 3 discount, 4 certificate. Баланс — состояние на момент чтения, не начисление за период и не прибыль.

**Проверено 11.10 на одном существующем госте:** идентификатор `delivery.order.customer.id` из архивного заказа НЕ совпал с loyalty `customer/info.id`. Запрос type=id с delivery ID вернул400 `Transport_WrongCustomerId`/`Customer_CustomerNotFound`. Запрос по телефону из того же заказа вернул200 и другой loyalty ID, один wallet с балансом и даты первой/последней покупки. Это подтверждает доступность источника и необходимость bridge, не полную связность всей базы.

Хранить отдельные пространства ключей: `delivery_customer_id`, `loyalty_customer_id`, `starter_customer_id`, canonical internal person/customer key. Сохранять связь, метод, дату и неоднозначность. Нормализованный телефон — вспомогательный bridge, не безусловное доказательство одного человека (семейные телефоны, дубли, агрегаторные подмены). Имя/адрес не использовать как автоматический join.

`whenRegistered` / OLAP CustomerCreatedDate не равны первой покупке. `firstOrderDate`/`lastProcessedOrderDate` профиля отражают лояльность в её scope; проверить организацию и полноту каналов перед сравнением с OLAP. Ни одного списка всех клиентов или универсального RFM метода в проверенных customer routes Cloud не обнаружено: базовую популяцию собирать из OLAP/Starter, известные профили обогащать точечно.

### Бонусные транзакции — fresh-live + official-contract

POST `/api/1/loyalty/iiko/customer/transactions/by_date`:

```json
{"organizationId":"<org>","customerId":"<verified-loyalty-id>","dateFrom":"2026-09-01 00:00:00.000","dateTo":"2026-09-30 23:59:59.999","pageNumber":0,"pageSize":100}
```

Даты **UTC**, обе границы включительны; не переносить OLAP exclusive-to. `pageNumber` начинается с0. Сервер может проигнорировать запрошенный pageSize выше своего максимума, поэтому учитывать response.pageSize. Ответ `{transactions,pageSize,pageNumber}` без total. Вычитать страницы до пустой/неполной по фактической pageSize, проверять продвижение, dedup `transactions[].id`; при наполнении во время выгрузки применять фиксированный cutoff/повторный контроль. Большой период делить; окна с общей включительной границей дедуплицировать.

Альтернатива POST `/api/1/loyalty/iiko/customer/transactions/by_revision` `{organizationId,customerId,revision,lastTransactionId?,pageSize}`. `lastTransactionId` без revision нельзя; transaction ID исключается, revision включается если lastTransactionId задан. Response `{transactions,lastRevision,lastTransactionId,pageSize}`. Сохранять оба cursor поля, dedup ID, проверять продвижение; не выдумывать числовой server max.

Item: `id`, `revision`, `type`, `typeName`, `sum`, `whenCreated` UTC, `balanceBefore/After`, `walletId`, `programId`, `marketingCampaignId`, `organizationId`, `terminalGroupId`, `posOrderId`, `orderNumber`, `orderSum`, `whenCreatedOrder` UTC, `isDelivery`, `isIgnored`; coupon/certificate детали могут присутствовать. Типы и знаки сохранять; не считать любое начисление заказом, любые движения списанием или любую транзакцию расходом по номиналу. `posOrderId` — кандидат join на POS order ID, `orderNumber` не глобальный ключ.

Проверка11.10: с разрешённым loyalty ID, период01.05–01.06UTC,page0,size1 получена200 и одна транзакция с перечисленными полями. Проверена доступность и зерно, **полная пагинация не проверялась**. С неправильным delivery customer ID другой запрос вернул200/[]; пустой ответ не доказывает отсутствие истории, пока не верифицирован ID.

## iikoCloud: заказы и события обслуживания

POST `/api/1/deliveries/by_delivery_date_and_status` `{organizationIds:[...],deliveryDateFrom,deliveryDateTo,statuses?,sourceKeys?,courierIds?}`. Даты локальные для delivery terminal, формат `yyyy-MM-dd HH:mm:ss.fff`. Свежая OpenAPI гарантирует доступность только последних **7дней**. В этом request нет offset/pageSize. `tmp/fetch_june_2026_site_app_revenue_cloud.py:60–92` — repo-static образец коротких дневных интервалов и дедупликации, не доказательство текущего многомесячного архива или полноты.

История POST `/api/1/deliveries/history/by_delivery_date_and_phone`: обязательны `phone,organizationIds,rowsCount`; `deliveryDateFrom/To` local nullable; детали хранятся **90дней**; rowsCount maximum200. `startRevision` exclusive, описание ограничивает revision offset3часами. Это не offset-пагинация; поведение курса и полноту нужно проверить, не считать первые200 полным архивом. Исторические локальные выгрузки могут быть старше онлайн гарантии и имеют самостоятельную дату снимка.

Ответ `{correlationId,maxRevision,ordersByOrganizations:[{organizationId,orders:[...]}]}`. Wrapper заказа: `id` Cloud delivery ID, `posId` POS ID, `organizationId`, `externalNumber`, `timestamp`, `creationStatus`, `errorInfo`, `order`. `order` null допустим, наполнен после creationStatus Success. **Сохранять wrapper**, не только вложенный order.

В order: `customer` (regular/one-time), `phone`, `status`, `cancelInfo`, `completeBefore`, `whenCreated`, `whenConfirmed`, `whenPrinted`, `whenCookingCompleted`, `whenSended`, `whenDelivered`, `whenClosed`, `whenPacked`, `cookingStartTime`, `isAsap`, `deliveryDuration`, `deliveryZone`, `marketingSource`, `sourceKey`, `orderType`, `terminalGroupId`, `sum`, `processedPaymentsSum`, `items`, `combos`, `payments`, `discounts`, `loyaltyInfo`, parent/moved-from/moved-to IDs. One-time customers/агрегаторы могут не иметь устойчивого профиля лояльности.

- Enum актуального контракта: `Unconfirmed,WaitCooking,ReadyForCooking,CookingStarted,CookingCompleted,Waiting,OnWay,Delivered,Closed,Cancelled`. Closed — завершённый заказ, Delivered не равнозначен Closed; creationStatus — другая ось. Отмены отдельно, включая переезды между точками, возвраты сверять OLAP. В описании cancelInfo встречается Canceled с одним l, но enum — `Cancelled`; парсер должен использовать реальный enum и сохранять неизвестное.
- Cloud `isDeleted` deprecated. Архивный local sample24.05:67 вложенных заказов,66Closed,все67isDeleted=true. Нельзя удалить всю такую историю, приравняв это поле к OLAP OrderDeleted: сначала сверка смысла/учётного факта. Тот архив flat array потерял wrapper id/posId — строгий join из него уже невозможен без отдельного маппинга.
- Сумма `order.sum` — после скидок/надбавок, но может включать оплату бонусами. `items[].cost` — стоимость продажи строки, не ingredient food cost. Сверять платежи, бонусы и OLAP по ID перед расчётом оплаченных денег; не вычитать бонусы дважды.
- Даты операционных отметок в основном local terminal; `whenReceivedByApi/FromFront` (deprecated) — UTC. Не вычитать без приведения TZ.
- Владелец17.06 отметил задержку ручного нажатия «доставлен»; предзаказы отдельно. `whenDelivered` и completeBefore не доказывают точную фактическую задержку. Исторические84мин/67.6% в diagnostic.md:60 отменены поправкой:61. Не делать причинный вывод об оттоке из неподтверждённых таймштампов.
- SourceKey `site` покрывал и app. Канал определять по подтверждённому mapping marketingSource+Starter, неизвестные не приписывать сайту. Комментарий заказа не является отзывом; feedback/review/rating в обследованных44raw delivery файлах не найдено (`business-docs/marketing/customer-research/2026-10-08/own-review-evidence.md:10`). Отсутствие полей в этих файлах не утверждение обо всём Cloud.

## iikoServer OLAP: долгая история, корзины, расходы

- GET `/v2/reports/olap/columns?reportType=SALES` и `/v2/reports/olap/presets` сначала. POST `/v2/reports/olap` — чтение отчёта. `apps/api/app/services/iiko_revenue.py:54–73` содержит body, `OpenDate.Typed` range.from включительно/to следующий день исключён. Месячные chunk, фиксированная точка/TZ/полные дни; не переносить формат границ на `/reports/olap` без проверки.
- Fresh-live11.10 metadata200: `UniqOrderId.Id` = ID заказа, grouping/filtering true, aggregation false. `UniqOrderId` = Чеков, только aggregation. Order count нельзя суммировать по строкам SKU: уникальные order IDs/отдельный контроль.
- `Delivery.CustomerPhone`, `Delivery.CustomerName`, `Delivery.CustomerCreatedDateTyped` доступны group/filter; последнее «Дата создания клиента», не покупка. `OpenDate.Typed` назван **«Учетный день»**, не timestamp открытия. `Delivery.Number`, `OriginName`, `DishId`, `SoldWithDish.Id` также group/filter.
- `DishDiscountSumInt` «Сумма со скидкой», `Bonus.Sum` «Сумма бонуса», `DishReturnSum`, `DishReturnSum.withoutVAT` доступны; `DishReturnSum.withoutVAT` явно безНДС, не приравнивать его к базе других сумм.
- `OrderDeleted`, `DeletedWithWriteoff`, `Storned` metadata enum group/filter, значения читать из нынешней выборки/пресета, не выдумывать. `DeletedWithWriteoff` «Блюдо удалено», `Storned` «Возврат чека». Выделять signedreturns, cancellation и completedpaidfacts; не отсекать нулевые строки (подарки/компоненты несут затраты).
- `skills/teplo-rolls-analysis/references/sales-and-sets.md:30–91` — канон сверки и количества, :95–110 — контейнеры/компоненты сетов. `apps/api/app/services/pnl/iiko_sync.py:55` canonical revenue preset, `_fetch_raw:1024` reader. `skills/teplo-unit-economics` — полные переменные затраты; OLAP food cost не полная маржа клиента.
- Employee/test/service orders отделять по подтверждённому mapping, не по низкому чеку/100%-bonus на глаз. `business-docs/marketing/roadmap.md:63` поправка22.06:63% старого 100%-bonus хвоста — сотрудники. Старый ярлык «анти-персона» в той же строке не переносить.

## RFM: что уже существовало и что не доказано

`business-docs/marketing/diagnostic.md:33–40` — **историческая** сводка01.01.2025–15.06.2026,9367гостей,медиана интервала21день,сегментные числа; не текущая база. `push-content-library.md:30–40` различает iiko population по телефону (все доступные каналы) и Starter clusters (собственная популяция/границы). Названия сегментов нельзя механически сопоставлять.

Найдены в main ignored `research/raw/iiko/orders_delivery/olap_rfm_daily_*.xml` и `olap_guest_retention_*.xml`. Доступные raw поля sample: `OpenDate.Typed,OrderNum,Delivery.CustomerPhone,DishDiscountSumInt`. Ссылка на `research/processed/iiko/guest_retention/rfm_summary.json` есть в документах, но сам файл и воспроизводимый RFM scoring код в проверенных main/sibling research/integrations/tmp не найдены. **Числовые границы R/F/M не восстановлены**, не выдавать интервал31–90/91–180дней из маркетингового текста за полный scoring или действующую настройку Starter.

Новая сегментация должна хранить as_of, lookback, eligibleorderdefinition, observedfirst/lastdate, источник identity, покрытие/цензурирование. Для приоритета бывших постоянных нужен priorhistoricalF/M и личный interpurchaseinterval, не только текущий RFM. Первое наблюдение в ограниченном окне не доказывает acquisitiondate; отсутствие после cutoff не причина ухода.

## Evidence manifest

- Официальная [iikoCloud OpenAPI](https://api-ru.iiko.services/api-docs/docs), fresh HTTP20011.10, 2,342,269bytes; путь API сам отдаётJSONOpenAPI, web tool не открыл, прямой публичный GET работал. Схемы `GetCustomerInfoRequest/Response`, `GuestBalanceInfo`, `GetTransactionsReportByPeriodRequest/Response`, `GetTransactionsReportByRevisionRequest/Response`, `TransportTransactionsReportItem`, `OrdersByDeliveryDateAndStatusRequest`, `OrdersHistoryByDeliveryDateAndPhoneRequest`, `OrderInfo`, `Order`.
- Минимальная проверка 11.10.2026: metadata200; customer/info с delivery ID400; по телефону200 и другой ID; транзакции с правильным loyalty ID200/одна строка. Сохраняются только результаты и способ повторения, без контактов/индивидуальных ID/балансов/текстов клиента.
- Массовая выгрузка, полный join Starter/Cloud/OLAP и свежий RFM **не выполнены** в этой проверке; доступность поля/одной записи не равна полному историческому покрытию.
