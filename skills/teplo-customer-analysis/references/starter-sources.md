# Starter: источники клиентского анализа

Проверено 11.10.2026. **Fresh schema-only**: публичный [OpenAPI tenant Тепла](https://crm.starterapp.ru/fmteplo/api/v1/openapi.json) прочитан без авторизации: HTTP 200, application/json, 911152 байта, 373 пути, `info.version=0.1.0`, SHA-256 `f89fd0b0163c6b997ee76ce1b57bdf1cd7cd21f138b4791ac9ec7441287d8721`. Успешный ответ спецификации не подтверждает успешное чтение клиентов/право кабинета или заполненность поля. Актуальные личные данные в этом исследовании не выгружались. Снимок схемы использован для проверки; при новом запуске читай свежую спецификацию по ссылке.

Все пути ниже относительно `https://crm.starterapp.ru/fmteplo` (`servers[0].url=/fmteplo`). Раздел `/api/v1/frontend/` далее обозначен `v1`, `/api/v2/frontend/` — `v2`. Проверять актуальную схему при повторении. Источник точного поля — `components.schemas.<имя>`, метода — `paths.<путь>.<метод>`. Не считать старое число 282 путей из `business-docs/marketing/data-channels.md:41` актуальным.

## Что брать и зачем

| Задача | Метод и response schema | Поля и границы |
|---|---|---|
| Список клиентов, первичная выборка | `POST v2/customers`, тело `CustomerBodyFilters`; ответ `GetCustomersFrontend` — **массив** `CustomerInListFrontend` | `id`, `ordersTotalSum`, `ordersCount`, `rfm`, `score`, nullable `recency`, `frequency`, `monetary`, `balance`, `balanceSpent`, `bonusProgramAccountsBalance`, `bonusProgramAccountsSpent`, `createdAt`, nullable `earliestDeliveryTime`. Есть телефон/имя — только для закрытой связки, в отчёт не нужны |
| Контроль полноты клиентов | `POST v2/customer/total`, то же тело и query `shop_id` | `GetTotal`: `total`, `totalActive`, `percentOfActive`. Значение «активный» схемой не определено |
| Штатный экспорт клиентов | `POST v2/customer/export`, то же тело | Право `export-customers:view`; формат ответа в схеме `{}`, полнота/PII/соблюдение пагинации не установлены |
| Деталь клиента | `GET v1/customer/{customer_id}` | `CustomerFrontend`: `firstOrder`, `lastCheck{price,date}`, `avgCheck`, `totalCheck`, `mostPopularDishes[{index,title,quantity}]`, `mostFavoriteCategories[{index,title,rateCategory}]`, `createdAt`, `earliestDeliveryTime`, `rfm`, `segment`, `coreId`, `isDeleted`, статусы лояльности, `marketingConsent`, `allowDistribution`, `consentStatus`, `phoneStatus`, `language`. Есть `tokens` и сторонние account-поля — не сохранять полностью |
| История заказов одного клиента | `GET v1/customer/{customer_id}/orders?status=done_orders`, отдельно `/orders/total?status=done_orders` | Список **`{items:[OrderFrontendInCustomer]}`**; total `{total}`. Обязательный enum `OrdersGroup`: `done_orders`, `in_progress_orders`, `canceled_orders`. `offset=0`, `limit=50`, допустимо 1–100. Сохранять отмены отдельно, не превращать их в повторные покупки |
| Все заказы для воспроизводимого RFM | `GET v1/orders`, `GET v1/order/total` | Список **массив** `OrderFrontend`; total `{total,totalSum}`. Фильтры `statuses`, `shop_ids`, `corners`, `discount_ids`, `promos`, `date_min/date_max`, `creation_date_min/max`, `search`, query `shop_id`; `offset=0`, `limit=50`, `order_by=-delivery_time`. Смысл date_min vs creation_date и включение границ надо подтвердить; временное поле не выбирать по догадке |
| Детальная корзина и события | `GET v1/order/{order_id}`; альтернативно `GET v1/order?code=<starterId>` | `OrderSingleFrontend`: см. денежные поля ниже; `orderItems`, `comboGroups`, `discounts`, `payments`, `feedback`, `createdAt`, `submittedDate`, `doneAt`, `canceledAt`, `updatedAt`, `deliveryTime`, `deliveryTimeTo`, `deliveryTimeStamps`, `cookToTime`, `isLate`, `source`, `customer`, `shop` |
| История статусов заказа | `GET v1/order/{starter_id}/history` | **Массив** `State{status,receivedAt,author,corner,id}`. Параметр здесь starter_id, а в detail — order_id. `receivedAt` — время получения события, не обязательно физического прибытия |
| RFM/кластеры | `GET v1/clusters?shop_id=...` | `GetClustersFrontend{clusters,labels,lostCluster,dataSeries}`. `ClusterFrontend{id,title,activeCustomersCount,segments,totalSum,customersCount}`; `SegmentFrontend{id,recency,frequency,monetary,title,description,totalSum,customersCount,activeCustomersCount}`. Порогов, горизонта расчёта и истории изменения меток в схеме нет |
| Сохранённые сегменты | `GET v1/customer/segments` или `GET v2/customer/segments`; `GET v1/customer/segments/total`; detail `GET v1/customer/segment/{segment_id}` | Список `{segments:[...]}`, `offset=0`, `limit=50` (1–100), `shop_id`; detail `CustomSegmentFrontend` содержит title/id и фильтры. В total **нет shop_id**: сравнивать знаменатели осторожно |
| История клиента/контактов | `GET v1/customer/{customer_id}/history` | `{history:[HistoryFrontend]}`; `historyLimit=10`, `historyPage=1`, total не описан. Поля `id,at,event,source,relatedObjectId,starterOrderId,posOrderId,details`; enum включает `create_order`, `message_is_sent`, `notification_is_read`, `notification_is_follow`, `notification_created`, `feedback_created`, `answer_created`, `loyalty_level_upgraded/downgraded`, события авторизации/блокировки. Наличие enum не доказывает фактическую полноту истории |

### Фильтры клиентов

`CustomerBodyFilters` необязательные поля: `customer_orders_count_min/max`, `customer_recency_min/max`, `customer_monetary_min/max`, `customer_orders_total_sum_min/max`, `balance_min/max`, `bonus_program_accounts_balance_min/max`, `created_at_min/max`, `birthday_min/max`, `shop_ids`, `corners`, `cities`, `promos`, `discount_ids`, `phones`, `sex`, `marketing_consent`, `customer_rfm`, `customSegmentId`, `extraFilters`, `search`; пагинация `offset=0`, `limit=50`, `order_by=-created_at`. `shop_id` отдельно query integer/null. Не подменять `shop_id` UUID публичной витрины.

**Важное отличие M:** описание `customer_monetary_min/max` буквально говорит «минимальный/максимальный средний чек пользователя». `customer_orders_total_sum_min/max` — отдельная сумма заказов. Поэтому значение `monetary` Starter нельзя автоматически назвать суммарной выручкой RFM. Список хранит `frequency` number, `ordersCount` integer, `score` integer, а сегмент — R/F/M integer. Пока не подтверждены UI/алгоритм, сохранять сырые значения и `native_starter_rfm` отдельно от собственного расчёта `rfm_as_of`.

Есть v1 GET `/customers`, `/customer/total`, `/customer/export` с аналогичными query-фильтрами. Для v2 POST список и total — документированные **читающие** операции. При пагинации повторять один и тот же фильтр, явно ограничивать размер страницы; null limit не считать доказанным «всё». Массив query не превращать в `[]`-имя параметра без проверки кодирования.

## Денежные поля: сначала сверить семантику на заказе

Схема не описывает валютную единицу/алгебру сумм достаточно, чтобы считать их бухгалтерским фактом без сверки.

- `OrderSingleFrontend`: `price`, `totalPrice`, `bonuses`, nullable `discountPrice`, nullable `sumToPay`, `serviceFeePrice`, `serviceFees`, `mainBonusProgramAccountSpent`, `bonusProgramAccountsSpent`, `paymentMethod`, `paymentStatus` string.
- `OrderItemsSchema`: `code` integer, `price`, `discountPrice`, `totalPrice`, `quantity` integer, `toppings[{topping,quantity,price}]`, `menuItem{code:string,status,name,description,weight,sortIndex,ingredients}`, `comboGroupId`, `comboName`, `comboAxes`, `comboDiscount`, `comboItemPrice`.
- `OrderDiscountSchemaFrontend`: `id`, `name`, `sum`, `type` (`discount`, `promocode`, `increase`, `doubleMeal`), nullable `promocode`, nullable `meal`. В **списке** заказов `OrderDiscountFrontend` содержит только nullable `code`, поэтому одной list-строки для экономики недостаточно.
- `PaymentInOrderFrontend`: безопасная аналитическая проекция `id,orderId,paymentId,paymentSystem,status,amount,originalAmount,createdAt,paidAt,type,bankFee,serviceFeeAmount,receipts,items`. Статус enum **`INITIATED`, `PAYED`, `RETURNED`, `CANCELED`**. `type=MANUAL/AUTO` не является методом оплаты. Не сохранять PAN/expDate/cardId/paymentUrl/email/phone; ответ содержит их поля.
- Успешная онлайн-транзакция, возврат, наличная оплата и оплаченная бонусами часть — разные факты. Список payments может не описывать весь денежный поток всех способов оплаты. Нельзя считать каждую попытку оплаты новой выручкой, складывать originalAmount с amount или повторно вычитать bonuses из суммы, уже их учитывающей. Сверять с iiko и его оплатами/выручкой; частичные возвраты/переоткрытие чека требуют проверки.
- `CustomerInList.ordersTotalSum`, `CustomerFrontend.totalCheck/avgCheck`, `Order.totalPrice` — агрегаты разных уровней с неизвестным горизонтом и составом. Хранить раздельно от вычисленного `paid_cash_revenue_in_window`.
- Стоимость доставки клиенту в этой схеме не выделена очевидным полем `deliveryPrice` в `OrderSingleFrontend`; смотреть фактическую корзину/сборы/iiko. Её нельзя подменять оплатой курьеру. `serviceFeePrice` не назвать доставкой без проверки.
- `mostPopularDishes` — ранжированный текущий агрегат без срока и стабильного SKU GUID; `index` не считать ID блюда. Точные предпочтения до ухода пересчитать по датированным orderItems, разделяя покупку и подарок.

## Лояльность

1. `GET v1/customer/{customer_id}/transactions`, `offset=0,limit=50` (1–100); `/transactions/total` возвращает `{total}`. Ответ `{transactions:[LoyaltyTransaction]}`: `id`, `sum`, `status`, `cancelledAt`, `comment`, `orderSum`, `balanceAfter`, `createdAt`, `iikoCreatedAt`, `type`, `starterOrderId`, `posOrderId`, `orderId`, `orderNumber`, `crmSum`, `iikoBalance`, `confirmFromIikoAt`, `bonusProgramAccountId`. Это лучший из найденных контрактов-кандидатов для связи бонусной операции с заказом; live полнота не проверена.
2. `GET v1/customers/{customer_id}/main-bonus-program-transactions`: `offset=0`, `limit=20`, диапазон 1–100, `shop_id`. `/bonus-program-transactions`: `restaurant_account_id` UUID optional, `shop_id`, **нет описанной offset/limit пагинации**.
3. Оба из п.2 отвечают `BonusProgramTransactionsResponse{total,transactions}`. `total{balance,selectedBonusProgramAccount,totalCount?}`; `transactions[{id,createdAt,type,amount,balanceAfter,comment,bonusProgramAccount}]`. Не заявлять, что этот DTO содержит orderId — его там нет. Nullable totalCount не трактовать нулём.
4. Provider iikoCloud и selfRefill/selfWithdraw выключены — **dated live 07.10**, не свежий факт: `skills/teplo-starter-promotion-capabilities/references/current-constraints-2026-10-07.md:14–19`. +100 нельзя автоматически считать retention, нужны происхождение/правило/событие.

## Отзывы и контакты

- `GET v1/feedback/filters`, `/feedback/tags`; `GET v1/feedbacks` → массив `FeedbackFrontend`, `offset=0,limit=50,order_by=-created_at`; `/feedback/total` → `{total,averageRating}`. Фильтры `shop_id,shops,cities,statuses,ratings,tag_ids,date_min,date_max,has_reply,search`.
- Поля `id,createdAt,status,rating,comment,tags,answer,pointsAwarded,customer.id,order.id,order.starterId,order.posId,order.number,order.deliveryTime,order.shop`. `FeedbackStatus=accepted/processed` — обработка, не тональность. `customer.ordersCount` — состояние при чтении, не доказательство числа покупок на дату отзыва.
- Деталь `GET v1/feedback/{feedback_id}`; экспорт `/feedback/export` требует отдельного права и неописанным форматом. Не запускать `/generate`, `/answer`, присвоение тегов/смену статуса ради чтения.
- `GET v1/distributions` → массив `DistributionFrontend`, `offset=0,limit=50,order_by,search,shop_id`; `/distribution/total` → `{total}`, принимает только shop_id, **не search**. Детали `GET v1/distribution/{distribution_id}` → `DistributionDetailSchema{followedCustomersCount,ordersCustomersCount,sentMessagePercent,notSentMessagePercent,ordersSum,distribution}`. В distribution `sendAt,timezone,isScheduled,status,totalCount,countOrders,customerVisits,attributionWindowHours,target`.
- `GET v1/distribution/{distribution_id}/counts` → `all,done,error,visitsCount,ordersCount,visitsWithNoOrderCount`.
- `GET v1/distribution/{distribution_id}/{message_type}`, message_type `all/error/done`; `offset=0,limit=100`; `metrics=followed_customers/orders_customers/followed_customers_with_no_order`; `order_by=id` из enum. Ответ `{messages:[{id,customer}],category}`. В customer: id, nullable lastVisit, nullable lastOrder. Эти поля не являются полным журналом всех заказов/открытий; последнее событие не равно единственному событию.
- Индивидуальные события sent/read/follow можно искать в customer history (выше), но полнота/связь с автоматическими триггерами не подтверждена. `done` сообщения не приравнивать к прочтению. Заказы в attributionWindowHours не доказывают причинный прирост.

## Ключи и полнота

- В Starter `customer_id` — integer. `CustomerFrontend.coreId` — nullable string, **схема не доказывает, что это iiko customer UUID**. В заказе `id` и `starterId` оба integer, `posId` nullable string. В транзакции есть все три order-связки; в отзыве — customer/order IDs.
- Межсистемную связь сначала подтвердить на нескольких реальных совпадениях `posId` ↔ iiko order.id плюс организация/точка/дата. Не join по одному номеру заказа. Для клиентов сохранять отдельные source IDs; телефон нормализовать как кандидат связи, не доказательство тождества всех профилей/домохозяйства. Одинаковое имя/адрес не ключ.
- `OrderSource=web/app/iOS/Android` описывает Starter источник, не рекламный канал и не все способы заказа Тепла. UTM и analytics nullable, их наличие не устанавливает атрибуцию или межканальную полноту.
- Покупки для RFM: подтверждённо выполненные, отмены/возвраты отдельно. Не включать draft/created/notConfirmed/sending_to_iiko только потому, что есть сумма. `OrderStatus` полный enum: draft, created, notConfirmed, sending_to_iiko, checked, in_progress, awaitingDelivery, delivery, done, canceled; posStatus отдельно.
- Сохранять дату снимка, окно заказов, точку/организацию, фильтры, границы/TZ, HTTP статус, actual rows/unique IDs/total, пропуски и неохваченные каналы. До полного обхода current list/первые50 не являются базой клиентов.
- Текущие rfm/баланс/статус/любимые блюда нельзя присоединять к старому отзыву как историческое состояние. Строить признаки `as_of` по информации, доступной до события.

## Что не делать при сборе

`POST v1/customer/segment` — **создать сегмент**, а не прочитать его. `/customer/segment/import`, `/segment/from_distribution`, `/customer/bonuses`, `/customer/update-balance`, POST бонусных транзакций, `/distribution*` POST, отправки/изменения заказа — изменения. Это нельзя использовать как «проверку» в read-only исследовании. Списки/total v2 POST — отдельные явно документированные исключения, не основание считать безопасным любой POST. Авторизованный браузер/штатный экспорт; не извлекать cookie/токены или обходить вход.

## Пять главных неизвестных перед расчётом

1. Текущее право/доступ к клиентским спискам и истории, реальные total/полнота заказов/каналов; схема сама это не подтверждает.
2. Окно, даты, правила и обновление Starter RFM; что означают frequency/monetary/score, существуют ли исторические снимки (не найдены).
3. Денежная алгебра price/totalPrice/discountPrice/sumToPay/bonuses и рубли vs копейки; полнота оплат/частичных возвратов и courier/delivery составляющих.
4. Подтверждённые межсистемные ключи customer/coreId/posId и разрешение дубликатов/смены телефонов; доля несвязанных заказов.
5. Значение обещанного/фактического времени и полнота истории сообщений/автоматических retention-пушей: доставлено, прочитано и причинный эффект — разные события.

Навигационные локальные источники (актуальный checkout): `business-docs/marketing/data-channels.md:40–54` — старые указатели; `skills/teplo-promotions/references/sources-and-refresh.md:140–160` — контракт бонусов/рассылок 07.10; `skills/teplo-starter-promotion-capabilities/references/configuration-and-verification.md:114–157` — прежний контракт/документация; `business-docs/marketing/customer-research/2026-10-08/own-review-evidence.md:47–65` — отзывы schema-only, исторические объёмы не считать текущими.
