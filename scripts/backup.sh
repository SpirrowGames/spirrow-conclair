#!/bin/bash
# Daily snapshot of the conclair database.
#
# Writes a timestamped pg_dump (custom format, gzip-compressed) to the
# backup dir. Once the NAS is mounted, an external rsync should replicate
# the directory. Until then, snapshots accumulate locally and 30+-day
# files are pruned to bound disk usage.
#
# Usage:
#   ./scripts/backup.sh                       # default backup dir
#   BACKUP_DIR=/path/to/dest ./scripts/backup.sh
#
# Env (optional overrides):
#   POSTGRES_SUPER_USER       (default: postgres)
#   POSTGRES_SUPER_PASSWORD   (read from /home/sgadmin/services/infra/.env)
#   PG_HOST                   (default: 127.0.0.1)
#   PG_PORT                   (default: 5432)
#   DB                        (default: conclair)
#   BACKUP_DIR                (default: /home/sgadmin/services/spirrow/spirrow-conclair/backups)
#   RETENTION_DAYS            (default: 30)
set -euo pipefail

# Source super-user password from infra .env (mode 600).
INFRA_ENV="/home/sgadmin/services/infra/.env"
if [[ -f "$INFRA_ENV" ]]; then
    # shellcheck disable=SC1090
    source "$INFRA_ENV"
fi

PG_USER="${POSTGRES_SUPER_USER:-postgres}"
PG_PASS="${POSTGRES_SUPER_PASSWORD:-}"
PG_HOST="${PG_HOST:-127.0.0.1}"
PG_PORT="${PG_PORT:-5432}"
DB="${DB:-conclair}"
BACKUP_DIR="${BACKUP_DIR:-/home/sgadmin/services/spirrow/spirrow-conclair/backups}"
RETENTION_DAYS="${RETENTION_DAYS:-30}"

if [[ -z "$PG_PASS" ]]; then
    echo "ERROR: POSTGRES_SUPER_PASSWORD not set (looked in $INFRA_ENV)" >&2
    exit 1
fi

mkdir -p "$BACKUP_DIR"
TS=$(date -u +%Y%m%dT%H%M%SZ)
OUT="$BACKUP_DIR/conclair-${TS}.dump.gz"

# pg_dump runs from the postgres container so the binary version matches
# the server. -Fc = custom format (compressed, restorable with pg_restore).
docker exec -e PGPASSWORD="$PG_PASS" infra-postgres \
    pg_dump -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$DB" -Fc \
    | gzip > "$OUT"

# Permissions: owner-only, no group/world read (snapshots may contain
# meaningful conversation content).
chmod 600 "$OUT"

# Retention pruning.
find "$BACKUP_DIR" -maxdepth 1 -name 'conclair-*.dump.gz' -mtime +"$RETENTION_DAYS" -delete

echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) backup ok: $OUT ($(stat -c%s "$OUT") bytes)"
