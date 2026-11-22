# VPS deployment handoff

1. Copy this repository to `/opt/cvme` and create `/opt/cvme/.env` from `.env.example`.
2. Set `ENVIRONMENT=production`, a real hostname in `ALLOWED_HOSTS`, and strong unique values for `POSTGRES_PASSWORD`, `APP_SECRET`, `ADMIN_USERNAME`, and `ADMIN_PASSWORD`. `APP_SECRET` must be at least 32 characters.
3. Set `DATABASE_URL` exactly for the Compose PostgreSQL service, then set the AI, Companies House, Resend, candidate, and outreach values. Set `OUTREACH_AUTO_SEND=false` for the first manual check.
4. Set an unused `APP_PORT` (the default is `18080`), start with `docker compose up -d --build`, and verify `curl -f http://127.0.0.1:$APP_PORT/healthz` returns JSON status `ok`.
5. Put the real hostname into `Caddyfile.example`, install it as the Caddy configuration, and set `ALLOWED_HOSTS` to that hostname.
6. The Compose `scheduler` iterates active workspaces in their own timezones and enqueues work to the Redis-backed `worker`. The legacy systemd timer is retained only for migration/rollback compatibility; do not run both schedulers against the same workspace.

Run `docker compose run --rm --no-deps app python -m app.daily_outreach` once manually before enabling the timer. It performs the same preflight as the timer and will exit nonzero until every production value is ready.

After `docker compose up -d --build`, run `sh ./deploy/verify-vps.sh`. It validates the Compose configuration, running services, health endpoint, and daily-worker preflight. Do not enable the timer until it exits successfully.

Run `POSTGRES_USER=tailormail POSTGRES_DB=tailormail sh ./scripts/backup_postgres.sh` from a privileged VPS shell to back up PostgreSQL, and separately back up the named `artifacts` volume. The script retains 14 days by default; put `/var/backups/tailormail` on persistent storage.
