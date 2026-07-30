# Teplo production deploy

Single-host deployment on Ubuntu 24.04 with Docker Compose and Caddy
(automatic Let's Encrypt SSL). Designed for one tenant, one VDS.

## Layout

- `docker-compose.prod.yml` — services: postgres + api + scheduler + web + caddy.
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
nano .env.integrations               # integration secrets — REQUIRED, see SECRETS.md
./check-prod-secrets.sh              # names every missing value; it must pass first

# Build the images.
docker compose -f docker-compose.prod.yml --env-file .env.prod build

# Bring up the database and create the schema before anything connects to it.
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d postgres
docker compose -f docker-compose.prod.yml --env-file .env.prod run --rm api \
  alembic upgrade head

# Start the rest.
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d
```

Same shape as every other flow in this file: build, migrate, then start. See
"Updating after a git pull" for why the order matters once the host is live.

Caddy will obtain a Let's Encrypt certificate for `${TEPLO_DOMAIN}` on
the first HTTPS request as long as DNS already points to this server.

For Sber/T-Bank production credentials, follow `SECRETS.md`. Do not put bank
tokens or mTLS private keys in git or in shell history.

## Updating after a git pull

**Migrate before the new code starts serving traffic.** The steps below are
ordered on purpose — see "Why this order" right after the snippet.

```bash
cd /opt/teplo
git pull --ff-only
cd deploy
./check-prod-secrets.sh

# 1. Back up the database before touching the schema.
#    Use the script, not a bare `pg_dump | gzip`: it checks the exit code, refuses
#    to keep an empty dump, writes through a temp file and takes a lock. A piped
#    one-liner reports gzip's exit code, so a failed dump still looks like a valid
#    archive. The script writes /opt/teplo/backups/teplo_<timestamp>.dump in the
#    custom format that deploy/backup/pg_restore.sh expects.
/opt/teplo/deploy/backup/pg_backup.sh

# 2. Build the new images. Running containers keep serving the old code.
docker compose -f docker-compose.prod.yml --env-file .env.prod build api scheduler web

# 3. Sanity-check the revision chain with the NEW image: exactly one head.
#    Two heads mean parallel branches collided — merge them before migrating.
docker compose -f docker-compose.prod.yml --env-file .env.prod run --rm api alembic heads

# 4. Migrate using that same freshly built image.
docker compose -f docker-compose.prod.yml --env-file .env.prod run --rm api \
  alembic upgrade head

# 5. Recreate the services with the new code.
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d api scheduler web
```

### Why this order

**New code on an old schema breaks production.** A migration is additive for the
*old* code (it simply ignores the new columns), but the *new* code expects the
columns to already exist. Bringing containers up first opens a window where every
request touching the changed tables fails. This is not hypothetical: on 2026-07-24
`api` spent about nine minutes returning HTTP 500 for every `safe_allocations`
query, because the containers were recreated with migration `0211` in the code
while the database was still on `0208`.

**Use `run --rm`, not `exec api`.** Source code is baked into the image, so the
*running* `api` container does not contain the new migration files at all —
`exec api alembic upgrade head` would silently apply nothing new. `run --rm`
starts a throwaway container from the image you just built, leaving the live
containers alone.

Between steps 4 and 5 the old code runs against the new schema. That direction is
safe for additive migrations (new nullable columns, new tables): SQLAlchemy always
lists columns explicitly and never issues `SELECT *`, so the old code does not see
them. A migration that *drops* or *renames* something is not additive — take a
short maintenance window instead.

**It also protects incoming webhooks.** Recreating `api` changes its IP, and Caddy
retries a failed dial for 15 seconds only (`lb_try_duration` in `Caddyfile`). That
budget covers `up -d`, but not `up -d --build` plus a migration on top — anything
the bank or iiko posts during a longer window is lost for good. Keeping the build
and the migration outside the restart keeps the gap inside the retry budget.

`alembic upgrade head` applies **every** pending migration, not just yours.
Check what else is queued before you start (below).

### Migration gotchas

**Revision ids must fit in 32 characters.** `alembic_version.version_num` is
`varchar(32)`. A longer `revision = "..."` fails on the final `UPDATE
alembic_version` — *after* the DDL ran — and the whole migration transaction rolls
back. The naming convention here (`NNNN_snake_case_description`) sits right at the
edge: eight of the 241 revisions are exactly 32 characters long. Shorten the id and
the file name, rebuild the image (the id is baked in), then migrate again.

**One head only.** Parallel branches that both fork from the same revision produce
two heads, and `alembic upgrade head` refuses to run. Whoever merges second
rebases their chain onto the other, so numbering stays linear — see step 3.

### Checks before you start

```bash
# What is about to be deployed, and does it carry someone else's migrations?
git log --oneline <previous-prod-commit>..HEAD
git diff --name-only <previous-prod-commit>..HEAD -- apps/api/alembic/versions

# Is someone else deploying right now? /opt/teplo is shared between agents.
pgrep -fa 'docker compose'
docker images --format '{{.Repository}}\t{{.CreatedSince}}' | grep teplo-prod
```

Two people deploying at once is a real failure mode, not a theoretical one: on
2026-07-30 two branches were building on this host within the same minute.
Parallel `docker compose` runs fight over the same containers
("container is marked for removal and cannot be started"), and `alembic upgrade
head` applies *both* chains at once. If someone else is mid-deploy, wait — their
build already carries your commit if you merged first.

Do not check the migration chain with the *old* image: `alembic heads` inside a
container built before your `git pull` reports the previous head. That is why the
check sits at step 3, after `build`.

## Syncing local code without git

From the local repository root, preview the transfer first:

```bash
deploy/sync-code-to-server.sh teplo-prod /opt/teplo
```

The script runs `rsync -av --delete` in dry-run mode by default and excludes
production env files, file secrets, backups, raw/private data and local build
caches. To apply the reviewed sync, pass `--apply` explicitly:

```bash
deploy/sync-code-to-server.sh --apply teplo-prod /opt/teplo
```

## Logs and status

```bash
docker compose -f docker-compose.prod.yml --env-file .env.prod ps
docker compose -f docker-compose.prod.yml --env-file .env.prod logs -f api
docker compose -f docker-compose.prod.yml --env-file .env.prod logs -f caddy
```

## Backups

Scheduled backups run from a systemd timer, not from cron — see
`deploy/backup/README.md` for installation and restore.

```bash
systemctl status teplo-backup.timer     # daily at 03:30, Persistent=true
/opt/teplo/deploy/backup/pg_backup.sh   # same script, on demand (before a deploy)
```

`pg_backup.sh` writes `/opt/teplo/backups/teplo_<timestamp>.dump` (custom format,
restored by `pg_restore.sh`), verifies that the dump is non-empty, and rotates
anything older than 14 days.

Do not add a second cron entry with a hand-written `pg_dump | gzip`: it produces
`.sql.gz` files that `pg_restore.sh` cannot read and that the rotation glob
(`teplo_*.dump`) never deletes, so they accumulate forever in the same directory.
