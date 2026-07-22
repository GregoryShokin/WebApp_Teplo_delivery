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

# The rendered file REPLACES the remote one, so a key the template does not carry is
# wiped from production (that is how IIKO_CLOUD_* were lost on 2026-07-21 — the loss
# only surfaced at the next container recreate). Refuse to overwrite while the remote
# file has filled-in keys the new one would not: add them to env.integrations.example
# and the local .env, or re-run with ALLOW_DROP_KEYS=1 when the drop is intentional.
nonempty_keys() {
  sed -n 's/^\([A-Za-z_][A-Za-z0-9_]*\)=..*/\1/p' | sort -u
}

remote_keys="$(ssh "$host" "cat '$remote_dir/.env.integrations' 2>/dev/null" | nonempty_keys)"
new_keys="$(nonempty_keys < "$tmp_dir/.env.integrations")"
dropped="$(comm -23 <(printf '%s\n' "$remote_keys") <(printf '%s\n' "$new_keys") | grep -v '^$' || true)"

if [[ -n "$dropped" && "${ALLOW_DROP_KEYS:-0}" != "1" ]]; then
  echo "Refusing to push: these keys have a value on $host but not in the new file:" >&2
  printf '  %s\n' $dropped >&2
  echo "Add them to deploy/env.integrations.example (and to $local_env), or set ALLOW_DROP_KEYS=1." >&2
  exit 1
fi

ssh "$host" "set -e; install -d -m 700 '$remote_dir/secrets' '$remote_dir/secrets/sber'"
scp "$tmp_dir/.env.integrations" "$host:$remote_dir/.env.integrations.upload"

if [[ -d "$tmp_dir/secrets/sber" ]]; then
  scp "$tmp_dir"/secrets/sber/* "$host:$remote_dir/secrets/sber/"
fi

ssh "$host" "set -e; cd '$remote_dir'; \
  if [ -f .env.integrations ]; then cp -p .env.integrations \".env.integrations.bak-\$(date +%Y%m%d-%H%M%S)\"; fi; \
  install -m 600 .env.integrations.upload .env.integrations; \
  rm -f .env.integrations.upload; \
  chmod 700 secrets secrets/sber; \
  find secrets -type f -exec chmod 600 {} +; \
  docker compose -f docker-compose.prod.yml --env-file .env.prod up -d api scheduler; \
  docker compose -f docker-compose.prod.yml --env-file .env.prod exec -T api python -m app.scripts.sync_integration_secrets"

echo "Integration secrets pushed to $host:$remote_dir."
echo "Run check: ssh $host 'cd $remote_dir && docker compose -f docker-compose.prod.yml --env-file .env.prod exec -T api python -m app.scripts.sync_integration_secrets --check'"
