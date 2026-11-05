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
