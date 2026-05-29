# Интеграция ящиков Mail.ru

## Назначение

Документ описывает подключение ящиков Mail.ru к локальному коннектору: проверку доступа, чтение писем, синхронизацию переписок, создание новых писем и ответы в существующие цепочки.

Текущий рабочий сценарий: два независимых источника из `.env`: личный ящик и рабочий ящик. По умолчанию ручные команды используют личный ящик; рабочий ящик выбирается через `--account workmail`, а read-only операции можно запускать сразу по обоим источникам через `--account all`. Корпоративный API Mail.ru/VK WorkSpace не используется, подключение идет по IMAP/SMTP.

## Коротко

| Задача | Метод | Команда |
| --- | --- | --- |
| Проверить доступ | IMAP + SMTP login | `check` |
| Получить список папок | IMAP `LIST` | `folders` |
| Синхронизировать письма | IMAP `UID SEARCH` + `UID FETCH` | `sync` |
| Посмотреть список писем | SQLite local read | `messages` |
| Прочитать письмо | SQLite local read | `show` |
| Создать новое письмо | SMTP `send_message` | `send` |
| Ответить в цепочку | SMTP + `In-Reply-To`/`References` | `reply` |

Скрипт: `research/scripts/mail/mailru_mailbox.py`.

Локальная база: `research/private/mail/mail.sqlite3`.

Вложения: `research/private/mail/attachments/`.

## Доступ

Для каждого Mail.ru ящика нужен пароль внешнего приложения. В настройках Mail.ru выбирается доступ:

```text
Полный доступ к Почте
SMTP, IMAP, POP3
```

Настройки протоколов:

| Параметр | Значение |
| --- | --- |
| IMAP host | `imap.mail.ru` |
| IMAP port | `993` |
| SMTP host | `smtp.mail.ru` |
| SMTP port | `465` |
| Security | SSL/TLS |

Переменные в локальном `.env`:

```dotenv
MAILRU_EMAIL=mailbox@example.com
MAILRU_APP_PASSWORD=external-application-password
MAILRU_WORKMAIL=workmail@example.com
MAILRU_WORKMAIL_PASSWORD=external-application-password
MAILRU_ACCOUNT=personal
MAILRU_IMAP_HOST=imap.mail.ru
MAILRU_IMAP_PORT=993
MAILRU_SMTP_HOST=smtp.mail.ru
MAILRU_SMTP_PORT=465
MAIL_DB_PATH=research/private/mail/mail.sqlite3
MAIL_ATTACHMENTS_DIR=research/private/mail/attachments
```

Пароль внешнего приложения нельзя хранить в Markdown, Google Docs, задачах или чате. Только локальный `.env`.

## Метод 1. Проверка подключения

Команда для личного ящика:

```bash
python3 research/scripts/mail/mailru_mailbox.py check
```

Команда для рабочего ящика:

```bash
python3 research/scripts/mail/mailru_mailbox.py --account workmail check
```

Команда для обоих источников:

```bash
python3 research/scripts/mail/mailru_mailbox.py --account all check
```

Что делает:

1. Читает `.env`.
2. Подключается к `imap.mail.ru:993`.
3. Делает IMAP login.
4. Получает список папок.
5. Подключается к `smtp.mail.ru:465`.
6. Делает SMTP login.
7. Создает локальную SQLite-базу, если ее еще нет.

Ожидаемый результат:

```text
IMAP OK: imap.mail.ru:993, folders=...
SMTP OK: smtp.mail.ru:465
DB: research/private/mail/mail.sqlite3
```

Если команда падает на IMAP/SMTP login, чаще всего причина в неверном пароле внешнего приложения или в удаленном/отозванном пароле в настройках Mail.ru.

## Метод 2. Получение папок

Команда:

```bash
python3 research/scripts/mail/mailru_mailbox.py folders
```

Что делает:

1. Выполняет IMAP `LIST`.
2. Декодирует русские имена папок из IMAP modified UTF-7.
3. Сохраняет папки в `mail_folders`.
4. Показывает системные признаки папок: `\Inbox`, `\Sent`, `\Trash`, `\Spam`, `\Drafts`.

Пример результата:

```text
INBOX [INBOX] attrs=\Inbox
Отправленные [...] attrs=\Sent sent
Корзина [...] attrs=\Trash
```

Эта команда нужна перед ручным выбором папки для синхронизации.

## Метод 3. Синхронизация писем

Базовая команда:

```bash
python3 research/scripts/mail/mailru_mailbox.py sync --limit 50
```

Для синхронизации обоих источников:

```bash
python3 research/scripts/mail/mailru_mailbox.py --account all sync --limit 50
```

По умолчанию синхронизируются:

1. `INBOX`;
2. папка отправленных, если сервер помечает ее как `\Sent` или название похоже на `sent` / `отправленные`.

Синхронизация конкретной папки:

```bash
python3 research/scripts/mail/mailru_mailbox.py sync --folder INBOX --limit 100
python3 research/scripts/mail/mailru_mailbox.py sync --folder "Отправленные" --limit 100
```

Полная повторная выборка выбранного диапазона:

```bash
python3 research/scripts/mail/mailru_mailbox.py sync --full --limit 100
```

Синхронизация всех папок:

```bash
python3 research/scripts/mail/mailru_mailbox.py sync --all-folders --limit 100
```

Как работает синхронизация:

| Шаг | Логика |
| --- | --- |
| Выбор папки | IMAP `SELECT` в режиме read-only |
| Контроль версии папки | `UIDVALIDITY` |
| Поиск новых писем | IMAP `UID SEARCH` от последнего сохраненного UID |
| Загрузка письма | IMAP `UID FETCH` с `RFC822`, `FLAGS`, `INTERNALDATE` |
| Парсинг | стандартный Python `email` parser |
| Дедупликация | уникальный ключ `account_email + folder + uidvalidity + uid` |
| Состояние | `mail_folders.last_uid` и `mail_folders.uidvalidity` |

Важно: синхронизация открывает папки read-only. Она не удаляет письма, не перемещает письма и не помечает их прочитанными.

## Метод 4. Чтение писем

Показать последние письма:

```bash
python3 research/scripts/mail/mailru_mailbox.py messages --limit 20
```

Прочитать письмо по локальному ID:

```bash
python3 research/scripts/mail/mailru_mailbox.py show 123
```

Показать HTML-версию тела:

```bash
python3 research/scripts/mail/mailru_mailbox.py show 123 --html
```

Ограничить размер вывода:

```bash
python3 research/scripts/mail/mailru_mailbox.py show 123 --max-chars 5000
```

Что хранится по письму:

| Поле | Назначение |
| --- | --- |
| `message_id` | оригинальный `Message-ID` письма |
| `in_reply_to` | связь с письмом, на которое отвечали |
| `references_header` | цепочка предыдущих `Message-ID` |
| `thread_key` | локальный ключ переписки |
| `subject` | исходная тема |
| `normalized_subject` | тема без `Re:` / `Fwd:` |
| `from_addr`, `to_addrs`, `cc_addrs` | участники |
| `body_text`, `body_html` | текстовая и HTML-версии |
| `flags_json` | IMAP-флаги |
| `has_attachments` | есть ли вложения |

Вложения сохраняются в `research/private/mail/attachments/`, а в базе хранится путь, размер, тип и `sha256`.

## Метод 5. Синхронизация переписок

Показать последние переписки:

```bash
python3 research/scripts/mail/mailru_mailbox.py summary --limit 20
```

Группировка строится локально:

| Приоритет | Источник ключа |
| --- | --- |
| 1 | первый `Message-ID` из `References` |
| 2 | `In-Reply-To` |
| 3 | собственный `Message-ID` |
| 4 | хеш нормализованной темы, если заголовков нет |

Чтобы видеть полную переписку, нужно синхронизировать и входящие, и отправленные. Поэтому стандартный режим `sync` берет `INBOX` + `Отправленные`.

Ограничение: если участники меняли тему вручную или почтовый клиент не проставил `Message-ID`/`References`, цепочка может разбиться на несколько локальных переписок. Это нормальный риск для IMAP/SMTP-интеграций.

## Метод 6. Поиск документов от контрагентов

Справочник почтовых отправителей актов, счетов и отчетов:
[mail_document_senders.csv](/research/processed/mail/mail_document_senders.csv).

Текущий подтвержденный список:

| Email отправителя | Контрагент | Что искать |
| --- | --- | --- |
| `account@lemma.ru` | Лемма | счета за техподдержку iiko |
| `hello@biz-panel.com` | Синапс | отчеты о Яндекс Директ |
| `webmaster@insaitov.ru` | Синопсис | счета за SEO |
| `donotreply@iiko.ru` | iiko | счета за подписку и приложения для курьеров |
| `leshkina@starterapp.ru` | StarterApp | документы по сайту/приложению |
| `office@docsinbox.ru` | Доксинбокс | счета и закрывающие документы |
| `noreply@clean-rf.ru` | Экоцентр | счета и комплекты документов |
| `info@ohrana-ug7.ru` | Охрана Юг | счета охранного предприятия |
| `info@sa-ug.ru` | Спецавто Юг | счета охранной сигнализации |
| `smmbux@yandex.ru` | ИП Билинский | УПД из письма `Документы для ИП Шокина Кристина Юрьевна...`: отдельно абонентское обслуживание/продвижение и рекламный бюджет за период |
| `buh@vdonsk.ru` | Микроэл | документы интернет-провайдера |
| `askad02@mail.ru` | Наталья / налоговый агент | документы налогового агента |

После синхронизации можно искать письма от конкретного отправителя в локальной базе:

```bash
sqlite3 research/private/mail/mail.sqlite3 "
SELECT id, folder_name, date_utc, from_addr, subject
FROM mail_messages
WHERE lower(from_addr) LIKE '%account@lemma.ru%'
ORDER BY COALESCE(date_utc, '') DESC
LIMIT 20;"
```

Для автоматической обработки этот CSV должен стать входом для отдельного шага классификации почтовых документов: `email -> counterparty_id -> accounting_block -> document_types`.

## Метод 7. Создание нового письма

Сухой прогон без отправки:

```bash
python3 research/scripts/mail/mailru_mailbox.py send \
  --to client@example.com \
  --subject "Тема письма" \
  --text "Текст письма" \
  --dry-run
```

Отправить письмо:

```bash
python3 research/scripts/mail/mailru_mailbox.py send \
  --to client@example.com \
  --subject "Тема письма" \
  --text "Текст письма"
```

Отправить нескольким адресатам:

```bash
python3 research/scripts/mail/mailru_mailbox.py send \
  --to client1@example.com \
  --to client2@example.com \
  --cc manager@example.com \
  --subject "Тема письма" \
  --text-file research/private/mail/drafts/message.txt
```

Отправить HTML и вложение:

```bash
python3 research/scripts/mail/mailru_mailbox.py send \
  --to client@example.com \
  --subject "Тема письма" \
  --text-file research/private/mail/drafts/message.txt \
  --html-file research/private/mail/drafts/message.html \
  --attachment research/private/mail/drafts/file.pdf
```

Что делает `send`:

1. Собирает MIME-письмо.
2. Добавляет `From`, `To`, `Cc`, `Subject`, `Date`, `Message-ID`.
3. Добавляет plain text тело.
4. При наличии добавляет HTML-альтернативу.
5. При наличии добавляет вложения.
6. Отправляет через SMTP.
7. Записывает попытку в `mail_outbox`.
8. После успешной отправки пытается сохранить копию в папку `Отправленные` через IMAP `APPEND`.

`Bcc` поддерживается через `--bcc`, но не записывается в заголовки письма.

## Метод 8. Ответ на письмо

Сухой прогон без отправки:

```bash
python3 research/scripts/mail/mailru_mailbox.py reply 123 \
  --text "Здравствуйте! Ответ по вашему письму..." \
  --dry-run
```

Отправить ответ:

```bash
python3 research/scripts/mail/mailru_mailbox.py reply 123 \
  --text "Здравствуйте! Ответ по вашему письму..."
```

Ответ с HTML и вложением:

```bash
python3 research/scripts/mail/mailru_mailbox.py reply 123 \
  --text-file research/private/mail/drafts/reply.txt \
  --html-file research/private/mail/drafts/reply.html \
  --attachment research/private/mail/drafts/file.pdf
```

Что делает `reply`:

1. Берет письмо из `mail_messages` по локальному ID.
2. Определяет адрес получателя.
3. Создает тему `Re: ...`, если ее еще нет.
4. Ставит `In-Reply-To` равным `Message-ID` исходного письма.
5. Дополняет `References`.
6. Отправляет письмо через SMTP.
7. Пишет результат в `mail_outbox`.
8. Пытается сохранить копию в `Отправленные`.

Получателя можно переопределить вручную:

```bash
python3 research/scripts/mail/mailru_mailbox.py reply 123 \
  --to another@example.com \
  --text "Текст ответа"
```

## Локальная база

| Таблица | Назначение |
| --- | --- |
| `mail_folders` | папки IMAP, `UIDVALIDITY`, последний синхронизированный UID |
| `mail_messages` | письма, заголовки, тела, флаги, локальная привязка к папке |
| `mail_threads` | локальные переписки |
| `mail_attachments` | метаданные и локальные пути вложений |
| `mail_outbox` | попытки отправки новых писем и ответов |

Проверить технические счетчики:

```bash
sqlite3 research/private/mail/mail.sqlite3 \
  "SELECT 'folders', count(*) FROM mail_folders
   UNION ALL SELECT 'messages', count(*) FROM mail_messages
   UNION ALL SELECT 'threads', count(*) FROM mail_threads
   UNION ALL SELECT 'attachments', count(*) FROM mail_attachments;"
```

## Безопасность

1. Пароль внешнего приложения хранится только в `.env`.
2. `.env`, `research/private/` и сырые вложения не коммитятся.
3. Синхронизация не меняет состояние писем на сервере.
4. Команды отправки нужно сначала проверять через `--dry-run`.
5. Если пароль внешнего приложения попал в чат или документ, его нужно отозвать в Mail.ru и создать новый.
6. Для каждого нового личного ящика нужен отдельный пароль внешнего приложения.

## Рабочий порядок для первого ящика

```bash
python3 research/scripts/mail/mailru_mailbox.py check
python3 research/scripts/mail/mailru_mailbox.py folders
python3 research/scripts/mail/mailru_mailbox.py sync --limit 50
python3 research/scripts/mail/mailru_mailbox.py messages --limit 20
python3 research/scripts/mail/mailru_mailbox.py show 123
python3 research/scripts/mail/mailru_mailbox.py reply 123 --text "Текст ответа" --dry-run
python3 research/scripts/mail/mailru_mailbox.py reply 123 --text "Текст ответа"
```

Для регулярной работы достаточно периодически запускать:

```bash
python3 research/scripts/mail/mailru_mailbox.py sync --limit 100
```
