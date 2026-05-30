# Операционные интеграции

Скрипты и credentials для регулярных выгрузок данных из внешних систем. В отличие от `research/`, этот слой — рабочий: скрипты запускаются по расписанию (вручную или через scheduler) и поставляют данные для бизнес-процессов и приложения.

Логика разделения:
- **integrations/** — код и credentials для общения с внешними API
- **research/scripts/** — downstream-обработка (классификаторы, агрегаторы), не общается с API напрямую
- **apps/api/** — runtime приложения; со временем поглотит часть интеграций

## Интеграции

| Папка | Внешняя система | Credentials | Что делает |
|---|---|---|---|
| [iiko/](iiko/) | iiko Server | — (HTTP login) | Экспорт сотрудников, OLAP, продаж, выручки, заказов, плана счетов; билдеры экономического блока и P&L |
| [sber/](sber/) | Sber Business API | `.pem` TLS-сертификат + ключ (gitignored) | Экспорт выписки банка поступлений, построение cashflow, сверка с iiko |
| [tbank/](tbank/) | T-Bank Business Open API | `.env` токен | Экспорт выписки банка расходов, парсеры платёжек, OCR, сопоставление с ДДС |
| [mango/](mango/) | Mango Office VPBX | `.env` ключи | Экспорт телекоммуникационных данных |
| [mailru/](mailru/) | Mail.ru IMAP/SMTP | `.env` логин/пароль | Чтение и ответы на письма из личных ящиков |

## Спецификации API

Документация конкретных API endpoints, аутентификации и форматов данных живёт в `app-spec/integrations/`:
- [iiko Server API](../app-spec/integrations/iiko/server-api-endpoints.md)
- [Sber API](../app-spec/integrations/sber/api-endpoints.md)
- [T-Bank API](../app-spec/integrations/tbank/api-endpoints.md)
- [Mango (телефония)](../app-spec/integrations/mango/telephony.md)
- [Mail.ru](../app-spec/integrations/mailru/personal-mailbox.md)
- [iiko Courier Service](../app-spec/integrations/iiko/courier-service/)
- [СБИС ЭДО](../app-spec/integrations/sbis-edo/api-endpoints.md)
