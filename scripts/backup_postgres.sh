#!/bin/sh
set -eu
: "${POSTGRES_DB:?POSTGRES_DB is required}"
: "${POSTGRES_USER:?POSTGRES_USER is required}"
: "${BACKUP_DIR:=./backups}"
mkdir -p "$BACKUP_DIR"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
docker compose exec -T db pg_dump -Fc -U "$POSTGRES_USER" "$POSTGRES_DB" > "$BACKUP_DIR/tailormail-$stamp.dump"
find "$BACKUP_DIR" -type f -name 'tailormail-*.dump' -mtime +14 -delete
