# T-Bank API: выписка и безопасные агрегаты

Дата сборки: 2026-05-19.

Источник: T-Bank Business Open API `GET /api/v1/statement`; raw хранится только в `research/private/tbank/`.
В processed-файлы не перенесены полные строки выписки, полные счета, назначения платежей, ФИО и полные ИНН.

## Короткий ответ

- Период операций: `2026-02-01 - 2026-05-19`.
- Операций: 584.
- Поступления всего: 11243113.69 руб.
- Списания всего: 11453025.83 руб.
- Net cashflow: -209912.14 руб.
- Сумма с нераспознанным направлением: 0.00 руб.

## Выгруженные файлы

- `research/processed/tbank/operation_categories.csv`
- `research/processed/tbank/counterparty_summary.csv`
- `research/processed/tbank/cashflow_daily.csv`

## Структура ответа

- Top-level поля: `operations`.
- Поля операции: `_operation_date, _source_file, accountAmount, accountCurrencyDigitalCode, accountNumber, acquirerId, authCode, authorizationDate, bic, cardNumber, category, chargeDate, counterParty, description, docDate, documentNumber, drawDate, mcc, merch, operationAmount, operationCurrencyDigitalCode, operationDate, operationId, operationStatus, payPurpose, payVo, payer, priority, receiver, rrn, rubleAmount, tax, trxnPostDate, typeOfOperation, ucid, vo`.
- Поля `payer`: `acct, bankName, bicRu, corAcct, inn, kpp, name`.
- Поля `receiver`: `acct, bankName, bicRu, corAcct, inn, kpp, name`.
- Поля балансов: `не найдены`.

## Форматы дат

| Поле | Наблюдаемый формат |
| --- | --- |
| `operationDate` | ISO 8601 UTC fractional; ISO 8601 UTC seconds |

## Edge cases

- Операций без категории: 0.
- Операций без распознанной даты: 0.
- Операций с нераспознанным направлением: 0.
- Входящие движения T-Bank помечены как `требует проверки`: не считать их выручкой без сверки со Sber/iiko.
- Категорий: 13.
- Псевдонимов контрагентов: 69.

## Raw-источники

- `research/private/tbank/statement_2026-02-01_2026-05-31_p01.json`
