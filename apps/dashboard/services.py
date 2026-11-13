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
SUMMARY_APPS = ["privacy", "moderation", "communities", "events", "geo", "news", "posts", "pvc", "membership", "donations", "candidates", "follows", "accounts",
                "integrations"]
CARD_ORDER = ["audit_chain_broken", "incident_deadlines", "privacy_requests_due", "reports_review", "reports_urgent", "appeals_waiting", "legal_due", "community_hidden_waiting", "events_reported", "events_hidden_waiting", "geo_review_waiting", "geo_units_changed", "geo_preview_waiting", "news_pending", "pvc_pending", "membership_approvals", "membership_payments_review",
              "donations_review", "candidates_unpublished", "ban_failed", "background_tasks_failing", "data_integrity_issues", "communities_unmoderated", "events_in_blackout", "access_review_due"]
GROUPS = [("people", "People"), ("candidates", "Candidates and following"), ("content", "Content"), ("pvc", "PVC"),
          ("membership", "Membership"), ("donations", "Donations"), ("settings", "Settings"),
          ("languages", "Languages")]
ADMIN_ROLES = {Role.SYSTEM_ADMIN, Role.NEWS_REVIEWER, Role.PVC_OFFICER, Role.MEMBERSHIP_OFFICER, Role.FINANCE_OFFICER, Role.MODERATOR,
               Role.PRIVACY_OFFICER}
CACHE_SECONDS = 30
APP_LABELS = {"privacy": "Privacy and security", "moderation": "Moderation", "communities": "Communities", "events": "Events", "geo": "Geography", "news": "News", "posts": "Candidate posts", "pvc": "PVC checks", "membership": "Membership", "donations": "Donations",
              "candidates": "Candidates", "follows": "Following", "accounts": "Accounts", "integrations": "Integrations"}


def admin_roles(user) -> list[str]:
    if not (user and user.is_authenticated and user.is_active):
        return []
    roles = set(RoleAssignment.objects.filter(user=user, is_active=True, scope_type=ScopeType.GLOBAL, role__in=ADMIN_ROLES)
                .values_list("role", flat=True))
    if RoleAssignment.objects.filter(user=user, is_active=True, role=Role.MODERATOR, scope_type=ScopeType.COMMUNITY).exists():
        roles.add(Role.MODERATOR)  # a community moderator (spec 17 §8.2)
    return sorted(roles)


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
    if has_role(user, Role.PVC_OFFICER) or has_role(user, Role.MEMBERSHIP_OFFICER):
        areas.append("geography")  # spec 19 §15.1: system admins see it all; officers the review tab
    if sysadmin or Role.MODERATOR in roles or has_role(user, Role.NEWS_REVIEWER):
        areas.append("moderation")  # spec 13 §4.9; community moderators too (spec 17)
    if has_role(user, Role.NEWS_REVIEWER):
        areas.append("news")
    if has_role(user, Role.PRIVACY_OFFICER):
        areas.append("privacy")  # spec 14 §4.13 (implied by system admin)
    if sysadmin:
        areas += ["announcements", "posts", "team", "audit", "integrations", "communities", "events", "settings", "languages"]  # 15; 17; 18; 20; 21
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
    from apps.integrations.models import WebhookReceipt

    last = WebhookReceipt.objects.filter(provider="clerk", signature_valid=True).order_by("-received_at").first()
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
    local = getattr(settings, "MEDIA_BACKEND", "local") != "cloudinary"  # spec 15 §7
    out.append(_item("media", "Media storage", "warning" if local and not settings.DEBUG else "ready",
                     "Local disk" + (" (development)" if settings.DEBUG and local else "") if local else "Cloudinary"))
    # spec 15 §12.1: one line per external service, in words the frontend renders; never a setting's value
    from apps.core import outbox
    from apps.integrations import registry

    words = {"ok": "ready", "failing": "warning", "unavailable": "error", "not_configured": "not_set", "not_in_use": "info"}
    for svc in registry.snapshot():
        detail = {"not_in_use": "Not in use", "unavailable": "Unavailable (circuit open)", "failing": "Last call failed"}.get(svc["state"], "")
        out.append(_item("integrations", svc["name"], words[svc["state"]], detail))
    st = outbox.stats(now)
    stuck = st["oldest_due_age_s"] >= outbox.STUCK_AFTER.total_seconds()
    out.append(_item("integrations", "Background tasks", "error" if stuck else "warning" if st["failed"] else "ready",
                     f"Oldest waiting {st['oldest_due_age_s'] // 60} min" if stuck else f"{st['failed']} failed" if st["failed"] else ""))
    from apps.core.ready import cache_is_shared

    out.append(_item("integrations", "Shared cache", "ready" if cache_is_shared() else "info" if settings.DEBUG else "error",
                     "" if cache_is_shared() else "Per-process memory" + (" (development)" if settings.DEBUG else "")))
    from apps.core import integrity  # spec 16 §11.2

    li = integrity.last()
    if li is None:
        out.append(_item("security", "Data integrity", "warning", "Not checked yet · run check_integrity"))
    else:
        bad = li.get("issues", 0) or li.get("errors", 0)
        out.append(_item("security", "Data integrity", "warning" if bad else "ready",
                         integrity.summary(li) if bad else ""))
    # spec 17 §15.3
    from apps.access.models import RoleAssignment as RA
    from apps.core.models import JobRun

    run = JobRun.objects.filter(job="sync_communities", ok=True).order_by("-finished_at").first()
    out.append(_item("security", "Communities synced", "ready" if run else "warning",
                     (run.summary[:120] if run and run.summary else "") if run else "Not run yet · run sync_communities"))
    mods = RA.objects.filter(role=Role.MODERATOR, scope_type=ScopeType.GLOBAL, is_active=True, user__is_active=True).exists()
    out.append(_item("security", "Moderators in place", "ready" if mods else "warning", "" if mods else "No platform-wide moderator yet"))
    # spec 18 §15.3
    from apps.core import blackouts

    unset = blackouts.unset_period_offices()
    out.append(_item("security", "Campaign periods", "warning" if len(unset) == 7 else "ready",
                     ("Not set for " + ", ".join(unset)) if unset else ""))
    out += geo_readiness(now)
    tick = JobRun.objects.filter(job="events_tick", ok=True).order_by("-finished_at").first()
    fresh = bool(tick and tick.finished_at and tick.finished_at >= now - timedelta(minutes=30))
    out.append(_item("security", "Events jobs", "ready" if fresh else "warning", "" if fresh else "events_tick hasn't run in 30 minutes"))
    from apps.search.index import health as search_health

    h = search_health()  # spec 12 §6.6: entries vs public items, within 1%
    out.append(_item("search", "Search index", "ready" if h["ok"] else "warning",
                     f"{h['entries']} of {h['public']} public items" + ("" if h["ok"] else " · run rebuild_search")))
    from apps.privacy import checks as sec

    words = {"PASS": "ready", "WARN": "warning", "FAIL": "error"}
    memo = {}
    audit_state, audit_words = sec.audit_state(now, memo)
    restore_state, restore_words = sec.restore_state(now, memo)
    for name, state in sec.run(now, memo):
        if name == "Audit log chain intact":
            out.append(_item("security", audit_words, words[audit_state]))
        elif name == "Restore test within 100 days":
            out.append(_item("security", restore_words, words[restore_state]))
        elif name == "Admin two-step verification claim configured" and state != "PASS":
            out.append(_item("security", "Admin two-step verification: claim missing" if state == "FAIL" else
                             "Admin two-step verification: no admin sign-in yet", words[state]))
        else:
            out.append(_item("security", name, words[state]))
    return {"items": out, "blackouts": [_blackout_payload(w, now) for w in blackouts.windows()]}


def geo_readiness(now):
    """Spec 19 §15.3: which geography is loaded, from where, and how old it is. Never hard-codes a number of units."""
    from django.conf import settings

    from apps.geo.models import Dataset
    from apps.geo.services import current_datasets, is_sample_only

    cur = {d.kind: d for d in current_datasets()}
    pu = cur.get(Dataset.Kind.POLLING_UNITS)
    when = lambda d: (d.published_on or d.applied_at.date()).isoformat()  # noqa: E731
    if pu is None:
        out = [_item("geography", "Geography", "error", "No polling-unit list loaded · run load_geography")]
    else:
        detail = f"{pu.polling_units} polling units · {pu.source[:80]} · {when(pu)}"
        sample = is_sample_only()
        old = (pu.published_on or pu.applied_at.date()) < (now - timedelta(days=365)).date()
        state = "error" if sample and not settings.DEBUG else "warning" if sample or old else "ready"
        out = [_item("geography", "Geography", state, ("Only the sample is loaded · " if sample else "")
                     + ("Older than 12 months · " if old else "") + detail)]
    missing = [k.label for k in Dataset.Kind if k != Dataset.Kind.POLLING_UNITS and k not in cur]
    out.append(_item("geography", "Constituencies", "warning" if missing else "ready",
                     ("Not loaded: " + ", ".join(missing).lower()) if missing else ""))
    return out


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
