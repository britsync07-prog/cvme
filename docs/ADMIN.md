# Administrator guide

The admin surface is available at `/admin` for `admin` and `superadmin` users. JSON drill-down endpoints include `/admin/users/{id}`, `/admin/campaigns`, `/admin/metrics`, `/admin/email-log`, and `/admin/support`.

1. Configure `.env` and run `docker compose up -d`; the app, worker, scheduler, Redis, and PostgreSQL are health-checked independently.
2. Open `/admin` with the configured owner/admin account. Use `/admin/users` for filtered JSON, the user controls for activation/roles, and `/admin/audit.csv` for audit export.
3. Configure Stripe price IDs in the database `plans` rows before enabling Checkout. Set `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, and point Stripe to `/billing/webhook`.
4. Configure Google OAuth with the Gmail callback and sign-in callback URLs. Gmail send scope requires Google's consent-screen verification for production.
5. Keep `ENCRYPTION_KEY`, `APP_SECRET`, database passwords, and provider keys outside Git. Rotate them through a planned migration; existing encrypted tenant credentials require the old key during rotation.

For incidents, pause a workspace by setting `workspaces.paused=true` and inspect worker logs and `AuditLog`. Never replay a Stripe event without its original signed payload.
