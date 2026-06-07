#!/usr/bin/env bash
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/opt/teplo/backups}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
POSTGRES_CONTAINER="${POSTGRES_CONTAINER:-teplo-postgres}"
POSTGRES_USER="${POSTGRES_USER:-teplo}"
POSTGRES_DB="${POSTGRES_DB:-teplo}"

umask 077
mkdir -p "${BACKUP_DIR}"

LOG_FILE="${BACKUP_DIR}/backup.log"
LOCK_FILE="${BACKUP_DIR}/.pg_backup.lock"

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S%z')" "$*" | tee -a "${LOG_FILE}" >&2
}

if ! [[ "${RETENTION_DAYS}" =~ ^[0-9]+$ ]]; then
  log "ERROR: RETENTION_DAYS must be a non-negative integer, got '${RETENTION_DAYS}'"
  exit 1
fi

exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
  log "ERROR: another backup run is already in progress"
  exit 1
fi

timestamp="$(date '+%Y%m%d_%H%M%S')"
dump_file="${BACKUP_DIR}/teplo_${timestamp}.dump"
tmp_file="${dump_file}.tmp"

cleanup_tmp() {
  rm -f "${tmp_file}"
}
trap cleanup_tmp EXIT

log "Starting pg_dump to ${dump_file}"

if docker exec "${POSTGRES_CONTAINER}" pg_dump -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" -Fc > "${tmp_file}"; then
  if [[ ! -s "${tmp_file}" ]]; then
    log "ERROR: pg_dump exited with code 0, but produced an empty dump: ${tmp_file}"
    exit 1
  fi
else
  status=$?
  log "ERROR: pg_dump failed with exit code ${status}; incomplete dump: ${tmp_file}"
  exit "${status}"
fi

mv "${tmp_file}" "${dump_file}"
size_bytes="$(wc -c < "${dump_file}" | tr -d '[:space:]')"
log "Backup created: ${dump_file} (${size_bytes} bytes)"

log "Rotating dumps older than ${RETENTION_DAYS} days"
while IFS= read -r old_dump; do
  log "Deleting old dump: ${old_dump}"
  rm -f "${old_dump}"
done < <(find "${BACKUP_DIR}" -maxdepth 1 -type f -name 'teplo_*.dump' -mtime +"${RETENTION_DAYS}" -print)

# Optional offsite copy. Disabled by default.
# To enable it, install and configure rclone, then set:
#   RCLONE_REMOTE="remote-name:path/to/teplo/backups"
if [[ -n "${RCLONE_REMOTE:-}" ]]; then
  if ! command -v rclone >/dev/null 2>&1; then
    log "ERROR: RCLONE_REMOTE is set, but rclone is not installed"
    exit 1
  fi

  log "Uploading ${dump_file} to ${RCLONE_REMOTE}"
  if rclone copy "${dump_file}" "${RCLONE_REMOTE}" --checksum; then
    log "Offsite upload finished"
  else
    status=$?
    log "ERROR: rclone upload failed with exit code ${status}"
    exit "${status}"
  fi
fi

log "Backup finished successfully"
