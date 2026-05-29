# iikoServer API endpoint map

Дата фиксации: 2026-05-18. Обновлено по персоналу и финансам: 2026-05-20, см. [22-iiko-employees-api.md](/app-spec/integrations/iiko/employees-api.md) и [22-iiko-finance-chart-of-accounts.md](/app-spec/integrations/iiko/finance-chart-of-accounts.md).

## Назначение

Этот документ — локальная карта iikoServer Resto API для проекта "Тепло". Его задача: чтобы следующие агенты не начинали с повторного изучения WADL, официальных статей и пробных запросов, а сразу понимали:

- как авторизоваться;
- какой формат дат использовать;
- где брать продажи, заказы, блюда, скидки, отмены, доставку, склад, ФОТ и справочники;
- какие endpoint'ы безопасны для чтения, а какие создают/меняют данные;
- какие ограничения уже обнаружены на практике.

Источник карты endpoint'ов: фактический WADL текущего сервера `GET /resto/api/application.wadl`, снятый 2026-05-18. Всего в WADL найдено 276 method/path combinations.

## Важная бизнес-поправка

Факт:
Гагарина не работает с января 2024 года.

Источник:
Уточнение владельца в рабочем чате, 2026-05-18.

Период:
Актуально для всей управленческой картины после января 2024.

Вывод:
Если iiko возвращает сущности `Foodmarket Тепло Гагарина`, их нельзя считать активной текущей точкой. Для текущих продаж и операций использовать `Foodmarket Тепло Черникова`, если отдельно не анализируется история.

Действие:
Во всех отчетах за 2024-2026 фильтровать активный контур как Черникова и явно помечать Гагарина как историческую/закрытую сущность.

## Безопасность

- Никогда не писать токены, логины, пароли, SHA1 и полные строки авторизации в Markdown.
- Секреты брать только из `ENV` или `.env` в корне проекта.
- Для iiko-запросов передавать токен query-параметром `key`.
- Не делать `POST`, `PUT`, `DELETE`, если задача не просит изменить данные явно.
- Даже `GET` может иметь side effect, если в path есть `create`, `update`, `delete`, `restore`, `clean`, `increase`, `initReplication`, `send`, `register`, `add`.
- Для аналитики использовать только `GET` и read-only `POST /v2/reports/olap`, если нужен сложный OLAP-запрос телом.
- Запросы выполнять последовательно.
- Не запрашивать период больше одного месяца за один запрос.

## Авторизация

Базовые переменные в `ENV`:

```text
IIKO_SERVER_BASE_URL
IIKO_SERVER_LOGIN
IIKO_SERVER_PASSWORD_SHA1
IIKO_SERVER_TOKEN
IIKO_SERVER_TIMEOUT_SECONDS
```

Если токен истек, обновить его:

```bash
curl -k -sS -X POST "$IIKO_SERVER_BASE_URL/resto/api/auth" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  --data-urlencode "login=$IIKO_SERVER_LOGIN" \
  --data-urlencode "pass=$IIKO_SERVER_PASSWORD_SHA1"
```

Ответ — строка токена. В чат и документы ее не выводить.

Базовый шаблон read-only запроса:

```bash
curl -k -sS -G "$IIKO_SERVER_BASE_URL/resto/api/reports/olap" \
  --data-urlencode "key=$IIKO_SERVER_TOKEN" \
  --data-urlencode "report=SALES" \
  --data-urlencode "from=01.05.2026" \
  --data-urlencode "to=17.05.2026" \
  --data-urlencode "groupRow=OpenDate.Typed" \
  --data-urlencode "agr=OrderNum" \
  --data-urlencode "agr=DishDiscountSumInt" \
  --data-urlencode "summary=false"
```

## Форматы дат

| Зона API | Формат дат | Проверено | Комментарий |
| --- | --- | --- | --- |
| `/reports/olap` | `dd.MM.yyyy` | Да | проверить |
| `/employees/attendance` | `yyyy-MM-dd` | Да | `dd.MM.yyyy` дает 400; основной источник журнала явки, см. [22](/app-spec/integrations/iiko/employees-api.md) |
| `/employees/schedule` | `yyyy-MM-dd` | Да | endpoint работает, но за 2025-11..2026-05 вернул 0 строк; не считать подтвержденным нулем плана |
| `/reports/storeOperations` | `dd.MM.yyyy` | Да | Использовать для складских операций и документов инвентаризации |
| `/v2/documents/*` | строка по WADL | Не проверено полностью | Сначала пробовать `yyyy-MM-dd`, затем сверять ошибку API |
| `/reports/delivery/*` | строка по WADL | Endpoint найден | Формат нужно проверить отдельным коротким запросом |

## Практически проверенные endpoint'ы

| Данные | Endpoint | Метод | Проверенный статус | Что брать | Примечание |
| --- | --- | --- | --- | --- | --- |
| Авторизация | `/auth` | POST | 200 | token | Вызов через `/resto/api/auth` |
| WADL / карта API | `/application.wadl` | GET | 200 | endpoint'ы и параметры | Вызов через `/resto/api/application.wadl` |
| Подразделения | `/corporation/departments` | GET | 200 | точки/департаменты | Гагарина историческая, Черникова активная |
| Группы | `/corporation/groups` | GET | 200 | группы/терминальные группы | Нужны для справочников |
| Продукты | `/v2/entities/products/list` | GET | 200 | меню, SKU, группы, описания | Ответ JSON, большой |
| Старый products v1 | `/entities/products/list` | GET | 404 | не использовать | Использовать v2 |
| OLAP продажи | `/reports/olap` | GET | 200 | продажи, заказы, блюда, скидки, доставка | Ответ XML |
| OLAP TRANSACTIONS / Главная касса | `/reports/olap` | GET | 200 | операции `Финансы -> План счетов -> Главная касса`: `Account.Name`, `Contr-Account.Name`, `TransactionSide`, `Document`, `CashFlowCategory`, `Comment`, `Sum.Incoming`, `Sum.Outgoing` | Канонический источник wallet `ТК Черникова`; `report=TRANSACTIONS`, даты `dd.MM.yyyy`, период не больше месяца; за 2026-02-01..2026-05-20 найдено 279 строк и 13 корсчетов, см. [22](/app-spec/integrations/iiko/finance-chart-of-accounts.md) |
| OLAP колонки | `/v2/reports/olap/columns` | GET | 200 | список группировок/агрегатов | Ответ JSON |
| OLAP TRANSACTIONS columns | `/v2/reports/olap/columns?reportType=TRANSACTIONS` | GET | 200 | 116 полей TRANSACTIONS, включая `Account.*`, `Contr-Account.*`, `CashFlowCategory`, `TransactionSide`, `Sum.Incoming`, `Sum.Outgoing` | Использовать перед изменением набора `groupRow` для финансовых отчетов |
| OLAP presets | `/v2/reports/olap/presets` | GET | найден в WADL | сохраненные отчеты | Можно использовать для типовых отчетов |
| ДДС saved presets | `/v2/reports/olap/byPresetId/8c13763a-35bf-9f27-017f-5468b1e70022` и `...0023` | GET | 200 | агрегированные отчеты ДДС по статьям/подразделениям | Полезны как агрегаты, но не заменяют журнал Главной кассы: нет полного разреза корсчет + комментарий |
| P&L preset / отчет о прибылях и убытках | `/v2/reports/olap/byPresetId/8c13763a-35bf-9f27-017f-5468b1e70021` | GET | 200 | P&L строки по `Account.Name`, `Account.Type`, `Store`, `Sum.ResignedSum` | Для строки ОПиУ `Траты на курьерскую службу` брать `Account.Name=Зарплата курьеров`; параметры `summary=false`, `dateFrom/dateTo=YYYY-MM-DD`; `dateTo` - исключающая верхняя граница, для апреля 2026 использовать `dateTo=2026-05-01` |
| План счетов / accounts | `/v2/entities/accounts/list` | GET | 200 | справочник счетов: `id`, `code`, `name`, `type`, `customTransactionsAllowed` | Для Главной кассы: `name=Главная касса`, `type=CASH`; в processed/Markdown id обезличивать |
| Остатки по счету и контрагентам | `/v2/reports/balance/counteragents` | GET | 200 | остатки на `timestamp` по `account` и `counteragent` | Может пригодиться для `wallet_balance_snapshots`; не является журналом операций |
| Кассовые смены | `/v2/cashshifts/list` | GET | 200 | смены, `salesCash`, `salesCard`, `payIn`, `payOut` | Нужен обязательный `status`; полезно для сверки наличной выручки, не заменяет `Главная касса` |
| Платежи смены | `/v2/cashshifts/payments/list/{sessionId}` | GET | 200 | платежи конкретной кассовой смены | Может содержать комментарии/user ids; держать raw в private/raw |
| Внутренние перемещения | `/v2/documents/internalTransfer` | GET | 200, pilot empty | документы internal transfer | Пилот 2026-04-01..2026-04-07 со статусом `PROCESSED` не дал строк для Главной кассы |
| Явки | `/employees/attendance` | GET | 200, ненулевые записи | факт явки: `employeeId`, `roleId`, `dateFrom`, `dateTo`, `attendanceType`, `departmentName` | Подтверждено 2026-05-20: февраль-май 2026 = 1 155 строк активной Черниковой; старый вывод про 0 часов был ошибкой парсинга `<attendance>`, см. [22-iiko-employees-api.md](/app-spec/integrations/iiko/employees-api.md) |
| Явки по подразделению | `/employees/attendance/department/{departmentId}` | GET | 200 | те же строки явки, отфильтрованные по точке | Рабочий department-specific путь; вариант `/byDepartment/{departmentId}` на этом сервере дал 409 |
| Типы явок | `/employees/attendance/types` | GET | 200 | `Р`, `Б`, `О`, `П`, `Н`, `В`, payRate | `status` справочника не равен статусу утверждения/оплаты смены |
| График | `/employees/schedule` | GET | 200, 0 записей | плановые смены | Endpoint работает, но за 2025-11..2026-05 данных нет; не считать отсутствие плана нулем |
| Типы графика | `/employees/schedule/types` | GET | 200 | шаблоны смен: startTime, lengthMinutes, tariff, overtime | Справочник полезен, но не является плановым расписанием |
| Зарплатные настройки | `/employees/salary` | GET | 200, 39 записей | payment-настройки сотрудников | Не заменяет расчет payroll и факт выплат |
| Роли сотрудников | `/employees/roles` | GET | 200, 14 записей | role id/code/name, hourly/fixed settings | Нужен для маппинга `roleId` из явок |
| Сотрудники | `/employees` | GET | 200 | HR-справочник | Raw содержит PII; в processed/Markdown только схема и hash |
| iiko payroll list | `/v2/payrolls/list` | GET | 200, 0 записей | payroll-документы, если ведутся | Пилот 2026-04-01..2026-04-07 по Черниковой пустой |
| Выручка по официантам | `/v2/reports/olap/byPresetId/{id}` | GET | 200 | `WaiterName`, `DishDiscountSumInt`, `DishSumInt` | Preset есть и возвращает строки, но `WaiterName` = PII; это не сменный revenue ledger |
| Списания | `/v2/documents/writeoff` | GET | найден в WADL | складские списания | Проверять отдельно по короткому периоду |
| Приходные накладные | `/documents/export/incomingInvoice` | GET | 200 | товарные строки приходных накладных | `from/to=YYYY-MM-DD`; для P&L строк `Вспомогательные товары` и `Закупка расходников на ТТ`; `product` в строке = GUID товара |
| Инвентаризации / недостачи и излишки | `/reports/storeOperations` | GET | 200 | документы инвентаризации, суммы излишков и недостач | `dateFrom/dateTo=dd.MM.yyyy`, `documentTypes=INCOMING_INVENTORY`; в ответе `type=INVENTORY_CORRECTION`, `documentType=INCOMING_INVENTORY` |
| Store operations | `/reports/storeOperations` | GET | 200 | складские операции | Для товарных строк использовать `productDetalization=true`; для итогов документов достаточно `false` |
| Product expense | `/reports/productExpense` | GET | найден в WADL | расход продуктов | Нужен department |
| Цикл доставки | `/reports/delivery/orderCycle` | GET | найден в WADL | времена цикла заказа | Проверить параметры target* |
| Курьеры | `/reports/delivery/couriers` | GET | найден в WADL | доставка по курьерам | В рабочие документы переносить агрегаты |

## Где брать управленческие данные

Главное правило для P&L: выручку, скидки и себестоимость по направлениям сначала искать в iiko OLAP `Отчет о выручке по направлениям`. Этот отчет является первичным источником для строк ОПиУ по направлениям `Роллы`, `Пицца`, `Горячий цех`, `Бар`; Google Sheets хранит перенос/раскладку, а processed CSV являются агрегированными снимками.

| Блок | Основной источник | Endpoint / файл | Частота | Следующее действие |
| --- | --- | --- | --- | --- |
| Выручка, скидки, себестоимость по направлениям P&L | iiko OLAP `Отчет о выручке по направлениям` | `/reports/olap` / сохраненный OLAP-отчет в iiko | Ежемесячно, при закрытии ОПиУ | Использовать как первичный источник для строк выручки, скидок и food cost; если отчет не обновлен, фиксировать `нет данных`, а не `0` |
| Продажи по дням | iiko OLAP | `/reports/olap`, `groupRow=OpenDate.Typed` | Ежедневно | Сохранять агрегат: дата, выручка, заказы, средний чек, скидка |
| Средний чек | iiko OLAP | `DishDiscountSumInt / OrderNum` или `DishDiscountSumInt.average` | Ежедневно | Считать в панели собственника |
| Каналы заказов | iiko OLAP | `OriginName`, `OrderType`, `Delivery.ServiceType` | Еженедельно | Расшифровать пустой `OriginName` |
| Продажи по категориям | iiko OLAP | `DishCategory`, `DishGroup`, `DishGroup.TopParent` | Еженедельно | Чистить пустые/технические категории |
| Продажи по блюдам | iiko OLAP | `DishName`, `DishAmountInt`, `DishDiscountSumInt` | Еженедельно | Делать меню-анализ |
| Скидки и акции | iiko OLAP | `DiscountSum`, `discountWithoutVAT`, `OrderDiscount.Type`, `ItemSaleEventDiscountType` | Ежедневно/еженедельно | Сравнить скидки с валовой маржой |
| Себестоимость | iiko OLAP + P&L | `ProductCostBase.*`, Google Sheets ОПиУ | Еженедельно/ежемесячно | Проверить техкарты и корректность себестоимости |
| Инвентаризации / результаты ревизий | iiko `Товары и склады` -> `Документы инвентаризации` | `/reports/storeOperations`, `documentTypes=INCOMING_INVENTORY` | Еженедельно/ежемесячно | Ревизии = продуктовые инвентаризации, обычно по понедельникам; упаковка = отдельные инвентаризации по первым числам. Хранить `documentId`, `documentNum`, `date`, `sum`, `documentSum`, `secondaryAccount`; для товарного контекста включать `productDetalization=true` |
| Отмены | iiko OLAP | `OrderDeleted`, `Delivery.CancelCause` | Ежедневно | Разделять отмены клиента и внутренние ошибки |
| Возвраты чека | iiko OLAP | `Storned`, `DishReturnSum.withoutVAT` | Ежедневно | Проверить причины возвратов |
| Удаления / списания в заказах | iiko OLAP | `DeletedWithWriteoff`, `RemovalType` | Еженедельно | Отдельно сверить с `/v2/documents/writeoff` |
| Складские списания / акты списания | iiko `Товары и склады` -> `Акты списания` | `/v2/documents/writeoff` | Еженедельно/ежемесячно | Для P&L строки `Списание продукции и сырья` брать проведенные акты за месяц и суммировать `items[].cost` |
| Доставка | iiko OLAP + delivery reports | `Delivery.DelayAvg`, `Delivery.WayDurationAvg`, `/reports/delivery/orderCycle` | Ежедневно | Настроить пороги опозданий |
| Курьерская служба для P&L | iiko `Финансы` -> `Отчет о прибылях и убытках` | `/v2/reports/olap/byPresetId/8c13763a-35bf-9f27-017f-5468b1e70021`, `Account.Name=Зарплата курьеров` | Ежемесячно | Для строки ОПиУ `Траты на курьерскую службу` брать `SUM(Sum.ResignedSum)` за месяц; период задавать как первое число месяца -> первое число следующего месяца; апрель 2026 = 248 401 |
| Курьерские операционные метрики | Google Sheets + iiko | `График курьеров`, `/reports/delivery/couriers` | Еженедельно | Использовать для контроля доставок/смен, не как основной источник суммы P&L |
| ФОТ / явки | iiko + Google Sheets | `/employees/attendance`, `Расчет зарплат NEW` | Еженедельно | Факт явки брать из iiko; правила начислений, депозиты, фонд и выплаты остаются по payroll-логике, см. [22-iiko-employees-api.md](/app-spec/integrations/iiko/employees-api.md) и [19-payroll-module-spec.md](/docs/business-control/19-payroll-module-spec.md) |
| P&L / ОПиУ | Google Sheets | `Копия ОПиУ...`, лист `Факт` | Ежемесячно | Сверять с iiko выручкой |
| ДДС | Google Sheets | `2026 ДДС — Классический • ИП Шокина`, https://docs.google.com/spreadsheets/d/1ZkyXPcotiyEeJ-NJxDxf-u2UP9tJFh27HAA_ZjpCYco/edit, листы `ДДС: месяц`, `ДДС: Сводный`, `ДДС: статьи` | Ежемесячно | Для прямых ДДС-статей ОПиУ брать статью ДДС за месяц; не использовать для строк `Содержание торговых точек` и `Телекоммуникации` без отдельной логики |
| ТК Черникова / торговая касса | iiko `Финансы -> План счетов -> Главная касса` | `/reports/olap`, `report=TRANSACTIONS`, фильтр `Account.Name=Главная касса` | Ежедневно/ежемесячно | Первичный источник wallet `ТК Черникова`: наличная выручка, депозиты курьеров, оперативные расходы. Корсчет `Задолженность перед поставщиками` -> `Оплата поставщикам`; остальные корсчета через rule engine, см. [22-iiko-finance-chart-of-accounts.md](/app-spec/integrations/iiko/finance-chart-of-accounts.md) |
| Баланс | Google Sheets | `Копия Баланс...`, лист `Баланс` | Ежемесячно | До сверки не считать чистым фактом из-за расхождения проверки |

## Склад: акты списания

Подтвержденный read-only endpoint для iiko `Товары и склады` -> `Акты списания`:

```text
GET /resto/api/v2/documents/writeoff
```

Минимальный запрос для месячной строки P&L:

```text
key=<token>
dateFrom=2026-04-01
dateTo=2026-04-30
status=PROCESSED
```

Проверенный формат дат для этого endpoint'а - `yyyy-MM-dd`.

Как считать итог:

| Поле | Значение / смысл |
| --- | --- |
| `response[]` | список актов списания |
| `id` | стабильный id акта |
| `dateIncoming` | дата/время акта |
| `documentNumber` | номер акта |
| `status` | для P&L использовать `PROCESSED` |
| `accountId` | счет/причина списания; расшифровывается через справочник счетов |
| `items[]` | товарные строки внутри акта |
| `items[].cost` | стоимость списанной товарной строки |

Для строки ОПиУ `Списание продукции и сырья` использовать итог за месяц:

```text
Списание продукции и сырья = SUM(items[].cost) по всем актам списания со статусом PROCESSED за месяц
```

Контрольная проверка по локальному raw-снимку `documents_writeoff_2026-04-01_2026-04-30_dateFormat_yyyy-MM-dd.json`:

| Период | Проведенных актов | Сумма `items[].cost` |
| --- | ---: | ---: |
| 2026-04-01..2026-04-30 | 101 | 91 552.95 |
| 2026-04-01 | 3 | 550.74 |

## Склад: инвентаризации

Подтвержденный read-only endpoint для документов инвентаризации:

```text
GET /resto/api/reports/storeOperations
```

Минимальный запрос для проверки документа:

```text
key=<token>
dateFrom=27.04.2026
dateTo=27.04.2026
documentTypes=INCOMING_INVENTORY
productDetalization=false
showCostCorrections=false
```

Как распознавать инвентаризацию в ответе:

| Поле | Значение / смысл |
| --- | --- |
| `type` | `INVENTORY_CORRECTION` |
| `documentType` | `INCOMING_INVENTORY` |
| `documentId` | стабильный id документа |
| `documentNum` | номер документа в iiko |
| `primaryStore` | склад; для текущего контура Черникова обычно `Основной склад Черникова` |
| `secondaryAccount` | счет корректировки: `Недостача инвентаризации` или `Излишки инвентаризации` |
| `sum`, `cost` | подписанная сумма строки |
| `documentSum` | сумма строки без знака |

Для управленческой проверки за 27.04.2026 API вернул документ инвентаризации `0021`, `documentId=df45381d-c983-41f6-af9e-6dc297603cb5`, склад `Основной склад Черникова`. В документе две строки:

| Счет | Направление | Сумма |
| --- | --- | ---: |
| `Недостача инвентаризации` | `incoming=false` | `-11992.75` |
| `Излишки инвентаризации` | `incoming=true` | `8276.56` |

Контрольное нетто документа: `-3716.19`. Если нужна быстрая проверка доступа по сумме из положительной строки, сверять `8276.56`; если нужен финансовый эффект документа целиком, использовать нетто `-3716.19`.

Для строки ОПиУ `Результаты инвентаризации коробки д/пиццы` нужен тот же endpoint, но с `productDetalization=true`, чтобы получить товарные строки внутри документа и сложить `разница сумма, р` по нужным коробкам.

Для строк по конкретным товарам поле `product` в ответе `storeOperations` приходит как GUID. Названия и коды товаров брать из read-only справочника:

```text
GET /resto/api/v2/entities/products/list
```

Практическая проверка 2026-05-19 для строки ОПиУ `Результаты инвентаризации упаковки (кроме коробок для пиццы)`: запрос за март 2026 с `productDetalization=true` вернул 615 товарных строк. Документ инвентаризации упаковки найден как `documentNum=0012`, дата `02.03.2026`, 47 товарных строк. Для управленческой строки нужен whitelist упаковочных товаров/ID; широкий поиск по словам не использовать.

Товарный whitelist для P&L хранится в `research/processed/economic_block/pnl_product_whitelist.csv`. Для расчета использовать только строки `include_status = include`; строки `exclude` и `requires_owner_review` служат контролем качества и вопросами владельцу.

## Приходные накладные

Подтвержденный read-only endpoint для iiko `Товары и склады` -> приходные накладные:

```text
GET /resto/api/documents/export/incomingInvoice
```

Минимальный запрос за месяц:

```text
key=<token>
from=2026-03-01
to=2026-03-31
```

Формат дат: `YYYY-MM-DD`. Формат `dd.MM.yyyy` для этого endpoint'а не подходит: проверка 2026-05-19 вернула `409` с сообщением о неверном формате даты.

Ключевые поля ответа:

| Поле | Значение / смысл |
| --- | --- |
| `document.dateIncoming` | дата прихода |
| `document.documentNumber` | номер накладной |
| `document.status` | использовать только `PROCESSED` |
| `document.supplier` | GUID поставщика |
| `item.product` | GUID товара |
| `item.amount` | количество |
| `item.sum` | сумма товарной строки |
| `item.price` | цена |
| `item.store` | GUID склада |

Названия и коды товаров брать из справочника:

```text
GET /resto/api/v2/entities/products/list
```

Практическая проверка 2026-05-19: `GET /documents/export/incomingInvoice` за март 2026 вернул 528 товарных строк. Суммы по мартовскому контрольному Excel совпали для `Закупки расходников на ТТ` и `Вспомогательных товаров`, если отбирать конкретные iiko-товары. Нужен whitelist товарных ID/названий: например, широкий поиск по `вакуум` дает лишний товар `Пакет вакуум 160/200` на 694 руб., который не входит в мартовскую строку `пакеты для вакуматора`.

Товарный whitelist для этих строк также хранится в `research/processed/economic_block/pnl_product_whitelist.csv`.

Owner decision 2026-05-25 (C12): для платежного календаря нужен integration endpoint/source приложения по неоплаченным поставкам и ожидаемому timing оплаты поставщикам. Базовый iiko-кандидат - приходные накладные и supplier data; нужно отдельно проверить, какие поля iiko позволяют отличить неоплаченную поставку от оплаченной и получить/вывести ожидаемую дату оплаты. До проверки не считать, что `/documents/export/incomingInvoice` сам по себе содержит полный payment status.

## OLAP: продажи и операции

Для P&L-методологии отдельный приоритет имеет сохраненный/рабочий OLAP `Отчет о выручке по направлениям`. В нем использовать показатели `сумма без скидки`, `сумма со скидкой`, `сумма скидки`, `себестоимость`. Управленческий маппинг: `Роллы` = `Суши` + `Специи, роллы Черникова`, `Горячий цех` = `Шаурма`.

Базовый endpoint:

```text
GET /resto/api/reports/olap
```

Обязательные параметры:

| Параметр | Что ставить |
| --- | --- |
| `key` | токен |
| `report` | чаще всего `SALES`; для доставки также работает `DELIVERIES` |
| `from` | начало периода, `dd.MM.yyyy` |
| `to` | конец периода, `dd.MM.yyyy` |
| `groupRow` | группировка; можно передавать несколько раз |
| `agr` | агрегат; можно передавать несколько раз |
| `summary` | `false` для детализированных строк |

Проверенные группировки:

| Группировка | Что дает |
| --- | --- |
| `OpenDate.Typed` | учетный день |
| `Department` | торговое предприятие / точка |
| `RestaurantSection` | отделение |
| `OrderType` | тип заказа |
| `Delivery.ServiceType` | `COURIER`, `PICKUP` |
| `OriginName` | источник заказа, например `site` |
| `PayTypes` | тип оплаты |
| `DishCategory` | категория блюда |
| `DishGroup` | группа блюда |
| `DishGroup.TopParent` | верхняя группа блюда |
| `DishName` | блюдо / SKU |
| `OrderDeleted` | удален ли заказ |
| `Storned` | возврат чека |
| `Delivery.CancelCause` | причина отмены доставки |
| `RemovalType` | причина удаления блюда |
| `DeletedWithWriteoff` | удаление блюда со списанием/без списания |
| `Delivery.Courier` | курьер; в документы переносить только агрегаты |
| `Delivery.Region` | район доставки |
| `Delivery.MarketingSource` | маркетинговый источник доставки |

Проверенные агрегаты:

| Агрегат | Что дает |
| --- | --- |
| `OrderNum` | количество чеков/заказов в группе |
| `GuestNum` | гости; в доставке часто совпадает с заказами |
| `DishAmountInt` | количество блюд |
| `DishSumInt` | сумма до скидки |
| `DishDiscountSumInt` | сумма со скидкой |
| `sumAfterDiscountWithoutVAT` | сумма со скидкой без НДС |
| `DiscountSum` | сумма скидки |
| `discountWithoutVAT` | скидка без НДС |
| `DishDiscountSumInt.average` | средняя сумма заказа |
| `ProductCostBase.ProductCost` | себестоимость |
| `ProductCostBase.Percent` | себестоимость, % |
| `ProductCostBase.Profit` | наценка |
| `Delivery.DelayAvg` | среднее опоздание доставки, мин |
| `Delivery.WayDurationAvg` | среднее время в пути, мин |
| `Delivery.WayDurationSum` | суммарное время в пути, мин |
| `Delivery.AggregatedAvgMark` | средняя оценка доставки, если заполнена |
| `DishReturnSum.withoutVAT` | сумма возврата без НДС |

## Рецепты запросов

Продажи по дням:

```text
report=SALES
from=01.05.2026
to=17.05.2026
groupRow=OpenDate.Typed
agr=OrderNum
agr=DishSumInt
agr=DishDiscountSumInt
agr=discountWithoutVAT
agr=DishAmountInt
summary=false
```

Категории и блюда:

```text
report=SALES
groupRow=DishCategory
# или DishGroup / DishName
agr=OrderNum
agr=DishAmountInt
agr=DishDiscountSumInt
agr=discountWithoutVAT
summary=false
```

Каналы и доставка:

```text
report=SALES
groupRow=Delivery.ServiceType
groupRow=OriginName
groupRow=OrderType
agr=OrderNum
agr=DishDiscountSumInt
summary=false
```

Отмены, возвраты, удаления:

```text
report=SALES
groupRow=OrderDeleted
groupRow=Delivery.CancelCause
groupRow=Storned
groupRow=RemovalType
agr=OrderNum
agr=DishSumInt
agr=DishDiscountSumInt
summary=false
```

Опоздания и время в пути:

```text
report=SALES
groupRow=Delivery.ServiceType
agr=OrderNum
agr=Delivery.DelayAvg
agr=Delivery.WayDurationAvg
agr=Delivery.WayDurationSum
summary=false
```

## Как создавать новые локальные выгрузки

Если нужен новый локальный сборщик, создавать его отдельно от Markdown-документов, например:

```text
research/scripts/iiko/export_<block>.py
```

Минимальные правила сборщика:

1. Читать секреты из `ENV`, не из аргументов командной строки.
2. Обновлять токен через `/auth`, если запрос вернул ошибку авторизации.
3. Дробить период на месяцы или меньшие интервалы.
4. Делать запросы последовательно.
5. Сырые данные с персональными/финансовыми строками класть только в `research/raw/` или `research/private/`.
6. В документы переносить только агрегаты: суммы, количества, доли, выводы и действия.
7. Каждый итог фиксировать в формате `Факт / Источник / Период / Вывод / Действие`.

## Как создавать или менять данные в iiko

В WADL есть много `POST`, `PUT`, `DELETE`. Для управленческой аналитики они не нужны. Их нельзя запускать без отдельной явной задачи владельца.

Разрешенный по умолчанию режим для агентов:

```text
GET only
```

Исключение:

```text
POST /auth
POST /v2/reports/olap
```

`POST /v2/reports/olap` допустим только как read-only построение отчета, если GET-параметров не хватает.

Запрещено без явного разрешения:

- импорт документов;
- создание/обновление/удаление продуктов;
- изменение сотрудников, ролей, графиков, оплат;
- unprocess/check/import складских документов;
- любые DELETE;
- любые POST/PUT кроме авторизации и read-only OLAP.

## Полный каталог endpoint'ов из WADL

Ниже полный каталог method/path combinations, найденных в WADL. `key` в WADL не указан как параметр каждого метода, но фактически нужен для авторизованных запросов.

### `/reports`
| Метод | Path | Параметры / тело | Риск |
| --- | --- | --- | --- |
| `GET` | `/reports/delivery/consolidated` | department (query, xs:string)<br>writeoffAccounts (query, xs:string)<br>dateFrom (query, xs:string)<br>dateTo (query, xs:string) | read-only |
| `GET` | `/reports/delivery/couriers` | department (query, xs:string)<br>dateFrom (query, xs:string)<br>dateTo (query, xs:string)<br>targetCommonTime (query, xs:string)<br>targetOnTheWayTime (query, xs:string)<br>targetDoubledOrders (query, xs:string)<br>targetTripledOrders (query, xs:string)<br>targetTotalOrders (query, xs:string) | read-only |
| `GET` | `/reports/delivery/halfHourDetailed` | department (query, xs:string)<br>dateFrom (query, xs:string)<br>dateTo (query, xs:string) | read-only |
| `GET` | `/reports/delivery/loyalty` | department (query, xs:string)<br>dateFrom (query, xs:string)<br>dateTo (query, xs:string)<br>metricType (query, xs:string) | read-only |
| `GET` | `/reports/delivery/orderCycle` | department (query, xs:string)<br>dateFrom (query, xs:string)<br>dateTo (query, xs:string)<br>targetPizzaTime (query, xs:string)<br>targetCuttingTime (query, xs:string)<br>targetOnShelfTime (query, xs:string)<br>targetInRestaurantTime (query, xs:string)<br>targetOnTheWayTime (query, xs:string)<br>targetTotalTime (query, xs:string) | read-only |
| `GET` | `/reports/delivery/regions` | department (query, xs:string)<br>dateFrom (query, xs:string)<br>dateTo (query, xs:string) | read-only |
| `GET` | `/reports/ingredientEntry` | date (query, xs:string)<br>product (query, xs:string)<br>productArticle (query, xs:string)<br>department (query, xs:string)<br>includeSubtree (query, xs:boolean) | read-only |
| `GET` | `/reports/monthlyIncomePlan` | department (query, xs:string)<br>dateFrom (query, xs:string)<br>dateTo (query, xs:string) | read-only |
| `GET` | `/reports/olap` | report (query, xs:string)<br>summary (query, xs:boolean)<br>groupRow (query, xs:string)<br>groupCol (query, xs:string)<br>agr (query, xs:string)<br>from (query, xs:string)<br>to (query, xs:string) | read-only |
| `GET` | `/reports/productExpense` | department (query, xs:string)<br>dateFrom (query, xs:string)<br>dateTo (query, xs:string)<br>hourFrom (query, xs:int)<br>hourTo (query, xs:int) | read-only |
| `GET` | `/reports/sales` | department (query, xs:string)<br>dateFrom (query, xs:string)<br>dateTo (query, xs:string)<br>hourFrom (query, xs:int)<br>hourTo (query, xs:int)<br>dishDetails (query, xs:boolean)<br>allRevenue (query, xs:boolean) | read-only |
| `GET` | `/reports/storeOperations` | dateFrom (query, xs:string)<br>dateTo (query, xs:string)<br>presetId (query, xs:string)<br>stores (query, xs:string)<br>documentTypes (query, xs:string)<br>productDetalization (query, xs:boolean)<br>showCostCorrections (query, xs:boolean) | read-only |
| `GET` | `/reports/storeReportPresets` |  | read-only |

### `/v2`
| Метод | Path | Параметры / тело | Риск |
| --- | --- | --- | --- |
| `GET` | `/v2/assemblyCharts/byId` | id (query, xs:string) | read-only |
| `POST` | `/v2/assemblyCharts/delete` | body: application/json;charset=UTF-8 | опасно: меняет/создает/удаляет |
| `GET` | `/v2/assemblyCharts/getAll` | dateFrom (query, xs:string)<br>dateTo (query, xs:string)<br>includeDeletedProducts (query, xs:boolean)<br>includePreparedCharts (query, xs:boolean) | read-only |
| `GET` | `/v2/assemblyCharts/getAllUpdate` | knownRevision (query, xs:int)<br>dateFrom (query, xs:string)<br>dateTo (query, xs:string)<br>includeDeletedProducts (query, xs:boolean)<br>includePreparedCharts (query, xs:boolean) | read-only |
| `GET` | `/v2/assemblyCharts/getAssembled` | date (query, xs:string)<br>productId (query, xs:string)<br>departmentId (query, xs:string) | read-only |
| `GET` | `/v2/assemblyCharts/getHistory` | productId (query, xs:string)<br>departmentId (query, xs:string) | read-only |
| `GET` | `/v2/assemblyCharts/getPrepared` | date (query, xs:string)<br>productId (query, xs:string)<br>departmentId (query, xs:string) | read-only |
| `GET` | `/v2/assemblyCharts/getTree` | date (query, xs:string)<br>productId (query, xs:string)<br>departmentId (query, xs:string) | read-only |
| `POST` | `/v2/assemblyCharts/save` | body: application/json;charset=UTF-8 | опасно: меняет/создает/удаляет |
| `POST` | `/v2/assemblyCharts/update` | body: application/json;charset=UTF-8 | опасно: меняет/создает/удаляет |
| `GET` | `/v2/cashshifts/byId/{sessionId}` |  | read-only |
| `GET` | `/v2/cashshifts/closedSessionDocument/{sessionId}` |  | read-only |
| `GET` | `/v2/cashshifts/list` | openDateFrom (query, xs:string)<br>openDateTo (query, xs:string)<br>departmentId (query, xs:string)<br>groupId (query, xs:string)<br>status (query, xs:string)<br>revisionFrom (query, xs:int) | read-only |
| `GET` | `/v2/cashshifts/payments/list/{sessionId}` | hideAccepted (query, xs:boolean) | read-only |
| `POST` | `/v2/cashshifts/save` | body: application/xml<br>body: application/json | опасно: меняет/создает/удаляет |
| `GET` | `/v2/corporation/settings` |  | read-only |
| `GET` | `/v2/documents/internalTransfer` | dateFrom (query, xs:string)<br>dateTo (query, xs:string)<br>status (query, xs:string)<br>revisionFrom (query, xs:int) | read-only |
| `POST` | `/v2/documents/internalTransfer` | body: application/json;charset=UTF-8 | опасно: меняет/создает/удаляет |
| `GET` | `/v2/documents/internalTransfer/byId` | id (query, xs:string) | read-only |
| `GET` | `/v2/documents/internalTransfer/byNumber` | documentNumber (query, xs:string) | read-only |
| `GET` | `/v2/documents/menuChange` | dateFrom (query, xs:string)<br>dateTo (query, xs:string)<br>status (query, xs:string)<br>revisionFrom (query, xs:int) | read-only |
| `POST` | `/v2/documents/menuChange` | body: application/json;charset=UTF-8 | опасно: меняет/создает/удаляет |
| `GET` | `/v2/documents/menuChange/byId` | id (query, xs:string) | read-only |
| `GET` | `/v2/documents/menuChange/byNumber` | documentNumber (query, xs:string) | read-only |
| `GET` | `/v2/documents/writeoff` | dateFrom (query, xs:string)<br>dateTo (query, xs:string)<br>status (query, xs:string)<br>revisionFrom (query, xs:int) | read-only |
| `POST` | `/v2/documents/writeoff` | body: application/json;charset=UTF-8 | опасно: меняет/создает/удаляет |
| `GET` | `/v2/documents/writeoff/byId` | id (query, xs:string) | read-only |
| `GET` | `/v2/documents/writeoff/byNumber` | documentNumber (query, xs:string) | read-only |
| `GET` | `/v2/entities/accounts/list` | includeDeleted (query, xs:boolean)<br>revisionFrom (query, xs:int) | read-only |
| `GET` | `/v2/entities/list` | rootType (query, xs:string)<br>includeDeleted (query, xs:boolean)<br>revisionFrom (query, xs:int) | read-only |
| `GET` | `/v2/entities/payInOutTypes/list` | includeDeleted (query, xs:boolean)<br>revisionFrom (query, xs:int) | read-only |
| `GET` | `/v2/entities/periodSchedules` | includeDeleted (query, xs:boolean)<br>id (query, xs:string)<br>revisionFrom (query, xs:int) | read-only |
| `GET` | `/v2/entities/periodSchedules/byId` | id (query, xs:string) | read-only |
| `GET` | `/v2/entities/priceCategories` | includeDeleted (query, xs:boolean)<br>id (query, xs:string)<br>revisionFrom (query, xs:int) | read-only |
| `GET` | `/v2/entities/priceCategories/byId` | id (query, xs:string) | read-only |
| `GET` | `/v2/entities/productScales` | includeDeleted (query, xs:boolean)<br>ids (query, xs:string) | read-only |
| `POST` | `/v2/entities/productScales` | body: application/x-www-form-urlencoded | опасно: меняет/создает/удаляет |
| `POST` | `/v2/entities/productScales/delete` | body: application/json;charset=UTF-8 | опасно: меняет/создает/удаляет |
| `POST` | `/v2/entities/productScales/restore` | body: application/json;charset=UTF-8 | опасно: меняет/создает/удаляет |
| `POST` | `/v2/entities/productScales/save` | body: application/json;charset=UTF-8 | опасно: меняет/создает/удаляет |
| `POST` | `/v2/entities/productScales/update` | body: application/json;charset=UTF-8 | опасно: меняет/создает/удаляет |
| `GET` | `/v2/entities/productScales/{productScaleId}` |  | read-only |
| `POST` | `/v2/entities/products/category/delete` | body: application/json;charset=UTF-8 | опасно: меняет/создает/удаляет |
| `GET` | `/v2/entities/products/category/list` | includeDeleted (query, xs:boolean)<br>revisionFrom (query, xs:int)<br>ids (query, xs:string) | read-only |
| `POST` | `/v2/entities/products/category/list` | body: application/x-www-form-urlencoded | опасно: меняет/создает/удаляет |
| `POST` | `/v2/entities/products/category/restore` | body: application/json;charset=UTF-8 | опасно: меняет/создает/удаляет |
| `POST` | `/v2/entities/products/category/save` | body: application/json;charset=UTF-8 | опасно: меняет/создает/удаляет |
| `POST` | `/v2/entities/products/category/update` | body: application/json;charset=UTF-8 | опасно: меняет/создает/удаляет |
| `POST` | `/v2/entities/products/delete` | body: application/json;charset=UTF-8 | опасно: меняет/создает/удаляет |
| `POST` | `/v2/entities/products/group/delete` | body: application/json;charset=UTF-8 | опасно: меняет/создает/удаляет |
| `GET` | `/v2/entities/products/group/list` | includeDeleted (query, xs:boolean)<br>ids (query, xs:string)<br>nums (query, xs:string)<br>codes (query, xs:string)<br>revisionFrom (query, xs:int)<br>parentIds (query, xs:string) | read-only |
| `POST` | `/v2/entities/products/group/list` | body: application/x-www-form-urlencoded | опасно: меняет/создает/удаляет |
| `POST` | `/v2/entities/products/group/restore` | overrideNomenclatureCode (query, xs:boolean)<br>body: application/json;charset=UTF-8 | опасно: меняет/создает/удаляет |
| `POST` | `/v2/entities/products/group/save` | generateFastCode (query, xs:boolean)<br>generateNomenclatureCode (query, xs:boolean)<br>body: application/json;charset=UTF-8 | опасно: меняет/создает/удаляет |
| `POST` | `/v2/entities/products/group/update` | overrideFastCode (query, xs:boolean)<br>overrideNomenclatureCode (query, xs:boolean)<br>body: application/json;charset=UTF-8 | опасно: меняет/создает/удаляет |
| `GET` | `/v2/entities/products/list` | includeDeleted (query, xs:boolean)<br>revisionFrom (query, xs:int)<br>ids (query, xs:string)<br>nums (query, xs:string)<br>codes (query, xs:string)<br>types (query, xs:string)<br>categoryIds (query, xs:string)<br>parentIds (query, xs:string) | read-only |
| `POST` | `/v2/entities/products/list` | body: application/x-www-form-urlencoded | опасно: меняет/создает/удаляет |
| `GET` | `/v2/entities/products/productScales` | includeDeletedProducts (query, xs:boolean)<br>productId (query, xs:string) | read-only |
| `POST` | `/v2/entities/products/productScales` | body: application/x-www-form-urlencoded | опасно: меняет/создает/удаляет |
| `POST` | `/v2/entities/products/restore` | overrideNomenclatureCode (query, xs:boolean)<br>body: application/json;charset=UTF-8 | опасно: меняет/создает/удаляет |
| `POST` | `/v2/entities/products/save` | generateFastCode (query, xs:boolean)<br>generateNomenclatureCode (query, xs:boolean)<br>body: application/json;charset=UTF-8 | опасно: меняет/создает/удаляет |
| `POST` | `/v2/entities/products/update` | overrideFastCode (query, xs:boolean)<br>overrideNomenclatureCode (query, xs:boolean)<br>body: application/json;charset=UTF-8 | опасно: меняет/создает/удаляет |
| `DELETE` | `/v2/entities/products/{productId}/productScale` |  | опасно: меняет/создает/удаляет |
| `GET` | `/v2/entities/products/{productId}/productScale` |  | read-only |
| `POST` | `/v2/entities/products/{productId}/productScale` | body: application/json;charset=UTF-8 | опасно: меняет/создает/удаляет |
| `POST` | `/v2/entities/quickLabels/delete` | body: application/json;charset=UTF-8 | опасно: меняет/создает/удаляет |
| `GET` | `/v2/entities/quickLabels/list` | includeDeleted (query, xs:boolean)<br>revisionFrom (query, xs:int)<br>id (query, xs:string)<br>departmentId (query, xs:string)<br>sectionId (query, xs:string) | read-only |
| `POST` | `/v2/entities/quickLabels/list` | body: application/x-www-form-urlencoded | опасно: меняет/создает/удаляет |
| `POST` | `/v2/entities/quickLabels/save` | body: application/json;charset=UTF-8 | опасно: меняет/создает/удаляет |
| `POST` | `/v2/entities/quickLabels/update` | body: application/json;charset=UTF-8 | опасно: меняет/создает/удаляет |
| `GET` | `/v2/entities/scheduleTypes` | includeDeleted (query, xs:boolean)<br>ids (query, xs:string) | read-only |
| `POST` | `/v2/entities/scheduleTypes` | body: application/x-www-form-urlencoded | опасно: меняет/создает/удаляет |
| `POST` | `/v2/entities/scheduleTypes/delete` | body: application/json;charset=UTF-8 | опасно: меняет/создает/удаляет |
| `POST` | `/v2/entities/scheduleTypes/restore` | body: application/json;charset=UTF-8 | опасно: меняет/создает/удаляет |
| `POST` | `/v2/entities/scheduleTypes/save` | body: application/json;charset=UTF-8 | опасно: меняет/создает/удаляет |
| `POST` | `/v2/entities/scheduleTypes/update` | body: application/json;charset=UTF-8 | опасно: меняет/создает/удаляет |
| `POST` | `/v2/entities/tips/create` | body: application/json;charset=UTF-8 | опасно: меняет/создает/удаляет |
| `GET` | `/v2/entities/tips/list` | includeDeleted (query, xs:boolean) | read-only |
| `POST` | `/v2/entities/tips/update` | body: application/json;charset=UTF-8 | опасно: меняет/создает/удаляет |
| `GET` | `/v2/entities/tips/{id}` |  | read-only |
| `POST` | `/v2/entities/tips/{id}/delete` |  | опасно: меняет/создает/удаляет |
| `POST` | `/v2/entities/tips/{id}/restore` |  | опасно: меняет/создает/удаляет |
| `POST` | `/v2/images/delete` | body: application/json;charset=UTF-8 | опасно: меняет/создает/удаляет |
| `GET` | `/v2/images/load` | imageId (query, xs:string) | read-only |
| `POST` | `/v2/images/save` | body: application/json;charset=UTF-8 | опасно: меняет/создает/удаляет |
| `POST` | `/v2/payInOuts/addPayOut` | body: application/json;charset=UTF-8 | опасно: меняет/создает/удаляет |
| `GET` | `/v2/payrolls/list` | dateFrom (query, xs:string)<br>dateTo (query, xs:string)<br>department (query, xs:string)<br>includeDeleted (query, xs:boolean)<br>revisionFrom (query, xs:int) | read-only |
| `GET` | `/v2/price` | dateFrom (query, xs:string)<br>dateTo (query, xs:string)<br>departmentId (query, xs:string)<br>includeOutOfSale (query, xs:boolean)<br>type (query, xs:string)<br>revisionFrom (query, xs:int) | read-only |
| `POST` | `/v2/push/subscribe` | body: application/json;charset=UTF-8 | опасно: меняет/создает/удаляет |
| `POST` | `/v2/push/unsubscribe` | body: application/json;charset=UTF-8 | опасно: меняет/создает/удаляет |
| `GET` | `/v2/reports/balance/counteragents` | timestamp (query, xs:string)<br>account (query, xs:string)<br>counteragent (query, xs:string)<br>department (query, xs:string) | read-only |
| `GET` | `/v2/reports/balance/stores` | timestamp (query, xs:string)<br>department (query, xs:string)<br>store (query, xs:string)<br>product (query, xs:string) | read-only |
| `GET` | `/v2/reports/egais/marks/list` | revisionFrom (query, xs:int)<br>fsRarId (query, xs:string) | read-only |
| `POST` | `/v2/reports/olap` | body: application/json;charset=UTF-8 | read-only отчет |
| `GET` | `/v2/reports/olap/byPresetId/{presetId}` | summary (query, xs:boolean)<br>dateFrom (query, xs:string)<br>dateTo (query, xs:string) | read-only |
| `GET` | `/v2/reports/olap/columns` | reportType (query, xs:string) | read-only |
| `GET` | `/v2/reports/olap/presets` |  | read-only |
| `GET` | `/v2/reports/olap/presets/{presetType}` |  | read-only |

### `/documents`
| Метод | Path | Параметры / тело | Риск |
| --- | --- | --- | --- |
| `POST` | `/documents/check/incomingInventory` | body: application/xml<br>body: application/json | опасно: меняет/создает/удаляет |
| `GET` | `/documents/export/incomingInvoice` | from (query, xs:string)<br>to (query, xs:string)<br>revisionFrom (query, xs:int)<br>supplierId (query, xs:string) | read-only |
| `GET` | `/documents/export/incomingInvoice/byNumber` | number (query, xs:string)<br>from (query, xs:string)<br>to (query, xs:string)<br>currentYear (query, xs:boolean) | read-only |
| `GET` | `/documents/export/lastDocuments` | requestedCount (query, xs:int)<br>storeId (query, xs:string)<br>productId (query, xs:string)<br>documentType (query, xs:string) | read-only |
| `GET` | `/documents/export/outgoingInvoice` | from (query, xs:string)<br>to (query, xs:string)<br>revisionFrom (query, xs:int)<br>supplierId (query, xs:string) | read-only |
| `GET` | `/documents/export/outgoingInvoice/byNumber` | number (query, xs:string)<br>from (query, xs:string)<br>to (query, xs:string)<br>currentYear (query, xs:boolean) | read-only |
| `POST` | `/documents/import/incomingInventory` | body: application/xml<br>body: application/json | опасно: меняет/создает/удаляет |
| `POST` | `/documents/import/incomingInvoice` | body: application/xml<br>body: application/json | опасно: меняет/создает/удаляет |
| `POST` | `/documents/import/outgoingInvoice` | body: application/xml<br>body: application/json | опасно: меняет/создает/удаляет |
| `POST` | `/documents/import/productionDocument` | body: application/xml<br>body: application/json | опасно: меняет/создает/удаляет |
| `POST` | `/documents/import/returnedInvoice` | body: application/xml<br>body: application/json | опасно: меняет/создает/удаляет |
| `POST` | `/documents/import/salesDocument` | body: application/xml<br>body: application/json | опасно: меняет/создает/удаляет |
| `POST` | `/documents/unprocess/incomingInvoice` | body: application/xml<br>body: application/json | опасно: меняет/создает/удаляет |
| `POST` | `/documents/unprocess/outgoingInvoice` | body: application/xml<br>body: application/json | опасно: меняет/создает/удаляет |

### `/orders`
| Метод | Path | Параметры / тело | Риск |
| --- | --- | --- | --- |
| `POST` | `/orders/add` | timeoutMs (query, xs:int) | опасно: меняет/создает/удаляет |
| `POST` | `/orders/checkAddress` |  | опасно: меняет/создает/удаляет |
| `POST` | `/orders/checkCreate` | timeoutMs (query, xs:int) | опасно: меняет/создает/удаляет |
| `GET` | `/orders/deliveryHistory` | customer (query, xs:string)<br>maxResults (query, xs:int) | read-only |
| `GET` | `/orders/deliveryHistoryByPhone` | phone (query, xs:string)<br>maxResults (query, xs:int) | read-only |
| `GET` | `/orders/deliveryOrders` | deliveryTerminalId (query, xs:string)<br>dateFrom (query, xs:string)<br>dateTo (query, xs:string)<br>deliveryStatus (query, xs:string) | read-only |
| `GET` | `/orders/info` | order (query, xs:string) | read-only |

### `/employees`
| Метод | Path | Параметры / тело | Риск |
| --- | --- | --- | --- |
| `GET` | `/employees` | includeDeleted (query, xs:boolean)<br>revisionFrom (query, xs:int) | read-only |
| `GET` | `/employees/attendance` | withPaymentDetails (query, xs:boolean)<br>from (query, xs:string)<br>to (query, xs:string)<br>revisionFrom (query, xs:int) | read-only |
| `GET` | `/employees/attendance/byDepartment/{departmentId}` | withPaymentDetails (query, xs:boolean)<br>from (query, xs:string)<br>to (query, xs:string)<br>revisionFrom (query, xs:int) | read-only |
| `GET` | `/employees/attendance/byDepartment/{departmentId}/byEmployee/{employeeId}` | withPaymentDetails (query, xs:boolean)<br>from (query, xs:string)<br>to (query, xs:string)<br>revisionFrom (query, xs:int) | read-only |
| `GET` | `/employees/attendance/byEmployee/{employeeId}` | withPaymentDetails (query, xs:boolean)<br>from (query, xs:string)<br>to (query, xs:string)<br>revisionFrom (query, xs:int) | read-only |
| `DELETE` | `/employees/attendance/byId/{attendanceId}` |  | опасно: меняет/создает/удаляет |
| `POST` | `/employees/attendance/create` | body: application/xml<br>body: application/json | опасно: меняет/создает/удаляет |
| `GET` | `/employees/attendance/department/{departmentId}` | withPaymentDetails (query, xs:boolean)<br>from (query, xs:string)<br>to (query, xs:string)<br>revisionFrom (query, xs:int) | read-only |
| `GET` | `/employees/attendance/department/{departmentId}/byEmployee/{employeeId}` | withPaymentDetails (query, xs:boolean)<br>from (query, xs:string)<br>to (query, xs:string)<br>revisionFrom (query, xs:int) | read-only |
| `GET` | `/employees/attendance/types` | includeDeleted (query, xs:boolean)<br>revisionFrom (query, xs:int) | read-only |
| `POST` | `/employees/attendance/update` | body: application/xml<br>body: application/json | опасно: меняет/создает/удаляет |
| `GET` | `/employees/availability/list` | from (query, xs:string)<br>to (query, xs:string)<br>department (query, xs:string)<br>role (query, xs:string)<br>user (query, xs:string) | read-only |
| `GET` | `/employees/byCode/{code}` | includeDeleted (query, xs:boolean) | read-only |
| `PUT` | `/employees/byCode/{code}` | body: application/xml<br>body: application/json | опасно: меняет/создает/удаляет |
| `GET` | `/employees/byDepartment/{departmentId}` | includeDeleted (query, xs:boolean) | read-only |
| `DELETE` | `/employees/byId/{id}` |  | опасно: меняет/создает/удаляет |
| `GET` | `/employees/byId/{id}` |  | read-only |
| `POST` | `/employees/byId/{id}` | body: application/x-www-form-urlencoded | опасно: меняет/создает/удаляет |
| `PUT` | `/employees/byId/{id}` | body: application/xml<br>body: application/json | опасно: меняет/создает/удаляет |
| `GET` | `/employees/roles` | includeDeleted (query, xs:boolean)<br>revisionFrom (query, xs:int) | read-only |
| `GET` | `/employees/roles/byCode/{code}` | includeDeleted (query, xs:boolean) | read-only |
| `PUT` | `/employees/roles/byCode/{code}` | includeDeleted (query, xs:boolean)<br>body: application/xml<br>body: application/json | опасно: меняет/создает/удаляет |
| `DELETE` | `/employees/roles/byId/{id}` |  | опасно: меняет/создает/удаляет |
| `GET` | `/employees/roles/byId/{id}` |  | read-only |
| `POST` | `/employees/roles/byId/{id}` | body: application/x-www-form-urlencoded | опасно: меняет/создает/удаляет |
| `PUT` | `/employees/roles/byId/{id}` | body: application/xml<br>body: application/json | опасно: меняет/создает/удаляет |
| `GET` | `/employees/roles/search` |  | read-only |
| `GET` | `/employees/salary` | revisionFrom (query, xs:int) | read-only |
| `GET` | `/employees/salary/byId/{employeeId}` |  | read-only |
| `GET` | `/employees/salary/byId/{employeeId}/{date}` |  | read-only |
| `POST` | `/employees/salary/byId/{employeeId}/{date}` | body: application/x-www-form-urlencoded | опасно: меняет/создает/удаляет |
| `GET` | `/employees/schedule` | withPaymentDetails (query, xs:boolean)<br>from (query, xs:string)<br>to (query, xs:string)<br>revisionFrom (query, xs:int) | read-only |
| `GET` | `/employees/schedule/byDepartment/{departmentId}` | withPaymentDetails (query, xs:boolean)<br>from (query, xs:string)<br>to (query, xs:string)<br>revisionFrom (query, xs:int) | read-only |
| `GET` | `/employees/schedule/byDepartment/{departmentId}/byEmployee/{employeeId}` | withPaymentDetails (query, xs:boolean)<br>from (query, xs:string)<br>to (query, xs:string)<br>revisionFrom (query, xs:int) | read-only |
| `GET` | `/employees/schedule/byEmployee/{employeeId}` | withPaymentDetails (query, xs:boolean)<br>from (query, xs:string)<br>to (query, xs:string)<br>revisionFrom (query, xs:int) | read-only |
| `DELETE` | `/employees/schedule/byId/{scheduleId}` |  | опасно: меняет/создает/удаляет |
| `POST` | `/employees/schedule/create` | body: application/xml<br>body: application/json | опасно: меняет/создает/удаляет |
| `GET` | `/employees/schedule/department/{departmentId}` | withPaymentDetails (query, xs:boolean)<br>from (query, xs:string)<br>to (query, xs:string)<br>revisionFrom (query, xs:int) | read-only |
| `GET` | `/employees/schedule/department/{departmentId}/byEmployee/{employeeId}` | withPaymentDetails (query, xs:boolean)<br>from (query, xs:string)<br>to (query, xs:string)<br>revisionFrom (query, xs:int) | read-only |
| `GET` | `/employees/schedule/types` |  | read-only |
| `POST` | `/employees/schedule/update` | body: application/xml<br>body: application/json | опасно: меняет/создает/удаляет |
| `GET` | `/employees/search` |  | read-only |
| `GET` | `/employees/waiterTeams` | includeDeleted (query, xs:boolean)<br>revisionFrom (query, xs:int) | read-only |
| `GET` | `/employees/waiterTeams/assignments` | revisionFrom (query, xs:int) | read-only |
| `PUT` | `/employees/waiterTeams/assignments` | body: application/xml<br>body: application/json | опасно: меняет/создает/удаляет |
| `GET` | `/employees/waiterTeams/assignments/byDepartment/{departmentId}` | revisionFrom (query, xs:int) | read-only |
| `GET` | `/employees/waiterTeams/byCode/{code}` | includeDeleted (query, xs:boolean) | read-only |
| `PUT` | `/employees/waiterTeams/byCode/{code}` | body: application/xml<br>body: application/json | опасно: меняет/создает/удаляет |
| `GET` | `/employees/waiterTeams/byDepartment/{departmentId}` | revisionFrom (query, xs:int)<br>includeDeleted (query, xs:boolean) | read-only |
| `DELETE` | `/employees/waiterTeams/byId/{id}` |  | опасно: меняет/создает/удаляет |
| `GET` | `/employees/waiterTeams/byId/{id}` |  | read-only |
| `POST` | `/employees/waiterTeams/byId/{id}` | body: application/x-www-form-urlencoded | опасно: меняет/создает/удаляет |
| `PUT` | `/employees/waiterTeams/byId/{id}` | body: application/xml<br>body: application/json | опасно: меняет/создает/удаляет |
| `GET` | `/employees/waiterTeams/search` |  | read-only |

### `/corporation`
| Метод | Path | Параметры / тело | Риск |
| --- | --- | --- | --- |
| `GET` | `/corporation/departments` | includeDeleted (query, xs:boolean)<br>revisionFrom (query, xs:int) | read-only |
| `GET` | `/corporation/departments/search` |  | read-only |
| `GET` | `/corporation/groupById/{id}` |  | read-only |
| `GET` | `/corporation/groups` | includeDeleted (query, xs:boolean)<br>revisionFrom (query, xs:int) | read-only |
| `GET` | `/corporation/groups/search` |  | read-only |
| `GET` | `/corporation/stores` | includeDeleted (query, xs:boolean)<br>revisionFrom (query, xs:int) | read-only |
| `GET` | `/corporation/stores/search` |  | read-only |
| `GET` | `/corporation/terminals` | includeDeleted (query, xs:boolean)<br>revisionFrom (query, xs:int) | read-only |
| `GET` | `/corporation/terminals/search` |  | read-only |

### `/products`
| Метод | Path | Параметры / тело | Риск |
| --- | --- | --- | --- |
| `GET` | `/products` | includeDeleted (query, xs:boolean)<br>revisionFrom (query, xs:int) | read-only |
| `GET` | `/products/accountingCategory` | includeDeleted (query, xs:boolean)<br>revisionFrom (query, xs:int) | read-only |
| `GET` | `/products/alcoholClass` | includeDeleted (query, xs:boolean)<br>revisionFrom (query, xs:int) | read-only |
| `GET` | `/products/cookingPlaceType` | includeDeleted (query, xs:boolean)<br>revisionFrom (query, xs:int) | read-only |
| `GET` | `/products/cookingType` | includeDeleted (query, xs:boolean)<br>revisionFrom (query, xs:int) | read-only |
| `GET` | `/products/customCategoryList` | includeDeleted (query, xs:boolean)<br>revisionFrom (query, xs:int) | read-only |
| `GET` | `/products/customCategoryValue` | includeDeleted (query, xs:boolean)<br>revisionFrom (query, xs:int) | read-only |
| `GET` | `/products/departmentEntity` | includeDeleted (query, xs:boolean)<br>revisionFrom (query, xs:int) | read-only |
| `POST` | `/products/import/deleteProducts` | body: application/xml<br>body: application/json | опасно: меняет/создает/удаляет |
| `POST` | `/products/import/product` | body: application/xml<br>body: application/json | опасно: меняет/создает/удаляет |
| `DELETE` | `/products/import/productGroups` | body: application/xml<br>body: application/json | опасно: меняет/создает/удаляет |
| `POST` | `/products/import/restoreProductGroups` | body: application/xml<br>body: application/json | опасно: меняет/создает/удаляет |
| `POST` | `/products/import/restoreProducts` | body: application/xml<br>body: application/json | опасно: меняет/создает/удаляет |
| `GET` | `/products/measureUnit` | includeDeleted (query, xs:boolean)<br>revisionFrom (query, xs:int) | read-only |
| `GET` | `/products/productCategory` | includeDeleted (query, xs:boolean)<br>revisionFrom (query, xs:int) | read-only |
| `GET` | `/products/productGroup` | includeDeleted (query, xs:boolean)<br>revisionFrom (query, xs:int) | read-only |
| `GET` | `/products/productTreeEntity` | includeDeleted (query, xs:boolean)<br>revisionFrom (query, xs:int) | read-only |
| `GET` | `/products/productTypeForCooking` | includeDeleted (query, xs:boolean)<br>revisionFrom (query, xs:int) | read-only |
| `GET` | `/products/restaurantSection` | includeDeleted (query, xs:boolean)<br>revisionFrom (query, xs:int) | read-only |
| `GET` | `/products/search` |  | read-only |
| `GET` | `/products/store` | includeDeleted (query, xs:boolean)<br>revisionFrom (query, xs:int) | read-only |
| `GET` | `/products/user` | includeDeleted (query, xs:boolean)<br>revisionFrom (query, xs:int) | read-only |

### `/deliverySettings`
| Метод | Path | Параметры / тело | Риск |
| --- | --- | --- | --- |
| `GET` | `/deliverySettings/getDeliveryCourierMobileSettings` |  | read-only |
| `GET` | `/deliverySettings/getDeliveryRestrictions` |  | read-only |
| `GET` | `/deliverySettings/getDeliveryTerminals` |  | read-only |

### `/rmsSettings`
| Метод | Path | Параметры / тело | Риск |
| --- | --- | --- | --- |
| `GET` | `/rmsSettings/getCouriers` |  | read-only |
| `GET` | `/rmsSettings/getEmployees` |  | read-only |
| `GET` | `/rmsSettings/getOrderTypes` |  | read-only |
| `GET` | `/rmsSettings/getPaymentTypes` |  | read-only |
| `GET` | `/rmsSettings/getRestaurantSections` |  | read-only |
| `GET` | `/rmsSettings/getRoles` |  | read-only |

### `/stopLists`
| Метод | Path | Параметры / тело | Риск |
| --- | --- | --- | --- |
| `GET` | `/stopLists/getDeliveryStopList` |  | read-only |

### `/auth`
| Метод | Path | Параметры / тело | Риск |
| --- | --- | --- | --- |
| `GET` | `/auth` | login (query, xs:string)<br>pass (query, xs:string)<br>licenseRequestId (query, xs:string) | read-only |
| `POST` | `/auth` | body: application/x-www-form-urlencoded | опасно: меняет/создает/удаляет |

### `/logout`
| Метод | Path | Параметры / тело | Риск |
| --- | --- | --- | --- |
| `GET` | `/logout` |  | read-only |
| `POST` | `/logout` | body: application/x-www-form-urlencoded | опасно: меняет/создает/удаляет |

### `/closeSession`
| Метод | Path | Параметры / тело | Риск |
| --- | --- | --- | --- |
| `GET` | `/closeSession/list` | dateFrom (query, xs:string)<br>dateTo (query, xs:string) | read-only |

### `/common`
| Метод | Path | Параметры / тело | Риск |
| --- | --- | --- | --- |
| `GET` | `/common/measureUnits` | includeDeleted (query, xs:boolean)<br>revisionFrom (query, xs:int) | read-only |

### `/edi`
| Метод | Path | Параметры / тело | Риск |
| --- | --- | --- | --- |
| `PUT` | `/edi/{senderId}/invoice` | body: application/xml<br>body: application/json | опасно: меняет/создает/удаляет |
| `PUT` | `/edi/{senderId}/orders/ack` | number (query, xs:string)<br>date (query, xs:string)<br>status (query, xs:string) | опасно: меняет/создает/удаляет |
| `GET` | `/edi/{senderId}/orders/bySeller` | gln (query, xs:string)<br>inn (query, xs:string)<br>kpp (query, xs:string)<br>name (query, xs:string) | read-only |
| `POST` | `/edi/{senderId}/orders/create` | body: application/xml<br>body: application/json | опасно: меняет/создает/удаляет |
| `GET` | `/edi/{senderId}/orders/list` | from (query, xs:string)<br>to (query, xs:string)<br>revisionFrom (query, xs:int) | read-only |
| `PUT` | `/edi/{senderId}/orders/register` | body: application/xml<br>body: application/json | опасно: меняет/создает/удаляет |
| `PUT` | `/edi/{senderId}/orders/send` | body: application/xml<br>body: application/json | опасно: меняет/создает/удаляет |
| `PUT` | `/edi/{senderId}/orders/unregister` | body: application/xml<br>body: application/json | опасно: меняет/создает/удаляет |
| `PUT` | `/edi/{senderId}/orders/update` | body: application/xml<br>body: application/json | опасно: меняет/создает/удаляет |
| `PUT` | `/edi/{senderId}/response` | body: application/xml<br>body: application/json | опасно: меняет/создает/удаляет |

### `/events`
| Метод | Path | Параметры / тело | Риск |
| --- | --- | --- | --- |
| `GET` | `/events` | from_time (query, xs:string)<br>to_time (query, xs:string)<br>from_rev (query, xs:int) | read-only |
| `POST` | `/events` | from_time (query, xs:string)<br>to_time (query, xs:string)<br>from_rev (query, xs:int)<br>body: application/xml | опасно: меняет/создает/удаляет |
| `POST` | `/events/add` | body: application/xml | опасно: меняет/создает/удаляет |
| `GET` | `/events/metadata` |  | read-only |
| `POST` | `/events/metadata` | body: application/xml | опасно: меняет/создает/удаляет |
| `GET` | `/events/sessions` | from_time (query, xs:string)<br>to_time (query, xs:string) | read-only |

### `/licence`
| Метод | Path | Параметры / тело | Риск |
| --- | --- | --- | --- |
| `GET` | `/licence` | token (query, xs:string)<br>moduleId (query, xs:int) | read-only |
| `POST` | `/licence` | body: application/x-www-form-urlencoded | опасно: меняет/создает/удаляет |
| `GET` | `/licence/check` | token (query, xs:string)<br>apiToken (query, xs:string)<br>moduleId (query, xs:int) | read-only |
| `POST` | `/licence/check` | body: application/x-www-form-urlencoded | опасно: меняет/создает/удаляет |
| `GET` | `/licence/clean` | token (query, xs:string)<br>apiToken (query, xs:string) | опасно/side effect |
| `POST` | `/licence/clean` | body: application/x-www-form-urlencoded | опасно: меняет/создает/удаляет |
| `GET` | `/licence/info` | moduleId (query, xs:int) | read-only |
| `GET` | `/licence/isModulePresent` | token (query, xs:string)<br>moduleId (query, xs:int) | read-only |

### `/replication`
| Метод | Path | Параметры / тело | Риск |
| --- | --- | --- | --- |
| `GET` | `/replication/byDepartmentId/{departmentId}/status` |  | read-only |
| `GET` | `/replication/serverType` |  | read-only |
| `GET` | `/replication/statuses` |  | read-only |

### `/suppliers`
| Метод | Path | Параметры / тело | Риск |
| --- | --- | --- | --- |
| `GET` | `/suppliers` | includeDeleted (query, xs:boolean)<br>revisionFrom (query, xs:int) | read-only |
| `GET` | `/suppliers/search` |  | read-only |
| `GET` | `/suppliers/{code}/pricelist` | date (query, xs:string) | read-only |

### `/test_replication`
| Метод | Path | Параметры / тело | Риск |
| --- | --- | --- | --- |
| `GET` | `/test_replication/createDeliveryTerminal` | departmentId (query, xs:string) | опасно/side effect |
| `GET` | `/test_replication/createDepartment` | departmentId (query, xs:string) | опасно/side effect |
| `GET` | `/test_replication/createProductWithTreeMenuChangeDocument` | price (query, xs:string) | опасно/side effect |
| `GET` | `/test_replication/getPriceByProductDepartment` | departmentId (query, xs:string)<br>productId (query, xs:string) | read-only |
| `GET` | `/test_replication/increaseRevision` |  | опасно/side effect |
| `GET` | `/test_replication/initReplication` |  | опасно/side effect |

### `/trans`
| Метод | Path | Параметры / тело | Риск |
| --- | --- | --- | --- |
| `POST` | `/trans/add` | body: application/xml<br>body: application/json | опасно: меняет/создает/удаляет |
| `POST` | `/trans/list` |  | опасно: меняет/создает/удаляет |
| `POST` | `/trans/list/byIds` |  | опасно: меняет/создает/удаляет |

### `/v3`
| Метод | Path | Параметры / тело | Риск |
| --- | --- | --- | --- |
| `POST` | `/v3/{service}.{method}` | body: application/xml | опасно: меняет/создает/удаляет |
| `GET` | `/v3/{service}/{method}` |  | read-only |

### `/version`
| Метод | Path | Параметры / тело | Риск |
| --- | --- | --- | --- |
| `GET` | `/version` |  | read-only |
| `GET` | `/version/test` |  | read-only |
