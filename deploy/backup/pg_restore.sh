#!/usr/bin/env bash
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/opt/teplo/backups}"
POSTGRES_CONTAINER="${POSTGRES_CONTAINER:-teplo-postgres}"
POSTGRES_USER="${POSTGRES_USER:-teplo}"
POSTGRES_DB="${POSTGRES_DB:-teplo}"

usage() {
  printf 'Usage: %s <path-to-dump>\n' "$0" >&2
}

if [[ $# -ne 1 ]]; then
  usage
  exit 2
fi

dump_file="$1"

umask 077
mkdir -p "${BACKUP_DIR}"
LOG_FILE="${BACKUP_DIR}/restore.log"

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S%z')" "$*" | tee -a "${LOG_FILE}" >&2
}

if [[ ! -f "${dump_file}" ]]; then
  log "ERROR: dump file does not exist: ${dump_file}"
  exit 1
fi

if [[ ! -s "${dump_file}" ]]; then
  log "ERROR: dump file is empty: ${dump_file}"
  exit 1
fi

confirm_restore() {
  if [[ "${CONFIRM:-}" == "yes" ]]; then
    return 0
  fi

  prompt="This will overwrite data in database '${POSTGRES_DB}'. Type 'yes' to continue: "
  if [[ -r /dev/tty ]]; then
    printf '%s' "${prompt}" > /dev/tty
    IFS= read -r answer < /dev/tty
  else
    log "ERROR: destructive restore requires CONFIRM=yes when no interactive terminal is available"
    exit 1
  fi

  if [[ "${answer}" != "yes" ]]; then
    log "Restore cancelled by user"
    exit 1
  fi
}

confirm_restore

log "Starting restore from ${dump_file} into ${POSTGRES_DB}"

if docker exec -i "${POSTGRES_CONTAINER}" pg_restore -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" --clean --if-exists < "${dump_file}"; then
  log "Restore finished successfully"
else
  status=$?
  log "ERROR: pg_restore failed with exit code ${status}"
  exit "${status}"
fi
