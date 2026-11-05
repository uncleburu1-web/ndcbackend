"""
Volunteering — spec 18 §9, §12.2 (FR-047–FR-050). The campaign sees the minimum; contact details only after acceptance and opt-in.
"""
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import serializers

from apps.audit.models import AuditLog
from apps.audit.services import record
from apps.core.exceptions import ServiceError
from apps.news.services import clean_text

from .models import (OPEN_STATUSES, AppStatus, Availability, Role, VolunteerApplication, VolunteerChange, VolunteerChangeKind,
                     VolunteerOpening, VolunteerUpdate)

PURPOSE = "volunteering"
UPDATES_PER_WEEK = 5


def _err(msg, code, status=400, **extra):
    e = ServiceError(msg, code=code, status_code=status)
    if extra:
        e.extra = extra
    return e


def _history(app, kind, actor=None):
    VolunteerChange.objects.create(application=app, kind=kind, actor=actor)


def require_campaign(user, candidate):
    from apps.events.services import can_organise, candidate_published

    if not can_organise(user, candidate):
        raise _err("Not found.", "not_found", 404)
    if not candidate_published(candidate):
        raise _err("This candidate isn't published, so volunteering can't be changed right now.", "candidate_not_published", 409)


# ------------------------------------------------------------------ the opening (§9.1)
def opening(candidate) -> VolunteerOpening:
    return VolunteerOpening.objects.filter(candidate=candidate).first() or VolunteerOpening(candidate=candidate)


def is_open(candidate) -> bool:
    from apps.events.services import candidate_published

    o = VolunteerOpening.objects.filter(candidate=candidate).first()
    return bool(o and o.open and candidate_published(candidate))


def set_opening(user, candidate, data) -> VolunteerOpening:
    from apps.candidates.reference import State

    require_campaign(user, candidate)
    errors = {}
    with transaction.atomic():
        o, _ = VolunteerOpening.objects.select_for_update().get_or_create(candidate=candidate)
        was_open = o.open
        if "intro" in data:
            o.intro = clean_text(data.get("intro") or "")
        if "roles" in data:
            roles = list(dict.fromkeys(data.get("roles") or []))
            if any(r not in Role.values for r in roles):
                errors["roles"] = ["Choose roles from the list."]
            o.roles = roles
        if "states" in data:
            states = list(dict.fromkeys(data.get("states") or []))
            if any(s not in State.values for s in states):
                errors["states"] = ["Choose states from the list."]
            o.states = states
        if "open" in data:
            o.open = bool(data.get("open"))
        if o.open:
            if not 30 <= len(o.intro) <= 1000:
                errors["intro"] = ["Say what volunteers will do, in 30 to 1,000 characters."]
            if not o.roles:
                errors["roles"] = ["Offer at least one role."]
        if errors:
            raise serializers.ValidationError(errors)
        now = timezone.now()
        if o.open and not was_open:
            o.opened_at, o.closed_at = now, None
        if was_open and not o.open:
            o.closed_at = now
        o.save()
        if was_open and not o.open:
            _end_open(candidate, user)
    record(AuditLog.Action.VOLUNTEER_OPENING_CHANGED, target=candidate, actor=user, open=o.open)
    return o


# ------------------------------------------------------------------ applying (§9.2)
def apply(user, candidate, data) -> VolunteerApplication:
    from apps.privacy.consent import require_consent

    if not (user and user.is_authenticated and user.is_active):
        raise _err("Sign in to volunteer.", "not_authenticated", 401)
    if not user.profile_complete:
        raise _err("Finish your profile first.", "profile_incomplete", 403)
    o = VolunteerOpening.objects.filter(candidate=candidate).first()
    if not is_open(candidate):
        raise _err("This campaign isn't taking volunteers right now.", "volunteering_closed", 409)
    if user.pk in organiser_ids_for_candidate(candidate):
        raise _err("Campaign accounts don't volunteer for their own campaign.", "permission_denied", 403)
    require_consent(user, PURPOSE)
    errors = {}
    roles = list(dict.fromkeys(data.get("roles") or []))
    if not roles or any(r not in o.roles for r in roles):
        errors["roles"] = ["Choose at least one of the roles this campaign offers."]
    state = data.get("state") or ""
    from apps.candidates.reference import State

    if state not in State.values or (o.states and state not in o.states):
        errors["state"] = ["Choose a state where this campaign needs people."]
    availability = list(dict.fromkeys(data.get("availability") or []))
    if any(a not in Availability.values for a in availability):
        errors["availability"] = ["Choose from the list."]
    note = clean_text(data.get("note") or "")
    if len(note) > 300:
        errors["note"] = ["Use up to 300 characters."]
    lga_text = " ".join((data.get("lga_text") or "").split())[:100]
    geo_lga = data.get("geo_lga") or None
    if geo_lga:
        from apps.geo.models import Lga

        lga = Lga.objects.filter(pk=geo_lga, active=True, state__slug=state).first()
        if lga is None:
            errors["geo_lga"] = ["Choose an LGA in this state."]
        elif not lga_text:
            lga_text = lga.name
    if errors:
        raise serializers.ValidationError(errors)
    try:
        with transaction.atomic():
            app = VolunteerApplication.objects.create(candidate=candidate, user=user, roles=roles, state=state, lga_text=lga_text,
                                                      geo_lga_id=geo_lga, availability=availability, note=note,
                                                      share_phone=bool(data.get("share_phone")))
            _history(app, VolunteerChangeKind.APPLIED, user)
    except IntegrityError:
        raise _err("You've already applied to volunteer for this campaign.", "already_applied", 409) from None
    return app


def organiser_ids_for_candidate(candidate) -> set:
    from apps.moderation.targets import _linked

    return set(_linked(candidate.pk))


def withdraw(user, app) -> VolunteerApplication:
    with transaction.atomic():
        a = VolunteerApplication.objects.select_for_update().get(pk=app.pk)
        if a.user_id != user.pk:
            raise _err("Not found.", "not_found", 404)
        if a.status not in OPEN_STATUSES:
            raise _err("This application isn't open.", "invalid_transition", 409)
        a.status, a.ended_at = AppStatus.WITHDRAWN, timezone.now()
        a.save(update_fields=["status", "ended_at", "updated_at"])
        _history(a, VolunteerChangeKind.WITHDRAWN, user)
    return a


# ------------------------------------------------------------------ the campaign (§9.3)
def decide(user, app, accept: bool, reason="") -> VolunteerApplication:
    from apps.notifications.services import notify

    require_campaign(user, app.candidate)
    reason = " ".join((reason or "").split())
    if len(reason) > 200:
        raise serializers.ValidationError({"reason": ["Use up to 200 characters."]})
    with transaction.atomic():
        a = VolunteerApplication.objects.select_for_update().select_related("user", "candidate").get(pk=app.pk)
        if a.status != AppStatus.PENDING and not (a.status == AppStatus.ACCEPTED and not accept):
            raise _err("Only pending applications can be decided.", "invalid_transition", 409)
        a.status = AppStatus.ACCEPTED if accept else AppStatus.DECLINED
        a.decided_at, a.decided_by = timezone.now(), user
        a.decline_reason = "" if accept else reason
        a.save(update_fields=["status", "decided_at", "decided_by", "decline_reason", "updated_at"])
        _history(a, VolunteerChangeKind.ACCEPTED if accept else VolunteerChangeKind.DECLINED, user)
        notify(a.user, "volunteer_decision", target=a.candidate, decision="accepted" if accept else "declined")
    return a


def contact(user, app, request=None) -> dict:
    """Full name, and the phone if the person chose to share it: accepted applications only; audited."""
    require_campaign(user, app.candidate)
    if app.status != AppStatus.ACCEPTED:
        raise _err("Contact details show only for accepted volunteers.", "invalid_transition", 409)
    _history(app, VolunteerChangeKind.PHONE_VIEWED, user)
    record(AuditLog.Action.VOLUNTEER_CONTACT_VIEWED, target=app.candidate, actor=user, request=request)
    return {"full_name": app.user.full_name, "phone_number": app.user.phone_number if app.share_phone else None}


def send_update(user, candidate, text, role="") -> VolunteerUpdate:
    require_campaign(user, candidate)
    text = clean_text(text)
    if not 10 <= len(text) <= 500:
        raise serializers.ValidationError({"text": ["Use 10 to 500 characters."]})
    if role and role not in Role.values:
        raise serializers.ValidationError({"role": ["Choose a role from the list."]})
    since = timezone.now() - timedelta(days=7)
    if VolunteerUpdate.objects.filter(candidate=candidate, created_at__gte=since).count() >= UPDATES_PER_WEEK:
        raise _err("A campaign can send at most 5 volunteer updates a week.", "update_limit", 409)
    with transaction.atomic():
        u = VolunteerUpdate.objects.create(candidate=candidate, role=role, text=text, created_by=user)
        from apps.core import outbox

        outbox.enqueue("volunteer_update_fanout", {"update": str(u.pk)}, dedupe_key=f"volunteer_update:{u.pk}")
    return u


def recipients(update):
    qs = VolunteerApplication.objects.filter(candidate=update.candidate, status=AppStatus.ACCEPTED)
    if update.role:
        qs = qs.filter(roles__contains=[update.role])
    return qs


# ------------------------------------------------------------------ ending (§9.4)
def _end_open(candidate, actor=None) -> int:
    from apps.notifications.services import notify

    n = 0
    for a in VolunteerApplication.objects.select_for_update().filter(candidate=candidate, status__in=OPEN_STATUSES).select_related("user"):
        a.status, a.ended_at = AppStatus.ENDED, timezone.now()
        a.save(update_fields=["status", "ended_at", "updated_at"])
        _history(a, VolunteerChangeKind.ENDED, actor)
        notify(a.user, "volunteer_decision", target=candidate, decision="ended")
        n += 1
    return n


def end_for_candidate(candidate) -> int:
    """The candidate was unpublished or archived: volunteering closes and open applications end."""
    with transaction.atomic():
        VolunteerOpening.objects.filter(candidate=candidate, open=True).update(open=False, closed_at=timezone.now())
        return _end_open(candidate)


def withdraw_all(user) -> int:
    """Consent withdrawn: open applications are withdrawn and contact details hidden at once."""
    n = 0
    with transaction.atomic():
        for a in VolunteerApplication.objects.select_for_update().filter(user=user, status__in=OPEN_STATUSES):
            a.status, a.ended_at = AppStatus.WITHDRAWN, timezone.now()
            a.save(update_fields=["status", "ended_at", "updated_at"])
            _history(a, VolunteerChangeKind.WITHDRAWN, user)
            n += 1
    return n


def erase_for_deleted_user(user) -> dict:
    from apps.core.history import allow_history_delete

    with transaction.atomic(), allow_history_delete():
        qs = VolunteerApplication.objects.filter(user=user)
        n = qs.count()
        qs.delete()
    return {"applications": n}


def prune(now=None) -> dict:
    """§17.2: declined, withdrawn and ended after 90 days; accepted until 90 days after the candidate's election; updates after a year."""
    from apps.core.history import allow_history_delete

    now = now or timezone.now()
    cutoff = now - timedelta(days=90)
    old = VolunteerApplication.objects.filter(
        Q(status__in=[AppStatus.DECLINED, AppStatus.WITHDRAWN, AppStatus.ENDED], updated_at__lt=cutoff)
        | Q(status=AppStatus.ACCEPTED, candidate__election_year__lt=(now - timedelta(days=90)).year))
    with transaction.atomic(), allow_history_delete():
        apps_n = old.count()
        old.delete()  # history goes with the application
    upd, _ = VolunteerUpdate.objects.filter(created_at__lt=now - timedelta(days=365)).delete()
    return {"applications": apps_n, "updates": upd}
