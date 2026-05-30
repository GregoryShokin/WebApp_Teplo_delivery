# Каталог AI-agent integration patterns

Дата фиксации: 2026-05-24.

## Назначение

Этот документ описывает переиспользуемые паттерны подключения AI-агентов и автоматизаций к источникам данных проекта «Тепло». Паттерны нужны для единого приложения: агент выступает как «глаза и руки» там, где источник не дает нормального API, а приложение все равно должно получить проверяемый факт, документ или управленческую запись.

Важное правило: способ доставки данных не меняет классификацию обеспеченности источника. Источник может быть `full` или `partial`, даже если данные приходят через IMAP, cookie-сессию или ручную форму; решают полнота периода, проверяемость, повторяемость и качество сверки, а не наличие красивого REST API.

## 1. `direct_api`

Статус: proven для Sber, T-Bank, iiko; proposed для Saby/СБИС ЭДО до подключения.

### Назначение

Получать структурированные факты напрямую из системы-источника: выписки, продажи, остатки, справочники, звонки, входящие документы, события Telegram bot API. Это базовый паттерн, если API покрывает нужный бизнес-факт и не требует ручного действия владельца каждый период.

### Прецеденты в проекте

| Источник | Что берем | Файл / документ |
| --- | --- | --- |
| Sber API | входящие движения, остатки, обороты, контрагенты по банку поступлений | [12-sber-api-endpoints.md](/app-spec/integrations/sber/api-endpoints.md), [06-data-sources.md](/business-docs/data-quality/data-sources.md) |
| T-Bank Business Open API | исходящие операции, поставщики, сверка оплат | [14-tbank-api-endpoints.md](/app-spec/integrations/tbank/api-endpoints.md), [06-data-sources.md](/business-docs/data-quality/data-sources.md) |
| iikoServer / Resto API | продажи, блюда, курьеры, явки, склад, OLAP-отчеты | [08-iiko-server-api-endpoints.md](/app-spec/integrations/iiko/server-api-endpoints.md), [22-iiko-employees-api.md](/app-spec/integrations/iiko/employees-api.md) |
| Mango VPBX API | звонки, пропущенные, записи, webhook завершения звонка | [15-telephony.md](/app-spec/integrations/mango/telephony.md) |
| Telegram Bot API | прием документов/фото от владельца, не банковская отправка | [payment_order_bot.py](/integrations/tbank/scripts/payment_order_bot.py) |
| СБИС/Saby ЭДО | входящие счета/акты/УПД после подключения | [11-sbis-edo-api-endpoints.md](/app-spec/integrations/sbis-edo/api-endpoints.md), [06-data-sources.md](/business-docs/data-quality/data-sources.md) |

### Предусловия

- У источника есть документированный или восстановленный read-only API.
- Есть устойчивый credential: OAuth, token, app key, client TLS или webhook secret.
- API возвращает первичный бизнес-факт, а не только косвенный сигнал.
- Известны лимиты, периодичность, timezone и правила пагинации.

### Компоненты

- Коннектор с read-only режимом по умолчанию.
- `source_credential` с типом секрета, scope, датой выпуска и сроком жизни; сам секрет хранится только в `.env` или `research/private/`.
- Нормализатор в доменную модель приложения: operation, sale, invoice, call, counterparty, balance snapshot.
- Идемпотентная загрузка по внешнему ID, периоду и хешу payload.
- Логирование `agent_run` и `agent_action` с маскированными URL/headers.

### Стоимость, частота, риски

| Параметр | Оценка |
| --- | --- |
| Стоимость внедрения | Средняя: 1-3 дня на источник, выше для OAuth/client TLS и сложных отчетов |
| Частота обновления | От near-real-time/webhook до ежедневной или ежемесячной загрузки |
| Риски | истечение token, смена API, лимиты, неполный scope, write endpoints рядом с read endpoints, ошибки timezone и accrual/cash basis |

### Жизненный цикл

1. Секрет выпускается владельцем и заносится в `.env` или секрет-хранилище.
2. Агент выполняет healthcheck без выгрузки PII-heavy payload.
3. Регулярный run пишет status: `success`, `partial`, `rate_limited`, `auth_failed`, `schema_changed`.
4. При `401/403`, `invalid_token` или падении TLS ставится `credential_stale`, владельцу создается задача на ротацию.
5. Изменение write-scope требует отдельного явного решения владельца.

### PII-обработка

Может работать с приватными данными: банковские назначения платежей, ФИО сотрудников, телефоны клиентов, записи звонков, контрагенты.

Safeguards:

- raw API payload, банковские назначения, записи разговоров, телефоны, ФИО и токены - только `research/private/`;
- `research/processed/` хранит агрегаты, обезличенные ключи, суммы, периоды, внешние ID и статусы качества;
- маскировать bearer tokens, cookies, телефоны, email, OAuth code/state в логах;
- для write-capable API хранить allowlist разрешенных операций и требовать owner approval.

### Критерии применимости

Выбираем `direct_api`, если API дает нужный факт с достаточной полнотой и стабильностью. Не выбираем его как единственный источник для accrual-расхода, если API банка видит только оплату, а месяц услуги живет в счете/УПД: тогда API банка становится сверкой, а первичный паттерн - `mail_with_ai_ocr` или `direct_api` ЭДО.

## 2. `google_sheets_csv_export`

Статус: proven.

### Назначение

Переносить уже ведущиеся управленческие Google Sheets в приложение без потери истории: ДДС, ОПиУ/P&L, баланс, зарплатные ведомости, УФД, ДЗ/КЗ, основные средства и другие таблицы, где владелец уже создал структуру и правила.

### Прецеденты в проекте

| Источник | Что берем | Файл / документ |
| --- | --- | --- |
| Google Drive | ДДС, ОПиУ/P&L, баланс, управленческие документы | [06-data-sources.md](/business-docs/data-quality/data-sources.md), [07-google-drive-discovery.md](/app-spec/integrations/google-drive-discovery.md) |
| Payroll Sheets | зарплаты, категории, фонд, удержания | [payroll engine spec](/app-spec/modules/staff/payroll/00-engine.md) |
| DDS Sheets | ДДС, платежный календарь, кошельки, ручная классификация | [21-dds-module-spec.md](/app-spec/modules/finance/dds/spec.md), [25-dds-filling-methodology.md](/business-docs/finance/dds-methodology.md) |
| Balance Sheets | баланс, ОС, ДЗ/КЗ, РБП/авансы поставщикам | [26-balance-module-spec.md](/app-spec/modules/finance/balance/spec.md) |
| P&L Sheets | построчная методология ОПиУ | [pnl-build-methodology.md](/business-docs/finance/pnl-methodology.md) |

### Предусловия

- Таблица имеет стабильный файл, листы и диапазоны.
- Есть правило, что считается источником факта, а что является расчетом/сверкой.
- Можно экспортировать диапазон как values/CSV без ручного форматирования.
- Понятны owner-defined формулы и ручные ячейки.

### Компоненты

- Google Drive / Sheets connector или CSV-export.
- Registry sheets: file_id, sheet_name, range, business_entity, period column, quality status.
- Snapshot layer: immutable raw snapshot в private/controlled storage и normalized processed tables.
- Formula audit: отдельно хранить значения, формулы, пустоты, ошибки и ручной ввод.

### Стоимость, частота, риски

| Параметр | Оценка |
| --- | --- |
| Стоимость внедрения | Низкая-средняя: быстро для плоских таблиц, выше для merged cells, формул и нестабильных листов |
| Частота обновления | Ежедневно для ДДС/платежного календаря, ежемесячно для ОПиУ/баланса/ОС |
| Риски | владелец переименует лист, изменит диапазон, сломает формулу, введет число без источника, API отдаст formatted value вместо raw value |

### Жизненный цикл

1. Зафиксировать file_id/range и владельца таблицы.
2. Снять read-only snapshot за период.
3. Нормализовать в длинный слой с `source_sheet`, `source_cell`, `period`, `article`, `value`, `quality_status`.
4. При смене структуры ставить `schema_changed` и не затирать последнюю валидную выгрузку.
5. После миграции в приложение оставить Google Sheets как read-only legacy или источник ручной сверки.

### PII-обработка

Может работать с приватными данными: зарплаты, ФИО сотрудников, контрагенты, комментарии к платежам, долги.

Safeguards:

- raw snapshots зарплаты, ДДС с назначениями, ДЗ/КЗ и комментарии - только `research/private/`;
- в `research/processed/` допускаются агрегаты по статьям/месяцам и technical IDs без ФИО/телефонов;
- payroll-персональные отчеты отделять от общих P&L/DDS витрин;
- не переносить в документы реальные фрагменты назначений платежей, если они могут содержать PII.

### Критерии применимости

Выбираем `google_sheets_csv_export`, если таблица уже является каноническим управленческим источником или временным контуром до модуля приложения. Не выбираем его для реконструкции факта, который надежнее живет в API или первичном документе.

## 3. `mail_with_ai_ocr`

Статус: proven для IMAP-синхронизации и документного контура; OCR/AI extraction должен быть оформлен как следующий слой поверх уже скачанных вложений.

### Назначение

Читать письма и вложения, находить счета/акты/УПД, извлекать период услуги, сумму, контрагента и номер документа. Это главный паттерн для расходов, где дата оплаты не равна месяцу P&L, а первичный факт живет в PDF или теле письма.

### Прецеденты в проекте

| Источник | Что берем | Файл / документ |
| --- | --- | --- |
| Mail.ru личный ящик | письма, папки, переписки, вложения | [23-mailru-personal-mailbox.md](/app-spec/integrations/mailru/personal-mailbox.md), [mailru_mailbox.py](/integrations/mailru/scripts/mailru_mailbox.py) |
| WorkMail / `smmbux@yandex.ru` | документы по таргетированной рекламе | [pnl-build-methodology.md](/business-docs/finance/pnl-methodology.md) |
| `webmaster@insaitov.ru` | SEO-счета Синапса | [pnl-build-methodology.md](/business-docs/finance/pnl-methodology.md) |
| `donotreply@iiko.ru` | PDF-счета iiko | [pnl-build-methodology.md](/business-docs/finance/pnl-methodology.md) |
| `leshkina@starterapp.ru` | счет и УПД StarterApp | [pnl-build-methodology.md](/business-docs/finance/pnl-methodology.md) |
| `account@lemma.ru` | PDF-счет и УПД Леммы | [pnl-build-methodology.md](/business-docs/finance/pnl-methodology.md) |
| Mango invoices | proposed резервный путь D из 5 вариантов Mango | [15-telephony.md](/app-spec/integrations/mango/telephony.md) |

### Предусловия

- Источник регулярно присылает документы на доступный ящик.
- Есть app password / IMAP-доступ без интерактивного MFA на каждый запуск.
- У писем есть признаки поиска: sender, subject, attachment type, invoice number, contract number.
- PDF/скан содержит период услуги или дату документа, достаточную для P&L.

### Компоненты

- IMAP/SMTP connector с read-only sync по UID/UIDVALIDITY.
- Локальная private база писем и вложений: `research/private/mail/mail.sqlite3`, `research/private/mail/attachments/`.
- Реестр отправителей и правил: sender -> document family -> P&L/DDS article -> parser.
- OCR/PDF extraction: text layer, OCR fallback, LLM/AI для сложных PDF только с evidence spans.
- Cross-check с T-Bank/Sber/API по сумме, контрагенту, номеру счета и оплате.

### Стоимость, частота, риски

| Параметр | Оценка |
| --- | --- |
| Стоимость внедрения | Средняя: IMAP уже есть; 0.5-2 дня на семейство документов и сверки |
| Частота обновления | Почасово или ежедневно для входящих, ежемесячно для P&L закрытия |
| Риски | пароль приложения отозван, письмо попало в другую папку, PDF без text layer, дубль письма/счета, вложение с PII, phishing/подмена отправителя |

### Жизненный цикл

1. Подключить ящик через отдельный пароль внешнего приложения.
2. Синхронизировать read-only, не менять флаги писем на сервере.
3. Сохранить raw письмо и вложение в private, рассчитать sha256.
4. Извлечь `parsed_document` со статусом `parsed_ready`, `owner_review` или `low_confidence`.
5. Сверить с банком/ЭДО/API и повысить статус до `verified_by_crosscheck`.
6. При `AUTHENTICATIONFAILED` или резком падении объема писем ставить `credential_stale` или `source_silent`.

### PII-обработка

Да, паттерн почти всегда работает с приватными данными: email, подписи, телефоны, договоры, банковские реквизиты, УПД.

Safeguards:

- сырые письма, HTML body, вложения, OCR text и message metadata - только `research/private/`;
- в `research/processed/` можно хранить sender domain, document type, номер, дату, период, сумму, контрагента, sha256 и статус без тел писем;
- AI extraction должен сохранять evidence references, но не копировать длинные фрагменты документов в публичные markdown;
- attachments исполняемых типов не открывать автоматически.

### Критерии применимости

Выбираем `mail_with_ai_ocr`, если бизнес-факт подтверждается документом во вложении или письме. Для P&L подписок и подрядчиков это сильнее банковского cash-факта: банк подтверждает оплату, но не период услуги.

## 4. `telegram_ocr_bot`

Статус: proven для payment order intake; расширяемый на коммунальные платежки и другие фото/сканы.

### Назначение

Принимать от владельца фото, скан или PDF в Telegram, запускать OCR/парсер и создавать структурированную запись-кандидат: платежное основание, счет, акт, коммунальная начисленная сумма, supplier document. Паттерн подходит для бумажных документов и источников, где входящий канал - не API, а «сфотографировал и отправил».

### Прецеденты в проекте

| Источник | Что берем | Файл / документ |
| --- | --- | --- |
| T-Bank payment order intake | кандидат платежки из документа/фото/текста; бот не отправляет платеж в банк | [payment_order_bot.py](/integrations/tbank/scripts/payment_order_bot.py) |
| OCR macOS Vision | распознавание русско-английского текста с изображений | [ocr_image_macos.swift](/integrations/tbank/scripts/ocr_image_macos.swift) |
| Deterministic parsers | счета, УПД, накладные, платежки, известные контрагенты | [payment_parsers.py](/integrations/tbank/scripts/payment_parsers.py) |
| Водоканал / электроэнергия | коммунальные счета/акты, status `owner_review` при неполной паре или низкой уверенности | [payment_parsers.py](/integrations/tbank/scripts/payment_parsers.py) |

### Предусловия

- Документ можно сфотографировать или переслать в Telegram в читаемом качестве.
- Есть список разрешенных chat_id.
- Есть parser family или fallback в owner review.
- Для платежных действий есть отдельный human approval; бот сам не списывает деньги.

### Компоненты

- Telegram bot token и allowlist чатов.
- Private inbox: `research/private/tbank/payment_orders/`.
- OCR engine: macOS Vision, PDF text extraction или другой локальный OCR.
- Deterministic parser registry с confidence, required fields и owner review reasons.
- Candidate store: `payment_order_candidate`, `parsed_document`, `expense_accrual` для коммунальных начислений.

### Стоимость, частота, риски

| Параметр | Оценка |
| --- | --- |
| Стоимость внедрения | Средняя: бот уже есть; новый тип документа - от 0.5 дня до нескольких дней |
| Частота обновления | Event-driven: сразу после отправки документа; обычно ежедневно или по мере поступления счетов |
| Риски | плохое фото, OCR-ошибка в сумме/реквизитах, Telegram token leak, неразрешенный чат, документ с персональными данными, преждевременная отправка в банк |

### Жизненный цикл

1. Владелец отправляет фото/PDF/текст в разрешенный чат.
2. Бот скачивает файл в private tmp, запускает intake parser.
3. Parser выдает candidate со статусом `parsed_ready`, `owner_review`, `low_confidence` или `duplicate`.
4. Для платежек разрешен только soft-submit в папку `to_sign/`; банковская подпись остается отдельным явным действием.
5. Для коммунальных начислений создается отдельный accrual record, который можно связать с оплатой.

### PII-обработка

Да: документы могут содержать реквизиты, адреса, ФИО, телефоны, QR, банковские данные.

Safeguards:

- все загруженные файлы, OCR text, candidates и payload для подписи - только `research/private/`;
- Telegram chat_id, message_id и sender хранить как audit metadata, но не выносить в публичные отчеты;
- обязательные поля платежки проверять по checksum/regex, а не доверять OCR;
- при confidence ниже порога и для новых контрагентов - `owner_review`.

### Критерии применимости

Выбираем `telegram_ocr_bot`, если документ приходит физически, в мессенджере или как одноразовый скан, а не в стабильный email/API. Если те же документы начинают регулярно приходить в почту, лучше перевести поток в `mail_with_ai_ocr`.

## 5. `lk_browser_cookie`

Статус: partially proven для Mango Office как вариант A; proposed для рекламных кабинетов с MFA.

### Назначение

Использовать авторизованную browser session владельца для read-only доступа к личному кабинету, когда нормального login API нет или он закрыт CAPTCHA/MFA. Агент не логинится сам: владелец вручную проходит CAPTCHA/MFA, экспортирует cookie jar, после чего агент дергает уже известные read-only endpoints или экспорт.

### Прецеденты в проекте

| Источник | Что берем | Файл / документ |
| --- | --- | --- |
| Mango Office LK / финансы | расходы на продуктовый набор через `product-expenses/report-*` | [15-telephony.md](/app-spec/integrations/mango/telephony.md), [endpoint_map.md](/integrations/mango/scripts/endpoint_map.md), [export_telecom.py](/integrations/mango/scripts/export_telecom.py) |
| Рекламные кабинеты с MFA | proposed: расходы/балансы/выгрузки при известном endpoint или CSV-export | [06-data-sources.md](/business-docs/data-quality/data-sources.md) |

### Предусловия

- Владелец может легально зайти в LK и экспортировать cookies после MFA/CAPTCHA.
- Session cookie дает read-only доступ к нужному отчету.
- Известен endpoint, CSV export или стабильный backend call.
- Источник не запрещает такой read-only технический доступ в рамках владельческого аккаунта.

### Компоненты

- Secure cookie jar: например `research/private/mango/session.json` с правами `0600`.
- Endpoint map: URL, method, payload, pagination/polling, target row.
- HTTP client с browser-like headers и маскированием cookies.
- Expiry detector: redirect to login, HTTP 401/403, отсутствие target marker.
- Owner re-auth workflow: «залогиниться -> экспортировать cookies -> healthcheck».

### Стоимость, частота, риски

| Параметр | Оценка |
| --- | --- |
| Стоимость внедрения | Средняя-высокая: reverse engineering endpoint + private session handling |
| Частота обновления | От ежедневной до ежемесячной; зависит от срока cookie и бизнес-периода |
| Риски | cookie протухает за дни-недели, session hijack risk, CAPTCHA/MFA при обновлении, endpoint меняется, LK считает запрос suspicious, PII в HTML/CSV |

### Жизненный цикл

1. Агент проверяет наличие cookie jar.
2. Делает lightweight healthcheck: целевая страница без редиректа в login.
3. Запускает только allowlisted read-only endpoints.
4. При редиректе на auth или отсутствии target marker ставит `credential_stale` и просит владельца обновить cookie.
5. Не пытается обходить CAPTCHA или подбирать MFA-коды.

### PII-обработка

Да: LK может содержать финансовые отчеты, телефоны, договоры, платежные реквизиты и персональные профили.

Safeguards:

- cookies и raw HTML/CSV - только `research/private/`;
- в `agent_action` хранить masked URL и хеш response, не cookie value;
- ограничить host/domain и список endpoints;
- не сохранять screenshots LK в публичные артефакты, если на них есть PII.

### Критерии применимости

Выбираем `lk_browser_cookie`, если нужный факт доступен в LK, endpoint известен, а интерактивный login заблокирован CAPTCHA/MFA. Если endpoint неизвестен и нужно читать экран как пользователь, выбрать `ai_agent_lk_authorized`.

## 6. `ai_agent_lk_authorized`

Статус: proven as manual/agent workflow для Biz Panel; proposed для рекламных кабинетов без API.

### Назначение

AI-агент с авторизованной сессией ходит по личному кабинету, открывает дашборды, выбирает период, скачивает отчеты или считывает значения с UI. Это паттерн для «человеческого» интерфейса, где данные есть, но API либо нет, либо он не покрывает нужный показатель.

### Прецеденты в проекте

| Источник | Что берем | Файл / документ |
| --- | --- | --- |
| Biz Panel Синапса | фактический `Бюджет` Яндекс Директ, клики, показы, счета по договору | [pnl-build-methodology.md](/business-docs/finance/pnl-methodology.md), [06-data-sources.md](/business-docs/data-quality/data-sources.md) |
| Рекламные кабинеты | proposed: расходы, клики, заявки, остатки бюджета при отсутствии API | [06-data-sources.md](/business-docs/data-quality/data-sources.md), [24-marketing-metrics.md](/business-docs/marketing/metrics.md) |
| Mango headed Playwright | вариант C: владелец решает SmartCaptcha, агент продолжает сценарий | [15-telephony.md](/app-spec/integrations/mango/telephony.md) |

### Предусловия

- У владельца есть доступ в LK или magic link из письма.
- Сценарий можно выполнить в headed browser без нарушения правил источника.
- UI содержит период, сумму и/или export link, которые можно перепроверить.
- MFA/CAPTCHA проходит человек, агент не обходит защиту.

### Компоненты

- Browser automation: Playwright/in-app browser/headed run.
- Scenario script: login handoff, choose period, navigate tabs, export/read values.
- UI assertions: visible period, account/contract, report title, target metric.
- Evidence capture в private: screenshot/html/export hash.
- Parser for downloaded CSV/PDF/HTML table.

### Стоимость, частота, риски

| Параметр | Оценка |
| --- | --- |
| Стоимость внедрения | Средняя-высокая: 1-5 дней на устойчивый сценарий, особенно при MFA |
| Частота обновления | Обычно ежемесячно для P&L, возможно ежедневно для маркетинговых метрик |
| Риски | UI меняется, selector ломается, MFA каждый запуск, CAPTCHA, session timeout, human-in-the-loop задержки, screenshots с PII |

### Жизненный цикл

1. Агент открывает LK в авторизованном профиле или по свежей ссылке.
2. Владелец проходит MFA/CAPTCHA, если нужно.
3. Агент выполняет сценарий, фиксирует period/account/contract и target metric.
4. Данные сохраняются как `parsed_document` или `source_snapshot`, статус `parsed_ready`.
5. Сверка с банковской оплатой, счетом или другим источником повышает статус до `verified_by_crosscheck`.
6. При изменении UI ставится `source_changed`, сценарий не выдает «0» вместо неизвестного значения.

### PII-обработка

Может работать с приватными данными и коммерчески чувствительными метриками: рекламные кабинеты, договоры, клиенты, лиды, звонки.

Safeguards:

- browser profile, screenshots, downloaded reports and HTML - private only;
- хранить selector/action log без секретов и без полных экранных дампов в публичных docs;
- явно разделять read-only navigation и действия, которые меняют бюджет/кампанию;
- любые изменения кампаний, платежей или настроек требуют отдельного owner approval.

### Критерии применимости

Выбираем `ai_agent_lk_authorized`, если источник имеет LK и нужное значение можно получить глазами, но нет стабильного API/export endpoint. Если после discovery найден устойчивый backend endpoint и cookie достаточно, сценарий можно перевести в `lk_browser_cookie`.

## 7. `manual_structured_form`

Статус: partially proven через текущие Google Sheets и owner answers; proposed как форма в будущем приложении.

### Назначение

Дать владельцу или ответственному человеку короткую структурированную форму там, где источник пока не автоматизируется, требует управленческого суждения или содержит наличные/внутренние операции без внешнего machine-readable следа.

### Прецеденты в проекте

| Источник | Что берем | Файл / документ |
| --- | --- | --- |
| ДДС Google Sheets | ручная классификация по кошелькам, включая банк, сейф, ТК Черникова | [21-dds-module-spec.md](/app-spec/modules/finance/dds/spec.md), [25-dds-filling-methodology.md](/business-docs/finance/dds-methodology.md) |
| Баланс и ОС | owner-confirmed статусы, ДЗ/КЗ, РБП как авансы поставщикам | [26-balance-module-spec.md](/app-spec/modules/finance/balance/spec.md) |
| Mango fallback E | фикс-сумма с квартальной ручной сверкой | [15-telephony.md](/app-spec/integrations/mango/telephony.md) |
| Рекламные бюджеты/остатки | proposed: ручной ввод остатков там, где кабинет не дает API/export | [06-data-sources.md](/business-docs/data-quality/data-sources.md) |
| Наличные траты | proposed: структурированный ввод факта и фото-подтверждения | [06-data-sources.md](/business-docs/data-quality/data-sources.md) |

### Предусловия

- Источник не дает API/email/export с приемлемой стоимостью или требует человеческого решения.
- Набор полей можно стандартизировать: дата, период, сумма, статья, кошелек, контрагент, доказательство, комментарий.
- Есть ответственный за ввод и периодичность.
- Форма не должна позволять свободный хаотичный текст как единственный источник факта.

### Компоненты

- UI form или Google Form/Sheet как временный контур.
- Справочники статей, кошельков, контрагентов, источников и типов доказательств.
- Required evidence: attachment, link, photo, bank operation id, owner note.
- Approval workflow: draft -> submitted -> reviewed -> posted.
- Change log по каждому полю.

### Стоимость, частота, риски

| Параметр | Оценка |
| --- | --- |
| Стоимость внедрения | Низкая для формы, средняя для хорошего approval/audit |
| Частота обновления | От ежедневной для наличных до ежемесячной для остатков/сверок |
| Риски | человеческая ошибка, поздний ввод, дубли, отсутствие evidence, смешение cash/accrual, PII в комментариях |

### Жизненный цикл

1. Создать форму с минимальным набором обязательных полей.
2. Привязать ввод к source, period, article, wallet/counterparty.
3. Сохранять черновик и финальную запись отдельно.
4. При изменении записи писать old/new value и автора.
5. Если позже появляется API/email/LK-автоматизация, manual form остается fallback и контуром корректировок.

### PII-обработка

Да: вручную могут ввести ФИО, телефоны, назначения платежей, комментарии по сотрудникам и контрагентам.

Safeguards:

- поля комментариев считать PII by default;
- attachments и raw notes - private only;
- в processed/app vitrine выводить normalized article, amount, period, source status, без лишнего свободного текста;
- справочники и выпадающие списки использовать вместо ручного ввода, где возможно.

### Критерии применимости

Выбираем `manual_structured_form`, если автоматизация дороже риска ошибки или пока нет машинного источника. Не используем свободную форму как замену API/email, если первичный источник уже найден.

## Decision tree

Входные признаки источника: есть API, есть PDF/вложения, есть LK, есть MFA/CAPTCHA, тип данных (`счета`, `акты`, `остатки`, `факт`, `ручное суждение`).

1. Если есть read-only API, который возвращает нужный бизнес-факт за период, выбрать `direct_api`.
2. Если источник - уже ведущаяся управленческая Google Sheet или CSV-таблица с owner-defined логикой, выбрать `google_sheets_csv_export`.
3. Если первичный факт - счет, акт, УПД или PDF-вложение в почте, выбрать `mail_with_ai_ocr`.
4. Если первичный факт - бумажный документ, фото, скан или одноразовая пересылка в Telegram, выбрать `telegram_ocr_bot`.
5. Если есть LK, API нет, MFA/CAPTCHA мешает логину, но после ручного входа можно переиспользовать cookie и известный endpoint/export, выбрать `lk_browser_cookie`.
6. Если есть LK, API нет, endpoint неизвестен или нужно читать UI/дашборд как пользователь, выбрать `ai_agent_lk_authorized`.
7. Если нет машинного источника или факт требует управленческого решения, выбрать `manual_structured_form`.

Tie-breakers:

- Для расходов по услугам первичный месяц P&L брать из счета/акта/УПД, а банковский API использовать как сверку оплаты.
- Для ДДС-строк, которые владелец уже классифицирует в ДДС Sheets, Google Sheets остается первичным источником до переноса логики в приложение.
- Для источника с несколькими контурами выбирать паттерн по конкретному факту: Mango звонки = `direct_api`, Mango финансы = `lk_browser_cookie` или `mail_with_ai_ocr`, Mango фикс-сумма = `manual_structured_form`.
- Если автоматизация временно сломана, fallback не меняет coverage class автоматически: меняется только `quality_status` конкретного периода.

### Карта известных источников

| Источник / класс из `06-data-sources.md` | Основной паттерн | Резерв / комментарий |
| --- | --- | --- |
| Sber API | `direct_api` | primary для входящих банковских движений; ДДС/P&L классификация по правилам |
| T-Bank Business Open API | `direct_api` | primary для расходов cash basis; сверка для документных accrual-расходов |
| iikoServer / iiko | `direct_api` | primary для продаж, склада, курьеров, явок; счета iiko как поставщика идут через `mail_with_ai_ocr` |
| Google Drive / ДДС / ОПиУ / баланс / ЗПВ / УФД / ДЗ-КЗ / ОС | `google_sheets_csv_export` | до миграции в модули приложения |
| ЭДО DocsInbox / СБИС | `direct_api` | если API не подключен: manual export или `mail_with_ai_ocr` как резерв по письмам |
| Mail.ru / WorkMail | `mail_with_ai_ocr` | IMAP raw private, processed только структурные поля |
| Biz Panel Синапса | `ai_agent_lk_authorized` | если появится стабильная cookie/API-схема, можно перевести в `lk_browser_cookie` |
| Рекламные кабинеты | `direct_api`, если есть официальный API | иначе `ai_agent_lk_authorized`; для MFA+known export - `lk_browser_cookie`; для остатков без выгрузки - `manual_structured_form` |
| Телефония Mango VPBX | `direct_api` | звонки, записи, webhooks |
| Телефония Mango финансы | `lk_browser_cookie` | резерв: `mail_with_ai_ocr` по счетам/УПД; fallback: `manual_structured_form` фикс-суммой |
| Касса / наличные / прочие банковские источники | `google_sheets_csv_export` или `manual_structured_form` | если факт есть в iiko Главная касса - `direct_api`; если бумажный чек - `telegram_ocr_bot` |
| Коммунальные платежки | `telegram_ocr_bot` | если поставщик присылает PDF на email, перейти на `mail_with_ai_ocr` |
| Билинский, Синапс, StarterApp, iiko-поставщик, Лемма | `mail_with_ai_ocr` | T-Bank/Sber только сверяют оплату |

## Общий слой приложения для аудита агентов

### `data_source`

Регистрирует источник как бизнес-сущность, независимо от паттерна доставки.

Ключевые поля: `id`, `name`, `source_type`, `primary_pattern`, `business_owner`, `coverage_class`, `pii_level`, `canonical_periodicity`, `current_status`, `last_successful_run_at`.

### `source_credential`

Не хранит секрет, а хранит ссылку на секрет и его жизненный цикл.

Ключевые поля: `id`, `source_id`, `credential_type` (`api_token`, `oauth`, `client_tls`, `imap_app_password`, `cookie_jar`, `browser_profile`, `telegram_bot_token`, `manual_owner`), `secret_ref`, `scope`, `issued_at`, `expires_at`, `last_checked_at`, `status`, `rotation_owner`, `failure_reason`.

Статусы: `active`, `stale`, `revoked`, `missing`, `needs_owner_action`, `blocked_by_mfa`, `blocked_by_captcha`.

### `agent_run`

Один запуск агента или автоматизации.

Ключевые поля: `id`, `source_id`, `pattern`, `agent_name`, `trigger_type`, `requested_period_start`, `requested_period_end`, `started_at`, `finished_at`, `status`, `code_version`, `input_hash`, `output_hash`, `pii_access_level`, `private_artifact_root`, `processed_artifact_ref`, `error_class`.

Статусы: `success`, `partial`, `owner_review_required`, `auth_failed`, `rate_limited`, `schema_changed`, `source_silent`, `blocked_by_captcha`, `failed`.

### `agent_action`

Детальный audit trail внутри запуска.

Ключевые поля: `id`, `run_id`, `sequence_no`, `action_type` (`http_get`, `http_post`, `imap_fetch`, `browser_click`, `browser_read`, `ocr_extract`, `parse`, `crosscheck`, `manual_approval`, `soft_submit`), `target_host`, `target_ref_masked`, `method`, `selector_or_endpoint`, `request_hash`, `response_hash`, `status`, `started_at`, `finished_at`, `human_required`, `error_message_masked`.

Правило: в `agent_action` не хранить bearer token, cookie, полный email body, screenshot с PII или полный текст документа; только ссылки на private artifact и хеши.

### `parsed_document`

Нормализованная запись из письма, Telegram OCR, LK export, ЭДО или PDF.

Ключевые поля: `id`, `source_id`, `run_id`, `raw_artifact_ref`, `raw_sha256`, `document_type`, `document_number`, `document_date`, `sender`, `counterparty_id`, `counterparty_name`, `inn`, `service_period_start`, `service_period_end`, `amount`, `currency`, `dds_article_candidate`, `pnl_article_candidate`, `parser_name`, `recognition_confidence`, `quality_status`, `evidence_ref`, `linked_bank_operation_id`, `linked_source_document_id`.

Статусы качества:

| Статус | Значение |
| --- | --- |
| `raw_synced` | raw artifact получен, еще не распознан |
| `parsed_ready` | обязательные поля извлечены, confidence достаточный |
| `low_confidence` | поля извлечены, но нужен человек |
| `owner_review` | есть правило или причина, требующая проверки владельца |
| `duplicate` | документ уже есть по sha256/номеру/периоду |
| `verified_by_crosscheck` | сверено с банком/API/ЭДО/другим источником |
| `rejected` | владелец или правило признали документ нерелевантным |
| `exported_to_app` | запись вошла в доменный модуль приложения |

### `source_snapshot`

Фиксирует снимок структурного источника: API page, CSV, Google Sheet range, LK export.

Ключевые поля: `id`, `source_id`, `run_id`, `snapshot_type`, `period_start`, `period_end`, `private_raw_ref`, `processed_ref`, `row_count`, `schema_hash`, `content_hash`, `quality_status`.

### `credential_event`

История проблем и ротаций доступа.

Ключевые поля: `id`, `credential_id`, `event_type`, `detected_at`, `detected_by_run_id`, `old_status`, `new_status`, `owner_action_required`, `resolution_note`.

### Общие правила хранения

1. `research/private/` - все raw payloads, cookies, OCR text, attachments, screenshots, body писем, назначения платежей, ФИО, телефоны, банковские реквизиты и payload для подписи.
2. `research/processed/` - только нормализованные агрегаты, технические IDs, периоды, суммы, статьи, статусы качества, sha256 и ссылки на private artifacts.
3. Любой AI/agent output должен иметь `source_id`, период, хеш raw evidence и статус качества.
4. Пустота, `0` и `не найдено` - разные состояния. Если источник не отдал данные, ставим `source_silent` / `owner_review`, а не ноль.
5. Для действий, которые могут менять внешний источник или инициировать платеж, нужен отдельный `manual_approval` action.
