#!/usr/bin/env bash
set -euo pipefail

caller_dir="$PWD"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

env_arg="${1:-.env.prod}"
if [[ "$env_arg" = /* ]]; then
  env_file="$env_arg"
elif (($# > 0)); then
  env_file="$caller_dir/$env_arg"
else
  env_file="$script_dir/.env.prod"
fi

cd "$script_dir"
errors=()
warnings=()

if [[ ! -f "$env_file" ]]; then
  echo "Missing $env_file. Run ./init-prod-env.sh first or copy env.prod.example." >&2
  exit 1
fi

file_mode() {
  stat -c "%a" "$1" 2>/dev/null || stat -f "%Lp" "$1"
}

env_value() {
  local key="$1"
  awk -v key="$key" '
    /^[[:space:]]*#/ { next }
    /^[[:space:]]*$/ { next }
    index($0, "=") == 0 { next }
    {
      name=$0
      sub(/=.*/, "", name)
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", name)
      if (name == key) {
        value=$0
        sub(/^[^=]*=/, "", value)
        gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
        gsub(/^"|"$/, "", value)
        gsub(/^'\''|'\''$/, "", value)
        print value
        exit
      }
    }
  ' "$env_file"
}

is_placeholder() {
  local value
  value="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
  [[ -z "$value" || "$value" == *change-me* || "$value" == *placeholder* || "$value" == *replace-with* || "$value" == *example.com* ]]
}

require_value() {
  local key="$1"
  local value
  value="$(env_value "$key")"
  if [[ -z "$value" ]]; then
    errors+=("$key is missing or empty")
    return
  fi
  if is_placeholder "$value"; then
    errors+=("$key still looks like a placeholder")
  fi
}

require_equal() {
  local key="$1"
  local expected="$2"
  local value
  value="$(env_value "$key")"
  if [[ "$value" != "$expected" ]]; then
    errors+=("$key must be $expected")
  fi
}

mode="$(file_mode "$env_file")"
case "$mode" in
  400|600) ;;
  *) warnings+=("$env_file mode is $mode; recommended mode is 600") ;;
esac

require_value TEPLO_DOMAIN
require_value POSTGRES_DB
require_value POSTGRES_USER
require_value POSTGRES_PASSWORD
require_value TEPLO_ADMIN_EMAIL
require_value TEPLO_ADMIN_PASSWORD
require_value JWT_SECRET_KEY

require_equal ENVIRONMENT production
require_equal AUTH_COOKIE_SECURE true
require_equal TEPLO_BANK_CLIENT_MODE live

jwt_secret="$(env_value JWT_SECRET_KEY)"
if [[ ${#jwt_secret} -lt 64 ]]; then
  errors+=("JWT_SECRET_KEY should be at least 64 characters")
fi

if [[ ! -d secrets ]]; then
  warnings+=("deploy/secrets does not exist; create it before adding file-based bank secrets")
fi

if ((${#warnings[@]} > 0)); then
  printf 'WARN: %s\n' "${warnings[@]}"
fi

if ((${#errors[@]} > 0)); then
  printf 'ERROR: %s\n' "${errors[@]}" >&2
  exit 1
fi

echo "Production secret check passed for $env_file."
