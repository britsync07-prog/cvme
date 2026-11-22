# TailorMail phases 1–10 audit

This checklist describes the code deployed from `main`.

| Phase | Implemented evidence |
| --- | --- |
| 1 · tenancy/database | Workspace memberships, tenant foreign keys, central workspace scoping, Alembic revisions `0001`–`0009`, export and GDPR deletion. |
| 2 · auth/security | Argon2, verification/reset, Google sign-in, Secure/HttpOnly/SameSite sessions, production CSRF, Redis rate limits, Turnstile hook, TOTP, roles, encrypted secrets, security headers. |
| 3 · public website | Landing, features/use cases, FAQ, trust section, database pricing, monthly/yearly presentation, legal pages, sitemap, robots, shared responsive CSS. |
| 4 · billing/trial | Trial, entitlements, Stripe Checkout/Portal/webhook verification and idempotency, invoices/payments/refunds, dunning jobs, banners, admin controls. |
| 5 · sender | Gmail OAuth refresh/storage, encrypted SMTP, reauth pause, warm-up/delay controls, opt-out footer, bounce/complaint suppression webhook. |
| 6 · AI | Workspace provider config, encrypted key, OpenAI-compatible endpoint, 300-second timeout, five retries, strict schema/factual/unchanged-CV checks, usage records. |
| 7 · dashboard/automation | Onboarding, tenant schedule/timezone, Redis worker, durable progress/cancel/retry jobs, notifications, suppression/blocklist. |
| 8 · admin | Metrics, user detail/search, role/status/verify/logout/reset/note/impersonation, plans/trials/pauses, campaign monitor, audit CSV, support status, suppression, kill switch, system settings. |
| 9 · compliance | Acceptable-use agreement, corporate-recipient policy, public-email requirement, suppression precedence, unsubscribe, opt-out footer, caps, warm-up, lawful-use disclaimer. |
| 10 · operations/quality | Compose app/worker/scheduler/Postgres/Redis/Nginx, health checks, Alembic startup, structured logs/Sentry hook, backups, environment template, docs, 39 passing tests. |

## External launch gates

Provider credentials and verification remain manual: Stripe prices/webhook, Google OAuth consent and Gmail-scope verification, Resend domain/DNS/webhook secret, Companies House key, sender credentials, Sentry, backups, and PECR/GDPR lawful-basis policy.
