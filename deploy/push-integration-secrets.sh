#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(cd "$script_dir/.." && pwd)"

host="${1:-teplo-prod}"
remote_dir="${2:-/opt/teplo/deploy}"
local_env="${LOCAL_ENV:-$repo_dir/.env}"
tmp_dir="$(mktemp -d)"

cleanup() {
  rm -rf "$tmp_dir"
}
trap cleanup EXIT

if [[ ! -f "$local_env" ]]; then
  echo "Missing local env file: $local_env" >&2
  exit 1
fi

python3 "$script_dir/scripts/build_integrations_env.py" \
  --source "$local_env" \
  --template "$script_dir/env.integrations.example" \
  --output "$tmp_dir/.env.integrations" \
  --secrets-dir "$tmp_dir/secrets"

chmod 600 "$tmp_dir/.env.integrations"

ssh "$host" "set -e; install -d -m 700 '$remote_dir/secrets' '$remote_dir/secrets/sber'"
scp "$tmp_dir/.env.integrations" "$host:$remote_dir/.env.integrations.upload"

if [[ -d "$tmp_dir/secrets/sber" ]]; then
  scp "$tmp_dir"/secrets/sber/* "$host:$remote_dir/secrets/sber/"
fi

ssh "$host" "set -e; cd '$remote_dir'; \
  install -m 600 .env.integrations.upload .env.integrations; \
  rm -f .env.integrations.upload; \
  chmod 700 secrets secrets/sber; \
  find secrets -type f -exec chmod 600 {} +; \
  docker compose -f docker-compose.prod.yml --env-file .env.prod up -d api scheduler; \
  docker compose -f docker-compose.prod.yml --env-file .env.prod exec -T api python -m app.scripts.sync_integration_secrets"

echo "Integration secrets pushed to $host:$remote_dir."
echo "Run check: ssh $host 'cd $remote_dir && docker compose -f docker-compose.prod.yml --env-file .env.prod exec -T api python -m app.scripts.sync_integration_secrets --check'"
