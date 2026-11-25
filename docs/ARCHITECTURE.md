# TailorJobhunt architecture

TailorJobhunt is a FastAPI web application backed by PostgreSQL. Every tenant-owned record carries `workspace_id`; `current_workspace()` and `require_owned()` are the central authorization boundary. SQLAlchemy models are migrated with Alembic.

Long work is queued in Redis (`tailormail:jobs`) with durable `outreach_jobs` rows recording status, progress, retries, errors, and cancellation. The worker runs research, AI tailoring, RenderCV, and delivery one company at a time. The scheduler evaluates each workspace's IANA timezone, schedule time, pause state, subscription status, and daily cap before enqueueing one job per day. Only the web container runs Alembic migrations; worker and scheduler startup deliberately never race schema upgrades. Nginx is available as the internal reverse proxy on `NGINX_PORT`; the deployed host currently terminates HTTPS upstream and forwards to the app.

AI provider keys, SMTP credentials, and Gmail OAuth tokens are encrypted with Fernet using `ENCRYPTION_KEY`. System mail uses the platform Resend sender; tenant outreach uses a connected Gmail or encrypted SMTP account. Campaigns and `AuditEvent` rows provide the exact email/PDF audit trail.

Public routes are landing, pricing, legal, signup, login, and recovery. Authenticated routes include dashboard, onboarding, leads, sent mail, billing, support, and settings. `/admin` and `/admin/*` require `admin` or `superadmin`.
