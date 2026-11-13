"""Create communities from geography (spec 17 §4, §12.2). Idempotent; never deletes; deactivates when geography does."""
from dataclasses import dataclass, field

from django.db import transaction
from django.utils.text import slugify

from .models import Community, Level, ReadScope

BATCH = 2000


@dataclass
class Result:
    created: dict = field(default_factory=lambda: {lv: 0 for lv in Level.values})
    updated: int = 0
    deactivated: int = 0
    placements: int = 0


def state_path(slug):
    return slug.replace("_", "-")


def state_name(slug, label):
    return "Federal Capital Territory" if slug == "fct" else f"{label} State"


def _apply(existing, key, want, res, level, new):
    """existing: {key: Community}. Create or update one row's name/path/parent/active."""
    c = existing.get(key)
    if c is None:
        new.append(Community(level=level, **want))
        res.created[level] += 1
        return None
    changed = [f for f, v in want.items() if getattr(c, f if not f.endswith("_id") else f) != v]
    if changed:
        if "active" in changed and not want["active"]:
            res.deactivated += 1
        for f in changed:
            setattr(c, f, want[f])
        res.updated += 1
        return c
    return None


def sync(dry_run=False) -> Result:
    from apps.candidates.reference import State
    from apps.geo.models import Lga, PollingUnit, Ward

    res = Result()
    with transaction.atomic():
        nat = Community.objects.filter(level=Level.NATIONAL).first()
        if nat is None:
            nat = Community.objects.create(level=Level.NATIONAL, state="", name="Nigeria", path="national")
            res.created[Level.NATIONAL] += 1
        states = {c.state: c for c in Community.objects.filter(level=Level.STATE)}
        for slug, label in State.choices:
            if slug not in states:
                states[slug] = Community.objects.create(level=Level.STATE, state=slug, name=state_name(slug, label), path=state_path(slug),
                                                        parent=nat)
                res.created[Level.STATE] += 1

        # LGAs: path segment is the slug of the name, or the INEC code when two LGAs in a state slug the same
        lgas = list(Lga.objects.select_related("state").order_by("state__code", "code"))
        seen, slugs = {}, {}
        for lga in lgas:
            s = slugify(lga.name) or lga.code
            slugs.setdefault((lga.state.slug, s), []).append(lga.pk)
        existing = {c.geo_lga_id: c for c in Community.objects.filter(level=Level.LGA)}
        new, dirty = [], []
        for lga in lgas:
            s = slugify(lga.name) or lga.code
            seg = s if len(slugs[(lga.state.slug, s)]) == 1 else lga.code
            parent = states[lga.state.slug]
            want = {"geo_lga_id": lga.pk, "state": lga.state.slug, "parent_id": parent.pk, "name": f"{lga.name} LGA",
                    "path": f"{parent.path}/{seg}", "active": lga.active, "read_scope": ReadScope.MEMBERS}
            if lga.pk in existing:
                want.pop("read_scope")  # an admin may have changed it
            c = _apply(existing, lga.pk, want, res, Level.LGA, new)
            if c:
                dirty.append(c)
            seen[lga.pk] = None
        Community.objects.bulk_create(new, batch_size=BATCH)
        Community.objects.bulk_update(dirty, ["state", "parent_id", "name", "path", "active"], batch_size=BATCH)
        lga_comm = {c.geo_lga_id: c for c in Community.objects.filter(level=Level.LGA)}

        wards = list(Ward.objects.select_related("lga").order_by("lga_id", "code"))
        existing = {c.geo_ward_id: c for c in Community.objects.filter(level=Level.WARD)}
        new, dirty = [], []
        for w in wards:
            parent = lga_comm[w.lga_id]
            want = {"geo_ward_id": w.pk, "state": parent.state, "parent_id": parent.pk, "name": f"Ward {w.code} · {w.name}",
                    "path": f"{parent.path}/{w.code}", "active": w.active and parent.active, "read_scope": ReadScope.MEMBERS}
            if w.pk in existing:
                want.pop("read_scope")
            c = _apply(existing, w.pk, want, res, Level.WARD, new)
            if c:
                dirty.append(c)
        Community.objects.bulk_create(new, batch_size=BATCH)
        Community.objects.bulk_update(dirty, ["state", "parent_id", "name", "path", "active"], batch_size=BATCH)
        ward_comm = {c.geo_ward_id: c for c in Community.objects.filter(level=Level.WARD)}

        existing = {c.geo_polling_unit_id: c for c in Community.objects.filter(level=Level.POLLING_UNIT)}
        new, dirty = [], []
        for pu in PollingUnit.objects.order_by("ward_id", "code").iterator(chunk_size=BATCH):
            parent = ward_comm[pu.ward_id]
            want = {"geo_polling_unit_id": pu.pk, "state": parent.state, "parent_id": parent.pk,
                    "name": f"Polling unit {pu.code} · {pu.name}", "path": f"{parent.path}/{pu.code}", "active": pu.active and parent.active,
                    "read_scope": ReadScope.MEMBERS}
            if pu.pk in existing:
                want.pop("read_scope")
            c = _apply(existing, pu.pk, want, res, Level.POLLING_UNIT, new)
            if c:
                dirty.append(c)
            if len(new) >= BATCH:
                Community.objects.bulk_create(new, batch_size=BATCH)
                new = []
        Community.objects.bulk_create(new, batch_size=BATCH)
        Community.objects.bulk_update(dirty, ["state", "parent_id", "name", "path", "active"], batch_size=BATCH)
        if dry_run:
            transaction.set_rollback(True)
    return res


def run_placements(batch=500) -> int:
    """Re-run placement for everyone with the consent and a successful PVC check (nightly safety net, §5.2)."""
    from apps.privacy.models import ConsentRecord
    from apps.pvc.models import PvcVerification

    from . import services

    consented = ConsentRecord.objects.filter(purpose=services.PURPOSE, withdrawn_at__isnull=True).values("user_id")
    users = (PvcVerification.objects.filter(status="successful", user_id__in=consented).values_list("user_id", flat=True))
    from apps.accounts.models import User

    from .models import CommunityMembership, How

    ids = set(users) | set(CommunityMembership.objects.filter(status="active", how=How.VERIFIED).values_list("user_id", flat=True))
    n = 0
    for u in User.objects.filter(pk__in=ids, is_active=True).iterator(chunk_size=batch):
        services.place(u)
        n += 1
    return n
