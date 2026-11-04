"""`check_security` and the readiness Security group (spec 14 §4.10). Names and PASS/FAIL/WARN only, never a value."""
import os
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

PASS, FAIL, WARN = "PASS", "FAIL", "WARN"
DEV_KEY_PREFIX = "insecure"


def _db_tls() -> bool:
    db = settings.DATABASES.get("default", {})
    mode = str((db.get("OPTIONS") or {}).get("sslmode", "")).lower()
    return mode in ("require", "verify-ca", "verify-full")


def _private_outside_public() -> bool:
    priv = os.path.realpath(str(settings.PRIVATE_MEDIA_ROOT))
    pub = os.path.realpath(str(settings.MEDIA_ROOT))
    return not (priv == pub or priv.startswith(pub.rstrip(os.sep) + os.sep))


def _mfa_claim_state():
    """FAIL when admins have signed in but none sent the `mfa` claim; WARN when no admin has signed in yet."""
    from apps.access.models import RoleAssignment, ScopeType
    from apps.access.security import ADMIN_ROLES

    holders = RoleAssignment.objects.filter(is_active=True, scope_type=ScopeType.GLOBAL, role__in=ADMIN_ROLES, user__last_seen_at__isnull=False)
    if not holders.exists():
        return WARN
    return PASS if holders.filter(user__mfa_enabled__isnull=False).exists() else FAIL


def last_run(job, memo=None):
    from apps.core.models import JobRun

    if memo is not None and job in memo:
        return memo[job]
    row = JobRun.objects.filter(job=job, finished_at__isnull=False).order_by("-started_at").first()
    if memo is not None:
        memo[job] = row
    return row


def audit_state(now=None, memo=None):
    """(state, words) for readiness and the attention card."""
    row = last_run("verify_audit", memo)
    if row is None:
        return WARN, "Audit log: not checked yet (run verify_audit)"
    when = f"{timezone.localtime(row.finished_at):%-d %b}"
    if row.ok:
        return PASS, f"Audit log: intact (checked {when})"
    return FAIL, f"Audit log: {row.error.split(': ', 1)[-1] if row.error else 'chain broken'} (checked {when})"


def restore_state(now=None, memo=None):
    now = now or timezone.now()
    row = last_run("restore_drill", memo)
    if row is None or not row.ok:
        return WARN, "Restore test: none recorded"
    age = now - row.finished_at
    ok = age <= timedelta(days=settings.RESTORE_DRILL_MAX_DAYS)
    return (PASS if ok else WARN), f"Restore test: {timezone.localtime(row.finished_at):%-d %b %Y}"


def run(now=None, memo=None) -> list[tuple[str, str]]:
    now = now or timezone.now()
    memo = {} if memo is None else memo
    s = settings
    out = []

    def add(name, ok, warn=False):
        out.append((name, PASS if ok else (WARN if warn else FAIL)))

    add("DEBUG off", not s.DEBUG)
    add("SECRET_KEY strong", len(s.SECRET_KEY) >= 50 and not s.SECRET_KEY.startswith(DEV_KEY_PREFIX))
    add("ALLOWED_HOSTS set", bool(s.ALLOWED_HOSTS) and "*" not in s.ALLOWED_HOSTS)
    add("HTTPS redirect on", bool(getattr(s, "SECURE_SSL_REDIRECT", False)))
    add("HSTS on", int(getattr(s, "SECURE_HSTS_SECONDS", 0) or 0) >= 60 * 60 * 24 * 180)
    origins = list(getattr(s, "CORS_ALLOWED_ORIGINS", []) or [])
    add("CORS origins use https", bool(origins) and all(o.startswith("https://") for o in origins))
    add("Personal data encryption keys set", bool([k for k in s.PII_ENCRYPTION_KEYS if k.strip()]))
    add("Clerk secret key set", bool(s.CLERK_SECRET_KEY))
    add("Clerk JWT key set", bool(s.CLERK_JWT_KEY), warn=True)
    add("Clerk webhook secret set", bool(s.CLERK_WEBHOOK_SIGNING_SECRET))
    add("Admin two-step verification required", bool(s.ADMIN_MFA_REQUIRED))
    add("Reverification for sensitive actions on", bool(s.REVERIFICATION_REQUIRED))
    add("Consent required for political data", bool(s.PRIVACY_CONSENT_REQUIRED))
    out.append(("Admin two-step verification claim configured", _mfa_claim_state()))
    add("Database uses TLS", _db_tls())
    add("Private media outside public media", _private_outside_public())
    try:
        from apps.donations.services import donations_open
        from apps.membership.services import membership_open

        payments_needed = donations_open() or membership_open()
    except Exception:
        payments_needed = True
    add("Payment webhook secret set", bool(s.PAYSTACK_SECRET_KEY) or not payments_needed)
    # spec 15 §11.4, §13.2
    from apps.core.ready import cache_is_shared

    add("Shared cache for rate limits", cache_is_shared())
    if getattr(s, "MEDIA_BACKEND", "local") == "cloudinary":
        add("Cloudinary settings set", bool(s.CLOUDINARY_CLOUD_NAME and s.CLOUDINARY_API_KEY and s.CLOUDINARY_API_SECRET
                                            and s.CLOUDINARY_UPLOAD_PRESET and s.CLOUDINARY_FOLDER))
        add("Cloudinary secret isn't the API key", s.CLOUDINARY_API_SECRET != s.CLOUDINARY_API_KEY)
        add("Cloudinary folder per environment", not s.CLOUDINARY_FOLDER.rstrip("/").endswith("/dev"), warn=True)
    else:
        add("Images on Cloudinary", False, warn=True)
    add("Security contact set", bool(s.SECURITY_CONTACT_EMAIL))
    add("Privacy contact set", bool(s.PRIVACY_CONTACT_EMAIL))
    out.append(("Restore test within 100 days", restore_state(now, memo)[0]))
    out.append(("Audit log chain intact", audit_state(now, memo)[0]))
    return out
