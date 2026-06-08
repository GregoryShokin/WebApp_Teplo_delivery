# Bootstrap боевой БД

Основной go-live runbook: [`LOCAL_TO_PROD_DB.md`](LOCAL_TO_PROD_DB.md).

Он описывает безопасный перенос локальной истории БД в prod:

- pre-go-live backup prod-БД в `pg_dump -Fc`;
- локальный `pg_dump -Fc` без данных `source_credential`;
- передачу дампа через `scp` или `rsync`;
- restore через `pg_restore --clean --if-exists --no-owner`;
- повторную синхронизацию prod integration secrets из `.env.integrations`;
- smoke-check и rollback.

## Важное про секреты

Не переносить `.env`, `.env.prod`, `.env.integrations`, банковские токены и
данные `source_credential` из локальной БД. Пока
`source_credential.value_encrypted` не является envelope encryption, полный дамп
БД считается чувствительным.

Prod-секреты вводятся и проверяются только на сервере, см. [`../SECRETS.md`](../SECRETS.md).

## Файлы в этом каталоге

- [`LOCAL_TO_PROD_DB.md`](LOCAL_TO_PROD_DB.md) - пошаговый runbook переноса
  локальной истории БД в prod без локальных integration secrets.
- `create-local-db-dump.sh` - helper для локального custom-дампа с exclude
  `source_credential` table data.
- `cleanup-test-artifacts.sql` - legacy/optional чистка тестовых артефактов.
  Использовать только если владелец данных отдельно подтвердил, что в импортной
  БД действительно есть тестовый слой, который нужно удалить.
