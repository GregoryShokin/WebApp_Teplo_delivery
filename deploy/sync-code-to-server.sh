#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  deploy/sync-code-to-server.sh [--apply] <ssh-target> [remote-dir]

Examples:
  deploy/sync-code-to-server.sh teplo-prod /opt/teplo
  deploy/sync-code-to-server.sh --apply teplo-prod /opt/teplo

Runs rsync in dry-run mode by default. Pass --apply to change the server.
EOF
}

apply=0
args=()

for arg in "$@"; do
  case "$arg" in
    --apply)
      apply=1
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    -*)
      echo "Unknown option: $arg" >&2
      usage >&2
      exit 2
      ;;
    *)
      args+=("$arg")
      ;;
  esac
done

if ((${#args[@]} < 1 || ${#args[@]} > 2)); then
  usage >&2
  exit 2
fi

if ! command -v rsync >/dev/null 2>&1; then
  echo "rsync is required" >&2
  exit 1
fi

ssh_target="${args[0]}"
remote_dir="${args[1]:-/opt/teplo}"
remote_dir="${remote_dir%/}"

if [[ -z "$ssh_target" || -z "$remote_dir" ]]; then
  echo "SSH target and remote dir must be non-empty" >&2
  exit 2
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(cd "$script_dir/.." && pwd)"

mode="dry-run"
dry_run_args=(--dry-run --itemize-changes)
if ((apply)); then
  mode="apply"
  dry_run_args=()
fi

excludes=(
  ".git"
  ".env"
  ".env.*"
  ".venv"
  ".venv-*"
  "node_modules"
  "dist"
  "test-results"
  ".pytest_cache"
  ".ruff_cache"
  "__pycache__"
  "deploy/.env.prod"
  "deploy/.env.integrations"
  "deploy/secrets"
  "deploy/backups"
  "research/raw"
  "research/private"
  "integrations/*/credentials"
  "integrations/*/private"
)

rsync_args=(
  -av
  --delete
  "${dry_run_args[@]}"
)

for pattern in "${excludes[@]}"; do
  rsync_args+=(--exclude "$pattern")
done

rsync_args+=(
  "$repo_dir/"
  "$ssh_target:$remote_dir/"
)

echo "Target: $ssh_target:$remote_dir/"
echo "Mode: $mode"
echo "WARNING: server-side production secrets are excluded and must not be deleted or overwritten:"
echo "  $remote_dir/deploy/.env.prod"
echo "  $remote_dir/deploy/.env.integrations"
echo "  $remote_dir/deploy/secrets"
echo "  $remote_dir/deploy/backups"
echo

exec rsync "${rsync_args[@]}"
