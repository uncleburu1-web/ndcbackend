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

Candidate photos are saved under `media/` (git-ignored) in development and served by Django only when `DEBUG` is on.

Errors always look like `{"error": {"status", "code", "message", "fields"}}`.
Codes to handle: `not_authenticated` (401), `account_deactivated` (403), `auth_unavailable` (503), `validation_error` (400), `throttled` (429), `invalid_transition` (409), `invalid_photo` (400), `storage_unavailable` (503).

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

## Tests

```bash
DJANGO_SETTINGS_MODULE=config.settings.test python manage.py test apps
```

Tests use real RS256-signed tokens and real Svix signatures. Only Clerk's network calls are replaced.
