# ndcbackend

NDCConnect API — Django 5.2 LTS + Django REST Framework + PostgreSQL. Authentication is Clerk (`feature-specs/02-authentication.md`).

## Run

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                   # fill in Clerk dev keys
python manage.py migrate
python manage.py runserver
```

Health check: `GET http://localhost:8000/api/v1/health/`

## Clerk setup (development instance)

1. Clerk Dashboard → **API keys**: copy the secret key into `CLERK_SECRET_KEY` (optionally the JWT public key into `CLERK_JWT_KEY` for networkless verification).
2. Clerk Dashboard → **Webhooks** → add endpoint `https://<your-tunnel>/api/v1/webhooks/clerk/`, subscribe to `user.created`, `user.updated`, `user.deleted`, and copy the signing secret into `CLERK_WEBHOOK_SIGNING_SECRET`. Locally, expose port 8000 with a tunnel (e.g. ngrok).
3. Set `CLERK_AUTHORIZED_PARTIES` to the frontend origin(s).

## API

| Method | Path | Who |
|---|---|---|
| GET | `/api/v1/health/` | anyone |
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
| POST | `/api/v1/news/<id>/report/` | anyone (5/hour) |
| GET / POST | `/api/v1/me/news/` · GET / PATCH `<id>/` · PUT / DELETE `<id>/image/` · POST `<id>/withdraw/` | signed in (writes: completed profile) |
| GET / POST | `/api/v1/admin/news/` · `<id>/` · `<id>/{approve,reject,remove,restore}/` · `reports/<id>/dismiss/` | news reviewer or system admin |
| GET / POST | `/api/v1/admin/announcements/` · `notify-count/` · `<id>/` · `<id>/image/` · `<id>/{pin,unpin,unpublish,restore}/` | system admin |
| GET | `/api/v1/me/notifications/` · `unread-count/` · POST `<id>/read/` · `read-all/` · GET / PATCH `preferences/` | signed in |

Single-candidate admin responses include `followers: {total, notify_on}` — counts only; no endpoint lists who follows a candidate.
Follows are political-opinion data (NDPA 2023): private to the user, never audited, deleted when the account is deleted.

Candidate photos are saved under `media/` (git-ignored) in development and served by Django only when `DEBUG` is on.

Errors always look like `{"error": {"status", "code", "message", "fields"}}`.
Codes to handle: `not_authenticated` (401), `account_deactivated` (403), `auth_unavailable` (503), `validation_error` (400), `throttled` (429), `invalid_transition` (409), `invalid_photo` (400), `storage_unavailable` (503), `too_many_pending` (409), `self_review` (403), `publishing_paused` (409, with `until`), `too_many_pinned` (409), `source_required` (400).

## Roles (no UI yet)

```bash
python manage.py grant_role ada@example.com system_admin
python manage.py grant_role --clerk-id user_123 candidate --scope-type candidate --scope-id <candidate-uuid>
python manage.py revoke_role ada@example.com moderator --reason "rotation"
python manage.py deactivate_user spam@example.com --reason "spam"   # also bans in Clerk; re-run to retry a failed ban
python manage.py reactivate_user spam@example.com
```

The user must have signed in or signed up at least once (so the local record exists). Every command is audited as `system:cli`.
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

## News and announcements (spec 07)

- **News reviewers:** `python manage.py grant_role reviewer@example.com news_reviewer`. Reviewers can't review their own submissions.
- **Official announcements** are published by system admins. "Notify everyone" sends one in-app notification per user after the save commits; if it failed, re-run `python manage.py deliver_announcement <article-id>` (safe to repeat).
- **Blackouts:** `CAMPAIGN_BLACKOUTS` (JSON in `.env`, defaults to the 2027 windows) pauses campaign announcements and community approvals in the 24 hours before polls.
- **Daily:** `python manage.py prune_news` (rejected/withdrawn after 180 days; removed and dismissed reports after 1 year) and `python manage.py prune_notifications` (read after 180 days, unread after 365).
- Article images are saved under `media/news/` (metadata stripped, WebP, aspect ratio kept).

## Tests

```bash
DJANGO_SETTINGS_MODULE=config.settings.test python manage.py test apps
```

Tests use real RS256-signed tokens and real Svix signatures. Only Clerk's network calls are replaced.
