# ndcbackend

NDCConnect API — Django 5.2 LTS + Django REST Framework + PostgreSQL. Authentication is Clerk (`feature-specs/02-authentication.md`).

## Run

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt                    # production servers: requirements.txt only
cp .env.example .env                                   # fill in Clerk dev keys
python manage.py migrate
python manage.py runserver
```

Health check: `GET http://localhost:8000/api/v1/health/` · readiness: `GET /api/v1/ready/` · API docs (development): `/api/v1/schema/docs/`

## Clerk setup (development instance)

1. Clerk Dashboard → **API keys**: copy the secret key into `CLERK_SECRET_KEY` (optionally the JWT public key into `CLERK_JWT_KEY` for networkless verification).
2. Clerk Dashboard → **Webhooks** → add endpoint `https://<your-tunnel>/api/v1/webhooks/clerk/`, subscribe to `user.created`, `user.updated`, `user.deleted`, and copy the signing secret into `CLERK_WEBHOOK_SIGNING_SECRET`. Locally, expose port 8000 with a tunnel (e.g. ngrok).
3. Set `CLERK_AUTHORIZED_PARTIES` to the frontend origin(s).

## API

| Method | Path | Who |
|---|---|---|
| GET | `/api/v1/health/` (status, database, `api_version`, `commit`) | anyone |
| GET / PATCH | `/api/v1/me/` | signed in (`Authorization: Bearer <Clerk session token>`) |
| POST | `/api/v1/webhooks/clerk/` | Clerk (Svix-signed) |
| GET | `/api/v1/candidates/` (`?position=&state=&election_year=&q=&page=`) | anyone — published only |
| GET | `/api/v1/candidates/positions/` | anyone |
| GET | `/api/v1/candidates/<slug>/` | anyone — 404 unless published |
| GET / POST | `/api/v1/admin/candidates/` | system admin |
| GET / PATCH | `/api/v1/admin/candidates/<id>/` | system admin |
| POST | `/api/v1/admin/candidates/<id>/{verify,unverify,publish,unpublish,archive,restore}/` | system admin |
| PUT / DELETE | `/api/v1/admin/candidates/<id>/photo/` (multipart `photo`, `rights_confirmed=true`) | system admin |
| GET / POST | `/api/v1/admin/candidates/<id>/accounts/` · DELETE `…/accounts/<user_id>/` | system admin |
| PUT / PATCH / DELETE | `/api/v1/me/follows/<slug>/` — follow (201/200) · `{"notify": bool}` · unfollow (always 204) | signed in |
| GET | `/api/v1/me/follows/` (Following page, + `hidden_count`) · `?slugs=a,b` (≤ 50, follow state) | signed in |

| GET | `/api/v1/donations/config/` | anyone — `{open, min_kobo, …}` (compliance gate) |
| POST | `/api/v1/donations/` | anyone (token optional) → `{reference, checkout_url}` |
| GET | `/api/v1/donations/<reference>/` | anyone with the reference (receipt) |
| GET | `/api/v1/me/donations/` | signed in |
| GET | `/api/v1/finance/donations/` · `/api/v1/finance/summary/` | anyone — successful donations only |
| POST | `/api/v1/webhooks/payments/<paystack\|fake>/` | provider signature — one URL for donations (`NDC-`) and membership fees (`MEM-`) |
| GET | `/api/v1/pvc/config/` | anyone — INEC links + "Get your PVC" guidance |
| GET / PUT / DELETE | `/api/v1/me/pvc/` · PATCH `badge/` | signed in — VIN masked; never returned in full |
| GET | `/api/v1/membership/config/` | anyone — `{open, fee_kobo, requires_approval}` |
| GET / PUT | `/api/v1/me/membership/` · `photo/` · POST `pay/` · `cancel/` · GET `card/` | signed in (applying/paying: completed profile) |
| GET | `/api/v1/membership/payments/<reference>/` | anyone with the reference (no personal data) |
| GET / POST | `/api/v1/admin/pvc/…` (incl. `settings/`) | PVC officer, membership officer or system admin (settings PATCH: system admin) |
| GET / POST | `/api/v1/admin/membership/…` (incl. `register.csv`, `register-photos.zip`) | membership officer or system admin (settings: system admin) |
| GET / POST | `/api/v1/admin/donations/` · `<id>/` · `<id>/recheck/` · `export.csv` | system admin or finance officer |
| GET | `/api/v1/news/` (`?source=official\|community&state=&page=`) · `/api/v1/news/<id>/` | anyone — published articles only |
| POST | `/api/v1/news/<id>/report/` · `/api/v1/candidates/<slug>/report/` (profile) | anyone (5/hour) → `{reference}`, or `{route}` for copyright |
| GET / POST | `/api/v1/me/news/` · GET / PATCH `<id>/` · PUT / DELETE `<id>/image/` · POST `<id>/withdraw/` | signed in (writes: completed profile) |
| GET / POST | `/api/v1/admin/news/` · `<id>/` · `<id>/{approve,reject,remove,restore}/` (remove needs `rule`) · `reports/<id>/dismiss/` | news reviewer or system admin |
| GET / POST | `/api/v1/admin/announcements/` · `notify-count/` · `<id>/` · `<id>/image/` · `<id>/{pin,unpin,unpublish,restore}/` | system admin |
| GET | `/api/v1/candidates/<slug>/posts/` · `<id>/` · POST `<id>/report/` | anyone — visible candidate posts only |
| GET / POST | `/api/v1/campaign/candidates/` · `<candidate_id>/posts/` · GET / PATCH / DELETE `<post_id>/` · PUT / DELETE `<post_id>/image/` | accounts linked to that candidate (`candidate` role) |
| GET / POST | `/api/v1/admin/posts/` · `<id>/` · `<id>/{remove,restore}/` (remove needs `rule`) · `reports/<id>/dismiss/` · `/api/v1/admin/candidates/<id>/posts/` | system admin |
| GET | `/api/v1/rules/` · `/api/v1/reports/status/?reference=` | anyone (spec 13) |
| POST | `/api/v1/report/copyright/` | anyone — Copyright Act s. 54(2) fields → `{reference}` |
| GET / POST | `/api/v1/me/reports/` · `/api/v1/me/moderation/` · POST `/api/v1/me/appeals/` · `/api/v1/me/counter-notices/` | signed in |
| GET / POST | `/api/v1/admin/moderation/cases/` · `<id>/` · `<id>/{claim,release,keep,remove,restore,escalate,warn,pause,lift-pause,hold}/` · `<id>/reports/<report_id>/reporter/` (system admin, audited) | moderator, news reviewer, system admin (as the role table) |
| GET / POST | `/api/v1/admin/moderation/appeals/` · `<id>/decide/` | as above; never the original decider |
| GET / POST / PATCH | `/api/v1/admin/moderation/legal/` · `<id>/` · `<id>/{acknowledge,hold,owner-notified}/` · `<id>/attachment/` · `<id>/items/<item>/` · `/api/v1/admin/moderation/compliance/?year=` (`&format=csv`, audited) | system admin |
| GET | `/api/v1/me/notifications/` · `unread-count/` · POST `<id>/read/` · `read-all/` · GET / PATCH `preferences/` | signed in |
| GET | `/api/v1/search/?q=` (`type`, `state`, `position`, `year`, `source`, `since`, `page`) · `/api/v1/search/suggest/?q=` · `/api/v1/states/` · `/api/v1/states/<state>/` | anyone — anonymous, cacheable 60 s, public items only (spec 12) |
| GET | `/api/v1/admin/overview/` | any admin role — only that role's cards and numbers (spec 11) |
| GET / POST | `/api/v1/admin/team/` (`?revoked=1`) · POST `<assignment_id>/revoke/` | system admin |
| GET | `/api/v1/admin/users/?q=` (3+ chars, or `filter=ban_failed`) · `<id>/` · POST `<id>/{phone,deactivate,reactivate}/` | system admin (views and reveals audited) |
| GET | `/api/v1/admin/audit/` (`area, action, actor, target, from, to`) · `actions/` | system admin (searches audited) |
| GET | `/api/v1/ready/` | anyone — names and ok/failed only (spec 15) |
| GET | `/api/v1/schema/` | system admin (production), anyone (development) |
| POST | `/api/v1/media/upload-signature/` (`{purpose, target_id}`) | signed in, for a target you may change |
| GET / POST | `/api/v1/admin/integrations/` · `webhooks/` (`?provider=&status=`) · `outbox/` (`?status=`) · POST `outbox/<id>/retry/` (reverified) | system admin |

Single-candidate admin responses include `followers: {total, notify_on}` — counts only; no endpoint lists who follows a candidate.
Follows are political-opinion data (NDPA 2023): private to the user, never audited, deleted when the account is deleted.

Candidate photos are saved under `media/` (git-ignored) in development and served by Django only when `DEBUG` is on.

Errors always look like `{"error": {"status", "code", "message", "fields", "request_id"}}`; the full code list is in `docs/api.md`.
Codes to handle: `not_authenticated` (401), `account_deactivated` (403), `auth_unavailable` (503), `validation_error` (400), `throttled` (429), `invalid_transition` (409), `invalid_photo` (400), `storage_unavailable` (503), `too_many_pending` (409), `self_review` (403), `publishing_paused` (409, with `until`), `too_many_pinned` (409), `source_required` (400), `candidate_not_published` (409), `posting_paused` (409), `self_moderation` (403), `self_change` (403), `last_system_admin` (409), `user_not_found` (404), `role_exists` (409), `user_inactive` (409), `query_too_short` (400), `invalid_role` (400), `query_too_long` (400), `invalid_filter` (400), `search_unavailable` (503), `removed` (410, with `rule`, `rule_title`, `kind`, `source`), `rule_required` (400), `legal_basis_required` (400), `not_your_case` (403), `same_decider` (403), `submissions_paused` (403, with `until`), `appeal_window_closed` (409), `appeal_exists` (409).

## Roles

System admins grant and revoke roles at `/admin/team` (spec 11). The commands below stay for bootstrapping the first admin and emergencies.

```bash
python manage.py grant_role ada@example.com system_admin
python manage.py grant_role --clerk-id user_123 candidate --scope-type candidate --scope-id <candidate-uuid>
python manage.py revoke_role ada@example.com moderator --reason "rotation"
python manage.py deactivate_user spam@example.com --reason "spam"   # also bans in Clerk; re-run to retry a failed ban
python manage.py reactivate_user spam@example.com
```

The user must have signed in or signed up at least once (so the local record exists). Every command is audited as `system:cli`.
There is always at least one system admin: revoking or deactivating the last one fails, in the browser and on the CLI (grant someone else first).
Nobody can change their own roles or deactivate themselves from the browser.
There is no password superuser and no Django admin site: platform users never have passwords.

## Donations (spec 05)

Donations are **closed by default** and stay closed until every gate condition passes:
`DONATIONS_ENABLED`, a configured provider, limits, and `DONATIONS_COMPLIANCE_APPROVED` (set only after legal sign-off).
Check with `python manage.py check_donations` (never prints keys).

- **Local development needs no keys:** set `PAYMENT_PROVIDER=fake`, `DONATIONS_ENABLED=True` and `DONATIONS_COMPLIANCE_APPROVED=True` in `.env`.
  - "Donate" takes you to a dev checkout page with Pay / Fail / Pay (webhook lost) / Leave, which sends a correctly signed webhook.
  - The fake provider refuses to run without `DEBUG`, and production settings refuse it outright.
- **Paystack:** put a **test** key in `.env` as `PAYSTACK_SECRET_KEY=sk_test_…`. Never paste keys in chat or commit them.
  - In the Paystack dashboard, set the webhook URL to `https://<your-api>/api/v1/webhooks/payments/paystack/`.
  - Live keys belong only on the production server.
- **Run every 15 minutes once deployed:** `python manage.py sweep_payments` (`sweep_donations` still works as an alias). It re-verifies pending donations and membership payments, abandons 24-hour-old ones, and clears 90-day-old IP hashes.
- **Finance staff:** `python manage.py grant_role finance@example.com finance_officer`.

## Membership and PVC (spec 06)

Membership **payments** are closed until `MEMBERSHIP_ENABLED=True` and `MEMBERSHIP_DATA_APPROVED=True` (after party/legal sign-off). PVC verification and saving details work regardless.

- **VIN and NIN are encrypted** with `PII_ENCRYPTION_KEYS`.
  - Production refuses to start without it. Generate a key with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
  - To rotate, put the new key first and keep the old ones after it.
  - Development creates `.dev_pii_key` automatically (git-ignored).
- **Member photos** live in `PRIVATE_MEDIA_ROOT` (default `private_media/`, git-ignored). They are never served by `/media/`, only through authenticated views.
- **Officers:** `python manage.py grant_role officer@example.com membership_officer`.
- **Daily:** `python manage.py prune_pvc` deletes the VINs of submissions rejected more than 90 days ago.
- **Local try-out:** add `MEMBERSHIP_ENABLED=True`, `MEMBERSHIP_DATA_APPROVED=True` and `PAYMENT_PROVIDER=fake` to `.env`.

## PVC verification (spec 08)

PVC is its own unit: membership only asks `pvc.services.is_verified(user)`.

- **Checks are manual:** an officer checks the voter on INEC's checker (`cvr.inecnigeria.org/vvs`) and records the result. Nothing is sent to INEC.
- **Officers:** `python manage.py grant_role officer@example.com pvc_officer` (membership officers can use the queue too). Nobody can decide their own check.
- Users get an in-app notification when their check is verified or rejected, and can opt in to a public "Verified voter" badge.
- System admins edit the "Get your PVC" guidance at `/admin/pvc/settings`. "Register, transfer or replace" stays hidden until `INEC_PVC_URL` is set.

## Candidate posts (spec 09)

- Accounts linked to a candidate (admin → candidate → linked accounts, or `grant_role <email> candidate --scope-type candidate --scope-id <candidate-uuid>`) publish straight away at `/campaign`. There is no Pending.
- Followers with the bell on get an in-app notification after the post commits. If delivery failed, re-run `python manage.py deliver_post <post-id>` (safe to repeat). Edits and restores never notify again.
- Posting pauses for the candidate's office inside `CAMPAIGN_BLACKOUTS`; deleting still works.
- Moderators and system admins moderate reported posts at `/admin/moderation`; `/admin/posts` stays for system admins (nobody linked to the candidate can moderate its posts).
- **Daily:** `python manage.py prune_posts` (deleted/removed posts after 1 year, except legal holds).

## News and announcements (spec 07)

- **News reviewers:** `python manage.py grant_role reviewer@example.com news_reviewer`. Reviewers can't review their own submissions.
- **Official announcements** are published by system admins. "Notify everyone" sends one in-app notification per user after the save commits; if it failed, re-run `python manage.py deliver_announcement <article-id>` (safe to repeat).
- **Blackouts:** `CAMPAIGN_BLACKOUTS` (JSON in `.env`, defaults to the 2027 windows) pauses campaign announcements and community approvals in the 24 hours before polls.
- **Daily:** `python manage.py prune_news` (rejected/withdrawn after 180 days; removed after 1 year, except legal holds) and `python manage.py prune_notifications` (read after 180 days, unread after 365).
- Article images are saved under `media/news/` (metadata stripped, WebP, aspect ratio kept).

## Admin dashboard and background jobs (spec 11)

- `/admin` shows each admin role its own queues, weekly counts, readiness (gates and settings as set/not set, never values), background jobs and recent activity. Numbers are cached 30 seconds per role set.
- Every scheduled command records a `JobRun` (shown under Background jobs; "Overdue" when it hasn't run in time). Suggested schedule:
  - every 15 minutes: `sweep_payments` (or the alias `sweep_donations`), `moderation_tick` (also raises community items hidden over 48 hours to urgent) and `events_tick` (event reminders, registration deadlines, blackout flags; spec 18);
  - daily: `check_donations`, `prune_pvc`, `prune_news`, `prune_notifications`, `prune_posts`, `prune_moderation`, `prune_communities`, `prune_events`, `prune_volunteers`;
  - nightly: `sync_communities --placements` (spec 17);
  - hourly: `privacy_tick`;
  - weekly: `prune_jobruns` (job records older than 90 days), `verify_audit`, `prune_audit`, `check_integrity` (spec 16).

## Search (spec 12)

- **Needs the `pg_trgm` extension.** `migrate` creates it; on managed PostgreSQL, if the app's database user isn't allowed to, run `CREATE EXTENSION pg_trgm;` once as the database owner, then `migrate` again.
- The index (`search.SearchEntry`) holds public items only and updates in the same transaction as every candidate, article and post save. `python manage.py rebuild_search` rebuilds it (weekly, and after deploys); the admin Readiness shows "Search index" and the job shows under Background jobs.
- Searches are anonymous and never stored. Django logs show `q=[redacted]`. **Reverse proxy:** strip the query string for `/api/v1/search/` and `/api/v1/candidates/` from access logs too (for nginx, log `$uri` instead of `$request_uri` for those paths).
- `PATCH /me` accepts `home_state` (optional, private; only pre-fills state filters in the browser).

## Reporting and moderation (spec 13)

- **Rules** live in code (`apps/moderation/rules.py`: R1–R10, version, date, change history) and are served at `/api/v1/rules/`. They ship marked "Draft — under legal review" (`RULES_DRAFT`) until the party's legal team signs them off. Bump `RULES_VERSION`/`RULES_UPDATED` and add a `CHANGES` line when they change: signed-in users then see a one-time banner.
- **One process for everything public:** reports on news, announcements, candidate posts and candidate profiles become cases in `/admin/moderation`. People decide; report counts never remove anything (a burst of reports only raises a flag).
- **Every removal cites a rule.** The public URL then answers 410 with the rule and nothing else. Content changes always go through the owning app (`news.services.remove/restore`, `posts.services.remove_post/restore_post`), which call back into moderation so the case, notifications and compliance figures stay consistent.
- **Roles:** `moderator` (grantable at `/admin/team`) handles community news and candidate posts; news reviewers handle community news; system admins also handle official announcements, candidate profiles (corrected in the candidate editor, then closed with Keep), escalations and legal notices. Nobody acts on their own content, a candidate they're linked to, a report they filed, or an appeal of their own decision. Claims lapse after 4 hours.
- **Authors** are notified of removals, warnings and pauses (7, 30 or 90 days of no community news), and can appeal once within 14 days; someone other than the original decider decides.
- **Legal notices** (system admins, `/admin/moderation/legal`): agency notices need a legal basis and get a 48-hour deadline; court orders; copyright notices (also from the public form `/report/copyright`) with counter-notices. Attachments go to `PRIVATE_MEDIA_ROOT` (PDF, JPEG, PNG; 10 MB) and every download is audited. A legal hold stops `prune_news`, `prune_posts` and `prune_moderation` for that item.
- **Settings:** `MODERATION_LEGAL_EMAIL` (shown on `/rules` for agencies; NDCConnect records acknowledgements, it doesn't send them), `COPYRIGHT_COUNTER_NOTICE_DAYS` (default 10, pending a lawyer's confirmation), `COMPLIANCE_SAFETY_STATEMENT`.
- **NITDA annual report:** `/admin/moderation/compliance` shows every figure for a year (Lagos time) with its definition; "Download CSV" is audited and holds no personal data. `prune_moderation` freezes a year's figures before it deletes old cases.
- **Schedule:** `moderation_tick` every 15 minutes (releases stale claims, burst flags, copyright windows); `prune_moderation` daily (closed cases, their reports and appeals 1 year after closing).

## Security and privacy (spec 14)

- **Data register** (`apps/privacy/register.py`): every category of personal data, its purpose, lawful basis (NDPA 2023), who sees it, retention and the job that enforces it. `/api/v1/privacy/` (and the web app's `/privacy`) is generated from it. A test fails when a model isn't in the register or in `NOT_PERSONAL` with a reason. Wording ships marked draft (`PRIVACY_DRAFT`) until the DPO and legal team sign it off.
- **Consent** (`apps/privacy/consent.py`): following candidates, membership applications, PVC checks and the verified-voter badge each need a `ConsentRecord` for the purpose's current version (403 `consent_required` with the text; the web app shows a dialog and retries). Withdrawing from Your data undoes the processing at once. Records from before this unit were migrated as `before_records`.
- **18+:** a profile is complete only once `adult_confirmed_at` is set (`PATCH /me {"adult_confirmed": true}`).
- **Your data** (`/account/privacy`): download (`POST /me/data-export/`, a ZIP built in memory and streamed, 3 a day), consents, privacy requests (`PRV-…`, 30-day clock paused while waiting for the person), and self-service deletion (`POST /me/delete/`, deletes the user in Clerk, then erases at once).
- **Admin two-step verification:** in the Clerk dashboard, (1) turn on multi-factor (Pro plan since Feb 2026; authenticator app and passkeys, SMS only as a backup) and (2) add the custom session claim `"mfa": "{{user.two_factor_enabled}}"` under Sessions → Customize session token. With `ADMIN_MFA_REQUIRED` (on by default in production) every `/api/v1/admin/` request from an admin role needs `mfa: true` (403 `mfa_required`).
- **Reverification:** reveals (VIN, NIN, phone, member photo, legal attachments), exports, role grants/revokes, deactivation and incident close/notice need a second factor verified within `REVERIFY_MINUTES` (10), read from Clerk's `fva` claim; the error uses Clerk's reverification shape so the web app's `useReverification()` prompts and retries. Your own download and deletion need any factor within 10 minutes. The table is `apps/access/security.py`.
- **Incidents** (`/admin/privacy/incidents`, privacy officers and system admins): NDPC (72 h from awareness, NDPA s. 40) and ngCERT (72 h from detection, Cybercrimes (Amendment) Act 2024 s. 21) clocks, notification records, in-app security notices, and the breach register CSV. Nothing is sent automatically.
- **Audit log** is hash-chained (SHA-256, `seq`/`prev_hash`/`hash`). `verify_audit` checks it; `prune_audit` removes entries and privacy records older than 6 years and keeps an anchor.
- **Access review:** every 90 days (`/admin/team/review`); holders inactive 60 days are flagged.
- **Settings:** `PRIVACY_CONTACT_EMAIL`, `SECURITY_CONTACT_EMAIL`, `CONTROLLER_NAME`, `HOSTING_LOCATION`, `ADMIN_MFA_REQUIRED`, `REVERIFY_MINUTES`, `CROSS_ORIGIN_RESOURCE_POLICY` (`same-site`; `cross-origin` for split domains). Production `DATABASE_URL` must end in `?sslmode=require` (or `verify-full`).
- **`python manage.py check_security`** prints PASS/FAIL/WARN and a name per check (never values) and exits 1 on any FAIL; the admin readiness panel shows the same under Security. CI (`.github/workflows/security.yml`) runs the tests, `check_security` with production settings and dummy values, `pip-audit` and gitleaks.
- **Backups and restore tests:** see `docs/backups.md`; record each quarterly test with `python manage.py restore_drill --ok "notes"`.
- **Schedule:** `privacy_tick` hourly; `verify_audit` and `prune_audit` weekly.

## API and integration (spec 15)

- **Contract:** `docs/openapi.json` (generated) and `docs/api.md` (conventions, error codes, rate limits, how to add an
  endpoint). Regenerate after any API change:
  `DJANGO_SETTINGS_MODULE=config.settings.schema python manage.py spectacular --format openapi-json --file docs/openapi.json --validate --fail-on-warn`,
  then `npm run api:types` in `ndcfrontend`. Breaking changes need an entry in `docs/api-changes.md` (`manage.py api_compat` in CI).
- **Environments:** development `config.settings.dev` (Clerk development instance, `PAYMENT_PROVIDER=fake`, `MEDIA_BACKEND=local`);
  tests `config.settings.test`; staging and production `config.settings.prod` with their own Clerk instance, Paystack test
  (staging) or live (production) keys, `MEDIA_BACKEND=cloudinary` and `CLOUDINARY_FOLDER=ndcconnect/staging` or `ndcconnect/prod`,
  `CACHE_BACKEND=database`. Never copy production personal data to staging.
- **Shared cache:** production uses PostgreSQL's database cache (`CACHE_BACKEND=database`; the `ndc_cache` table is created by
  `migrate`), so rate limits and circuit breakers hold across Gunicorn workers. `check_security` and `/ready/` fail on the
  per-process memory cache outside development.
- **Cloudinary setup** (public images only; membership photos stay on NDCConnect's servers):
  1. Use the party's own Cloudinary account. Settings → API Keys: copy the cloud name and API key into `CLOUDINARY_CLOUD_NAME`
     and `CLOUDINARY_API_KEY`, the API secret into `CLOUDINARY_API_SECRET` (backend `.env` only — never in the frontend or Git).
  2. Set `CLOUDINARY_FOLDER` per environment and `MEDIA_BACKEND=cloudinary`.
  3. `python manage.py cloudinary_setup` creates the signed upload preset (`CLOUDINARY_UPLOAD_PRESET`, default
     `ndcconnect_signed`): JPEG/PNG/WebP only, originals capped at 2000 px, metadata stripped.
  4. Existing local images: `python manage.py migrate_media_to_cloudinary --dry-run`, then without `--dry-run`
     (idempotent; `--delete-local` removes the local files after a verified upload).
  5. In the web app set `VITE_MEDIA_ORIGIN=https://res.cloudinary.com`.
- **Uploads:** the browser asks `POST /media/upload-signature/` for one signed upload, sends the file straight to Cloudinary,
  then sends `{"asset": {public_id, version, signature}}` to the usual image endpoint, which re-checks the asset with
  Cloudinary (folder, format, ≤ 5 MB, ≥ 400 px) before linking it. In `local` mode the signature answer is `{"mode": "server"}`
  and the file goes to the image endpoint as before.
- **Outbox:** work that must happen after a commit and must not be lost (notification fan-out, image deletion and hiding,
  Clerk ban retries, webhook retries) is written as an `OutboxJob` in the same transaction and tried right after the commit.
  Run `python manage.py process_outbox` every minute (or `process_outbox --loop` as a long-running process); failures retry
  after 1 min, 5 min, 30 min, 2 h and 12 h, then show as "Background tasks failing" on `/admin` and at `/admin/integrations`.
- **Webhook register:** every Clerk and Paystack webhook leaves a `WebhookReceipt` (IDs, types, a body hash; never the payload).
  Duplicates change nothing; failures are retried. `PAYSTACK_WEBHOOK_IPS` optionally restricts Paystack's source addresses (off by default).
- **External calls** go through `apps/integrations/http.py`: connect 3 s; read 10 s Paystack, 5 s Clerk, 10 s Cloudinary
  (30 s server uploads); retries only for idempotent calls; a circuit breaker (5 failures in 60 s → 30 s open). It connects
  directly (no `HTTPS_PROXY` support): the servers need outbound HTTPS to the providers.
- **Logs:** one line per request with the route name, status, duration and request ID (`LOG_FORMAT=json` for JSON lines);
  requests over `SLOW_REQUEST_MS` (1500) are warnings. Error responses carry the request ID; search for it in the logs.
- **Schedule:** `process_outbox` every minute; `prune_outbox` and `prune_webhooks` daily; `media_audit` weekly.

## Database (spec 16)

- **Data dictionary:** `docs/schema.md` (every table, column, delete rule and constraint) and `docs/erd.mmd` (Mermaid), generated
  from the models. After any model change run `python manage.py schema_doc` and commit both; CI runs `schema_doc --check`, which
  also fails on a model without a docstring, a JSON column without a reason or a delete rule outside `apps/core/schema_policy.py`.
- **When `migrate` stops** with `ConstraintViolated: <rule>: N existing row(s) break this rule`, nothing was changed. Run
  `python manage.py check_integrity --constraint <rule>` for the IDs, fix those records through the app (or ask for a data fix),
  then run `migrate` again.
- **History is append-only in the database:** event tables and the audit log refuse edits and deletes. Only the prune jobs delete
  old rows (`apps.core.history.allow_history_delete()`).
- **`check_integrity`** (weekly) counts what the database can't enforce; the result shows on `/admin` (Readiness → Data
  integrity, and a card when there are issues). Counts only.
- **`IP_HASH_KEY`:** a long random string used to hash IP addresses for abuse limits (falls back to `SECRET_KEY`). Set it before
  launch; changing it later only resets abuse limits.
- **Geography:** `python manage.py load_geography <file.csv> --source "<where it came from>" --published-on YYYY-MM-DD` previews
  INEC's polling-unit list (format in `data/geo/README.md`); add `--apply` to apply it (spec 19). Loading the same file again changes
  nothing; places missing from a newer file become inactive. `python manage.py match_geography --dry-run` reports how many existing memberships,
  PVC checks and candidates match exactly; without `--dry-run` it adds the links (people's own text is never changed).
  `data/geo/sample.csv` is invented test data, not INEC's list.
- **Candidate import:** `python manage.py import_candidates [--file data/candidates/ndc_2027.json] [--dry-run] [--list]` loads
  researched candidate profiles as unverified drafts for admin review (50 NDC 2027 candidates; see `data/candidates/README.md`).
  Safe to run again: it never changes a verified, published or hand-made record.
- **News import:** `python manage.py import_news --as <admin email> [--announcements] [--file …] [--dry-run] [--list]` publishes
  researched election news (50 items) or NDCConnect's own announcements with graphics as official articles, without
  notifying anyone (see `data/news/README.md`). Safe to run again: anything already there is left as it is.
- **Demo donations (local only):** `python manage.py seed_demo_donations --count 1500 --total 557000000` seeds simulated donations
  to preview the Finance pages: synthetic names, `example.com/.org/.net` emails, `+23480000…` demo phone numbers, fictional
  addresses, varied states, named and anonymous donors, the NDC fund and published candidates, dates over `--days` or
  `--start/--end`. Each row has `is_demo=True` (the database also requires an `NDC-DEMO-` reference and provider `demo`); real
  totals, donor limits, CSV exports and the admin dashboard exclude them, and the Finance pages label them DEMO DATA. No payment
  provider, webhook or receipt is involved. It refuses unless `DEBUG` is on and the database is on this machine. Rerunning with
  the same numbers does nothing; `--replace` swaps them, `--remove` deletes only demo rows. It reconciles count and kobo total
  in the database before committing.
- **Setup:** after `migrate`, run `python manage.py sync_communities`. It creates the national and 37 state communities and, from
  `apps/geo`, one per LGA, ward (registration area) and polling unit; run it again after every `load_geography` (`--dry-run` shows
  what would change). Running it twice changes nothing; communities whose geography went inactive become read-only.
- **Placement:** people join national and state communities themselves (consent "Communities", then a chosen state). A verified PVC
  matched to `geo` places them in their state, LGA, ward and polling unit automatically (outbox job `communities_place`).
  `sync_communities --placements` re-runs placement for everyone nightly as a safety net.
- **Moderators:** Admin → Team → Grant role → Moderator, then **Where** (a state, or Nigeria) and optionally an LGA. They see and
  decide cases in that community and the levels below only. Admin → Communities shows the tree, settings (posting open, who can
  read, NDC members only) and moderators. Nobody, admins included, can see who is in a community.
- **Schedule:** `sync_communities --placements` nightly, `prune_communities` daily (deleted items 30 days, removed items and ended
  memberships per spec 17 §17.3).
- **Checks:** `check_integrity` adds community checks (memberships missing their parent level, members of closed communities,
  count drift (fixed), items hidden over 48 hours).

## Events and volunteering (spec 18)

- **Who organises:** a candidate's linked campaign accounts (Admin → Candidates → Accounts) from `/campaign/<candidate>` → Events,
  and system admins for party events (Admin → Events → Party events, optionally NDC members only). Every event shows its organiser.
- **The law in the rules:** set `CAMPAIGN_PERIODS` from INEC's timetable (the defaults are the 2027 general election's). Rallies and
  canvassing outside the office's period are refused; no campaign event may start inside a `CAMPAIGN_BLACKOUTS` window; places of
  worship, police stations and public offices can't be venues; publishing needs the declarations (no inducements, lawful venue,
  police told for public rallies). Readiness shows "Campaign periods" and "Events jobs".
- **Registration:** signed-in adults with the "Events" consent; ticket codes; a waitlist when full (promoted in order, notified);
  reminders 24 hours before (1 hour for online events). Organisers see counts, and short names with ticket codes only from 24 hours
  before to 24 hours after the event (each opening audited, no export). Nobody else can see who registered.
- **Volunteering:** off until the campaign opens it (`/campaign/<candidate>` → Volunteers). Campaigns see first name + last
  initial, area, roles and note; full name and phone only after accepting someone who opted in (audited). Updates to accepted
  volunteers, up to 5 a week.
- **Moderation:** reports go to 13's cases (type Events, rule R11, reasons "inducement" and "banned venue"); moderators can hide while
  deciding, then remove or restore.
- **Schedule:** `events_tick` every 15 minutes; `prune_events` and `prune_volunteers` daily (spec 18 §17.2).
- **Checks:** `check_integrity` adds event count drift, events over capacity, waitlists with free places and open volunteering for
  unpublished candidates.

## States and geography (spec 19)

- **Loading INEC's list:** Admin → Geography → Files → upload the CSV (system admins), or `python manage.py load_geography <file>`.
  Both preview first: problems with line numbers, then what would be added, renamed, brought back, retired or moved, per state, and
  how many memberships and PVC checks point at retired or moved units. Nothing changes until you apply (`--apply`, or the Apply
  button). Write who checked the file and how in Notes / `--notes` (spec 19 §4.1).
- **The 95% rule:** a polling-unit file with fewer than 95% of the active units needs two different system admins: the first apply is
  recorded and refused, a second admin then applies with "Apply as the second admin" (or `--confirm-by <their email>` on the
  command line, which counts as one admin). `GEO_SECOND_ADMIN_MIN_UNITS` (default 1000) keeps small test lists out of it.
- **Constituencies:** `load_geography <file> --kind senatorial_districts|federal_constituencies|state_constituencies` with columns
  `state_code,code,name,lga_codes,ward_codes` (codes separated by `;`, wards as `LGA-ward`, e.g. `07-05`). Samples in `data/geo/`.
- **After an apply** the worker updates communities and search and tells people whose polling unit was retired or moved
  (`geo_unit_changed`, once each).
- **Existing free text:** `match_geography` (nightly) links exact matches as `matched`; `geo_notify_unconfirmed` (hourly, 500 a run)
  asks each person once to confirm on their Account page. Admin → Geography → Review lists records with typed text but no link,
  "not listed" ones, retired units and PVC changes waiting for an officer (membership officers see memberships, PVC officers see PVC
  checks). PVC officers confirm the polling unit when they verify a check; communities use only those links and exact matches.
- **Public:** `/states/<state>/<lga>` pages and the `/polling-units` finder (INEC's names and codes, always linking to INEC's locator,
  `INEC_PU_LOCATOR_URL`). Wards and polling units have no pages of their own.
- **Checks:** `check_integrity` adds broken location chains, active places under retired ones, constituencies covering retired places
  and officer-confirmed PVC locations without a polling unit. Readiness shows "Geography" and "Constituencies".

## Settings (spec 20)

- **Endpoints:** everything for one person under `/api/v1/me/settings/` (private, `no-store`, `settings_write` 60/min): `GET` all;
  `PATCH profile/`, `language/`, `accessibility/`, `notifications/`, `privacy/`; `POST`/`DELETE public-profile/` (on needs
  `{"consent": true, "version": n}`); `POST security/sign-out-others/` (reverification; revokes the other Clerk sessions);
  `POST import-device/` (once, before the account saves its own). `POST /me/pause/` (reverification, `{"confirm": "pause"}`) and
  `POST /me/reactivate/`. Public `GET /people/<handle>/` (404 unless on; `noindex`; 5-minute cache) and `POST /people/<handle>/report/`.
  System admins: `GET`/`PATCH /admin/settings/defaults/` (audited).
- **Paused accounts:** `deactivation_kind = "self"`. Signing in still works; only `/me/`, `/me/reactivate/`, `/me/data-export/` and
  `/me/delete/` answer, everything else is 403 `account_paused`. Future event registrations are cancelled and pending volunteer
  applications withdrawn; nothing new is delivered. Admin deactivation (`admin`) always wins and only an admin reverses it.
- **Defaults for new accounts** (Admin → Settings defaults): notification categories, theme, text size, and which languages can be
  chosen (only those in `settings_services.TRANSLATIONS_COMPLETE`, English until spec 21). Existing people keep what they have.
- **Public profile moderation:** reports open a `profile` case for platform-wide moderators; Remove takes `parts`
  (`["bio"]`, `["photo"]` or both) and hides them without editing; Restore shows them again.
- **About:** `/api/v1/health/` returns `api_version` and `commit` (from `RAILWAY_GIT_COMMIT_SHA`) for Settings → About.

## Deploying on Railway (ahead of spec 22)

Two services from this repo plus the Postgres service, all in one Railway project and region:

| Service | Start command | Notes |
|---|---|---|
| `ndcbackend` (web) | `gunicorn config.wsgi --bind 0.0.0.0:$PORT --workers 2 --timeout 60 --access-logfile -` | Pre-deploy: `python manage.py migrate --noinput && python manage.py createcachetable`. Health check `/api/v1/health/`. Public domain. Volume at `/data` (private media). |
| `ndc-worker` | `python manage.py run_schedule` | No domain, no pre-deploy. Drains the outbox every 5 seconds and runs every scheduled job when it is due (`apps/core/management/commands/run_schedule.py`), so no cron services are needed. |

- Python is pinned by `.python-version`; Railpack installs `requirements.txt`. `config.wsgi` defaults to the production settings.
- On Railway the production settings add `healthcheck.railway.app` and `RAILWAY_PUBLIC_DOMAIN` to the allowed hosts; `/api/v1/health/` skips the HTTPS redirect (it carries no data).
- **Variables (both services):** `DJANGO_SETTINGS_MODULE=config.settings.prod`, `DATABASE_URL=${{Postgres.DATABASE_URL}}?sslmode=require` (private network), `NUM_PROXIES=1`, `CLERK_AUTHORIZED_PARTIES` and `CORS_ALLOWED_ORIGINS` (the frontend's origin), `PRIVATE_MEDIA_ROOT=/data/private_media` (web), and the secrets, set in the Railway dashboard only: `DJANGO_SECRET_KEY`, `CLERK_SECRET_KEY`, `CLERK_WEBHOOK_SIGNING_SECRET`, `PII_ENCRYPTION_KEYS`, `IP_HASH_KEY`, `PAYSTACK_SECRET_KEY`, `CLOUDINARY_*`.
- **Use the same `PII_ENCRYPTION_KEYS` and `IP_HASH_KEY` everywhere that writes to the same database** (both services, and your laptop if it points at Railway's Postgres). A laptop without `PII_ENCRYPTION_KEYS` generates its own `.dev_pii_key`, and VINs and NINs it saves can't be read by the deployed app (or the reverse).
- Production refuses the fake payment provider: set `PAYMENT_PROVIDER=paystack` (donations and membership stay closed by their gates). Admin two-step verification is on by default (`ADMIN_MFA_REQUIRED`); without Clerk Pro and the `mfa` claim, admins get 403 on `/api/v1/admin/` until it is set up or the variable is set to `False` on a test deployment.
- Images: production doesn't serve `MEDIA_ROOT`, so public images need `MEDIA_BACKEND=cloudinary`.
- `FRONTEND_BASE_URL` must be the web app's address (payment callbacks and links in notices). Donations and membership stay closed until their switches are set (`DONATIONS_ENABLED`, `DONATIONS_COMPLIANCE_APPROVED`, `MEMBERSHIP_ENABLED`, `MEMBERSHIP_DATA_APPROVED`, all `False` on Railway) and a Paystack key is present; the Paystack webhook is `/api/v1/webhooks/payments/paystack/`.
- A boolean variable that points at a missing shared variable is an empty string, which stops Django at startup: define the shared value first.
- Granting the first admin on Railway: run `grant_role <email> system_admin` from a laptop whose `DATABASE_URL` points at Railway, after the person has signed in once; after that, Admin → Team.
- After the first deploy: `python manage.py check_security` and `/api/v1/ready/` (both run in the web service's shell: `railway ssh` or the dashboard).

## Tests

```bash
python manage.py test apps --parallel
```

`--parallel` runs the tests across your CPU cores. `tblib` (in `requirements-dev.txt`) keeps the real traceback when a test fails
in that mode; without it a failure shows only as "cannot pickle 'traceback' object". Drop `--parallel` to run one at a time.

`manage.py test` always uses `config.settings.test` (unless you pass `--settings`) and ignores `.env` except `DATABASE_URL`,
so your local keys, gates and consent switches can't change the results. Works the same on Windows (PowerShell) and Linux.

Tests use real RS256-signed tokens and real Svix signatures. Only Clerk's network calls are replaced.
