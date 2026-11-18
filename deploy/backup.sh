#!/usr/bin/env sh
set -eu

root=${1:-/opt/tailormail}
backup_dir=${2:-/var/backups/tailormail}
timestamp=$(date -u +%Y%m%dT%H%M%SZ)

mkdir -p "$backup_dir"
cd "$root"
docker compose exec -T db pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" | gzip > "$backup_dir/database-$timestamp.sql.gz"
docker run --rm -v "$(docker volume inspect -f '{{ .Mountpoint }}' ${COMPOSE_PROJECT_NAME:-cvme-outreach}_artifacts):/artifacts:ro" -v "$backup_dir:/backup" alpine tar -czf "/backup/artifacts-$timestamp.tar.gz" -C /artifacts .
find "$backup_dir" -type f -mtime +14 -delete
