#!/bin/bash
# Restore a conclair backup created by scripts/backup.sh.
#
# Usage:
#   ./scripts/restore.sh /path/to/conclair-YYYYMMDDTHHMMSSZ.dump.gz
#
# WARNING: This drops and recreates the `conclair` database. The
# spirrow-conclair systemd service should be stopped first to avoid
# active connection contention.
#
# After running, restart the service:
#   sudo systemctl start spirrow-conclair.service
set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "Usage: $0 <path-to-dump.gz>" >&2
    exit 2
fi

DUMP="$1"
if [[ ! -f "$DUMP" ]]; then
    echo "ERROR: $DUMP not found" >&2
    exit 1
fi

INFRA_ENV="/home/sgadmin/services/infra/.env"
# shellcheck disable=SC1090
source "$INFRA_ENV"

PG_USER="${POSTGRES_SUPER_USER:-postgres}"
PG_PASS="$POSTGRES_SUPER_PASSWORD"
PG_HOST="${PG_HOST:-127.0.0.1}"
PG_PORT="${PG_PORT:-5432}"
DB="${DB:-conclair}"

# Confirm with user before destroying the live DB.
echo "About to restore $DUMP onto $PG_HOST:$PG_PORT/$DB."
read -r -p "Type 'yes' to continue: " confirm
if [[ "$confirm" != "yes" ]]; then
    echo "Aborted."
    exit 0
fi

# Stop the conclair service if it's running so connections are released.
if systemctl is-active --quiet spirrow-conclair.service; then
    echo "Stopping spirrow-conclair.service…"
    sudo systemctl stop spirrow-conclair.service
    STARTED_BY_US=1
else
    STARTED_BY_US=0
fi

echo "Dropping and recreating database '$DB'…"
docker exec -e PGPASSWORD="$PG_PASS" infra-postgres \
    psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d postgres \
    -v ON_ERROR_STOP=1 \
    -c "DROP DATABASE IF EXISTS $DB WITH (FORCE);" \
    -c "CREATE DATABASE $DB OWNER conclair_app;"

echo "Restoring dump…"
gunzip -c "$DUMP" | docker exec -i -e PGPASSWORD="$PG_PASS" infra-postgres \
    pg_restore -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$DB" --no-owner --role=conclair_app

# Re-grant ownership / public schema permissions to conclair_app.
docker exec -e PGPASSWORD="$PG_PASS" infra-postgres \
    psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$DB" \
    -c "GRANT ALL ON SCHEMA public TO conclair_app;"

if [[ "$STARTED_BY_US" -eq 1 ]]; then
    echo "Restarting spirrow-conclair.service…"
    sudo systemctl start spirrow-conclair.service
fi

echo "Restore complete."
