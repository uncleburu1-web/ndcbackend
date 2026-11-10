"""
People's location links (spec 19 §7.3, §8, §10). Where someone votes is personal data (rule 5): these functions are
called only from the person's own `/me` views, the membership and PVC forms, and officers' review queue. Audit entries
carry the record kind and ID, never place names.

Placement in communities (17) stays with `pvc.services.verified_location`, which ignores links the person chose on a
verified check until an officer accepts them (§8.1).
"""
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.core.exceptions import ServiceError

from . import services
from .models import Change, Lga, PollingUnit

KINDS = ("membership", "pvc")
BATCH = 500


def place(lga, ward, pu):
    """The person's own view of a link: names, codes, and whether INEC still lists it."""
    def one(obj, code):
        return {"id": obj.pk, "name": obj.name, "code": code, "active": obj.active} if obj else None

    return {"lga": one(lga, lga.code if lga else ""), "ward": one(ward, ward.code if ward else ""),
            "polling_unit": one(pu, pu.delimitation if pu else "")}


def _models():
    from apps.membership.models import GeoSource as MS, Membership
    from apps.pvc.models import GeoSource as PS, PvcVerification

    return {"membership": (Membership, MS), "pvc": (PvcVerification, PS)}


def read_choice(data):
    """IDs and the "My place isn't listed" flag from request data. Returns None when the client sent neither."""
    ids = services.chain_ids(data)
    not_listed = bool(data.get("geo_not_listed"))
    if not any(ids.values()) and not not_listed:
        return None
    return {**ids, "not_listed": not_listed}


def set_choice(obj, kind, state, choice, *, source="chosen", fields=("lga", "ward", "polling_unit")):
    """Write a choice onto a membership or PVC record (not saved). The free text is written from the chosen names
    (rule 4: text stays beside the link); "not listed" keeps the typed text and marks the record for review."""
    if choice["not_listed"]:
        obj.geo_lga = obj.geo_ward = obj.geo_polling_unit = None
        obj.geo_source = "review"
        return obj
    _, lga, ward, pu = services.validate_chain(state, choice["lga"], choice["ward"], choice["polling_unit"])
    if lga is None:
        raise ServiceError("Choose your LGA.", code="geo_mismatch", status_code=400)
    obj.geo_lga, obj.geo_ward, obj.geo_polling_unit = lga, ward, pu
    obj.lga = lga.name
    if "ward" in fields and hasattr(obj, "ward"):
        obj.ward = ward.name if ward else ""
    if pu is not None:
        obj.polling_unit = pu.name[:160]
    obj.geo_source = source
    return obj


# ------------------------------------------------------------------ who needs to confirm (§8.1, §23.6)
def _needs_q(kind):
    """Records with a location but no link the person or an officer has confirmed, or pointing at a retired place."""
    retired = Q(geo_polling_unit__active=False) | Q(geo_ward__active=False) | Q(geo_lga__active=False)
    # Only states whose INEC list is loaded: there is nothing to choose from elsewhere, so nobody is asked (or notified).
    loaded = Q(state__in=Lga.objects.filter(active=True).values("state__slug"))
    if kind == "membership":
        return (~Q(lga="") & Q(geo_source__in=["", "matched"]) & loaded) | retired
    return ((~Q(lga="") & Q(geo_source__in=["", "matched"]) & Q(pending_geo_lga__isnull=True) & loaded)
            | (retired & Q(pending_geo_lga__isnull=True)))


def needs_confirmation(kind, obj) -> bool:
    M, _ = _models()[kind]
    return M.objects.filter(_needs_q(kind), pk=obj.pk).exists()


def _record(kind, user):
    M, _ = _models()[kind]
    qs = M.objects.filter(user=user).select_related("geo_lga", "geo_ward", "geo_polling_unit")
    if kind == "membership":
        qs = qs.exclude(status__in=["draft", "rejected", "cancelled", "refunded"])
    return qs.first()


def confirmations(user):
    """GET /me/geo-confirm/: the person's own records that need confirming, each with a suggestion (§8.1, §8.3)."""
    out = []
    for kind in KINDS:
        rec = _record(kind, user)
        if rec is None or not needs_confirmation(kind, rec):
            continue
        current = place(rec.geo_lga, rec.geo_ward, rec.geo_polling_unit)
        suggestion = services.suggest(rec.state, rec.lga, getattr(rec, "ward", ""), rec.polling_unit)
        if rec.geo_lga_id and rec.geo_lga.active:  # an exact-match link is the best pre-fill
            suggestion = {"lga": {**current["lga"], "score": 1.0},
                          "ward": {**current["ward"], "score": 1.0} if current["ward"] and current["ward"]["active"] else suggestion["ward"],
                          "polling_unit": ({**current["polling_unit"], "score": 1.0}
                                           if current["polling_unit"] and current["polling_unit"]["active"] else suggestion["polling_unit"])}
        out.append({"kind": kind, "state": rec.state, "typed": {"lga": rec.lga, "ward": getattr(rec, "ward", ""),
                                                                "polling_unit": rec.polling_unit},
                    "current": current, "retired": any(v and not v["active"] for v in current.values()),
                    "suggestion": suggestion})
    return out


def confirm(user, kind, data, request=None):
    """POST /me/geo-confirm/{kind}/ (§8.1). On a verified PVC check the choice waits for an officer (placement unchanged)."""
    from apps.audit.models import AuditLog
    from apps.audit.services import record

    if kind not in KINDS:
        raise ServiceError("Not found.", code="not_found", status_code=404)
    choice = read_choice(data)
    if choice is None:
        raise ServiceError("Choose your LGA, ward and polling unit, or say your place isn't listed.", code="validation_error",
                           status_code=400)
    M, _ = _models()[kind]
    with transaction.atomic():
        rec = M.objects.select_for_update().filter(pk=getattr(_record(kind, user), "pk", None)).first()
        if rec is None:
            raise ServiceError("Not found.", code="not_found", status_code=404)
        waits = kind == "pvc" and rec.status == "successful"
        if waits and not choice["not_listed"]:
            _, lga, ward, pu = services.validate_chain(rec.state, choice["lga"], choice["ward"], choice["polling_unit"])
            if lga is None:
                raise ServiceError("Choose your LGA.", code="geo_mismatch", status_code=400)
            rec.pending_geo_lga, rec.pending_geo_ward, rec.pending_geo_polling_unit = lga, ward, pu
        elif waits:
            rec.pending_geo_lga = rec.pending_geo_ward = rec.pending_geo_polling_unit = None
            rec.geo_source = "review"  # the officer-confirmed link stays; the queue picks it up
        else:
            set_choice(rec, kind, rec.state, choice)
        rec.save()
    record(AuditLog.Action.GEO_LINK_CHANGED, target=rec, actor=user, request=request, record=kind, by="person",
           waits_for_officer=waits)
    return rec


# ------------------------------------------------------------------ review queue (§8.4)
def review_kinds(user):
    from apps.access.models import Role
    from apps.access.services import has_role, is_system_admin

    if is_system_admin(user):
        return list(KINDS)
    return [k for k, role in (("membership", Role.MEMBERSHIP_OFFICER), ("pvc", Role.PVC_OFFICER)) if has_role(user, role)]


def review_q(kind):
    """Free text but no link, "not listed", a retired place, or (PVC) a person's choice waiting for an officer."""
    retired = Q(geo_polling_unit__active=False) | Q(geo_ward__active=False) | Q(geo_lga__active=False)
    q = (Q(geo_lga__isnull=True) & ~Q(lga="")) | Q(geo_source="review") | retired
    if kind == "pvc":
        q |= Q(pending_geo_lga__isnull=False)
    if kind == "membership":
        q &= ~Q(status__in=["draft"])
    return q


def review_queue(kind, *, state=None, lga=None):
    M, _ = _models()[kind]
    qs = M.objects.filter(review_q(kind)).select_related("geo_lga", "geo_ward", "geo_polling_unit")
    if kind == "pvc":
        qs = qs.select_related("pending_geo_lga", "pending_geo_ward", "pending_geo_polling_unit")
    if state:
        qs = qs.filter(state=state)
    if lga:
        qs = qs.filter(Q(geo_lga=lga) | Q(pending_geo_lga=lga) if kind == "pvc" else Q(geo_lga=lga))
    return qs.order_by("state", "lga", "created_at")


def why(kind, rec):
    if rec.geo_source == "review":
        return "not_listed"
    if kind == "pvc" and rec.pending_geo_lga_id:
        return "person_choice"
    if any(o is not None and not o.active for o in (rec.geo_lga, rec.geo_ward, rec.geo_polling_unit)):
        return "retired"
    return "unlinked"


def review_action(officer, kind, pk, data, request=None):
    """POST /admin/geography/review/{kind}/{id}/: `link` (IDs), `accept` (a PVC person's choice) or `ask` (§8.1 notice)."""
    from apps.audit.models import AuditLog
    from apps.audit.services import record

    if kind not in review_kinds(officer):
        raise ServiceError("You can't review these records.", code="permission_denied", status_code=403)
    M, _ = _models()[kind]
    action = data.get("action")
    if action not in ("link", "accept", "ask"):
        raise ServiceError("Choose link, accept or ask.", code="validation_error", status_code=400)
    with transaction.atomic():
        rec = M.objects.select_for_update().filter(pk=pk).first()
        if rec is None:
            raise ServiceError("Not found.", code="not_found", status_code=404)
        if rec.user_id == officer.pk:
            raise ServiceError("You can't review your own record.", code="self_review", status_code=403)
        if action == "ask":
            ask(rec, kind)
        elif action == "accept":
            if kind != "pvc" or not rec.pending_geo_lga_id:
                raise ServiceError("There's no choice waiting to accept.", code="invalid_transition", status_code=409)
            rec.geo_lga, rec.geo_ward, rec.geo_polling_unit = rec.pending_geo_lga, rec.pending_geo_ward, rec.pending_geo_polling_unit
            rec.lga = rec.geo_lga.name
            rec.polling_unit = rec.geo_polling_unit.name[:160] if rec.geo_polling_unit else rec.polling_unit
            rec.pending_geo_lga = rec.pending_geo_ward = rec.pending_geo_polling_unit = None
            rec.geo_source = "officer"
            rec.save()
        else:
            choice = read_choice({**data, "geo_not_listed": False})
            if choice is None or (kind == "pvc" and not choice["polling_unit"]):
                raise ServiceError("Choose the LGA, ward and polling unit.", code="validation_error", status_code=400)
            # PVC: the officer's choice is what communities use (§8.2). Membership: a link the member can still confirm.
            set_choice(rec, kind, rec.state, choice, source="officer" if kind == "pvc" else "matched")
            if kind == "pvc":
                rec.pending_geo_lga = rec.pending_geo_ward = rec.pending_geo_polling_unit = None
            rec.save()
        if kind == "pvc" and action != "ask":
            from apps.core import outbox

            outbox.enqueue("communities_place", {"user": str(rec.user_id)}, dedupe_key=f"communities_place:{rec.user_id}")
    record(AuditLog.Action.GEO_LINK_CHANGED, target=rec, actor=officer, request=request, record=kind, by="officer", step=action)
    return rec


# ------------------------------------------------------------------ notices (§10)
def ask(rec, kind):
    from apps.notifications.models import Kind
    from apps.notifications.services import notify_geo

    notify_geo(rec.user, Kind.GEO_CONFIRM, kind)
    type(rec).objects.filter(pk=rec.pk).update(geo_confirm_asked_at=timezone.now())


def notify_unconfirmed(limit=BATCH) -> dict:
    """`geo_notify_unconfirmed`: one geo_confirm per record not yet asked, at most `limit` a run (§12.2)."""
    out = {}
    left = limit
    for kind in KINDS:
        M, _ = _models()[kind]
        qs = M.objects.filter(_needs_q(kind), geo_confirm_asked_at__isnull=True).select_related("user").order_by("created_at")
        if kind == "membership":
            qs = qs.exclude(status__in=["draft", "rejected", "cancelled", "refunded"])
        n = 0
        for rec in qs[:left]:
            with transaction.atomic():
                ask(rec, kind)
            n += 1
        out[kind] = n
        left -= n
    return out


def notify_unit_changed(dataset) -> dict:
    """After an apply (outbox): one geo_unit_changed per record linked to a retired or moved polling unit."""
    from apps.notifications.models import Kind, Notification
    from apps.notifications.services import notify_geo

    changes = Change.objects.filter(dataset=dataset, level="polling_unit")
    retired = set(changes.filter(action="retired").values_list("code", flat=True))
    moved = set(changes.filter(action="moved").values_list("old_value", flat=True))
    ids = dict(PollingUnit.objects.filter(delimitation__in=retired | moved).values_list("pk", "delimitation"))
    out = {"membership": 0, "pvc": 0}
    if not ids:
        return out
    since = dataset.applied_at or timezone.now()
    for kind in KINDS:
        M, _ = _models()[kind]
        for rec in M.objects.filter(geo_polling_unit_id__in=list(ids)).select_related("user"):
            change = "moved" if ids[rec.geo_polling_unit_id] in moved else "retired"
            word = ("mem" if kind == "membership" else "pvc") + (".mov" if change == "moved" else ".ret")
            if Notification.objects.filter(recipient=rec.user, kind=Kind.GEO_UNIT_CHANGED, decision=word, created_at__gte=since).exists():
                continue  # the job ran before (retries never send twice)
            with transaction.atomic():
                notify_geo(rec.user, Kind.GEO_UNIT_CHANGED, kind, change)
            out[kind] += 1
    return out


def changed_unconfirmed() -> int:
    """Dashboard card (§15.2): records still pointing at a retired place, with nothing waiting from the person."""
    total = 0
    for kind in KINDS:
        M, _ = _models()[kind]
        retired = Q(geo_polling_unit__active=False) | Q(geo_ward__active=False) | Q(geo_lga__active=False)
        qs = M.objects.filter(retired)
        if kind == "pvc":
            qs = qs.filter(pending_geo_lga__isnull=True)
        total += qs.count()
    return total


def review_count(kinds=KINDS) -> int:
    return sum(_models()[k][0].objects.filter(review_q(k)).count() for k in kinds)
