# API changes

Every change to the v1 contract, newest first (spec 15 §4.11). Breaking changes must name the operation as
`METHOD /api/v1/path/` so `manage.py api_compat` can match them.

## 2026-11-04 — unit 15 (API and integration)

Additive:
- Every error envelope has `request_id`; every response has the `X-Request-ID` header.
- 429 answers carry `Retry-After` and `retry_after`; the message says how long to wait.
- Image objects (candidate photos, post and article images) gain `srcset`.
- New: `GET /api/v1/ready/`, `GET /api/v1/schema/`, `POST /api/v1/media/upload-signature/`,
  `GET /api/v1/admin/integrations/`, `…/webhooks/`, `…/outbox/`, `POST …/outbox/{id}/retry/`.
- Image endpoints (`PUT …/photo/`, `PUT …/image/`) also accept JSON `{"asset": {public_id, version, signature}, "rights_confirmed": true}`.
- Notification kinds `membership_decision` (with `membership.status`) and `donation_receipt` (with `donation.reference`).
- New error codes in the catalogue: `invalid_payload`, `webhook_not_configured`, `media_unavailable`.

Tightened (agreed in spec 15 §4.7; the web app only sends valid values):
- Unknown values for choice filters answer `400 invalid_filter` instead of being ignored: `GET /api/v1/candidates/`
  (`state`, `position`), `GET /api/v1/candidates/positions/` (`state`), `GET /api/v1/news/` (`source`, `state`),
  `GET /api/v1/admin/pvc/` (`status`, `state`), `GET /api/v1/admin/membership/payments/` (`status`),
  `GET /api/v1/admin/membership/register.csv` and `…/register-photos.zip` (`state`), `GET /api/v1/admin/moderation/appeals/`
  and `GET /api/v1/admin/moderation/legal/` (`status`).

First snapshot of `docs/openapi.json`.
