"""
Weekly data-integrity check (spec 16 §11.2). Counts only, never contents. The last result is kept in the shared cache
for readiness and the admin dashboard; the job run records the summary.

    python manage.py check_integrity                          # all checks
    python manage.py check_integrity --constraint <name>      # IDs of rows that break one constraint (for fixing)
"""
from datetime import timedelta

from django.apps import apps
from django.core.cache import cache
from django.db import connection, models
from django.utils import timezone

CACHE_KEY = "integrity:last"
CASE_TARGETS = {"article": ("news", "Article"), "post": ("posts", "CandidatePost"), "candidate": ("candidates", "Candidate")}
IMAGE_FIELDS = [("candidates", "Candidate", "photo_800", "photo_asset"), ("news", "Article", "image", "image_asset"),
                ("posts", "CandidatePost", "image", "image_asset")]


def _orphan_targets():
    from apps.moderation.models import Case, Report

    # Closed cases may outlive their item: prune_news deletes rejected articles after 180 days, prune_moderation deletes
    # closed cases after a year. Only cases still being worked (and their reports) pointing at nothing are a problem.
    n = 0
    for kind, (app, model) in CASE_TARGETS.items():
        ids = apps.get_model(app, model).objects.values("pk")
        n += Case.objects.filter(target_kind=kind).exclude(status="closed").exclude(target_id__in=ids).count()
        n += Report.objects.filter(target_kind=kind).exclude(case__status="closed").exclude(target_id__in=ids).count()
    return n


def _search_not_public():
    from apps.search import index
    from apps.search.models import SearchEntry

    return sum(SearchEntry.objects.filter(kind=k).exclude(object_id__in=index.public_ids(k)).count() for k in index.SEARCHABLE)


def _candidate_roles_missing():
    from apps.access.models import RoleAssignment, ScopeType
    from apps.candidates.models import Candidate

    return (RoleAssignment.objects.filter(scope_type=ScopeType.CANDIDATE, is_active=True)
            .exclude(scope_id__in=Candidate.objects.values("pk")).count())


def _images_without_asset():
    n = 0
    for app, model, name, fk in IMAGE_FIELDS:
        n += apps.get_model(app, model).objects.exclude(**{name: ""}).filter(**{f"{fk}__isnull": True}).count()
    return n


def _assets_unlinked(now):
    from apps.media.models import MediaAsset

    qs = MediaAsset.objects.filter(delivery=MediaAsset.Delivery.PUBLIC, deleted_at__isnull=True, created_at__lt=now - timedelta(days=1))
    for app, model, _name, fk in IMAGE_FIELDS:
        qs = qs.exclude(pk__in=apps.get_model(app, model).objects.filter(**{f"{fk}__isnull": False}).values(fk))
    return qs.count()


def _undecryptable():
    from apps.core import crypto
    from apps.membership.models import Membership
    from apps.pvc.models import PvcVerification

    bad = 0
    for M, f in ((Membership, "nin_encrypted"), (PvcVerification, "vin_encrypted")):
        for token in M.objects.exclude(**{f: b""}).values_list(f, flat=True).iterator():
            try:
                crypto._fernet().decrypt(bytes(token))
            except Exception:
                bad += 1
    return bad


def _usable_passwords():
    from apps.accounts.models import User

    return User.objects.exclude(password="").exclude(password__startswith="!").count()


def _extra_settings_rows():
    from apps.membership.models import MembershipSettings
    from apps.pvc.models import PvcSettings

    return max(0, MembershipSettings.objects.count() - 1) + max(0, PvcSettings.objects.count() - 1)


def _not_valid_constraints():
    with connection.cursor() as cur:
        cur.execute("SELECT count(*) FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace "
                    "WHERE n.nspname = current_schema() AND NOT c.convalidated")
        return cur.fetchone()[0]


def _community_chain_broken():
    """Spec 17 §12.4: an active lower-level membership without the matching active parent memberships."""
    with connection.cursor() as cur:
        cur.execute("""
            SELECT count(*) FROM communities_communitymembership m JOIN communities_community c ON c.id = m.community_id
            WHERE m.status = 'active' AND m.level IN ('lga', 'ward', 'polling_unit') AND NOT EXISTS (
              SELECT 1 FROM communities_communitymembership p WHERE p.user_id = m.user_id AND p.status = 'active'
              AND p.community_id = c.parent_id)""")
        return cur.fetchone()[0]


def _community_inactive_members():
    from apps.communities.models import CommunityMembership

    return CommunityMembership.objects.filter(status="active", community__active=False).count()


def _community_counts_drift():
    """Recount members, posts, comments and likes; fix the stored counts in place and report how many were wrong."""
    from django.db.models import Count, F, OuterRef, Q, Subquery
    from django.db.models.functions import Coalesce

    from apps.communities.models import Community, CommunityPost

    def sub(model, fk, extra):
        return Coalesce(Subquery(model.objects.filter(**{fk: OuterRef("pk")}, **extra).order_by().values(fk)
                                 .annotate(n=Count("pk")).values("n")[:1]), 0)

    from apps.communities.models import CommunityComment, CommunityMembership, CommunityReaction

    n = 0
    comms = Community.objects.annotate(m=sub(CommunityMembership, "community", {"status": "active"}),
                                       p=sub(CommunityPost, "community", {"status": "published"}))
    for c in comms.filter(~Q(member_count=F("m")) | ~Q(post_count=F("p"))):
        Community.objects.filter(pk=c.pk).update(member_count=c.m, post_count=c.p)
        n += 1
    posts = CommunityPost.objects.annotate(cm=sub(CommunityComment, "post", {"status": "published"}),
                                           r=sub(CommunityReaction, "post", {}))
    for p in posts.filter(~Q(comment_count=F("cm")) | ~Q(reaction_count=F("r"))):
        CommunityPost.objects.filter(pk=p.pk).update(comment_count=p.cm, reaction_count=p.r)
        n += 1
    return n


def _community_hidden_overdue(now):
    from apps.communities.models import CommunityComment, CommunityPost

    cutoff = now - timedelta(hours=48)
    return (CommunityPost.objects.filter(status="hidden", hidden_at__lt=cutoff).count()
            + CommunityComment.objects.filter(status="hidden", hidden_at__lt=cutoff).count())


def _event_counts_drift():
    """Recount registrations; fix the stored counts in place and report how many events were wrong (spec 18 §12.5)."""
    from django.db.models import Count, F, Q

    from apps.events.models import Event

    n = 0
    qs = Event.objects.annotate(r=Count("registrations", filter=Q(registrations__status="registered")),
                                w=Count("registrations", filter=Q(registrations__status="waitlisted")),
                                c=Count("registrations", filter=Q(registrations__checked_in_at__isnull=False)))
    for e in qs.filter(~Q(registered_count=F("r")) | ~Q(waitlist_count=F("w")) | ~Q(checked_in_count=F("c"))):
        Event.objects.filter(pk=e.pk).update(registered_count=e.r, waitlist_count=e.w, checked_in_count=e.c)
        n += 1
    return n


def _events_over_capacity():
    from django.db.models import Count, F, Q

    from apps.events.models import Event

    return Event.objects.filter(capacity__isnull=False).annotate(
        r=Count("registrations", filter=Q(registrations__status="registered"))).filter(r__gt=F("capacity")).count()


def _waitlist_with_free_places(now):
    from django.db.models import Count, F, Q

    from apps.events.models import Event

    qs = Event.objects.filter(status="published", starts_at__gt=now).annotate(
        r=Count("registrations", filter=Q(registrations__status="registered")),
        w=Count("registrations", filter=Q(registrations__status="waitlisted")))
    return qs.filter(w__gt=0).filter(Q(capacity__isnull=True) | Q(r__lt=F("capacity"))).count()


def _volunteers_unpublished():
    from django.db.models import Q

    from apps.volunteers.models import VolunteerApplication

    return VolunteerApplication.objects.filter(status__in=["pending", "accepted"]).filter(
        ~Q(candidate__status="published") | Q(candidate__verified_at__isnull=True)).count()


def _geo_chain_broken():
    """Spec 19 §12.4: a record's ward link outside its LGA link, or its polling-unit link outside its ward link."""
    from django.db.models import F, Q

    from apps.candidates.models import Candidate
    from apps.membership.models import Membership
    from apps.pvc.models import PvcVerification

    n = 0
    for M in (Membership, PvcVerification):
        n += M.objects.filter(Q(geo_ward__isnull=False, geo_lga__isnull=False) & ~Q(geo_ward__lga=F("geo_lga"))).count()
        n += M.objects.filter(Q(geo_polling_unit__isnull=False, geo_ward__isnull=False) & ~Q(geo_polling_unit__ward=F("geo_ward"))).count()
        n += M.objects.filter(Q(geo_lga__isnull=False) & ~Q(geo_lga__state__slug=F("state"))).count()
    n += Candidate.objects.filter(Q(geo_ward__isnull=False, geo_lga__isnull=False) & ~Q(geo_ward__lga=F("geo_lga"))).count()
    return n


def _geo_active_under_inactive():
    from apps.geo.models import PollingUnit, Ward

    return (Ward.objects.filter(active=True, lga__active=False).count()
            + PollingUnit.objects.filter(active=True, ward__active=False).count())


def _geo_areas_inactive():
    from django.db.models import Q

    from apps.geo.models import ConstituencyArea

    return ConstituencyArea.objects.filter(constituency__active=True).filter(Q(lga__active=False) | Q(ward__active=False)).count()


def _geo_officer_without_unit():
    from apps.pvc.models import PvcVerification

    return PvcVerification.objects.filter(geo_source="officer", geo_polling_unit__isnull=True).count()


CHECKS = [
    ("orphan_case_targets", "Open moderation cases or reports pointing at missing items", lambda now: _orphan_targets()),
    ("search_not_public", "Search entries for items that aren't public", lambda now: _search_not_public()),
    ("candidate_roles_missing", "Candidate roles for missing candidates", lambda now: _candidate_roles_missing()),
    ("images_without_asset", "Images with no asset record", lambda now: _images_without_asset()),
    ("assets_unlinked", "Public image assets linked to nothing for over a day", _assets_unlinked),
    ("undecryptable", "Encrypted NINs or VINs the current keys can't read", lambda now: _undecryptable()),
    ("usable_passwords", "Accounts with a usable password (Clerk signs everyone in)", lambda now: _usable_passwords()),
    ("extra_settings_rows", "Extra settings rows", lambda now: _extra_settings_rows()),
    ("not_valid_constraints", "Constraints not yet validated", lambda now: _not_valid_constraints()),
    ("community_chain_broken", "Community memberships without their parent levels", lambda now: _community_chain_broken()),
    ("community_inactive_members", "Members still in closed communities", lambda now: _community_inactive_members()),
    ("community_counts_drift", "Community counts that were wrong (now fixed)", lambda now: _community_counts_drift()),
    ("community_hidden_overdue", "Community items hidden for over 48 hours", _community_hidden_overdue),
    ("event_counts_drift", "Event registration counts that were wrong (now fixed)", lambda now: _event_counts_drift()),
    ("events_over_capacity", "Events with more people registered than their capacity", lambda now: _events_over_capacity()),
    ("event_waitlist_with_places", "Waitlisted people on events with free places", _waitlist_with_free_places),
    ("volunteers_unpublished_candidates", "Open volunteer applications for unlisted candidates", lambda now: _volunteers_unpublished()),
    # spec 19 §12.4
    ("geo_chain_broken", "Location links whose ward, LGA or state don't belong together", lambda now: _geo_chain_broken()),
    ("geo_active_under_inactive", "Active wards or polling units under a retired parent", lambda now: _geo_active_under_inactive()),
    ("geo_areas_inactive", "Constituencies covering a retired LGA or ward", lambda now: _geo_areas_inactive()),
    ("geo_officer_without_unit", "Officer-confirmed PVC locations with no polling unit", lambda now: _geo_officer_without_unit()),
]


def summary(r) -> str:
    n, e = r.get("issues", 0), r.get("errors", 0)
    return f"{n} issue{'' if n == 1 else 's'}" + (f", {e} check{'' if e == 1 else 's'} failed" if e else "")


def run(now=None) -> dict:
    now = now or timezone.now()
    counts = {}
    for key, _label, fn in CHECKS:
        try:
            counts[key] = int(fn(now))
        except Exception:  # one failing check never hides the others
            counts[key] = -1
    result = {"checked_at": now.isoformat(), "counts": counts, "issues": sum(v for v in counts.values() if v > 0),
              "errors": sum(1 for v in counts.values() if v < 0)}
    try:
        cache.set(CACHE_KEY, result, None)
    except Exception:
        pass
    return result


def last():
    try:
        return cache.get(CACHE_KEY)
    except Exception:
        return None


# ------------------------------------------------------------------ one constraint's offending rows (§10.4)
def find_constraint(name):
    for m in apps.get_models():
        for c in m._meta.constraints:
            if c.name == name:
                return m, c
    return None, None


def violating_ids(name, limit=1000):
    m, c = find_constraint(name)
    if m is None:
        raise LookupError(f"No constraint named {name}")
    if not isinstance(c, models.CheckConstraint):
        raise LookupError(f"{name} isn't a check constraint; duplicates are found by its fields instead")
    from django.db.models.sql import Query

    q = Query(model=m, alias_cols=False)
    where = q.build_where(c.condition)
    compiler = q.get_compiler(connection=connection)
    sql, params = where.as_sql(compiler, connection)
    table = connection.ops.quote_name(m._meta.db_table)
    pk = connection.ops.quote_name(m._meta.pk.column)
    with connection.cursor() as cur:
        cur.execute(f"SELECT {pk} FROM {table} WHERE NOT ({sql}) ORDER BY {pk} LIMIT %s", [*params, limit])
        return m._meta.label, [str(r[0]) for r in cur.fetchall()]
