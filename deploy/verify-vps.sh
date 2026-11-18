#!/usr/bin/env sh
set -eu

root=${1:-/opt/tailormail}
cd "$root"
app_port=${APP_PORT:-18080}

docker compose config -q
docker compose ps --status running
curl --fail --silent --show-error "http://127.0.0.1:${app_port}/healthz"
docker compose exec -T app python -c "from app.daily_outreach import production_preflight_errors; errors = production_preflight_errors(); print('daily preflight: ok' if not errors else 'daily preflight:\n' + '\n'.join(errors)); raise SystemExit(bool(errors))"
