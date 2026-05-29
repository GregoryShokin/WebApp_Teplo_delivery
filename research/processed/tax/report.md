# Tax WorkMail pass

Дата pass: 2026-05-25. Источник: WorkMail налогового агента `askad02@mail.ru` / S48.

IMAP sync после локального pass: `2026-05-25`, INBOX, 12 новых писем; новых писем от `askad02@mail.ru` после `#837` не найдено.

## Вывод

- P&L/API строка `Налоги` ниже EBITDA остается расчетным accrual/model начислением: 7% от iiko OLAP выручки без скидок.
- Cash-выплата `659 783` фиксируется по owner correction как только УСН 6%, не как 7% и не вместе с 1% страховых взносов.
- Tax payable ведется отдельно от P&L. Parsed seed: письмо `#837` от 2026-05-20 `0,2 %.xls, ЕНП до 28.05.docx`.

## Parsed Tax Payable Seed

| Source | Obligation | Amount | Source Due Date | Owner Close Deadline | Status |
| --- | --- | ---: | --- | --- | --- |
| WorkMail `#837`, attachment `ЕНП до 28.05.docx` | ЕНП / налоговая задолженность | 18 556,93 ₽ | 2026-05-28 | 2026-06-30 | `pending_parse` |
| WorkMail `#837`, attachment `0,2 %.xls` | Страховые взносы от несчастных случаев / травматизм 0,2% | 78,95 ₽ | 2026-05-28 | 2026-06-30 | `pending_parse` |

Период и payment match пока не закрыты; это не owner-question. Банк используется как cash/control, а не как источник accrual P&L.

## Files

- `research/processed/tax/tax_payables_2026.csv`
- `research/processed/tax/workmail_tax_agent_messages.csv`
