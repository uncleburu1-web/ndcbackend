"""
Admin account security (spec 14 §4.6, §6.6), enforced at the authentication boundary so no admin view can forget it:
- every `/api/v1/admin/…` request from someone holding an admin role needs two-step verification (`mfa` claim),
  when ADMIN_MFA_REQUIRED is on;
- the sensitive actions below need a factor verified within REVERIFY_MINUTES (`fva` claim), returned to the browser
  in Clerk's reverification shape so `useReverification()` can prompt and retry.
Tests that use DRF's force_authenticate bypass authentication entirely; the security tests use signed tokens.
"""
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from apps.core.exceptions import ServiceError

from .models import Role, RoleAssignment, ScopeType

ADMIN_PREFIX = "/api/v1/admin/"
ADMIN_ROLES = {Role.SYSTEM_ADMIN, Role.NEWS_REVIEWER, Role.PVC_OFFICER, Role.MEMBERSHIP_OFFICER, Role.FINANCE_OFFICER, Role.MODERATOR,
               Role.PRIVACY_OFFICER}

# url name → methods (and an optional query test) needing a fresh SECOND factor (spec 14 §3.5).
SECOND_FACTOR = {
    "admin-pvc-reveal": {"POST"},
    "admin-membership-reveal-nin": {"POST"},
    "admin-membership-photo": {"GET"},
    "admin-user-phone": {"GET", "POST"},
    "admin-moderation-legal-attachment": {"GET"},
    "admin-donations-export": {"GET"},
    "admin-membership-register": {"GET"},
    "admin-membership-register-photos": {"GET"},
    "admin-moderation-compliance": {"GET:csv"},
    "admin-privacy-incidents": {"GET:csv"},
    "admin-team": {"POST"},
    "admin-team-revoke": {"POST"},
    "admin-team-review": {"POST"},
    "admin-user-deactivate": {"POST"},
    "admin-user-reactivate": {"POST"},
    "admin-privacy-incident-close": {"POST"},
    "admin-privacy-incident-notice": {"POST"},
    "admin-privacy-request-export": {"POST"},
    "admin-integrations-outbox-retry": {"POST"},  # spec 15 §15.3
}
# Your own data download and account deletion: any factor, recently (spec 14 §3.5).
ANY_FACTOR = {"my-data-export": {"POST"}, "my-delete": {"POST"}}


class MfaRequired(ServiceError):
    pass


def is_admin(user) -> bool:
    key = f"sec:is-admin:{user.pk}"
    hit = cache.get(key)
    if hit is None:
        hit = RoleAssignment.objects.filter(user=user, is_active=True, scope_type=ScopeType.GLOBAL, role__in=ADMIN_ROLES).exists()
        cache.set(key, hit, 30)
    return hit


def forget_admin(user):
    cache.delete(f"sec:is-admin:{user.pk}")


def mfa_enabled(claims) -> bool:
    v = (claims or {}).get("mfa")
    return v is True or str(v).lower() == "true"


def mfa_claim_present(claims) -> bool:
    return "mfa" in (claims or {})


def _ages(claims):
    fva = (claims or {}).get("fva")
    if isinstance(fva, (list, tuple)) and len(fva) == 2:
        try:
            return int(fva[0]), int(fva[1])
        except (TypeError, ValueError):
            pass
    return -1, -1


def fresh(claims, level) -> bool:
    minutes = settings.REVERIFY_MINUTES
    first, second = _ages(claims)
    ok_first = 0 <= first <= minutes
    ok_second = 0 <= second <= minutes
    if level == "first_factor":
        return ok_first or ok_second
    if second == -1 and not settings.ADMIN_MFA_REQUIRED:
        return ok_first  # no second factor set up and not required: Clerk downgrades the same way
    return ok_second


def _needs(url_name, method, request, table) -> bool:
    rules = table.get(url_name or "")
    if not rules:
        return False
    if method in rules:
        return True
    if f"{method}:csv" in rules:
        return request.GET.get("format") == "csv"
    return False


def reverification_error(level):
    e = ServiceError("Confirm it's you to continue.", code="reverification_required", status_code=403)
    e.extra = {"clerk_error": {"type": "forbidden", "reason": "reverification-error",
                               "metadata": {"reverification": {"level": level, "afterMinutes": settings.REVERIFY_MINUTES}}}}
    return e


def touch_last_seen(user, claims=None):
    now = timezone.now()
    mfa = mfa_enabled(claims) if mfa_claim_present(claims) else None
    if user.last_seen_at is None or now - user.last_seen_at > timedelta(hours=1) or (mfa is not None and mfa != user.mfa_enabled):
        type(user).objects.filter(pk=user.pk).update(last_seen_at=now, mfa_enabled=mfa)
        user.last_seen_at, user.mfa_enabled = now, mfa


def enforce(request, user, claims):
    """Called by ClerkSessionAuthentication after the token and account are verified."""
    django_request = getattr(request, "_request", request)
    path = django_request.path
    match = getattr(django_request, "resolver_match", None)
    url_name = match.url_name if match else ""
    method = django_request.method
    admin_path = path.startswith(ADMIN_PREFIX)
    if admin_path or url_name == "me":
        touch_last_seen(user, claims)
    if admin_path and is_admin(user):
        if settings.ADMIN_MFA_REQUIRED and not mfa_enabled(claims):
            e = MfaRequired("Turn on two-step verification to use the admin.", code="mfa_required", status_code=403)
            e.extra = {"claim_missing": not mfa_claim_present(claims)}
            raise e
        if settings.REVERIFICATION_REQUIRED and _needs(url_name, method, django_request, SECOND_FACTOR) and not fresh(claims, "second_factor"):
            raise reverification_error("second_factor" if settings.ADMIN_MFA_REQUIRED else "first_factor")
    if settings.REVERIFICATION_REQUIRED and _needs(url_name, method, django_request, ANY_FACTOR) and not fresh(claims, "first_factor"):
        raise reverification_error("first_factor")


def require_fresh(request, level="second_factor"):
    """For service-level checks outside the URL table (e.g. officer exports triggered from a request page)."""
    claims = getattr(getattr(request, "auth", None), "claims", None)
    if request is None or getattr(request, "auth", None) is None:
        return  # force-authenticated tests and internal calls
    if settings.REVERIFICATION_REQUIRED and not fresh(claims, level):
        raise reverification_error(level)


def note_admin_denied(request):
    """Spec 14 §4.6: more than 20 admin 403s in an hour writes one audit entry (a probing signal)."""
    user = getattr(request, "_user", None)  # never re-run authentication from the error handler
    if not (user and getattr(user, "is_authenticated", False)):
        return
    key = f"sec:denied:{user.pk}:{timezone.now():%Y%m%d%H}"
    try:
        n = cache.incr(key)
    except ValueError:
        cache.set(key, 1, 3700)
        n = 1
    if n == 21:
        from apps.audit.models import AuditLog
        from apps.audit.services import record

        record(AuditLog.Action.SECURITY_ADMIN_ACCESS_DENIED, target=user, actor=user, request=request, count=21)
