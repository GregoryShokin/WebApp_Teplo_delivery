# iiko Finance: chart of accounts and Main Cash

Дата фиксации: 2026-05-20.  
Последнее обновление: 2026-05-25 (закрыты owner-вопросы по `Алиса наличные`, `Перемещения`, `Всякое Гагарина`; `Всякое Черникова` выделено в отдельный review-слой).

Статус: **подключен read-only через iiko OLAP TRANSACTIONS**. Целевая выгрузка `Главная касса` за 2026-02-01..2026-05-20 дала 279 операций и 13 уникальных корсчетов.

## Назначение

Этот документ фиксирует проверенный способ получать операции из iiko `Финансы -> План счетов -> Главная касса`.

Основной бизнес-кейс: источник операций wallet `ТК Черникова` для будущего DDS-модуля. Физически это торговая касса активной точки Черникова: наличная выручка на баре, удержанные депозиты курьеров и оперативные расходы администраторов.

Потенциально тот же подход применим к другим кассовым счетам iiko, но текущая разведка ограничена только `Главная касса`.

## Связь с модулями

- DDS module: [21-dds-module-spec.md](/app-spec/modules/finance/dds/spec.md), раздел 2, `ТК Черникова`.
- Payroll module: [payroll engine spec](/app-spec/modules/staff/payroll/00-engine.md), `deposit_accounts` и `deposit_transactions`.
- Общая карта iiko API: [08-iiko-server-api-endpoints.md](/app-spec/integrations/iiko/server-api-endpoints.md).
- Processed-артефакты: `research/processed/iiko/finance_chart/`.
- Raw-артефакты: `research/raw/iiko/finance_chart_research/` (ignored, может содержать PII в комментариях iiko).

## Авторизация

Используется общий iikoServer Resto API контур из [08-iiko-server-api-endpoints.md](/app-spec/integrations/iiko/server-api-endpoints.md):

- секреты только из `.env` / `ENV`;
- токен передается query-параметром `key`;
- данные запрашивались только GET-запросами;
- если токен истек, общий `IikoClient` может обновить его через `POST /auth`, но операции данных не меняются.

## Найденные endpoint'ы

| Путь | Метод | Статус | Что возвращает | Формат дат | Обязательные параметры |
| --- | --- | --- | --- | --- | --- |
| `/application.wadl` | GET | 200 | WADL текущего iikoServer, 276 method/path combinations | — | `key` |
| `/v2/reports/olap/columns` | GET | 200 | каталог полей OLAP; для `TRANSACTIONS` найдено 116 полей | — | `reportType=TRANSACTIONS` |
| `/reports/olap` | GET | 200 | **канонический источник операций Главной кассы** через `report=TRANSACTIONS` | `dd.MM.yyyy`, на практике inclusive | `report`, `from`, `to`, `groupRow`, `agr` |
| `/v2/reports/olap/presets` | GET | 200 | сохраненные OLAP-presets; есть ДДС-presets, но они агрегируют по статьям | — | `key` |
| `/v2/reports/olap/byPresetId/{presetId}` | GET | 200 | сохраненные отчеты `Отчет о движении денежных средств` и `...по подразделениям` | `yyyy-MM-dd`, `dateTo` exclusive | `dateFrom`, `dateTo` |
| `/v2/entities/accounts/list` | GET | 200 | справочник счетов плана счетов; источник `account_id`, `code`, `type` | — | `includeDeleted` optional |
| `/v2/cashshifts/list` | GET | 200 | кассовые смены, `salesCash`, `salesCard`, `payIn`, `payOut` | `yyyy-MM-dd` | `status` обязателен |
| `/v2/cashshifts/payments/list/{sessionId}` | GET | 200 | платежи по кассовой смене | — | `sessionId` в path |
| `/v2/reports/balance/counteragents` | GET | 200 | остатки по счету и контрагентам на timestamp | `yyyy-MM-ddTHH:mm:ss` | `timestamp`; `account` полезен |
| `/v2/reports/balance/stores` | GET | 200 | складские остатки, не операции кассы | `yyyy-MM-ddTHH:mm:ss` | `timestamp` |
| `/v2/documents/internalTransfer` | GET | 200, pilot empty | документы внутренних перемещений; в пилоте строк Главной кассы не дал | `yyyy-MM-dd` | `dateFrom`, `dateTo`, `status` |

Вывод: прямого finance endpoint вида `/finance/accounts` или `/finance/cashFlow` в WADL не найдено. Основной путь к операциям плана счетов на этом сервере — OLAP `TRANSACTIONS`.

## Основной endpoint

```text
GET /resto/api/reports/olap
```

Параметры для операций Главной кассы:

```text
report=TRANSACTIONS
summary=false
from=01.04.2026
to=30.04.2026
groupRow=DateTime.DateTyped
groupRow=Account.Name
groupRow=Contr-Account.Name
groupRow=TransactionSide
groupRow=TransactionType
groupRow=Document
groupRow=CashFlowCategory
groupRow=Comment
agr=Sum.ResignedSum
agr=Sum.Incoming
agr=Sum.Outgoing
```

Фильтр `Account.Name = Главная касса` применяется на стороне экспортера после получения OLAP rows.

Скрипт:

```text
research/scripts/iiko/export_finance_chart.py
```

Raw за целевой период:

```text
research/raw/iiko/finance_chart_research/glavnaya_kassa_2026-02.json
research/raw/iiko/finance_chart_research/glavnaya_kassa_2026-03.json
research/raw/iiko/finance_chart_research/glavnaya_kassa_2026-04.json
research/raw/iiko/finance_chart_research/glavnaya_kassa_2026-05.json
```

## Схема операции Главная касса

Raw JSON wrapper экспортера:

```json
{
  "endpoint": "/reports/olap",
  "method": "GET",
  "status": 200,
  "period_start": "2026-04-01",
  "period_end": "2026-04-30",
  "params_redacted": {
    "report": "TRANSACTIONS",
    "summary": "false",
    "from": "01.04.2026",
    "to": "30.04.2026",
    "groupRow": ["DateTime.DateTyped", "Account.Name", "Contr-Account.Name"],
    "agr": ["Sum.ResignedSum", "Sum.Incoming", "Sum.Outgoing"]
  },
  "account_filter": "Главная касса",
  "records": [
    {
      "DateTime.DateTyped": "Wed Apr 01 00:00:00 MSK 2026",
      "Account.Id": "<main_cash_account_id_redacted>",
      "Account.Code": "1.13",
      "Account.Name": "Главная касса",
      "Account.Type": "CASH",
      "Contr-Account.Id": "<corr_account_id_redacted>",
      "Contr-Account.Code": "<redacted>",
      "Contr-Account.Name": "Торговые кассы",
      "Contr-Account.Type": "CASH",
      "TransactionSide": "DEBIT",
      "TransactionType": "PAYOUT",
      "Document": "1006",
      "CashFlowCategory": "",
      "Comment": "<redacted PII/comment>",
      "Sum.ResignedSum": "9067.000000000",
      "Sum.Incoming": "9067.000000000",
      "Sum.Outgoing": "0"
    }
  ]
}
```

Ключевые поля:

| Поле | Тип | Nullable | Business meaning | Target app column |
| --- | --- | --- | --- | --- |
| `records[].DateTime.DateTyped` | date-string | no | учетный день операции | `cashflow_transactions.operation_date` |
| `records[].Account.Id` | uuid | no | обезличенный id счета `Главная касса` | `cashflow_transactions.source_account_id` |
| `records[].Account.Name` | string | no | счет-источник wallet `ТК Черникова` | `wallets.source_account_name` |
| `records[].Account.Type` | enum | no | тип счета iiko, для Главной кассы `CASH` | `wallets.source_account_type` |
| `records[].Contr-Account.Id` | uuid | yes | обезличенный id корсчета | `cashflow_transactions.corr_account_id` |
| `records[].Contr-Account.Name` | string | no | корсчет, главный ключ классификации ДДС | `cashflow_transactions.corr_account_name` |
| `records[].Contr-Account.Type` | enum | yes | тип корсчета | `cashflow_transactions.corr_account_type` |
| `records[].TransactionSide` | enum | no | `DEBIT` = поступление, `CREDIT` = расход | `cashflow_transactions.direction` |
| `records[].TransactionType` | enum | no | тип транзакции iiko | `cashflow_transactions.source_operation_type` |
| `records[].Document` | string | yes | номер документа iiko | `source_documents.external_number` |
| `records[].CashFlowCategory` | string | yes | статья ДДС iiko; надежно заполнена не всегда | `cashflow_transactions.iiko_cashflow_category` |
| `records[].Comment` | string | yes | приватный контекст операции, может содержать PII | `cashflow_transactions.source_comment_private` |
| `records[].Sum.ResignedSum` | money-string | no | сумма со знаком в iiko | `cashflow_transactions.amount_signed` |
| `records[].Sum.Incoming` | money-string | no | дебет Главной кассы | `cashflow_transactions.debit_amount` |
| `records[].Sum.Outgoing` | money-string | no | кредит Главной кассы | `cashflow_transactions.credit_amount` |

Полная machine-readable схема: `research/processed/iiko/finance_chart/glavnaya_kassa_field_schema.csv`.

## Маппинг в cashflow_transactions

| iiko | Правило | Приложение |
| --- | --- | --- |
| `Account.Name = Главная касса` | wallet фиксирован | `wallet_id = ТК Черникова` |
| `DateTime.DateTyped` | привести к date в MSK | `operation_date` |
| `TransactionSide=DEBIT` / `Sum.Incoming` | положительное поступление | `amount_signed > 0`, `direction=inflow` |
| `TransactionSide=CREDIT` / `Sum.Outgoing` | отрицательный расход | `amount_signed < 0`, `direction=outflow` |
| `Contr-Account.Name` | основной ключ rule engine | `corr_account_name`, `classification_rule.input` |
| `CashFlowCategory` | использовать только как подсказку/автостатус | `iiko_cashflow_category` |
| `Comment` | хранить только в private/source layer | `source_comment_private`, не в Markdown |
| `Document` | внешний номер документа | `source_documents.external_number` |
| raw file path | traceability | `source_file`, `import_batch_id` |

Классификация:

```text
if corr_account == "Задолженность перед поставщиками":
    dds_article = "Оплата поставщикам"
    status = "iiko_auto_classified"
else:
    dds_article = rule_engine(corr_account, comment_private, direction, amount)
    status = "classified" или "owner_review"
```

## Справочник корсчетов

Период: 2026-02-01..2026-05-20. Май частичный, до 20 мая включительно.

| Корсчет | Debit count | Debit sum | Credit count | Credit sum | Гипотеза статьи ДДС | Статус |
| --- | ---: | ---: | ---: | ---: | --- | --- |
| Торговые кассы | 92 | 830 260.00 | 0 | 0.00 | Наличная торговая выручка | `rule_candidate_sales_cash` |
| Текущие расчеты с сотрудниками | 0 | 0.00 | 43 | 828 282.00 | Расходы на персонал | `rule_candidate_owner_review` |
| Алиса наличные | 3 | 417 023.00 | 0 | 0.00 | Партнерская дебиторка: клиент оплатил партнеру напрямую, cash появляется при последующем получении денег | `partner_receivable` |
| Прочие расходы | 0 | 0.00 | 1 | 156 938.00 | Прочие операционные расходы | `owner_review` |
| Перемещения | 0 | 0.00 | 4 | 84 000.00 | Legacy-перемещения времен точки Гагарина | `legacy_internal_transfer_review` |
| Задолженность перед поставщиками | 0 | 0.00 | 19 | 66 648.28 | Оплата поставщикам | `iiko_auto_classified` |
| Всякое Черникова | 0 | 0.00 | 25 | 53 567.92 | Мелкие расходы Черникова: пакеты, тряпки, губки и т.п.; нужен отдельный слой классификации | `requires_owner_review` |
| Депозиты сотрудников | 51 | 24 100.00 | 6 | 24 500.00 | Операционный корсчет депозитов курьеров; контроль DDS/ГарантФонд, не самостоятельный payroll-subledger | `deposit_control_source` |
| Прочие доходы | 15 | 22 850.92 | 0 | 0.00 | Прочие поступления | `owner_review` |
| Основные средства | 0 | 0.00 | 1 | 9 977.00 | Не определено | `owner_review` |
| Затраты на персонал | 0 | 0.00 | 1 | 3 071.14 | Расходы на персонал | `rule_candidate_owner_review` |
| Актуализация | 12 | 511.15 | 5 | 53.73 | Техническая корректировка | `owner_review` |
| Всякое Гагарина | 0 | 0.00 | 1 | 100.00 | Хвост старой точки; нужен для исторической информации | `legacy_reference` |

CSV: `research/processed/iiko/finance_chart/glavnaya_kassa_corraccounts_summary.csv`.

## Задолженность перед поставщиками

Это единственный подтвержденный корсчет, где iiko-бизнес-логика уже соответствует ДДС-статье:

| Корсчет | Направление | Строк | Сумма | Статья ДДС |
| --- | --- | ---: | ---: | --- |
| Задолженность перед поставщиками | credit / расход | 19 | 66 648.28 | Оплата поставщикам |

Для импорта можно ставить статус `iiko_auto_classified`, но все равно сохранять исходный документ и private-комментарий для аудита.

## Виды дебетовых поступлений

| Тип | Признак | Наблюдение | Действие |
| --- | --- | --- | --- |
| Наличная торговая выручка | `Contr-Account.Name = Торговые кассы` | 92 строки, 830 260.00 руб. | сверять с iiko OLAP SALES `PayTypes=Наличные` |
| Депозиты сотрудников | `Contr-Account.Name = Депозиты сотрудников` и debit | 51 строка, 24 100.00 руб. | использовать как DDS/control source депозитов курьеров; персональный payroll-остаток сверять с зарплатной ведомостью |
| Партнерская дебиторка | `Алиса наличные` | 3 строки, 417 023.00 руб. | не cash-in в момент заказа; cash-fact DDS возникает при фактическом получении денег от партнера |
| Прочие доходы | `Прочие доходы` | 15 строк, 22 850.92 руб. | rule engine по корсчету + комментарию |
| Технические корректировки | `Актуализация` | 12 debit-строк, 511.15 руб. | не смешивать с операционным cashflow без проверки |

## Виды кредитовых расходов

| Тип | Признак | Наблюдение | Действие |
| --- | --- | --- | --- |
| Расчеты с сотрудниками | `Текущие расчеты с сотрудниками` | 43 строки, 828 282.00 руб. | rule candidate, согласовать со схемой payroll/payments |
| Поставщики | `Задолженность перед поставщиками` | 19 строк, 66 648.28 руб. | авто: `Оплата поставщикам` |
| Прочие расходы | `Прочие расходы` | 1 строка, 156 938.00 руб. | owner review |
| Перемещения | `Перемещения` | 4 строки, 84 000.00 руб. | legacy/internal transfer review; счет времен Гагарина |
| Всякое Черникова | `Всякое Черникова` | 25 строк, 53 567.92 руб. | отдельный слой классификации мелких расходов по private-комментарию/чеку |
| Депозиты сотрудников | credit по `Депозиты сотрудников` | 6 строк, 24 500.00 руб. | использовать для DDS/reconciliation депозитов курьеров; не создавать payroll-событие без payroll-сверки |
| Основные средства | `Основные средства` | 1 строка, 9 977.00 руб. | классифицировать как CAPEX/инвест. cashflow после подтверждения |

## Связь с payroll

Owner decision 2026-05-24: корсчёт iiko/DDS `Депозиты сотрудников` на текущий момент **не является источником информации** для payroll-депозитов и списаний депозитов. На него не нужно ориентироваться при расчёте депозитного обязательства.

Уточнение владельца 2026-05-25: операционно депозиты курьеров ведутся администраторами через корсчет iiko `Депозиты курьеров` / `Депозиты сотрудников`; обычно удерживается 500 руб., иногда 200 руб. для подстраховочных смен. Это делает корсчет полезным DDS/control source и связью с `ГарантФонд`, но не отменяет правило: персональный payroll-subledger и расчет обязательств должны сверяться с зарплатной ведомостью.

Правило для приложения:

- primary source депозитов и списаний депозитов - зарплатная ведомость / payroll ledger;
- `Депозиты сотрудников` в iiko TRANSACTIONS остаётся DDS/reconciliation candidate, но не создаёт `deposit_withholding`, `deposit_return` или `deposit_writeoff` автоматически;
- комментарии могут содержать имя сотрудника/курьера, но это PII и остаётся только в private/raw;
- отдельный payroll bridge для этого корсчёта не строится до нового owner decision.

## Связь с iiko OLAP SALES

Наличная выручка должна сходиться так:

```text
SUM(TRANSACTIONS.Sum.Incoming)
where Account.Name = "Главная касса"
  and Contr-Account.Name = "Торговые кассы"

~= SUM(SALES.DishDiscountSumInt)
where PayTypes = "Наличные"
  and department = active Черникова
```

Дополнительная сверка: `/v2/cashshifts/list` возвращает `salesCash` по кассовым сменам. Этот endpoint не заменяет план счетов, но помогает искать расхождения по дням между продажами, кассовой сменой и дебетом Главной кассы.

## Риски и неизвестные

1. `CashFlowCategory` в iiko не является полным классификатором Главной кассы. Большинство строк требует rule engine.
2. OLAP rows агрегируются по выбранным `groupRow`. Для уникальности операции используется комбинация дата + счет + корсчет + side + type + document + private comment. Если в iiko есть две полностью одинаковые строки, OLAP может их схлопнуть.
3. `Comment` содержит потенциальную PII; не переносить в Markdown/processed.
4. `Депозиты сотрудников` / `Депозиты курьеров` являются DDS/control source депозитов курьеров, но не самостоятельным payroll-subledger.
5. `Всякое Черникова` и `Прочие расходы` требуют отдельного rule/review-слоя по private-комментариям и чекам.
6. `Всякое Гагарина` встретилось один раз в апреле 2026 и по owner decision 2026-05-25 является legacy reference старой точки.
7. `/v2/reports/balance/counteragents` может пригодиться для остатков wallet, но не заменяет журнал операций.
8. Май 2026 выгружен частично: 2026-05-01..2026-05-20.

## Открытые вопросы владельцу

1. ✅ закрыто 2026-05-25: `Алиса наличные` - партнерская дебиторка; клиент партнера платит партнеру напрямую, cash в DDS появляется только при фактическом получении денег.
2. ✅ закрыто 2026-05-25: `Перемещения` - рудиментарный счет времен точки Гагарина, использовать как legacy/internal transfer review.
3. `Всякое Черникова` и `Прочие расходы`: нужен отдельный слой классификации по private-комментарию/чеку; не закрывать автоматически в `Прочие`.
4. ✅ закрыто 2026-05-24: credit по `Депозиты сотрудников` не считать автоматически возвратом payroll-депозита; корсчёт не является источником payroll-депозитов.
5. ✅ закрыто 2026-05-25: `Всякое Гагарина` - исторический хвост старой точки, оставить как legacy reference.

## Следующее действие

Построить rule engine для `ТК Черникова`:

1. seed rules по 13 корсчетам из `glavnaya_kassa_corraccounts_summary.csv`;
2. отдельное правило `Задолженность перед поставщиками -> Оплата поставщикам`;
3. `Депозиты сотрудников` использовать как DDS/control source депозитов курьеров и сверять с payroll, без автоматического создания payroll-событий только из iiko;
4. сверка `Торговые кассы` с OLAP SALES `PayTypes=Наличные`;
5. owner review queue для всех строк со статусом `owner_review`.
