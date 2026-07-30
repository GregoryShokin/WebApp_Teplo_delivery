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
integrations_env_file="$script_dir/.env.integrations"
integrations_status=()

if [[ ! -f "$env_file" ]]; then
  echo "Missing $env_file. Run ./init-prod-env.sh first or copy env.prod.example." >&2
  exit 1
fi

file_mode() {
  stat -c "%a" "$1" 2>/dev/null || stat -f "%Lp" "$1"
}

env_value_from() {
  local file="$1"
  local key="$2"
  [[ -f "$file" ]] || return 0
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
  ' "$file"
}

env_value() {
  local key="$1"
  env_value_from "$env_file" "$key"
}

integrations_env_value() {
  local key="$1"
  env_value_from "$integrations_env_file" "$key"
}

is_placeholder() {
  local value
  value="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
  [[ -z "$value" || "$value" == *change-me* || "$value" == *placeholder* || "$value" == *replace-with* || "$value" == *example.com* ]]
}

any_env_value() {
  # Значение из любого из двух env-файлов: контейнеры грузят оба
  # (docker-compose.prod.yml, env_file: .env.prod + .env.integrations), и привязывать
  # проверку к конкретному файлу — значит отказать исправному стенду, где ключ лежит в соседнем.
  local key="$1"
  local value
  value="$(env_value "$key")"
  [[ -n "$value" ]] || value="$(integrations_env_value "$key")"
  printf '%s' "$value"
}

is_off() {
  # Выключатель контура: пусто = дефолт кода (включено). Гасят его явным false/0/no.
  local value
  value="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
  [[ "$value" == "false" || "$value" == "0" || "$value" == "no" ]]
}

require_value_either() {
  local key="$1"
  local value
  value="$(any_env_value "$key")"
  if [[ -z "$value" ]]; then
    errors+=("$key is missing or empty in .env.prod and .env.integrations")
    return
  fi
  if is_placeholder "$value"; then
    errors+=("$key still looks like a placeholder")
  fi
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

require_integrations_value() {
  local key="$1"
  local value
  value="$(integrations_env_value "$key")"
  if [[ -z "$value" ]]; then
    integrations_status+=("$key=missing")
    errors+=("$key is missing or empty in .env.integrations")
    return
  fi
  integrations_status+=("$key=set")
  if is_placeholder "$value"; then
    errors+=("$key in .env.integrations still looks like a placeholder")
  fi
}

optional_integrations_value() {
  local key="$1"
  local value
  value="$(integrations_env_value "$key")"
  if [[ -z "$value" ]]; then
    integrations_status+=("$key=missing")
    return
  fi
  integrations_status+=("$key=set")
  if is_placeholder "$value"; then
    warnings+=("$key in .env.integrations still looks like a placeholder")
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

require_integrations_number() {
  local key="$1"
  local value
  value="$(integrations_env_value "$key")"
  if [[ -z "$value" ]]; then
    integrations_status+=("$key=missing")
    errors+=("$key is missing or empty in .env.integrations")
    return
  fi
  integrations_status+=("$key=set")
  if ! [[ "$value" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
    errors+=("$key in .env.integrations must be numeric seconds")
  fi
}

optional_integrations_number() {
  local key="$1"
  local value
  value="$(integrations_env_value "$key")"
  if [[ -z "$value" ]]; then
    integrations_status+=("$key=missing")
    return
  fi
  integrations_status+=("$key=set")
  if ! [[ "$value" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
    errors+=("$key in .env.integrations must be numeric seconds")
  fi
}

mode="$(file_mode "$env_file")"
case "$mode" in
  400|600) ;;
  *) warnings+=("$env_file mode is $mode; recommended mode is 600") ;;
esac

if [[ ! -f "$integrations_env_file" ]]; then
  errors+=(".env.integrations is missing; copy env.integrations.example to .env.integrations")
else
  integrations_mode="$(file_mode "$integrations_env_file")"
  case "$integrations_mode" in
    400|600) ;;
    *) warnings+=(".env.integrations mode is $integrations_mode; recommended mode is 600") ;;
  esac
fi

require_value TEPLO_DOMAIN
require_value POSTGRES_DB
require_value POSTGRES_USER
require_value POSTGRES_PASSWORD
require_value TEPLO_ADMIN_EMAIL
require_value TEPLO_ADMIN_PASSWORD
require_value JWT_SECRET_KEY
# Без него Settings падает валидацией на импорте — api и scheduler не стартуют вовсе
# (app/core/config.py: "TBANK_WEBHOOK_TOKEN must be set in production").
require_value_either TBANK_WEBHOOK_TOKEN

require_equal ENVIRONMENT production
require_equal AUTH_COOKIE_SECURE true
require_equal TEPLO_BANK_CLIENT_MODE live

if [[ -f "$integrations_env_file" ]]; then
  require_integrations_value IIKO_SERVER_BASE_URL
  require_integrations_value IIKO_SERVER_LOGIN
  require_integrations_value IIKO_SERVER_PASSWORD
  optional_integrations_number IIKO_SERVER_TIMEOUT_SECONDS

  # Cloud app — every invoice push/update/payment goes through it; without these the
  # invoice card shows "auth: 'IIKO_CLOUD_APP_ID'".
  require_integrations_value IIKO_CLOUD_APP_ID
  require_integrations_value IIKO_CLOUD_API_LOGIN
  require_integrations_value IIKO_CLOUD_CLIENT_SECRET
  optional_integrations_value IIKO_WEBHOOK_TOKEN

  require_integrations_value TBANK_API_BASE_URL
  require_integrations_value TBANK_PAYMENT_BASE_URL
  require_integrations_value TBANK_API_ACCESS_TOKEN
  optional_integrations_value TBANK_API_ACCOUNT_NUMBER
  optional_integrations_number TBANK_API_TIMEOUT_SECONDS

  if [[ -z "$(integrations_env_value TBANK_API_ACCOUNT_NUMBER)" ]]; then
    warnings+=("TBANK_API_ACCOUNT_NUMBER is missing; T-Bank payment draft creation needs a payer account")
  fi

  bank_sync_providers=",$(env_value BANK_SYNC_PROVIDERS),"
  if [[ "$bank_sync_providers" == *",sber,"* ]]; then
    require_integrations_value SBER_API_BASE_URL
    require_integrations_value SBER_API_TOKEN_URL
    require_integrations_value SBER_API_CLIENT_ID
    require_integrations_value SBER_API_CLIENT_SECRET
    require_integrations_value SBER_API_ACCOUNT_NUMBER
    require_integrations_value SBER_API_ACCESS_TOKEN
    require_integrations_value SBER_API_REFRESH_TOKEN
    require_integrations_value SBER_API_TLS_CERT_PATH
    require_integrations_value SBER_API_TLS_KEY_PATH
  fi
fi

# --- Контуры, которые старт не роняют, а выключаются молча ---------------------------------
# Ни одно значение ниже не мешает подняться api и scheduler, поэтому здесь только WARN: пустое
# поле — законный выбор (Sber, Mango и Telegram так и живут). Молчать о нём всё же нельзя:
# отличить «выключено осознанно» от «забыли заполнить» по работающему стенду невозможно —
# почтовые проходы не пишут в лог даже «не настроено».

anthropic_key="$(any_env_value ANTHROPIC_API_KEY)"
if [[ -z "$anthropic_key" ]]; then
  warnings+=("ANTHROPIC_API_KEY is missing; PDF invoices are recognized by the deterministic layer only, and «ИИ-разбор» in Налоги plus fixed-asset revaluation answer 422")
else
  if [[ -z "$(any_env_value ANTHROPIC_BASE_URL)" ]]; then
    warnings+=("ANTHROPIC_BASE_URL is missing while ANTHROPIC_API_KEY is set; from a Russian IP Anthropic answers 403 to a valid key, so every model call fails unless the container reaches an allowed region through HTTPS_PROXY")
  elif [[ -z "$(any_env_value ANTHROPIC_RELAY_SECRET)" ]]; then
    warnings+=("ANTHROPIC_RELAY_SECRET is missing while ANTHROPIC_BASE_URL is set; deploy/anthropic-relay-worker.js answers 403 to callers without the shared secret, and that failure looks like a bad API key")
  fi
fi

mailru_configured=""
if [[ -n "$(any_env_value MAILRU_EMAIL)" && -n "$(any_env_value MAILRU_APP_PASSWORD)" ]]; then
  mailru_configured=1
fi
if [[ -n "$(any_env_value MAILRU_WORKMAIL)" && -n "$(any_env_value MAILRU_WORKMAIL_PASSWORD)" ]]; then
  mailru_configured=1
fi
if [[ -z "$mailru_configured" ]]; then
  if ! is_off "$(any_env_value MAIL_POLL_ENABLED)"; then
    warnings+=("MAILRU_* are missing while MAIL_POLL_ENABLED is on; the invoice mail poll returns not_configured and the scheduler logs nothing — «Страница на оплату» stays empty in silence")
  fi
  if ! is_off "$(any_env_value TAX_DOCUMENT_POLL_ENABLED)"; then
    warnings+=("MAILRU_* are missing while TAX_DOCUMENT_POLL_ENABLED is on; the accountant's documents are never fetched into the tax staging, also without a log line")
  fi
fi

if [[ -z "$(any_env_value SBIS_APP_CLIENT_ID)" || -z "$(any_env_value SBIS_SECRET_KEY)" ]]; then
  if ! is_off "$(any_env_value SBIS_SYNC_ENABLED)"; then
    warnings+=("SBIS_APP_CLIENT_ID/SBIS_SECRET_KEY are missing while SBIS_SYNC_ENABLED is on; the hourly EDO sync exits immediately and «Синхронизировать» answers 409")
  fi
fi

# Пустой токен не отключает вебхук, а отключает ЕГО ПРОВЕРКУ: app/api/v1/routes/webhooks.py
# (_verify_bearer) пропускает запрос без Authorization, и POST /api/v1/webhooks/iiko открыт
# любому — а он открывает и закрывает смены курьеров, то есть двигает учёт смен и ЗП.
if [[ -z "$(any_env_value IIKO_WEBHOOK_TOKEN)" ]]; then
  warnings+=("IIKO_WEBHOOK_TOKEN is missing; POST /api/v1/webhooks/iiko accepts unauthenticated courier shift events from anyone")
fi

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

if ((${#integrations_status[@]} > 0)); then
  printf 'INTEGRATION: %s\n' "${integrations_status[@]}"
fi

if ((${#errors[@]} > 0)); then
  printf 'ERROR: %s\n' "${errors[@]}" >&2
  exit 1
fi

echo "Production secret check passed for $env_file."
