# Bank Integration Map For DDS

Дата сборки: 2026-05-20.

Источник анализа: локальные документы `docs/business-control/*` и безопасные processed-агрегаты `research/processed/cashflow`, `research/processed/sber`, `research/processed/tbank`. Raw-выписки, полные назначения платежей, счета, ФИО, токены и реквизиты в этот документ не переносились.

## 1. Карта банковского контура

Денежная цепочка подтверждена владельцем 2026-05-19:

```text
Sber -> T-Bank -> контрагенты
       + T-Bank direct acquiring -> T-Bank -> контрагенты
```

Роли банков:

| Банк | Роль в контуре | Что считать фактом | Что нельзя делать автоматически |
| --- | --- | --- | --- |
| Sber | Основной банк входящей выручки, банк-аккумулятор эквайринга | Входящие операции с признаками эквайринга/приема платежей; исходящие на собственный счет T-Bank как внутренний перевод | Нельзя считать любой Sber credit выручкой; нельзя строить основную расходную часть ДДС по Sber |
| T-Bank | Основной банк расходов и расчетов с контрагентами | Исходящие поставщикам, сотрудникам, налогам, банку; входящие нужно разделять на перевод из Sber, собственный T-Bank эквайринг и прочие поступления | Нельзя считать весь T-Bank credit выручкой, потому что большая часть входящих - перенос уже учтенной выручки из Sber |

## 2. Подключенные источники банковского факта

| Источник | Статус | Проверенный период | Объем | Processed-артефакты | Назначение для DDS |
| --- | --- | --- | ---: | --- | --- |
| Sber API `statement/summary`, `statement/transactions` | Подключен read-only с 2026-05-19, промышленный контур | `2026-02-01` - `2026-05-19` | 640 операций, 108 дней; daily summary vs transactions: 108/108 ok | `research/processed/sber/bank_cashflow_daily.csv`, `bank_cashflow_monthly.csv`, `bank_operation_codes.csv`, `bank_counterparty_summary.csv`, `bank_cashflow_articles_draft.csv` | Основной источник банковской выручки Sber, контроль внутренних переводов Sber -> T-Bank, банковские комиссии Sber |
| Sber focused operations table | Построен из Sber raw безопасно | `2026-05-13` - `2026-05-18` | 34 операции | `research/processed/sber/operations_2026-05-13_2026-05-18*.csv` | Проверка структуры эквайринговых поступлений, комиссий и merchant/payment-contract каналов |
| T-Bank Business Open API `/api/v1/statement` | Подключен read-only с 2026-05-19, промышленный контур | Запрос `2026-02-01T00:00:00Z` - `2026-06-01T00:00:00Z`, операции по `2026-05-19` | 584 операции: 83 credit, 501 debit | `research/processed/tbank/operation_categories.csv`, `counterparty_summary.csv`, `cashflow_daily.csv` | Основной источник расходов DDS; входящие T-Bank разделяются на internal transfer, direct acquiring, loan/refund/other |
| T-Bank vs old DDS counterparty match | Processed-сверка | `2026-01..2026-03` | 76 групп + header | `research/processed/tbank/counterparties_2026-01_2026-03_dds_match.csv` | Мост от T-Bank контрагентов к историческим статьям ДДС |
| Combined bank classification | Построчная классификация хранится private, наружу опубликованы агрегаты | `2026-02-01` - `2026-05-19`; iiko-сверка мая ограничена `2026-05-01` - `2026-05-17` | 1224 операции, 1224 с flow_type, 409 требуют owner review | `research/processed/cashflow/dds_by_article_2026.csv`, `revenue_split.csv`, `iiko_vs_bank_reconciliation.csv`, `internal_transfer_check.csv`, `unclassified_operations_summary.csv` | Текущая управленческая классификация банковских операций и контрольные сверки |

## 3. Текущая классификация по flow type

Агрегаты за `2026-02-01` - `2026-05-19`:

| Flow type | Банк | Операций | Inflow | Outflow | Смысл |
| --- | --- | ---: | ---: | ---: | --- |
| `revenue_acquiring_sber` | Sber | 601 | 10 473 123.54 | 0.00 | Sber эквайринг и подтвержденный канал приема платежей |
| `revenue_acquiring_tbank` | T-Bank | 41 | 448 706.00 | 0.00 | Гипотеза прямого T-Bank эквайринга, требует подтверждения владельца |
| `internal_transfer_sber_to_tbank` | Sber | 19 | 0.00 | 9 396 000.00 | Сторона списания внутреннего перевода |
| `internal_transfer_sber_to_tbank` | T-Bank | 19 | 9 396 000.00 | 0.00 | Сторона поступления внутреннего перевода |
| `supplier_payment` | T-Bank | 208 | 0.00 | 5 429 979.40 | Платежи поставщикам и подрядчикам |
| `payroll_payment` | T-Bank | 21 | 0.00 | 3 363 645.00 | Переводы физлицам/ИП, предварительно персонал |
| `tax_payment` | T-Bank | 12 | 0.00 | 738 728.79 | Налоги/взносы/бюджетные платежи |
| `bank_fee` | Sber + T-Bank | 68 | 0.00 | 105 810.69 | РКО, комиссии, комиссии эквайринга |
| `loan_payment` | Sber + T-Bank | 18 | 49 651.18 | 50 248.18 | Кредит/овердрафт, требует разделения тела/процентов/комиссий |
| `refund` | T-Bank | 3 | 2 936.89 | 0.00 | Возвраты, тип возврата требует подтверждения |
| `other_inflow` | Sber + T-Bank | 14 | 1 565 819.62 | 0.00 | Прочие поступления, включая `depositFullWithdrawal` |
| `other_outflow` | Sber + T-Bank | 200 | 0.00 | 3 162 703.77 | Прочие списания, бизнес-карта, self-transfer-like операции, Sber debit review |

## 4. Поля банковской операции для будущего приложения

Canonical transaction record должен хранить банковский факт отдельно от классификации. Минимальный набор:

| Группа | Поля | Зачем нужно | Privacy boundary |
| --- | --- | --- | --- |
| Идентификация источника | `bank`, `source_system`, `source_run_id`, `source_file_private_ref`, `raw_operation_id`, `operation_id_hash` | Идемпотентная загрузка, аудит происхождения строки | В processed можно hash/ref; raw path и полный payload только private |
| Дата и период | `operation_date`, `document_date`, `posting_date`, `value_date`, `bank_timezone`, `month` | DDS-период, лаги эквайринга, сверки с iiko | Можно хранить в processed |
| Деньги | `direction`, `amount_abs`, `signed_amount`, `currency`, `account_amount`, `operation_amount`, `commission_amount`, `vat_amount`, `balance_before`, `balance_after` | ДДС, комиссии, контроль оборотов и баланса | Можно хранить агрегаты; полные счета не нужны |
| Собственный счет | `own_account_id`, `own_account_alias`, `account_role`, `bank_account_hash` | Определение движения между своими счетами | Полный номер счета только private; в processed alias/hash |
| Контрагент | `counterparty_id`, `counterparty_role` (`payer`/`receiver`), `counterparty_key_hash`, `inn_kind`, `inn_mask`, `account_present`, `name_present`, `bank_name_normalized`, `bic_hash_or_alias` | Правила по контрагенту, внутренние переводы, supplier mapping | Полные ИНН, счета, ФИО, названия физлиц и назначения только private; processed - hash/mask/alias |
| Банковская категория | `native_category`, `operation_code`, `operation_kind`, `type_of_operation`, `pay_vo` | Первичный быстрый классификатор | Можно хранить в processed |
| Acquiring-сигналы | `merchant_id_alias`, `acquirer_id_present`, `mcc`, `merch_present`, `rrn_present`, `auth_code_present`, `commission_source` | Отличить эквайринг от прочих поступлений | Merchant/acquirer ids лучше alias/hash; карты/rrn/authCode только private или boolean-present |
| Назначение платежа | `payment_purpose_private_ref`, `description_signature`, `description_snippet_safe`, `text_tokens_safe` | Rule engine и owner review | Полное назначение только private; processed - hash/signature, короткие нечувствительные summary |
| Классификация | `rule_id_matched`, `flow_type`, `dds_article_candidate`, `pnl_line_candidate`, `confidence`, `requires_owner_review`, `review_status`, `owner_comment_ref` | Управленческий DDS, очередь разметки, воспроизводимость | В processed можно flow/article/confidence; owner comments с ПДн - private |

## 5. Сущности для будущего rule engine

| Сущность | Назначение | Где хранить чувствительное |
| --- | --- | --- |
| `bank_accounts` | Собственные счета, роли счетов, банк, валюта, период активности | Полный номер счета, ИНН, КПП - private; processed хранит alias/hash |
| `bank_transactions_raw` | Неизмененный банковский payload | Только `research/private` или защищенное хранилище |
| `bank_transactions_normalized` | Нормализованная операция без полной чувствительной строки | Processed-safe subset плюс private reference |
| `bank_counterparties` | Counterparty hash, маски ИНН, тип лица, статистика оборотов | Полные реквизиты и ФИО - private |
| `bank_operation_rules` | Правила match -> flow_type/article | Паттерны с реальными назначениями и реквизитами - private; schema/template - processed |
| `bank_rule_matches` | Результаты применения правил по операции | Processed-safe без raw текста |
| `bank_flow_type_dictionary` | Словарь экономического смысла flow types | Processed |
| `dds_articles` | Управленческие статьи ДДС и связь с P&L | Processed, если нет чувствительных контрагентов |
| `owner_review_queue` | Группы операций для подтверждения владельцем | В processed только агрегаты/signature; full examples private |
| `bank_reconciliation_runs` | Сверки Sber/T-Bank/iiko, статусы, tolerance | Processed |

## 6. Privacy boundaries

Можно хранить в `research/processed`:

- месячные/дневные агрегаты, flow types, суммы, counts, статусы сверок;
- masked-INN, hash контрагента, aliases `CP0001`/`TCP_*`;
- `description_signature`, безопасные summary без реквизитов и ФИО;
- rule schema, generic patterns, quality status, owner questions;
- ссылки на private source files как технический reference без содержимого.

Только `research/private`:

- raw JSON/CSV банковских выписок;
- полные назначения платежей;
- полные счета, карты, ИНН физлиц, ФИО, договоры, rrn/authCode/cardNumber;
- реальные паттерны правил, если они включают фрагменты назначений или реквизиты;
- реестр собственных счетов с полными номерами;
- owner-review примеры на уровне строки.

