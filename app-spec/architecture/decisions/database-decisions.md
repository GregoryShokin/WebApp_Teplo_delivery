# Database architecture decisions

Дата фиксации: 2026-05-29.
Источник: бывшие §11 и §15 гибридного database architecture документа.

## 11. Архитектурные вопросы и зафиксированные решения

Варианты ниже сохранены для traceability: они показывают исходные конфликтные развилки, а строка «Решение владельца 2026-05-24/25» в каждом подразделе фиксирует принятый вариант.

### 11.1 `counterparty` DDS vs `supplier_counterparty` УДКЗ

**Решение владельца 2026-05-24: вариант A - единый `counterparty` + роли; `supplier_counterparty` УДКЗ становится профильным view/extension над `counterparty` с ролью `supplier`.**

Уточнение владельца 2026-05-25 (C12): единый supplier registry используется как общий выпадающий список поставщиков в платежном календаре, DDS, УДКЗ и balance views. Это не отменяет общий `counterparty`: поставщик является ролью/profile, а не отдельным master. Главные предохранители - aliases между источниками, поддержка нескольких ролей у одного контрагента и хранение supplier-specific полей в supplier-profile, а не в базовой карточке `counterparty`.

| Вариант | Суть | Плюсы | Минусы / риск | Что решает владелец |
| --- | --- | --- | --- | --- |
| A. Единый `counterparty` + role `supplier` | `supplier_counterparty` становится профильной таблицей/расширением к `counterparty` | один справочник aliases, меньше дублей, легче связывать банк/ЭДО/УДКЗ | миграция сложнее; нужно аккуратно с приватностью и разными ролями одного контрагента | готов ли владелец объединить рабочие поставщики, банки, сервисы, сотрудников/owner-группы в один master |
| B. Раздельные таблицы + `counterparty_id` bridge | УДКЗ сохраняет `supplier_counterparty`, но каждая запись может ссылаться на master `counterparty` | мягкая миграция, можно вести supplier-ledger без полного merge | остаётся риск рассинхрона aliases и статусов | кто отвечает за bridge и merge-очередь |
| C. Раздельные таблицы с discriminator/namespace | DDS и УДКЗ держат разные сущности, совпадения только через aliases/hash | минимум риска при импорте старых Sheets | сложные сверки оплат, документов и поставщиков; выше шанс дублей | допустима ли цена будущих сверок ради простого старта |

### 11.2 `cashflow_transaction` DDS vs `supplier_document.payment` / оплата в УДКЗ

**Решение владельца 2026-05-24: вариант A - DDS является единственным первоисточником cash-fact оплат поставщикам; УДКЗ хранит только `supplier_payment_match`.**

| Вариант | Суть | Плюсы | Минусы / риск | Что решает владелец |
| --- | --- | --- | --- | --- |
| A. DDS - единственный первоисточник cash fact | УДКЗ хранит только `supplier_payment_match` на `cashflow_transaction` | нет дубля оплат; банк/касса/кошелёк закрываются в одном месте | УДКЗ зависит от готовности DDS и качества классификации | считать ли ДДС обязательным перед закрытием УДКЗ |
| B. УДКЗ хранит собственный payment ledger, DDS сверяет | УДКЗ может закрываться по ручному импорту, а DDS потом матчится | удобно для исторического 2024 и ручной миграции | дубли и расхождения payment-факта; сложно объяснять баланс | нужна ли автономность УДКЗ на переходном этапе |
| C. Общий `payment`/`settlement` слой над DDS и УДКЗ | Создать нейтральную settlement-сущность, а DDS и УДКЗ читают её | архитектурно чисто для оплат, авансов, частичных закрытий | новая абстракция поверх уже описанных модулей; дороже MVP | оправдана ли отдельная settlement-модель сейчас |

### 11.3 `balance_source_reference` vs общий `source_reference` + `source_snapshot` + `agent_run`

**Решение владельца 2026-05-24: вариант A - audit полностью централизован через общий `source_reference`; `balance_source_reference` упраздняется.**

| Вариант | Суть | Плюсы | Минусы / риск | Что решает владелец |
| --- | --- | --- | --- | --- |
| A. Полная централизация | `balance_value.source_reference_id` ссылается только на общий `source_reference`; `balance_source_reference` не создаётся | единый audit trail для всех модулей | нужна миграция спеки баланса и UI на общий слой | можно ли отказаться от module-local audit сразу |
| B. Adapter layer | `balance_source_reference` остаётся как балансный adapter, но внутри имеет `source_reference_id` | совместимо с текущей спекой 26; легче внедрять баланс | две точки чтения audit; возможны дубли полей | считать ли это временным переходным решением |
| C. Полностью отдельный балансный audit | Баланс хранит свои source refs, общий audit живёт отдельно | проще для первого MVP баланса | нарушает правило централизованного audit; сложнее общая трассировка | допустимо ли исключение из общего правила |

### 11.4 `wallet` DDS vs `data_source` AI/integration

**Решение владельца 2026-05-24: вариант A - `wallet` описывает бизнес-кошелёк, `data_source` описывает способ доставки данных; связь N:M через `wallet_data_source`.**

| Вариант | Суть | Плюсы | Минусы / риск | Что решает владелец |
| --- | --- | --- | --- | --- |
| A. `wallet` - бизнес-сущность, `data_source` - источник доставки | T-Bank wallet связан с T-Bank data_source, но не является им | чистое разделение денег и интеграций | нужно поддерживать связку wallet/account/source | базовый рекомендуемый уровень разделения |
| B. `wallet` как subtype `data_source` для cash sources | Кошелёк и источник объединяются для банков/кассы | меньше сущностей в MVP | `Сейф`/`ТК Черникова` как бизнес-остаток смешиваются с API credentials | готов ли владелец принять смешение понятий ради простоты |
| C. `account`/`wallet` хранят source metadata напрямую | Без отдельной связи; wallet знает endpoint/source | быстро для DDS | плохо масштабируется на P&L, баланс, ЭДО и manual sources | допускается ли локальное решение только для DDS |

### 11.5 `prepaid_expenses` vs баланс «Выданные авансы поставщикам» vs `supplier_document` с типом аванса

**Решение владельца 2026-05-24/25: вариант B для supplier-based авансов + external components для остальных префиксов.** УДКЗ хранит supplier roll-forward и supplier-based авансы; рекламные кабинеты, Mango/телефония, аренда/подписки вне supplier roll-forward и будущие налоговые переплаты живут в своих источниках, но все агрегируются в Balance line «Выданные авансы поставщикам». «Расходы будущих периодов» = `not_applicable`.

Факт из memory: владелец 2026-05-24 уже выбрал управленческое упрощение для баланса: все префиксные платежи идут в строку «Выданные авансы поставщикам». Уточнение 2026-05-25: `prepayment_kind` обязателен только для регулярных/известных предоплат; налоговых предоплат в MVP нет. Псевдо-поставщиков для рекламных кабинетов и Mango в УДКЗ не создаём, если они не являются реальным supplier balance.

| Вариант | Суть | Плюсы | Минусы / риск | Что решает владелец |
| --- | --- | --- | --- | --- |
| A. Первичный реестр `prepaid_expense` в P&L/accrual | Любая предоплата создаёт prepaid balance и график списания в P&L | хорошо для подписок, аренды, рекламных бюджетов; баланс агрегирует остаток | УДКЗ supplier advances и P&L prepaid нужно синхронизировать | делать ли P&L/accrual владельцем всех префиксов |
| B. Первичный источник - УДКЗ/supplier roll-forward | Отрицательные supplier balances дают supplier-based авансы; остальные prepayment components приходят из contract/ad/Mango/tax sources | естественно для поставщиков и текущего файла УДКЗ; не раздувает УДКЗ псевдо-поставщиками | нужен агрегирующий Balance/P&L view поверх разных источников | как показывать единый advance view без смешения источников |
| C. Balance-owned advance register | Баланс ведёт свой реестр авансов/префиксов, P&L и УДКЗ дают компоненты | быстро закрывает строку баланса | баланс становится не терминальным модулем, противоречит спека 26 | готов ли владелец сделать исключение для баланса |

### 11.6 `dds_article` vs `pnl_line` vs `balance_line`

**Решение владельца 2026-05-24: вариант A - три раздельных справочника статей (`dds_article`, `pnl_line`, `balance_line`) связаны через mapping tables.**

| Вариант | Суть | Плюсы | Минусы / риск | Что решает владелец |
| --- | --- | --- | --- | --- |
| A. Три отдельных справочника + mapping tables | Оставить `dds_article`, `pnl_line`, `balance_line` как разные планы статей | отражает разные учетные плоскости cash/accrual/snapshot; меньше ложных совпадений | много mapping-правил и aliases | кто владеет маппингом и ревью новых статей |
| B. Единая `management_article` с типами | Один план статей с discriminator `dds/pnl/balance` | единый UI и aliases, проще поиск | риск смешать cash-flow, P&L и баланс; сложная иерархия | нужен ли единый план статей в первом MVP |
| C. Иерархический `article_group` + module-specific leaves | Общие группы, но конечные строки модульные | баланс между унификацией и методологией | сложнее объяснить пользователям и реализовать | выбрать ли компромиссную модель справочников |

### 11.7 Payroll `payments` vs DDS `cashflow_transaction`

**Решение владельца 2026-05-24/25: вариант A - `payroll_payment` является обязательством/ведомостью payroll-модуля; DDS хранит cash-fact через 1:1 matching с `cashflow_transaction` или через `payroll_payment_batch`. Wallet/source account выплаты хранится только в DDS, не в payroll-ведомости.**

| Вариант | Суть | Плюсы | Минусы / риск | Что решает владелец |
| --- | --- | --- | --- | --- |
| A. Payroll payment - обязательство/ведомость, DDS - cash fact | `payroll_payment` всегда матчится к `cashflow_transaction` или cash batch | защищает ПДн сотрудников; DDS видит агрегаты | нужен payment batch и приватные ссылки | насколько детально DDS должен видеть payroll |
| B. Все выплаты сотрудникам - DDS transactions с payroll metadata | Каждая выплата живёт в DDS как операция | простая сверка денег | DDS получает ПДн/персональные суммы; риск доступа | допустимо ли раскрытие payroll внутри finance |
| C. Отдельный `payroll_payment_batch` как единственный публичный bridge | Payroll хранит персональные выплаты, DDS только batches | приватность сильнее всего | сложнее расследовать расхождения без payroll-доступа | какие роли имеют право drill-down |

### 11.8 `source_document` DDS vs `parsed_document` integration

**Решение владельца 2026-05-24: вариант A - `parsed_document` хранит raw OCR/extract, `source_document` хранит подтверждённый бизнес-документ; promotion контролируется правилами или ручным review.**

| Вариант | Суть | Плюсы | Минусы / риск | Что решает владелец |
| --- | --- | --- | --- | --- |
| A. `parsed_document` = технический extraction, `source_document` = подтверждённый бизнес-документ | Чёткий pipeline raw -> parsed -> business | меньше мусора в бизнес-модуле | нужна стадия promotion/verification | кто подтверждает promotion |
| B. Единая document table | Все документы сразу в одной таблице со статусами | проще MVP | смешиваются низкоуверенные OCR-кандидаты и подтверждённые документы | допустим ли такой риск |
| C. Модульные документы (`edo_document`, `supplier_document`, `tax_document`) + общий parsed layer | Каждый модуль имеет свой документ после parsing | хорошо для разных процессов | больше дублирующих полей | нужен ли общий `source_document` как master |

### 11.9 Учёт ФД и налоги: недостаток спецификаций

**Решение владельца 2026-05-24/25: вариант C - асимметричный MVP: кредиты из Sber API, овердрафт как остаток тела по банковским API/выпискам, дивиденды и расчёты с собственниками из ДДС/owner register, owner loans через `owner_loan_register`, налоги ручным structured form из WorkMail налогового агента; полные модули ФД и налогов отложены.**

| Вариант | Суть | Плюсы | Минусы / риск | Что решает владелец |
| --- | --- | --- | --- | --- |
| A. Сначала отдельные полноценные спеки модулей | Не проектировать глубже `financial_obligation`, `loan_schedule`, `tax_charge`, `tax_payment` | не выдумываем методологию | SQL/реализация откладывается | когда разбирать УФД/налоги |
| B. MVP как ручные structured forms + audit | Вести кредиты/налоги вручную с source_reference | можно закрывать баланс раньше | ручной ввод без полной методологии | кто ответственный и частота |
| C. Использовать существующие источники как primary там, где уже есть owner decision | Кредиты из Sber API, дивиденды/owner payments из ДДС, owner loans из `owner_loan_register`, налоги через manual structured form + WorkMail | быстрее для известных строк | модуль получится неполным и асимметричным | допустим ли частичный модуль |

## 15. Принятые архитектурные решения 2026-05-24/25/27

| ID | Вариант | Решение | Затронутые модули |
| --- | --- | --- | --- |
| 11.1 | A + C12 | Единый `counterparty` + роли (`supplier`, `customer`, `bank`, `employee`, `owner`, `tax_authority`). Supplier registry для платежного календаря, DDS, УДКЗ и Balance - это общий `counterparty` с ролью/profile `supplier`; `supplier_counterparty` УДКЗ только профильное view/extension, не отдельный master. | Core, DDS, УДКЗ, P&L, Balance, Taxes, Payroll, Payment calendar |
| 11.2 | A | DDS - единственный первоисточник cash-fact оплат поставщикам. УДКЗ хранит только `supplier_payment_match(supplier_document_id, cashflow_transaction_id, matched_amount)`, без своего payment ledger. | DDS, УДКЗ, Balance, P&L |
| 11.3 | A | Audit полностью централизован: `balance_value.source_reference_id` ссылается на общий `source_reference`; `balance_source_reference` упраздняется. | Balance, Audit/integration, все финансовые модули |
| 11.4 | A | `wallet` и `data_source` разделены: `wallet` - бизнес-кошелёк, `data_source` - способ доставки данных; связь many-to-many через `wallet_data_source`. | DDS, Audit/integration, Balance |
| 11.5 | B + external components | УДКЗ хранит supplier-based авансы через supplier roll-forward. `prepayment_kind` обязателен только для регулярных/известных предоплат; аренда, подписки, рекламные кабинеты, Mango/телефония и будущие налоговые переплаты живут в своих источниках и агрегируются в `Выданные авансы поставщикам`. Налоговых предоплат в MVP нет. | УДКЗ, Balance, P&L, DDS, Taxes, Payment calendar |
| 11.6 | A | Три раздельных справочника статей: `dds_article`, `pnl_line`, `balance_line`. Связи между cash/accrual/snapshot плоскостями ведутся через mapping tables (`dds_article_pnl_mapping`, `pnl_balance_mapping` и т.п.). | DDS, P&L, Balance |
| 11.7 | A + C11 | `payroll_payment` / `Выплаты` - обязательство payroll-модуля без wallet/source account. DDS получает cash-fact либо 1:1 matching с `cashflow_transaction`, либо через `payroll_payment_batch`; доступ к индивидуальным суммам только у payroll-роли. | Payroll, DDS, Balance, P&L |
| 11.8 | A | Document pipeline: `parsed_document` (raw OCR extract, статус `extracted`/`auto_confirmed`/`needs_review`) -> `source_document` (подтверждённый бизнес-документ). Promotion автоматический по правилам или ручной через review. | Audit/integration, DDS, P&L, УДКЗ, Taxes |
| 11.9 | C + tax update | Асимметричный MVP для ФД и налогов: кредиты - Sber API, овердрафт - банковские API/выписки по телу, дивиденды и выплаты собственникам - ДДС-лист, займы собственников - `owner_loan_register`, налоги - manual structured form + WorkMail налогового агента. Полные модули `financial_activity` и `taxes` со своими спеками отложены. | Financial activity, Taxes, DDS, Balance, P&L |
| 12a | 2026-02-01 | Дата X старта приложения - 2026-02-01, совпадает с активным processed-контуром Sber/T-Bank/iiko. | Migration, DDS, Balance, P&L, Payroll, УДКЗ |
| 12b | C гибрид | Глубина миграции: master data + monthly totals баланс/ДДС/P&L за 2024-2025 + opening balances на 2026-02-01. Raw history построчно остаётся в исторических Google Sheets как `source_snapshot`, в БД не переносится. | Migration, Audit/integration, все модули |
| 15.1 | Payroll rules | Payroll готов к разработке по ключевым правилам: точные минуты без `>=40`, payroll-период вторник-понедельник, роли/станции только из `Учет смен`, unknown employee блокирует run, open shift закрывается в 22:00 с cap 12h, увольнение синхронизируется в iiko. | Payroll, iiko employees, Shift schedule |
| 15.2 | Payment calendar | Production-календарь планирует ближайший месяц; forecast выручки строится из iiko OLAP `Выручка по направлениям` по same-month истории + тренд + маркетинговые планы; cash gap = `total cash < 0`; internal transfers скрыты как строки календаря и неттируются в DDS. | Payment calendar, DDS, P&L |
| 15.3 | Supplier forecast | План оплат поставщикам строится гибридно: УДКЗ + iiko unpaid supplies для known AP, DDS cadence + rolling average для неизвестных будущих сумм, agent/owner adjustments с audit trail. | Payment calendar, УДКЗ, DDS, iiko, Audit |
| 15.4 | Fixed assets | ОС: порог 5 000 ₽, линейная помесячная амортизация, СПИ по категориям/карточкам, ввод управляющим, ремонт/модернизация по 15%, покупки контролируются из DDS, продажа не попадает в P&L. | Fixed assets, DDS, Balance, P&L |
| 15.5 | Fixed assets migration | Гагарина неактивна; ОС Гагарина до инвентаризации переводятся в складской provisional-контур; реестр 2025 `1GK6...` - seed, current truth только после инвентаризации 2026-06-01..2026-06-12. | Fixed assets, Balance, Migration |
| 15.6 | 2026-05-27 Staff page | Штат — отдельная страница master `employee`; имя read-only из iiko, должность/категория/надбавки app_managed. | Core, Payroll, Shift schedule, DDS, Balance, УДКЗ |
| 15.7 | 2026-05-27 Settings layer | Settings — централизованная страница `app_setting` с audit trail и категориями по модулям. | Core, все модули |
