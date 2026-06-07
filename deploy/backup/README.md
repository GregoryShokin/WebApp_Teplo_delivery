# Бэкап Postgres для прода Teplo

## Что это и зачем

`pg_backup.sh` раз в день делает снимок базы `teplo` из контейнера `teplo-postgres` через `pg_dump -Fc`. Это нужно, чтобы при сбое сервера, ошибочной миграции или потере данных можно было вернуть БД из последнего дампа.

**RPO = 24 часа**: при потере сервера теряем максимум сутки данных, потому что дамп делается раз в ночь.

## Где лежат бэкапы

По умолчанию дампы лежат в `/opt/teplo/backups` и называются `teplo_YYYYmmdd_HHMMSS.dump`, например `teplo_20260607_033000.dump`. Хранение: 14 дней (`RETENTION_DAYS=14`). Лог бэкапа: `/opt/teplo/backups/backup.log`, лог восстановления: `/opt/teplo/backups/restore.log`.

## Установка таймера

На сервере после `git pull`:

```bash
sudo cp /opt/teplo/deploy/backup/teplo-backup.service /etc/systemd/system/
sudo cp /opt/teplo/deploy/backup/teplo-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now teplo-backup.timer
```

Расписание по умолчанию: ежедневно в `03:30`, `Persistent=true` догонит пропущенный запуск после простоя. Проверка:

```bash
systemctl list-timers teplo-backup.timer
journalctl -u teplo-backup.service -n 50 --no-pager
tail -n 50 /opt/teplo/backups/backup.log
ls -lh /opt/teplo/backups/teplo_*.dump | tail -1
```

Альтернатива без systemd:

```cron
30 3 * * * BACKUP_DIR=/opt/teplo/backups RETENTION_DAYS=14 /opt/teplo/deploy/backup/pg_backup.sh
```

## Протокол восстановления

1. Остановить сервисы, которые пишут в БД:

```bash
cd /opt/teplo/deploy
docker compose -f docker-compose.prod.yml --env-file .env.prod stop api scheduler web
```

2. Выбрать дамп и восстановить его в основную БД `teplo`:

```bash
DUMP=/opt/teplo/backups/teplo_YYYYmmdd_HHMMSS.dump
CONFIRM=yes /opt/teplo/deploy/backup/pg_restore.sh "$DUMP"
```

Скрипт использует `pg_restore --clean --if-exists`, то есть перезаписывает объекты в текущей БД. Если нужна свежая БД вручную, сначала создать другую БД, затем выполнить `docker exec -i teplo-postgres pg_restore -U teplo -d <db_name> --clean --if-exists < "$DUMP"`.

3. Проверить миграционную версию и поднять сервисы:

```bash
docker exec teplo-postgres psql -U teplo -d teplo -Atc 'SELECT version_num FROM alembic_version;'
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d api scheduler web
```

## Тест восстановления

Бэкап без проверенного восстановления = иллюзия бэкапа. Минимум раз в месяц и после крупных миграций проверять дамп в отдельной БД:

```bash
DUMP=$(ls -1t /opt/teplo/backups/teplo_*.dump | head -1)
docker exec teplo-postgres dropdb -U teplo --if-exists teplo_restore_test
docker exec teplo-postgres createdb -U teplo teplo_restore_test
docker exec -i teplo-postgres pg_restore -U teplo -d teplo_restore_test --clean --if-exists < "$DUMP"
docker exec teplo-postgres psql -U teplo -d teplo_restore_test -Atc 'SELECT version_num FROM alembic_version;'
docker exec teplo-postgres dropdb -U teplo teplo_restore_test
```

## Offsite, опционально

Локальные бэкапы могут погибнуть вместе с диском VDS, поэтому лучше выгружать копию наружу. Установить и настроить `rclone`, затем создать `/etc/default/teplo-backup`:

```bash
RCLONE_REMOTE=remote-name:path/to/teplo/backups
```

После этого `pg_backup.sh` будет копировать каждый успешный дамп в этот remote.
