# Teplo production deploy

Single-host deployment on Ubuntu 24.04 with Docker Compose and Caddy
(automatic Let's Encrypt SSL). Designed for one tenant, one VDS.

## Layout

- `docker-compose.prod.yml` — services: postgres + api + web + caddy.
- `Caddyfile` — reverse proxy + automatic HTTPS. Reads `${TEPLO_DOMAIN}`
  from the environment.
- `.env.prod` — secrets and per-host config (not committed; see
  `env.prod.example`).
- `.env.integrations` — integration secrets for `api` and `scheduler` (not
  committed; see `env.integrations.example`).
- `SECRETS.md` — production secret procedure: `.env.prod`, file secrets,
  bank credentials, checks and rotation.
- `secrets/` — host-only file secrets mounted read-only to
  `/run/secrets/teplo` in `api` and `scheduler` containers.

## First-time bring-up

```bash
cd /opt/teplo/deploy
TEPLO_DOMAIN=app.company.ru TEPLO_ADMIN_EMAIL=admin@company.ru ./init-prod-env.sh
nano .env.prod                       # fill in domain/account fallbacks if needed
nano .env.integrations               # fill integration tokens if needed
./check-prod-secrets.sh

# Build images and start everything.
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d --build

# Apply database migrations.
docker compose -f docker-compose.prod.yml --env-file .env.prod \
  exec api alembic upgrade head
```

Caddy will obtain a Let's Encrypt certificate for `${TEPLO_DOMAIN}` on
the first HTTPS request as long as DNS already points to this server.

For Sber/T-Bank production credentials, follow `SECRETS.md`. Do not put bank
tokens or mTLS private keys in git or in shell history.

## Updating after a git pull

```bash
cd /opt/teplo
git pull --ff-only
cd deploy
./check-prod-secrets.sh
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d --build
docker compose -f docker-compose.prod.yml --env-file .env.prod \
  exec api alembic upgrade head
```

## Logs and status

```bash
docker compose -f docker-compose.prod.yml --env-file .env.prod ps
docker compose -f docker-compose.prod.yml --env-file .env.prod logs -f api
docker compose -f docker-compose.prod.yml --env-file .env.prod logs -f caddy
```

## Backups

`cron -e` for the `teplo` user:

```cron
0 3 * * * cd /opt/teplo/deploy && docker compose -f docker-compose.prod.yml --env-file .env.prod exec -T postgres pg_dump -U teplo teplo | gzip > /opt/teplo/backups/teplo-$(date +\%Y\%m\%d).sql.gz
```
