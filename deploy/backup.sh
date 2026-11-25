#!/usr/bin/env sh
set -eu
umask 077

root=${1:-/opt/tailormail}
backup_dir=${2:-/var/backups/tailormail}
timestamp=$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$backup_dir"
temporary_dir=$(mktemp -d "$backup_dir/.backup-$timestamp.XXXXXX")

cleanup() {
    rm -rf "$temporary_dir"
}
trap cleanup EXIT INT TERM

cd "$root"
database_backup="$temporary_dir/database-$timestamp.dump"
artifact_backup="$temporary_dir/artifacts-$timestamp.tar.gz"

docker compose exec -T db sh -c 'pg_dump -Fc -U "$POSTGRES_USER" "$POSTGRES_DB"' > "$database_backup"
docker compose exec -T db pg_restore -l < "$database_backup" > /dev/null
docker run --rm -v "$(docker volume inspect -f '{{ .Mountpoint }}' ${COMPOSE_PROJECT_NAME:-cvme-outreach}_artifacts):/artifacts:ro" -v "$temporary_dir:/backup" alpine tar -czf "/backup/$(basename "$artifact_backup")" -C /artifacts .
sha256sum "$database_backup" "$artifact_backup" > "$temporary_dir/SHA256SUMS"
mv "$database_backup" "$artifact_backup" "$temporary_dir/SHA256SUMS" "$backup_dir/"
find "$backup_dir" -type f -mtime +14 -delete
