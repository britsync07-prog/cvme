# Launch checklist

- Set unique `APP_SECRET`, `ENCRYPTION_KEY`, PostgreSQL credentials, `ALLOWED_HOSTS`, and `BASE_URL`.
- Configure Companies House API access, the OpenAI-compatible AI base URL/key/model, and RenderCV dependencies.
- Verify the system-email domain in Resend and publish SPF, DKIM, and DMARC records.
- Create Stripe products/prices, store monthly/yearly price IDs in `plans`, configure webhook signing, and test trial/payment-failure events.
- Create Google OAuth web credentials, add `/auth/google/callback` and `/settings/gmail/callback`, request Gmail sensitive-scope verification, and test mode users.
- Configure optional Cloudflare Turnstile keys, backups, monitoring/Sentry, and a restore drill.
- Add one candidate with consent, connect a tenant Gmail/SMTP account, configure workspace timezone/schedule/query, run a dry-run campaign, then enable sending.
- Review UK PECR/GDPR lawful basis, suppression handling, unsubscribe behavior, corporate-address policy, and the Acceptable Use Policy before sending.
