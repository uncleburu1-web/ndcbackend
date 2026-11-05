"""Exact (folded) matching of free text to the geography tables. Links are only ever added, never overwritten."""
from django.db import transaction

from apps.search.text import fold

from .models import Lga, PollingUnit, Ward


def _key(s):
    return fold(s or "").lower()


def _match_one(state_slug, lga_text, ward_text, pu_text):
    lga = ward = pu = None
    if state_slug and lga_text:
        lga = Lga.objects.filter(state__slug=state_slug, folded_name=_key(lga_text), active=True).first()
    if lga and ward_text:
        ward = Ward.objects.filter(lga=lga, folded_name=_key(ward_text), active=True).first()
    if lga and pu_text:
        q = PollingUnit.objects.filter(ward__lga=lga, active=True)
        q = q.filter(ward=ward) if ward else q
        hits = list(q.filter(folded_name=_key(pu_text))[:2])
        pu = hits[0] if len(hits) == 1 else None  # ambiguous names stay unlinked
        if pu and not ward:
            ward = pu.ward
    return lga, ward, pu


def match_all(dry_run=False) -> dict:
    from apps.candidates.models import Candidate
    from apps.membership.models import Membership
    from apps.pvc.models import PvcVerification

    out = {}
    targets = [("memberships", Membership, "lga", "ward", "polling_unit"),
               ("pvc_records", PvcVerification, "lga", None, "polling_unit"),
               ("candidates", Candidate, "area_name", "ward_name", None)]
    with transaction.atomic():
        for name, M, lf, wf, pf in targets:
            r = {"checked": 0, "lga": 0, "ward": 0, "polling_unit": 0}
            qs = M.objects.filter(geo_lga__isnull=True).exclude(**{lf: ""})
            for obj in qs.iterator():
                r["checked"] += 1
                lga, ward, pu = _match_one(obj.state, getattr(obj, lf), getattr(obj, wf) if wf else "", getattr(obj, pf) if pf else "")
                fields = {}
                if lga:
                    fields["geo_lga"] = lga
                    r["lga"] += 1
                if ward:
                    fields["geo_ward"] = ward
                    r["ward"] += 1
                if pu and hasattr(obj, "geo_polling_unit"):
                    fields["geo_polling_unit"] = pu
                    r["polling_unit"] += 1
                if fields and not dry_run:
                    M.objects.filter(pk=obj.pk).update(**fields)
                    if name == "pvc_records":  # a new link can place a verified voter in communities (spec 17 §5.2)
                        from apps.core import outbox

                        outbox.enqueue("communities_place", {"user": str(obj.user_id)}, dedupe_key=f"communities_place:{obj.user_id}",
                                       kick=False)
            out[name] = r
        if dry_run:
            transaction.set_rollback(True)
    return out
