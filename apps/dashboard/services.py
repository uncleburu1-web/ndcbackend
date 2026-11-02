"""
Admin home (spec 11 §6.2). The dashboard never queries another app's tables: each app answers
for itself through `admin_summary(user, now=...)` in its own `dashboard.py`.
"""
import importlib
import logging
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from apps.access.models import Role, RoleAssignment, ScopeType
from apps.access.services import has_role, is_system_admin
from apps.audit.display import entry_payload
from apps.audit.models import AuditLog
from apps.core import blackouts, jobs

from .cards import Card, Stat, to_dict

logger = logging.getLogger(__name__)

# Order matters: cards appear in this order (spec 11 §4.2).
SUMMARY_APPS = ["moderation", "news", "posts", "pvc", "membership", "donations", "candidates", "follows", "accounts"]
CARD_ORDER = ["reports_review", "reports_urgent", "appeals_waiting", "legal_due", "news_pending", "pvc_pending", "membership_approvals", "membership_payments_review",
              "donations_review", "candidates_unpublished", "ban_failed"]
GROUPS = [("people", "People"), ("candidates", "Candidates and following"), ("content", "Content"), ("pvc", "PVC"),
          ("membership", "Membership"), ("donations", "Donations")]
ADMIN_ROLES = {Role.SYSTEM_ADMIN, Role.NEWS_REVIEWER, Role.PVC_OFFICER, Role.MEMBERSHIP_OFFICER, Role.FINANCE_OFFICER, Role.MODERATOR}
CACHE_SECONDS = 30
APP_LABELS = {"moderation": "Moderation", "news": "News", "posts": "Candidate posts", "pvc": "PVC checks", "membership": "Membership", "donations": "Donations",
              "candidates": "Candidates", "follows": "Following", "accounts": "Accounts"}


def admin_roles(user) -> list[str]:
    if not (user and user.is_authenticated and user.is_active):
        return []
    return sorted(set(RoleAssignment.objects.filter(user=user, is_active=True, scope_type=ScopeType.GLOBAL, role__in=ADMIN_ROLES)
                      .values_list("role", flat=True)))


def admin_areas(user) -> list[str]:
    """Admin areas the caller can open (spec 11 §7, `/me.admin_areas`). Empty = not an admin."""
    roles = admin_roles(user)
    if not roles:
        return []
    sysadmin = Role.SYSTEM_ADMIN in roles
    areas = ["overview"]
    if sysadmin:
        areas.append("candidates")
    if has_role(user, Role.FINANCE_OFFICER):
        areas.append("donations")
    if has_role(user, Role.PVC_OFFICER) or has_role(user, Role.MEMBERSHIP_OFFICER):
        areas.append("pvc")
    if has_role(user, Role.MEMBERSHIP_OFFICER):
        areas.append("membership")
    if sysadmin or has_role(user, Role.MODERATOR) or has_role(user, Role.NEWS_REVIEWER):
        areas.append("moderation")  # spec 13 §4.9
    if has_role(user, Role.NEWS_REVIEWER):
        areas.append("news")
    if sysadmin:
        areas += ["announcements", "posts", "team", "audit"]
    return areas


def _summary(app: str, user, roles: list[str], now):
    mod = importlib.import_module(f"apps.{app}.dashboard")
    cacheable = getattr(mod, "CACHEABLE", True)
    key = f"admin-summary:{app}:{','.join(roles)}"  # role set only — never a user ID (spec 11 §6.2)
    if cacheable:
        hit = cache.get(key)
        if hit is not None:
            return hit
    items = [x.finish(now) if isinstance(x, Card) else x for x in mod.admin_summary(user, now=now)]
    data = [to_dict(x) for x in items]
    if cacheable:
        cache.set(key, data, CACHE_SECONDS)
    return data


def _blackout_payload(w, now):
    from apps.candidates.reference import Position

    labels = []
    for p in w["positions"]:
        try:
            labels.append(Position(p).label)
        except ValueError:
            labels.append(p)
    state = "active" if w["start"] <= now <= w["end"] else "upcoming" if now < w["start"] else "past"
    return {"offices": labels, "start": w["start"], "end": w["end"], "state": state}


def blackout_banners(now):
    ws = [_blackout_payload(w, now) for w in blackouts.windows()]
    active = [w for w in ws if w["state"] == "active"]
    soon = sorted((w for w in ws if w["state"] == "upcoming" and w["start"] - now <= timedelta(days=7)), key=lambda w: w["start"])
    return {"active": active, "next": soon[0] if soon else None}


def _item(group, label, state, detail=""):
    return {"group": group, "label": label, "state": state, "ok": state == "ready", "detail": detail}


def readiness(now):
    """Booleans and names only — never a setting's value (spec 11 §3, §4.2)."""
    from apps.accounts.models import ProcessedWebhook
    from apps.donations.services import donations_open
    from apps.donations.services import gate_status as donation_gates
    from apps.membership.services import gate_status as membership_gates
    from apps.membership.services import membership_open

    out = []
    for name, ok in donation_gates().items():
        out.append(_item("donations", name, "ready" if ok else "not_set"))
    out.append(_item("donations", "Donations are open" if donations_open() else "Donations are closed", "ready" if donations_open() else "info"))
    for name, ok in membership_gates().items():
        out.append(_item("membership", name, "ready" if ok else "not_set"))
    out.append(_item("membership", "Membership is open" if membership_open() else "Membership is closed",
                     "ready" if membership_open() else "info"))
    clerk_ok = bool(settings.CLERK_SECRET_KEY or settings.CLERK_JWT_KEY)
    out.append(_item("signin", "Clerk keys configured", "ready" if clerk_ok else "not_set"))
    out.append(_item("signin", "Clerk webhook signing secret", "ready" if settings.CLERK_WEBHOOK_SIGNING_SECRET else "not_set"))
    last = ProcessedWebhook.objects.order_by("-received_at").first()
    if last is None:
        out.append(_item("signin", "Last Clerk webhook", "warning", "None received yet"))
    else:
        stale = now - last.received_at > timedelta(days=7)
        out.append(_item("signin", "Last Clerk webhook", "warning" if stale else "ready",
                         f"{last.event_type} · {timezone.localtime(last.received_at):%d %b %Y, %H:%M}"))
    provider = settings.PAYMENT_PROVIDER
    if provider == "fake":
        out.append(_item("payments", "Payment provider", "ready" if settings.DEBUG else "error",
                         "Fake provider in use" + (" (development)" if settings.DEBUG else "")))
    else:
        out.append(_item("payments", "Payment provider", "ready", provider.capitalize()))
    out.append(_item("inec", "INEC PVC portal link", "ready" if settings.INEC_PVC_URL else "not_set"))
    out.append(_item("inec", "INEC voter checker link", "ready" if settings.INEC_VOTER_CHECK_URL else "not_set"))
    keys = [k for k in settings.PII_ENCRYPTION_KEYS if k.strip()]
    out.append(_item("security", "Personal data encryption key", "ready" if keys else "warning" if settings.DEBUG else "not_set",
                     "" if keys else "Development key" if settings.DEBUG else ""))
    backend = settings.STORAGES.get("default", {}).get("BACKEND", "") if hasattr(settings, "STORAGES") else ""
    local = backend.endswith("FileSystemStorage") or not backend
    out.append(_item("media", "Media storage", "warning" if local and not settings.DEBUG else "ready",
                     "Local disk" + (" (development)" if settings.DEBUG and local else "") if local else "Production store"))
    from apps.search.index import health as search_health

    h = search_health()  # spec 12 §6.6: entries vs public items, within 1%
    out.append(_item("search", "Search index", "ready" if h["ok"] else "warning",
                     f"{h['entries']} of {h['public']} public items" + ("" if h["ok"] else " · run rebuild_search")))
    return {"items": out, "blackouts": [_blackout_payload(w, now) for w in blackouts.windows()]}


def activity():
    rows = AuditLog.objects.exclude(action__startswith="audit.").select_related("actor").order_by("-created_at")[:15]
    return [entry_payload(e) for e in rows]


def overview(user, now=None):
    now = now or timezone.now()
    roles = admin_roles(user)
    cards, stats, failed = [], [], []
    for app in SUMMARY_APPS:
        try:
            for item in _summary(app, user, roles, now):
                (cards if item["type"] == "card" else stats).append(item)
        except Exception:  # one app failing never blanks the page (spec 11 §11)
            logger.exception("Admin summary failed for %s", app)
            failed.append({"app": app, "label": APP_LABELS[app]})
    cards.sort(key=lambda c: CARD_ORDER.index(c["key"]) if c["key"] in CARD_ORDER else 99)
    groups = []
    for key, label in GROUPS:
        items = [s for s in stats if s["group"] == key]
        if items:
            groups.append({"group": key, "label": label, "stats": items})
    data = {
        "generated_at": now,
        "roles": [{"role": r, "label": Role(r).label} for r in roles],
        "attention": cards,
        "stats": groups,
        "failed": failed,
        "blackouts": blackout_banners(now),
    }
    if is_system_admin(user):
        data["readiness"] = readiness(now)
        data["jobs"] = jobs.latest_runs(now)
        data["activity"] = activity()
    return data
