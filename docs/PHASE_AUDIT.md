# TailorJobhunt phase audit

| Phase | Current evidence |
| --- | --- |
| 1 - tenancy/database | Workspace memberships, tenant foreign keys, central scoping helpers, Alembic revisions 0001-0012, persisted workspace automation policy, export, soft-delete, and GDPR deletion. |
| 2 - auth/security | Argon2, verification/reset, Google sign-in, secure signed sessions, production CSRF, Redis rate limits, Turnstile hook, TOTP, roles, encrypted secrets, and security headers. |
| 3 - public website | Landing, pricing from database plans, monthly/yearly presentation, legal/cookie pages, sitemap, robots, and shared responsive CSS. |
| 4 - billing/trial | Trial, entitlements, Stripe Checkout/Portal/webhook verification and idempotency, invoices/payments/refunds, coupons, dunning jobs, and admin controls. |
| 5 - sender | Gmail OAuth refresh/storage, encrypted SMTP, per-workspace sender enforcement in production, warm-up/delay controls, opt-out footer, and bounce/complaint suppression. |
| 6 - AI | Workspace provider config, encrypted key, OpenAI-compatible endpoint, 300-second timeout, five retries, strict JSON/factual/unchanged-CV checks, and usage records. |
| 7 - dashboard/automation | Onboarding, tenant schedule/timezone/auto-send policy, Redis worker, durable progress/cancel/retry jobs for outreach and campaign preparation, notifications, suppression, and blocklists. |
| 8 - admin | Metrics, user detail/search, role/status/verify/logout/reset/note/impersonation, plan/trial/pause controls, campaign monitor, audit CSV, support, suppression, kill switch, and system settings. |
| 9 - compliance | Acceptable-use agreement, corporate-recipient policy, public-source discovery, suppression precedence, unsubscribe, opt-out footer, caps, warm-up, and lawful-use disclaimer. |
| 10 - operations/quality | Compose app/worker/scheduler/Postgres/Redis/Nginx with dynamic upstream resolution, health checks, migration ownership, structured logs/Sentry hook, HTML transactional mail, backups, environment template, and passing tests. |

## External launch gates

Stripe live prices/webhook, Google OAuth consent and Gmail-scope verification, Resend domain/DNS/webhook secret, job-provider credentials where enabled, sender credentials, Sentry, backup storage/restore drill, and the operator's PECR/GDPR lawful-basis review remain deployment configuration steps.
