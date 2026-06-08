#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "$-" == *x* ]]; then
  echo "ERROR: disable shell xtrace before creating DB dumps; it can expose connection strings." >&2
  exit 2
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
mode=""
compose_file="${repo_root}/apps/docker-compose.yml"
compose_service="postgres"
compose_user="teplo"
compose_db="teplo"
output="${TMPDIR:-/tmp}/teplo-local-history-$(date +%Y%m%d-%H%M%S).dump"

usage() {
  cat <<'USAGE'
Usage:
  deploy/prod-bootstrap/create-local-db-dump.sh --docker-compose [--output PATH]
  deploy/prod-bootstrap/create-local-db-dump.sh --database-url [--output PATH]

Creates a custom-format pg_dump and excludes source_credential table data.

Modes:
  --docker-compose        Dump from apps/docker-compose.yml postgres service.
  --database-url         Dump from LOCAL_DATABASE_URL. If the env var is missing,
                         the script prompts for it without echoing input.

Options:
  --output PATH          Dump path. Defaults to $TMPDIR/teplo-local-history-*.dump.
  --compose-file PATH    Override compose file for --docker-compose mode.
  --service NAME         Override compose service. Default: postgres.
  --db-user NAME         Override compose Postgres user. Default: teplo.
  --db-name NAME         Override compose database. Default: teplo.
  -h, --help             Show this help.
USAGE
}

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --docker-compose)
      mode="compose"
      shift
      ;;
    --database-url)
      mode="url"
      shift
      ;;
    --output)
      [[ $# -ge 2 ]] || fail "--output needs a path"
      output="$2"
      shift 2
      ;;
    --compose-file)
      [[ $# -ge 2 ]] || fail "--compose-file needs a path"
      compose_file="$2"
      shift 2
      ;;
    --service)
      [[ $# -ge 2 ]] || fail "--service needs a name"
      compose_service="$2"
      shift 2
      ;;
    --db-user)
      [[ $# -ge 2 ]] || fail "--db-user needs a name"
      compose_user="$2"
      shift 2
      ;;
    --db-name)
      [[ $# -ge 2 ]] || fail "--db-name needs a name"
      compose_db="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      fail "unknown argument: $1"
      ;;
  esac
done

[[ -n "$mode" ]] || {
  usage >&2
  exit 2
}

mkdir -p "$(dirname "$output")"
umask 077

dump_from_compose() {
  docker compose -f "$compose_file" exec -T "$compose_service" \
    pg_dump -U "$compose_user" -d "$compose_db" -Fc \
      --exclude-table-data=source_credential \
      "--exclude-table-data=*.source_credential" \
      > "$output"
}

dump_from_database_url() {
  command -v pg_dump >/dev/null 2>&1 || fail "pg_dump is required for --database-url mode"
  command -v python3 >/dev/null 2>&1 || fail "python3 is required for --database-url mode"

  local database_url="${LOCAL_DATABASE_URL:-}"
  if [[ -z "$database_url" ]]; then
    [[ -t 0 ]] || fail "LOCAL_DATABASE_URL is missing and stdin is not interactive"
    read -r -s -p "LOCAL_DATABASE_URL: " database_url
    printf '\n'
  fi

  python3 -c 'exec("""
import os
import subprocess
import sys
import tempfile
from urllib.parse import parse_qs, unquote, urlparse

output = sys.argv[1]
database_url = sys.stdin.read().strip()
if database_url.startswith("postgresql+asyncpg://"):
    database_url = "postgresql://" + database_url.removeprefix("postgresql+asyncpg://")
elif database_url.startswith("postgres://"):
    database_url = "postgresql://" + database_url.removeprefix("postgres://")

parsed = urlparse(database_url)
if parsed.scheme != "postgresql":
    raise SystemExit("LOCAL_DATABASE_URL must use postgresql:// or postgresql+asyncpg://")

host = parsed.hostname or "localhost"
port = str(parsed.port or 5432)
user = unquote(parsed.username or "")
password = unquote(parsed.password or "")
database = unquote(parsed.path.lstrip("/"))
if not user or not database:
    raise SystemExit("LOCAL_DATABASE_URL must include user and database name")

def pgpass_escape(value: str) -> str:
    return value.replace("\\\\", "\\\\\\\\").replace(":", "\\\\:")

with tempfile.NamedTemporaryFile("w", delete=False) as passfile:
    os.chmod(passfile.name, 0o600)
    passfile.write(
        ":".join(pgpass_escape(value) for value in (host, port, database, user, password))
        + "\\n"
    )

env = os.environ.copy()
env["PGPASSFILE"] = passfile.name
query = parse_qs(parsed.query)
if query.get("sslmode"):
    env["PGSSLMODE"] = query["sslmode"][0]

cmd = [
    "pg_dump",
    "-h",
    host,
    "-p",
    port,
    "-U",
    user,
    "-d",
    database,
    "-Fc",
    "--exclude-table-data=source_credential",
    "--exclude-table-data=*.source_credential",
    "-f",
    output,
]
try:
    subprocess.run(cmd, env=env, check=True)
finally:
    try:
        os.unlink(passfile.name)
    except FileNotFoundError:
        pass
""")' "$output" <<< "$database_url"
}

verify_dump_has_no_source_credentials() {
  local dump_list
  if command -v pg_restore >/dev/null 2>&1; then
    dump_list="$(pg_restore -l "$output")"
  elif [[ "$mode" == "compose" ]]; then
    dump_list="$(docker compose -f "$compose_file" exec -T "$compose_service" pg_restore -l < "$output")"
  else
    echo "WARN: pg_restore was not found; skipped local dump-list verification." >&2
    return 0
  fi

  if grep -E 'TABLE DATA .* source_credential( |$)' <<< "$dump_list" >/dev/null; then
    fail "dump contains source_credential TABLE DATA; refusing to continue"
  fi
}

case "$mode" in
  compose)
    dump_from_compose
    ;;
  url)
    dump_from_database_url
    ;;
esac

chmod 600 "$output"
verify_dump_has_no_source_credentials
echo "Created sensitive dump without source_credential data: $output"
