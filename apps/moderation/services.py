"""
Reporting and moderation (spec 13 §6). One process for every public item. Content changes go
through each app's own services (news, posts), which call back into `content_removed` /
`content_restored` here, so a removal from any screen ends up in the same case, with the same
notifications and the same compliance figures. People decide; report counts never remove anything.
"""
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.core.history import allow_history_delete
from apps.access.models import Role
from apps.access.services import has_role, is_system_admin
from apps.audit.models import AuditLog
from apps.audit.services import record
from apps.core import blackouts
from apps.core.exceptions import ServiceError

from . import rules as R
from . import targets as T
from .models import (Appeal, Case, CaseEvent, CaseSource, CaseStatus, Decision, LegalNotice, LegalNoticeItem, Report, ReportStatus,
                     Restriction, TargetKind, new_reference)

A = AuditLog.Action
CLAIM_MINUTES = 240
APPEAL_DAYS = 14
BURST_COUNT, BURST_WINDOW = 5, timedelta(hours=1)
PAUSE_DAYS = (7, 30, 90)
E = CaseEvent.Kind


def _err(msg, code, status=400, **extra):
    e = ServiceError(msg, code=code, status_code=status)
    if extra:
        e.extra = extra
    return e


def _event(case, kind, actor=None, note=""):
    CaseEvent.objects.create(case=case, event=kind, actor=actor if getattr(actor, "is_authenticated", False) else None, note=(note or "")[:1000])


def _ip_hash(ip):
    """07's HMAC of the IP: lets limits and burst checks work without keeping the address (one format, spec 16 F6)."""
    from apps.core.hashing import ip_hash

    return ip_hash(ip)


ip_hash = _ip_hash


def _ref(prefix, model):
    for _ in range(20):
        ref = new_reference(prefix)
        if not model.objects.filter(reference=ref).exists():
            return ref
    raise RuntimeError("Couldn't make a unique reference")


def election_period(at=None) -> bool:
    return blackouts.active_window(at=at) is not None


def _set_priority(case, priority, now=None):
    """Priority only goes up (spec 13 §12)."""
    rank = R.PRIORITY_RANK[priority]
    if rank < case.priority_rank:
        case.priority, case.priority_rank = priority, rank
        case.due_at = min(case.due_at, (now or timezone.now()) + timedelta(hours=R.TARGET_HOURS[priority]))


def _open_case(kind, obj, source=CaseSource.USER_REPORT, priority="normal", actor=None, note=""):
    """The live case for this item, or a new one. Caller holds a transaction."""
    case = Case.objects.select_for_update().filter(target_kind=kind, target_id=obj.pk).exclude(status=CaseStatus.CLOSED).first()
    now = timezone.now()
    if case is None:
        try:
            with transaction.atomic():
                case = Case.objects.create(target_kind=kind, target_id=obj.pk, source=source, priority=priority,
                                           priority_rank=R.PRIORITY_RANK[priority], due_at=now + timedelta(hours=R.TARGET_HOURS[priority]),
                                           title=T.title(kind, obj), state=T.state(kind, obj))
        except IntegrityError:  # a parallel report opened it
            return Case.objects.select_for_update().get(target_kind=kind, target_id=obj.pk, status__in=[CaseStatus.OPEN, CaseStatus.ESCALATED])
        _event(case, E.OPENED, actor, note or dict(CaseSource.choices)[source])
    return case


# --------------------------------------------------------------------------- reporting
def report(kind, obj, reason, details="", *, user=None, contact_email="", ip="") -> dict:
    """Spec 13 §6.1. Returns {"reference"} or {"route"} for copyright."""
    table = R.PROFILE_REASONS if kind == TargetKind.CANDIDATE else R.REASONS
    if reason not in table:
        raise _err("Choose a reason.", "validation_error")
    if not T.is_public(kind, obj):
        raise _err("Not found.", "not_found", 404)
    label, rule_id, priority = table[reason]
    if priority == "route":
        return {"route": "/report/copyright"}
    if reason in R.ELECTION_URGENT and election_period():
        priority = "urgent"
    user = user if user is not None and getattr(user, "is_authenticated", False) else None
    details = (details or "").strip()[:1000]
    now = timezone.now()
    with transaction.atomic():
        case = _open_case(kind, obj, priority=priority)
        _set_priority(case, priority, now)
        if reason in R.LEGAL_DEADLINE_REASONS:
            due = now + timedelta(hours=48)
            case.legal_due_at = min(case.legal_due_at or due, due)
        existing = Report.objects.filter(target_kind=kind, target_id=obj.pk, reporter=user, status=ReportStatus.OPEN).first() if user else None
        if existing:  # one open report per person per item: a second one updates the first
            existing.reason, existing.details = reason, details
            existing.save(update_fields=["reason", "details"])
            r = existing
        else:
            r = Report.objects.create(reference=_ref("RPT", Report), target_kind=kind, target_id=obj.pk, case=case, reason=reason,
                                      details=details, reporter=user, contact_email="" if user else (contact_email or "")[:254],
                                      ip_hash=_ip_hash(ip))
            _event(case, E.REPORT_ADDED, None, label)
        case.burst_flag = case.burst_flag or _burst(case, r, now)
        case.save()
    return {"reference": r.reference}


def _burst(case, r, now) -> bool:
    """Spec 13 §6.5: a hint, never a decision."""
    since = now - BURST_WINDOW
    recent = Report.objects.filter(case=case, created_at__gte=since)
    distinct = recent.exclude(reporter__isnull=True).values("reporter").distinct().count() + recent.filter(reporter__isnull=True).exclude(
        ip_hash="").values("ip_hash").distinct().count()
    same_ip = Report.objects.filter(ip_hash=r.ip_hash, created_at__gte=since).values("target_id").count() if r.ip_hash else 0
    return distinct >= BURST_COUNT or same_ip >= BURST_COUNT


def report_status(reference) -> dict | None:
    r = Report.objects.select_related("case").filter(reference=(reference or "").strip().upper()).first()
    if r is None:
        return None
    if r.status == ReportStatus.OPEN:
        label = "Under review" if r.case.assigned_to_id or r.case.status == CaseStatus.ESCALATED else "Open"
    else:
        label = dict(ReportStatus.choices)[r.status]
    return {"reference": r.reference, "status": r.status, "status_label": label}


# --------------------------------------------------------------------------- permissions
def _roles(user):
    return {
        "sys": is_system_admin(user),
        "mod": has_role(user, Role.MODERATOR),
        "news": has_role(user, Role.NEWS_REVIEWER),
    }


def can_view(user, case, obj=None) -> bool:
    r = _roles(user)
    if r["sys"]:
        return True
    obj = obj if obj is not None else T.load(case.target_kind, case.target_id)
    if case.target_kind == TargetKind.CANDIDATE or T.is_official(case.target_kind, obj):
        return False
    if case.target_kind == TargetKind.ARTICLE:
        return r["mod"] or r["news"]
    return r["mod"]


def visible_cases(user):
    """Queryset of cases this user may see in the queue (spec 13 §4.3)."""
    r = _roles(user)
    qs = Case.objects.all()
    if r["sys"]:
        return qs
    from apps.news.models import Article

    community = Article.objects.filter(source="community").values("pk")
    allowed = Q(target_kind=TargetKind.ARTICLE, target_id__in=community) if (r["mod"] or r["news"]) else Q(pk__in=[])
    if r["mod"]:
        allowed |= Q(target_kind=TargetKind.POST)
    return qs.filter(allowed)


def _check(user, case, obj, *, action=""):
    """Role table + no self-moderation (spec 13 §3.3, §4.3). Raises."""
    if not can_view(user, case, obj):
        raise _err("You can't act on this case.", "permission_denied", 403)
    sysadmin = is_system_admin(user)
    if case.status == CaseStatus.ESCALATED and not sysadmin and action not in ("view",):
        raise _err("This case was escalated to system administrators.", "permission_denied", 403)
    if user.pk in T.owner_ids(case.target_kind, obj):
        raise _err("You can't moderate your own content or a candidate you're linked to.", "self_moderation", 403)
    if Report.objects.filter(target_kind=case.target_kind, target_id=case.target_id, reporter=user).exists():
        raise _err("You reported this item, so someone else has to decide.", "self_moderation", 403)
    if (case.assigned_to_id and case.assigned_to_id != user.pk and not sysadmin and case.assigned_at
            and timezone.now() - case.assigned_at < timedelta(minutes=CLAIM_MINUTES) and action != "claim"):
        raise _err("Someone else is working on this case.", "not_your_case", 403)


def _locked(case_id):
    return Case.objects.select_for_update().get(pk=case_id)


# --------------------------------------------------------------------------- claim / escalate / keep
def claim(case, user, request=None):
    with transaction.atomic():
        c = _locked(case.pk)
        obj = T.load(c.target_kind, c.target_id)
        _check(user, c, obj, action="claim")
        if c.status == CaseStatus.CLOSED:
            raise _err("This case is closed.", "invalid_transition", 409)
        if (c.assigned_to_id and c.assigned_to_id != user.pk and c.assigned_at
                and timezone.now() - c.assigned_at < timedelta(minutes=CLAIM_MINUTES) and not is_system_admin(user)):
            raise _err("Someone else is working on this case.", "not_your_case", 403)
        c.assigned_to, c.assigned_at = user, timezone.now()
        c.save(update_fields=["assigned_to", "assigned_at"])
        _event(c, E.CLAIMED, user)
    record(A.MODERATION_CASE_CLAIMED, target=c, actor=user, request=request)
    return c


def release(case, user):
    with transaction.atomic():
        c = _locked(case.pk)
        if c.assigned_to_id == user.pk or is_system_admin(user):
            c.assigned_to, c.assigned_at = None, None
            c.save(update_fields=["assigned_to", "assigned_at"])
            _event(c, E.RELEASED, user)
    return c


def escalate(case, user, note, request=None):
    note = (note or "").strip()
    if not note:
        raise _err("Say why you're escalating it.", "validation_error")
    with transaction.atomic():
        c = _locked(case.pk)
        obj = T.load(c.target_kind, c.target_id)
        _check(user, c, obj)
        if c.status != CaseStatus.OPEN:
            raise _err("Only open cases can be escalated.", "invalid_transition", 409)
        c.status, c.escalated_by, c.assigned_to, c.assigned_at = CaseStatus.ESCALATED, user, None, None
        c.save()
        _event(c, E.ESCALATED, user, note)
    record(A.MODERATION_ESCALATED, target=c, actor=user, request=request)
    return c


def _notify_reporters(case, decision):
    from apps.notifications.services import notify_moderation

    seen = set()
    for r in case.reports.filter(reporter__isnull=False).select_related("reporter"):
        if r.reporter_id in seen:
            continue
        seen.add(r.reporter_id)
        notify_moderation(r.reporter, "report_outcome", case=case, decision=decision)


def _close(case, decision, user, *, rule_id="", note_to_author="", internal_note=""):
    now = timezone.now()
    case.status, case.decision, case.decided_by, case.decided_at, case.closed_at = CaseStatus.CLOSED, decision, user, now, now
    case.assigned_to, case.assigned_at = None, None
    if rule_id:
        case.rule = rule_id
    case.note_to_author = (note_to_author or case.note_to_author or "")[:500]
    if internal_note:
        case.internal_note = internal_note[:1000]
    case.save()
    new = ReportStatus.ACTIONED if decision == Decision.REMOVED else ReportStatus.DISMISSED
    case.reports.filter(status=ReportStatus.OPEN).update(status=new, resolved_at=now)


def keep(case, user, note="", request=None):
    with transaction.atomic():
        c = _locked(case.pk)
        obj = T.load(c.target_kind, c.target_id)
        _check(user, c, obj)
        if c.status == CaseStatus.CLOSED:
            raise _err("This case is already closed.", "invalid_transition", 409)
        _close(c, Decision.KEPT, user, internal_note=note)
        _event(c, E.KEPT, user, note)
        _notify_reporters(c, "kept")
    record(A.MODERATION_KEPT, target=c, actor=user, request=request)
    return c


def dismiss_report(report_obj, user, note="", request=None):
    """07/09's per-report Dismiss (kept as a thin wrapper): when no open reports remain, the case is kept."""
    with transaction.atomic():
        c = _locked(report_obj.case_id)
        obj = T.load(c.target_kind, c.target_id)
        _check(user, c, obj)
        r = Report.objects.select_for_update().get(pk=report_obj.pk)
        if r.status != ReportStatus.OPEN:
            raise _err("This report has already been resolved.", "invalid_transition", 409)
        r.status, r.resolved_at = ReportStatus.DISMISSED, timezone.now()
        r.save(update_fields=["status", "resolved_at"])
        last = not c.reports.filter(status=ReportStatus.OPEN).exists()
    if last and c.status != CaseStatus.CLOSED:
        return keep(c, user, note, request=request)
    return c


# --------------------------------------------------------------------------- remove / restore
def _validate_rule(rule_id):
    if rule_id not in R.RULE_IDS:
        raise _err("Choose the rule it broke.", "rule_required")


def remove(case, user, rule_id, note_to_author="", internal_note="", warn=False, request=None):
    """Spec 13 §6.2. The owning app removes the item; its service calls `content_removed`, which closes this case."""
    _validate_rule(rule_id)
    with transaction.atomic():
        c = _locked(case.pk)
        obj = T.load(c.target_kind, c.target_id)
        _check(user, c, obj)
        if c.status == CaseStatus.CLOSED:
            raise _err("This case is already closed.", "invalid_transition", 409)
        note = (note_to_author or "").strip()
        reason = f"{rule_id}: {R.rule_title(rule_id)}" + (f" — {note}" if note else "")
        if c.target_kind == TargetKind.ARTICLE:
            from apps.news import services as news

            if obj.status != "successful":
                raise _err("Only published items can be removed.", "invalid_transition", 409)
            news.remove(obj, user, reason, request=request, rule=rule_id, note_to_author=note, internal_note=internal_note)
        elif c.target_kind == TargetKind.POST:
            from apps.posts import services as posts

            posts.remove_post(obj, user, reason, request=request, rule=rule_id, note_to_author=note, internal_note=internal_note)
        else:
            raise _err("Candidate profiles aren't removed: correct the profile, then close the case with Keep.", "invalid_transition", 409)
        if warn:
            for author in T.authors(c.target_kind, obj):
                warn_user(author, user, rule_id, note or R.rule_title(rule_id), case=Case.objects.get(pk=c.pk))
    return Case.objects.get(pk=c.pk)


def content_removed(kind, obj, actor, *, rule_id="", note_to_author="", internal_note="", source=None):
    """Called by news/posts services inside their removal transaction (spec 13 §6.2). They insist on a rule first."""
    c = _open_case(kind, obj, source=source or CaseSource.PROACTIVE, actor=actor)
    _close(c, Decision.REMOVED, actor, rule_id=rule_id, note_to_author=note_to_author, internal_note=internal_note)
    _event(c, E.REMOVED, actor, f"{rule_id} {R.rule_title(rule_id)}".strip() + (f" — {note_to_author}" if note_to_author else ""))
    from apps.notifications.services import notify_moderation

    for author in T.authors(kind, obj):
        notify_moderation(author, "content_removed", case=c, decision="removed")
    _notify_reporters(c, "removed")
    record(A.MODERATION_REMOVED, target=c, actor=actor, rule=rule_id, kind=kind)
    return c


def content_restored(kind, obj, actor, note=""):
    """Called by news/posts restore services (spec 13 §6.2)."""
    c = Case.objects.select_for_update().filter(target_kind=kind, target_id=obj.pk, decision=Decision.REMOVED).order_by("-decided_at").first()
    if c is None:
        return None
    c.decision, c.restorable = Decision.RESTORED, False
    c.save(update_fields=["decision", "restorable"])
    _event(c, E.RESTORED, actor, note)
    LegalNoticeItem.objects.filter(case=c, outcome=LegalNoticeItem.Outcome.REMOVED).update(outcome=LegalNoticeItem.Outcome.RESTORED)
    record(A.MODERATION_RESTORED, target=c, actor=actor, kind=kind)
    return c


def restore(case, user, note="", request=None):
    with transaction.atomic():
        c = _locked(case.pk)
        obj = T.load(c.target_kind, c.target_id)
        _check(user, c, obj)
        if c.decision != Decision.REMOVED:
            raise _err("Only removed items can be restored.", "invalid_transition", 409)
        _restore_target(c, obj, user, note, request)
    return Case.objects.get(pk=c.pk)


def _restore_target(c, obj, user, note, request=None):
    if c.target_kind == TargetKind.ARTICLE:
        from apps.news import services as news

        news.restore(obj, user, request=request, note=note)
    else:
        from apps.posts import services as posts

        posts.restore_post(obj, user, request=request, note=note)


def author_removed(kind, obj):
    """The author deleted their own post while a case was open (spec 13 §11)."""
    c = Case.objects.select_for_update().filter(target_kind=kind, target_id=obj.pk).exclude(status=CaseStatus.CLOSED).first()
    if c:
        _close(c, Decision.AUTHOR_REMOVED, None)
        _event(c, E.AUTHOR_REMOVED, None)


# --------------------------------------------------------------------------- standing
def warn_user(target_user, by, rule_id, reason, case=None, request=None):
    _validate_rule(rule_id)
    r = Restriction.objects.create(user=target_user, kind=Restriction.Kind.WARNING, rule=rule_id, reason=(reason or "")[:500], case=case,
                                   starts_at=timezone.now(), created_by=by)
    if case:
        _event(case, E.WARNED, by, f"{rule_id} {reason}")
    from apps.notifications.services import notify_moderation

    notify_moderation(target_user, "account_warning", case=case, decision="warning")
    record(A.MODERATION_WARNED, target=r, actor=by, request=request, rule=rule_id)
    return r


def pause_news(case, by, days, rule_id, reason="", request=None):
    if days not in PAUSE_DAYS:
        raise _err("Choose 7, 30 or 90 days.", "validation_error")
    _validate_rule(rule_id)
    with transaction.atomic():
        c = _locked(case.pk)
        obj = T.load(c.target_kind, c.target_id)
        _check(by, c, obj)
        if c.target_kind != TargetKind.ARTICLE or T.is_official(c.target_kind, obj) or not obj.author_id:
            raise _err("Pauses apply to community news authors.", "invalid_transition", 409)
        now = timezone.now()
        r = Restriction.objects.create(user=obj.author, kind=Restriction.Kind.NEWS_PAUSE, rule=rule_id, reason=(reason or "")[:500], case=c,
                                       starts_at=now, ends_at=now + timedelta(days=days), created_by=by)
        _event(c, E.PAUSED, by, f"{days} days · {rule_id}")
        from apps.notifications.services import notify_moderation

        notify_moderation(obj.author, "submissions_paused", case=c, decision="paused")
    record(A.MODERATION_PAUSED, target=r, actor=by, request=request, days=days, rule=rule_id)
    return r


def lift_pause(restriction, by, note="", request=None):
    with transaction.atomic():
        r = Restriction.objects.select_for_update().get(pk=restriction.pk)
        if r.lifted_at or (r.ends_at and r.ends_at <= timezone.now()):
            return r
        r.lifted_at, r.lifted_by = timezone.now(), by
        r.save(update_fields=["lifted_at", "lifted_by"])
        if r.case_id:
            _event(r.case, E.PAUSE_LIFTED, by, note)
    record(A.MODERATION_PAUSE_LIFTED, target=r, actor=by, request=request)
    return r


def news_paused_until(user):
    now = timezone.now()
    r = (Restriction.objects.filter(user=user, kind=Restriction.Kind.NEWS_PAUSE, lifted_at__isnull=True, starts_at__lte=now, ends_at__gt=now)
         .order_by("-ends_at").first())
    return r.ends_at if r else None


def require_not_paused(user):
    until = news_paused_until(user)
    if until:
        raise _err(f"You can't submit community news until {timezone.localtime(until):%d %b %Y}.", "submissions_paused", 403,
                   until=until.isoformat())


def standing(user, months=12) -> dict:
    since = timezone.now() - timedelta(days=30 * months)
    from apps.news.models import Article

    mine = Article.objects.filter(author=user, source="community").values("pk")
    removals = Case.objects.filter(target_kind=TargetKind.ARTICLE, target_id__in=mine, decision=Decision.REMOVED, decided_at__gte=since).count()
    rs = Restriction.objects.filter(user=user, created_at__gte=since)
    suggestion = ""
    if removals >= 3:
        suggestion = "This author has had 3 or more removals in 12 months: consider a 30-day pause."
    elif removals >= 2:
        suggestion = "This author has had 2 removals in 12 months: consider a warning."
    return {"removals": removals, "warnings": rs.filter(kind=Restriction.Kind.WARNING).count(),
            "pauses": rs.filter(kind=Restriction.Kind.NEWS_PAUSE).count(), "paused_until": news_paused_until(user), "suggestion": suggestion}


# --------------------------------------------------------------------------- appeals
def _appeal_owner_ok(user, case=None, restriction=None):
    if restriction is not None:
        return restriction.user_id == user.pk
    obj = T.load(case.target_kind, case.target_id)
    return user.pk in T.owner_ids(case.target_kind, obj)


def open_appeal(user, statement, evidence_url="", *, case=None, restriction=None):
    statement = (statement or "").strip()
    if not statement:
        raise _err("Tell us why you think the decision was wrong.", "validation_error")
    if (case is None) == (restriction is None):
        raise _err("Choose what you're appealing.", "validation_error")
    if not _appeal_owner_ok(user, case, restriction):
        raise _err("You can only appeal decisions about your own content.", "permission_denied", 403)
    if case is not None:
        if case.decision != Decision.REMOVED:
            raise _err("Only removals can be appealed.", "invalid_transition", 409)
        start = case.decided_at
        kind = Appeal.Kind.REMOVAL
        exists = Appeal.objects.filter(case=case, kind=kind).exists()
    else:
        if restriction.kind != Restriction.Kind.NEWS_PAUSE:
            raise _err("Only pauses can be appealed.", "invalid_transition", 409)
        start = restriction.created_at
        kind = Appeal.Kind.PAUSE
        exists = Appeal.objects.filter(restriction=restriction, kind=kind).exists()
    if exists:
        raise _err("You've already appealed this decision.", "appeal_exists", 409)
    if timezone.now() - start > timedelta(days=APPEAL_DAYS):
        raise _err(f"Appeals must be made within {APPEAL_DAYS} days.", "appeal_window_closed", 409)
    try:
        with transaction.atomic():
            a = Appeal.objects.create(case=case, restriction=restriction, kind=kind, appellant=user, statement=statement[:2000],
                                      evidence_url=(evidence_url or "")[:500])
    except IntegrityError:
        raise _err("You've already appealed this decision.", "appeal_exists", 409) from None
    _event(case or restriction.case, E.APPEAL_OPENED, user) if (case or restriction.case) else None
    return a


def _original_decider(appeal):
    return appeal.case.decided_by_id if appeal.case_id else appeal.restriction.created_by_id


def can_decide_appeal(user, appeal) -> bool:
    if user.pk == _original_decider(appeal) or (appeal.appellant_id and user.pk == appeal.appellant_id):
        return False
    case = appeal.case or (appeal.restriction.case if appeal.restriction_id else None)
    if case is None:
        return is_system_admin(user)
    if case.escalated_by_id and not is_system_admin(user):
        return False  # escalated cases' appeals go to a (different) system admin
    obj = T.load(case.target_kind, case.target_id)
    return can_view(user, case, obj) and user.pk not in T.owner_ids(case.target_kind, obj)


def decide_appeal(appeal, user, upheld, reason="", request=None):
    with transaction.atomic():
        a = Appeal.objects.select_for_update(of=("self",)).select_related("case", "restriction").get(pk=appeal.pk)
        if a.status != Appeal.Status.PENDING:
            raise _err("This appeal has already been decided.", "invalid_transition", 409)
        if user.pk == _original_decider(a):
            raise _err("Someone other than the original decider has to decide this appeal.", "same_decider", 403)
        if not can_decide_appeal(user, a):
            raise _err("You can't decide this appeal.", "permission_denied", 403)
        reason = (reason or "").strip()
        if not upheld and not reason:
            raise _err("Give a reason the author will see.", "validation_error")
        a.status = Appeal.Status.UPHELD if upheld else Appeal.Status.DENIED
        a.decided_by, a.decided_at, a.decision_reason = user, timezone.now(), reason[:500]
        a.save()
        case = a.case or (a.restriction.case if a.restriction_id else None)
        if upheld:
            if a.kind == Appeal.Kind.REMOVAL:
                obj = T.load(a.case.target_kind, a.case.target_id)
                _restore_target(a.case, obj, user, "Restored after appeal", request)
            else:
                lift_pause(a.restriction, user, "Lifted after appeal", request)
        if case:
            _event(case, E.APPEAL_UPHELD if upheld else E.APPEAL_DENIED, user, reason)
        from apps.notifications.services import notify_moderation

        if a.appellant_id:
            notify_moderation(a.appellant, "appeal_decision", appeal=a, decision="upheld" if upheld else "denied")
    record(A.MODERATION_APPEAL_DECIDED, target=a, actor=user, request=request, upheld=upheld)
    return a


# --------------------------------------------------------------------------- legal notices
def _items_for(values):
    out = []
    for v in values:
        kind, obj = T.parse_url(v)
        if obj is None:
            raise _err(f"We couldn't find “{str(v)[:80]}” on NDCConnect.", "validation_error")
        out.append((kind, obj))
    return out


def log_notice(*, kind, sender_name, sender_contact, received_at, items, by=None, sender_org="", sender_reference="", legal_basis="",
               work_description="", declaration_accepted=False, signature="", ip="", request=None):
    if kind not in LegalNotice.Kind.values:
        raise _err("Choose the notice type.", "validation_error")
    if not (sender_name or "").strip() or not (sender_contact or "").strip():
        raise _err("Enter who sent the notice and how to contact them.", "validation_error")
    if kind == LegalNotice.Kind.AGENCY and not (legal_basis or "").strip():
        raise _err("An agency notice must state the law and section it relies on.", "legal_basis_required")
    if kind == LegalNotice.Kind.COPYRIGHT and not (declaration_accepted and signature and work_description):
        raise _err("A copyright notice needs the work, a signature and the declarations.", "validation_error")
    if not items:
        raise _err("Add at least one NDCConnect link.", "validation_error")
    targets = _items_for(items)
    due = received_at + timedelta(hours=48) if kind in (LegalNotice.Kind.AGENCY, LegalNotice.Kind.COURT) else None
    source = {LegalNotice.Kind.AGENCY: CaseSource.AGENCY, LegalNotice.Kind.COURT: CaseSource.COURT,
              LegalNotice.Kind.COPYRIGHT: CaseSource.COPYRIGHT}[kind]
    priority = "legal" if due else "urgent"
    with transaction.atomic():
        n = LegalNotice.objects.create(reference=_ref("LGL", LegalNotice), kind=kind, sender_name=sender_name[:150], sender_org=sender_org[:150],
                                       sender_contact=sender_contact[:254], sender_reference=sender_reference[:100],
                                       legal_basis=legal_basis[:300], received_at=received_at, due_at=due, work_description=work_description[:1000],
                                       declaration_accepted=bool(declaration_accepted), signature=signature[:150], ip_hash=_ip_hash(ip),
                                       created_by=by if getattr(by, "is_authenticated", False) else None)
        for tkind, obj in targets:
            c = _open_case(tkind, obj, source=source, priority=priority, actor=by)
            _set_priority(c, priority)
            if due:
                c.legal_due_at = min(c.legal_due_at or due, due)
            if c.source == CaseSource.USER_REPORT:
                c.source = source
            c.save()
            LegalNoticeItem.objects.create(notice=n, case=c)
            _event(c, E.NOTICE_LINKED, by, f"{n.reference} · {n.get_kind_display()}")
    record(A.MODERATION_LEGAL_NOTICE_LOGGED, target=n, actor=by if getattr(by, "is_authenticated", False) else None,
           actor_label="" if getattr(by, "is_authenticated", False) else "public:copyright_form", request=request, kind=kind, items=len(targets))
    return n


def acknowledge(notice, by, how, request=None):
    notice.acknowledged_at, notice.acknowledged_how = timezone.now(), (how or "")[:200]
    notice.save(update_fields=["acknowledged_at", "acknowledged_how"])
    record(A.MODERATION_LEGAL_NOTICE_UPDATED, target=notice, actor=by, request=request, change="acknowledged")
    return notice


def set_item_outcome(item, by, outcome, reason="", request=None):
    item = LegalNoticeItem.objects.select_related("notice", "case").get(pk=item.pk)
    c, n = item.case, item.notice
    if outcome == LegalNoticeItem.Outcome.REMOVED:
        rule_id = "R7" if n.kind == LegalNotice.Kind.COPYRIGHT else "LEGAL"
        if c.status != CaseStatus.CLOSED:
            obj = T.load(c.target_kind, c.target_id)
            if c.target_kind == TargetKind.CANDIDATE:
                raise _err("Candidate profiles aren't removed: correct the profile instead.", "invalid_transition", 409)
            with transaction.atomic():
                if c.target_kind == TargetKind.ARTICLE:
                    from apps.news import services as news

                    news.remove(obj, by, f"{R.rule_title(rule_id)} ({n.reference})", request=request, rule=rule_id, source=c.source,
                                note_to_author=reason)
                else:
                    from apps.posts import services as posts

                    posts.remove_post(obj, by, f"{R.rule_title(rule_id)} ({n.reference})", request=request, rule=rule_id, source=c.source,
                                      note_to_author=reason)
    elif outcome == LegalNoticeItem.Outcome.NOT_REMOVED:
        if not (reason or "").strip():
            raise _err("Say why it wasn't removed.", "validation_error")
        if c.status != CaseStatus.CLOSED:
            with transaction.atomic():
                cc = _locked(c.pk)
                _close(cc, Decision.KEPT, by, internal_note=reason)
                _event(cc, E.KEPT, by, reason)
    elif outcome == LegalNoticeItem.Outcome.RESTORED:
        if c.decision == Decision.REMOVED:
            obj = T.load(c.target_kind, c.target_id)
            with transaction.atomic():
                _restore_target(c, obj, by, reason or "Restored after counter-notice")
    else:
        raise _err("Choose an outcome.", "validation_error")
    LegalNoticeItem.objects.filter(pk=item.pk).update(outcome=outcome, outcome_reason=(reason or "")[:500])
    if not n.items.filter(outcome=LegalNoticeItem.Outcome.PENDING).exists():
        LegalNotice.objects.filter(pk=n.pk).update(status="closed")
    record(A.MODERATION_LEGAL_NOTICE_UPDATED, target=n, actor=by, request=request, change=outcome)
    return LegalNoticeItem.objects.get(pk=item.pk)


def set_hold(case, by, on, request=None):
    with transaction.atomic():
        c = _locked(case.pk)
        c.legal_hold = bool(on)
        c.save(update_fields=["legal_hold"])
        _event(c, E.HOLD_ON if on else E.HOLD_OFF, by)
    record(A.MODERATION_LEGAL_HOLD_CHANGED, target=c, actor=by, request=request, on=bool(on))
    return c


def held_ids(kind) -> set:
    return set(Case.objects.filter(target_kind=kind, legal_hold=True).values_list("target_id", flat=True))


def counter_notice(notice, author, text):
    text = (text or "").strip()
    if not text:
        raise _err("Explain why the content doesn't infringe copyright.", "validation_error")
    if notice.kind != LegalNotice.Kind.COPYRIGHT:
        raise _err("Counter-notices are for copyright removals.", "invalid_transition", 409)
    removed = [i.case for i in notice.items.select_related("case") if i.case.decision == Decision.REMOVED]
    if not any(_appeal_owner_ok(author, case=c) for c in removed):
        raise _err("You can only respond about your own content.", "permission_denied", 403)
    if notice.counter_notice_at:
        raise _err("A counter-notice was already sent.", "appeal_exists", 409)
    notice.counter_notice_at, notice.counter_notice_text, notice.counter_notice_by = timezone.now(), text[:2000], author
    notice.save(update_fields=["counter_notice_at", "counter_notice_text", "counter_notice_by"])
    for c in removed:
        _event(c, E.COUNTER_NOTICE, author)
    return notice


def mark_owner_notified(notice, by, at=None, request=None):
    notice.owner_notified_at = at or timezone.now()
    notice.save(update_fields=["owner_notified_at"])
    record(A.MODERATION_LEGAL_NOTICE_UPDATED, target=notice, actor=by, request=request, change="owner_notified")
    return notice


def refresh_restorable(now=None) -> int:
    """Copyright: after the counter-notice window passes unanswered, show "Can be restored" (spec 13 §4.6)."""
    now = now or timezone.now()
    days = settings.COPYRIGHT_COUNTER_NOTICE_DAYS
    n = 0
    for notice in LegalNotice.objects.filter(kind=LegalNotice.Kind.COPYRIGHT, owner_notified_at__isnull=False, status__in=["open", "closed"]):
        if notice.owner_notified_at + timedelta(days=days) <= now:
            n += Case.objects.filter(notice_items__notice=notice, decision=Decision.REMOVED, restorable=False).update(restorable=True)
    return n


# --------------------------------------------------------------------------- upkeep
def tick(now=None) -> dict:
    """Every 15 minutes (spec 13 §5.4): release stale claims, recompute burst flags and mark copyright removals
    restorable. Pauses end by their date (`news_paused_until` compares with now), so an expired pause needs no write.
    Notifications are written in the decision's transaction (stronger than §11's after-commit retry), so none to resend."""
    now = now or timezone.now()
    released = Case.objects.filter(assigned_at__lt=now - timedelta(minutes=CLAIM_MINUTES)).exclude(status=CaseStatus.CLOSED).update(
        assigned_to=None, assigned_at=None)
    return {"claims_released": released, "restorable": refresh_restorable(now), "bursts": recompute_bursts(now)}


def erase_for_deleted_user(user) -> dict:
    n = Report.objects.filter(reporter=user).update(reporter=None, contact_email="")
    return {"reports_detached": n}


# --------------------------------------------------------------------------- helpers for other apps
def open_reports_subquery(kind):
    """Annotate a queryset of articles/posts with their open report count (replaces 07/09's reverse relation)."""
    from django.db.models import Count, IntegerField, OuterRef, Subquery

    sub = (Report.objects.filter(target_kind=kind, target_id=OuterRef("pk"), status=ReportStatus.OPEN).order_by().values("target_id")
           .annotate(n=Count("pk")).values("n"))
    from django.db.models.functions import Coalesce

    return Coalesce(Subquery(sub, output_field=IntegerField()), 0)


def case_summary(kind, target_id) -> dict | None:
    """For 07/09's admin detail pages: "Reports: open the case" (spec 13 §8.2)."""
    c = (Case.objects.filter(target_kind=kind, target_id=target_id).order_by("-opened_at").first())
    if c is None:
        return None
    return {"id": c.id, "status": c.status, "priority": c.priority, "decision": c.decision, "rule": c.rule,
            "open_reports": c.reports.filter(status=ReportStatus.OPEN).count(), "reports": c.reports.count()}


def removal_notice(kind, obj) -> dict | None:
    """Spec 13 §4.5: the 410 body. Only the rule and the kind of removal; never the content or who sent a notice."""
    rule_id = getattr(obj, "removed_rule", "")
    if not rule_id and T.is_official(kind, obj):
        return None  # an editorial unpublish of an official announcement: simply gone (404)
    # Removals from before spec 13 have no rule: the notice says "our community rules" (§5.4).
    c = Case.objects.filter(target_kind=kind, target_id=obj.pk, decision=Decision.REMOVED).order_by("-decided_at").first()
    source = c.source if c else CaseSource.USER_REPORT
    label = {CaseSource.COPYRIGHT: "copyright", CaseSource.AGENCY: "legal", CaseSource.COURT: "legal"}.get(source, "rules")
    return {"removed": True, "rule": rule_id, "rule_title": R.rule_title(rule_id) if rule_id else "", "kind": T.kind_words(kind, obj),
            "source": label}


def raise_if_removed(kind, obj):
    """Public item endpoints: 410 Gone with the rule (spec 13 §4.5, §7), in the usual error envelope."""
    notice = removal_notice(kind, obj) if obj is not None else None
    if notice:
        raise _err("Removed by NDCConnect.", "removed", 410, **notice)


# --------------------------------------------------------------------------- public copyright form
def submit_copyright_notice(form: dict, ip="", user=None, request=None):
    """Spec 13 §4.7: the Copyright Act 2022 s. 54(2) list. Creates a notice and an urgent case."""
    required = ("name", "contact", "work", "url", "signature")
    missing = [k for k in required if not str(form.get(k, "")).strip()]
    ticks = [k for k in ("authority", "good_faith", "accurate") if form.get(k) is not True]
    if missing or ticks:
        raise ValidationError({**{k: ["Fill this in."] for k in missing}, **{k: ["Tick this to continue."] for k in ticks}})
    kind, obj = T.parse_url(str(form["url"]))
    if obj is None or not T.is_public(kind, obj):
        raise ValidationError({"url": ["Paste the link to a public page on NDCConnect."]})
    return log_notice(kind=LegalNotice.Kind.COPYRIGHT, sender_name=str(form["name"]).strip(), sender_contact=str(form["contact"]).strip(),
                      received_at=timezone.now(), items=[str(form["url"])], by=user, work_description=str(form["work"]).strip(),
                      declaration_accepted=True, signature=str(form["signature"]).strip(), ip=ip, request=request)


# --------------------------------------------------------------------------- reporter & author views
def my_reports(user):
    out = []
    for r in Report.objects.filter(reporter=user).select_related("case").order_by("-created_at")[:200]:
        st = report_status(r.reference)
        out.append({"reference": r.reference, "target_kind": r.target_kind, "title": "" if r.case.decision == Decision.REMOVED else r.case.title,
                    "reason": r.reason, "reason_label": R.reason_label(r.reason, r.target_kind), "status": r.status,
                    "status_label": st["status_label"], "created_at": r.created_at, "resolved_at": r.resolved_at})
    return out


def _owned_case_ids(user):
    """Cases about this user's community news, or posts of candidates they're linked to."""
    from apps.news.models import Article
    from apps.posts.models import CandidatePost
    from apps.posts.services import linked_candidates

    news = Article.objects.filter(author=user, source="community").values_list("pk", flat=True)
    posts = CandidatePost.objects.filter(candidate__in=linked_candidates(user)).values_list("pk", flat=True)
    return Case.objects.filter(Q(target_kind=TargetKind.ARTICLE, target_id__in=news) | Q(target_kind=TargetKind.POST, target_id__in=posts))


def _appeal_payload(a):
    if a is None:
        return None
    return {"id": a.id, "status": a.status, "status_label": a.get_status_display(), "created_at": a.created_at,
            "decided_at": a.decided_at, "reason": a.decision_reason if a.status != Appeal.Status.PENDING else ""}


def author_view(user) -> dict:
    """GET /me/moderation/: removals, warnings, pauses and appeals about your own content (never who reported or decided)."""
    now = timezone.now()
    removals = []
    for c in _owned_case_ids(user).filter(decision__in=[Decision.REMOVED, Decision.RESTORED]).order_by("-decided_at")[:100]:
        appeal = c.appeals.filter(kind=Appeal.Kind.REMOVAL).first()
        notice = LegalNotice.objects.filter(items__case=c, kind=LegalNotice.Kind.COPYRIGHT).first()
        removals.append({"case": c.id, "target_kind": c.target_kind, "target_id": c.target_id, "title": c.title, "rule": c.rule,
                         "rule_title": R.rule_title(c.rule), "note_to_author": c.note_to_author, "decided_at": c.decided_at,
                         "decision": c.decision, "source": c.source,
                         "can_appeal": c.decision == Decision.REMOVED and appeal is None and not notice
                         and now - c.decided_at <= timedelta(days=APPEAL_DAYS),
                         "appeal_until": c.decided_at + timedelta(days=APPEAL_DAYS), "appeal": _appeal_payload(appeal),
                         "copyright_notice": str(notice.pk) if notice else None,
                         "can_counter_notice": bool(notice and not notice.counter_notice_at and c.decision == Decision.REMOVED)})
    restrictions = []
    for r in Restriction.objects.filter(user=user).order_by("-created_at")[:50]:
        appeal = r.appeals.first()
        active = r.kind == Restriction.Kind.NEWS_PAUSE and not r.lifted_at and r.ends_at and r.ends_at > now
        restrictions.append({"id": r.id, "kind": r.kind, "kind_label": r.get_kind_display(), "rule": r.rule, "rule_title": R.rule_title(r.rule),
                             "reason": r.reason, "starts_at": r.starts_at, "ends_at": r.ends_at, "lifted_at": r.lifted_at, "active": bool(active),
                             "can_appeal": bool(active) and appeal is None and now - r.created_at <= timedelta(days=APPEAL_DAYS),
                             "appeal": _appeal_payload(appeal)})
    return {"paused_until": news_paused_until(user), "removals": removals, "restrictions": restrictions}


# --------------------------------------------------------------------------- reveal reporter
def reveal_reporter(report_obj, by, request=None) -> dict:
    if not is_system_admin(by):
        raise _err("Only system administrators can see who filed a report.", "permission_denied", 403)
    _event(report_obj.case, E.REPORTER_VIEWED, by)
    record(A.MODERATION_REPORTER_VIEWED, target=report_obj.case, actor=by, request=request)
    u = report_obj.reporter
    return {"reporter": {"id": u.pk, "email": u.email, "name": u.full_name} if u else None, "contact_email": report_obj.contact_email}


# --------------------------------------------------------------------------- compliance (spec 13 §4.8, §6.7)
DEFINITIONS = {
    "large_platform": ("Large Service Platform?", "Registered accounts (not deleted) at the time of the report. "
                       "The Code's threshold is more than 1,000,000 users."),
    "accounts_closed": ("Closed accounts", "Accounts deleted in Clerk during the year."),
    "accounts_deactivated": ("Deactivated accounts", "Accounts deactivated by NDCConnect administrators during the year."),
    "removed_user_report": ("Content removed after a user report", "Removals during the year on cases opened by a user report."),
    "removed_agency": ("Content removed with notice (agency)", "Removals during the year after an Authorised Government Agency notice."),
    "removed_copyright": ("Content removed with notice (copyright)", "Removals during the year after a copyright notice."),
    "removed_court": ("Content removed with a court order", "Removals during the year after a court order."),
    "removed_proactive": ("Content removed without notice", "Removals during the year that NDCConnect found itself."),
    "restored": ("Content removed and re-uploaded", "Removed items restored during the year (appeal, mistake or counter-notice)."),
    "complaints": ("Number of complaints", "Reports plus legal notices received during the year."),
    "resolved": ("Resolved complaints", "Cases opened during the year and closed by 31 December."),
    "unresolved": ("Unresolved complaints", "Cases opened during the year still open on 31 December."),
    "disinformation": ("Disinformation/misinformation takedowns", "Removals during the year under R3 (results) and R4 (false information)."),
    "harmful": ("Removals protecting people from harmful content", "Removals during the year under R1, R2, R5 and R6."),
}
SOURCE_KEYS = {CaseSource.USER_REPORT: "removed_user_report", CaseSource.AGENCY: "removed_agency", CaseSource.COPYRIGHT: "removed_copyright",
               CaseSource.COURT: "removed_court", CaseSource.PROACTIVE: "removed_proactive"}


def _year_bounds(year):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Africa/Lagos")
    return datetime(year, 1, 1, tzinfo=tz), datetime(year + 1, 1, 1, tzinfo=tz)


def _compute(year) -> dict:
    from django.contrib.auth import get_user_model

    start, end = _year_bounds(year)
    U = get_user_model()
    removals = CaseEvent.objects.filter(event=E.REMOVED, created_at__gte=start, created_at__lt=end).select_related("case")
    figures = {k: 0 for k in DEFINITIONS}
    figures["registered_users"] = U.objects.filter(clerk_deleted_at__isnull=True).count()
    figures["large_platform"] = "Yes" if figures["registered_users"] > 1_000_000 else "No"
    figures["accounts_closed"] = U.objects.filter(clerk_deleted_at__gte=start, clerk_deleted_at__lt=end).count()
    figures["accounts_deactivated"] = AuditLog.objects.filter(action=A.USER_DEACTIVATED, created_at__gte=start, created_at__lt=end).count()
    harmful = {"R1": 0, "R2": 0, "R5": 0, "R6": 0}
    for ev in removals:
        figures[SOURCE_KEYS.get(ev.case.source, "removed_user_report")] += 1
        rule_id = ev.note.split(" ", 1)[0] if ev.note else ev.case.rule
        if rule_id in ("R3", "R4"):
            figures["disinformation"] += 1
        if rule_id in harmful:
            harmful[rule_id] += 1
    figures["harmful"] = sum(harmful.values())
    figures["harmful_by_rule"] = harmful
    figures["restored"] = CaseEvent.objects.filter(event=E.RESTORED, created_at__gte=start, created_at__lt=end).count()
    figures["complaints"] = (Report.objects.filter(created_at__gte=start, created_at__lt=end).count()
                             + LegalNotice.objects.filter(received_at__gte=start, received_at__lt=end).count())
    opened = Case.objects.filter(opened_at__gte=start, opened_at__lt=end)
    figures["resolved"] = opened.filter(closed_at__isnull=False, closed_at__lt=end).count()
    figures["unresolved"] = opened.count() - figures["resolved"]
    return figures


def compliance_report(year: int) -> dict:
    """Pure reads; cached 10 minutes. Years already pruned come from the frozen ComplianceYear row."""
    from django.core.cache import cache

    from .models import ComplianceYear

    frozen = ComplianceYear.objects.filter(year=year).first()
    if frozen:
        figures = frozen.figures
    else:
        key = f"moderation:compliance:{year}"
        figures = cache.get(key)
        if figures is None:
            figures = _compute(year)
            cache.set(key, figures, 600)
    items = [{"key": k, "label": label, "value": figures.get(k, 0), "definition": d} for k, (label, d) in DEFINITIONS.items()]
    return {"year": year, "frozen": bool(frozen), "registered_users": figures.get("registered_users", 0),
            "harmful_by_rule": figures.get("harmful_by_rule", {}), "safety_statement": settings.COMPLIANCE_SAFETY_STATEMENT, "items": items}


def compliance_csv(year: int) -> str:
    import csv
    import io

    rep = compliance_report(year)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["year", "item", "value", "definition"])
    for it in rep["items"]:
        w.writerow([year, it["label"], it["value"], it["definition"]])
    for rule_id, n in rep["harmful_by_rule"].items():
        w.writerow([year, f"Removals under {rule_id}: {R.rule_title(rule_id)}", n, "Part of the harmful-content figure."])
    w.writerow([year, "Safety statement", rep["safety_statement"], "From settings (COMPLIANCE_SAFETY_STATEMENT)."])
    return buf.getvalue()


# --------------------------------------------------------------------------- retention (spec 13 §10)
def prune(now=None) -> dict:
    """Closed cases (with their reports, events and appeals) 1 year after closing, except legal holds and
    cases with a pending appeal. Years about to lose records are frozen into ComplianceYear first."""
    from .models import ComplianceYear

    now = now or timezone.now()
    old = Case.objects.filter(status=CaseStatus.CLOSED, closed_at__lt=now - timedelta(days=365), legal_hold=False).exclude(
        appeals__status=Appeal.Status.PENDING)
    years = set()
    for c in old.only("opened_at", "closed_at"):
        years.add(timezone.localtime(c.opened_at).year)
        years.add(timezone.localtime(c.closed_at).year)
    for y in sorted(years):
        if y < timezone.localtime(now).year and not ComplianceYear.objects.filter(year=y).exists():
            ComplianceYear.objects.create(year=y, figures=_compute(y))
    ids = list(old.values_list("pk", flat=True))
    n_reports = Report.objects.filter(case_id__in=ids).count()
    with allow_history_delete():  # spec 16 §6.3
        Case.objects.filter(pk__in=ids).delete()  # events, reports, appeals and notices' items cascade
    return {"cases": len(ids), "reports": n_reports, "years_frozen": len(years)}


def recompute_bursts(now=None) -> int:
    now = now or timezone.now()
    n = 0
    for c in Case.objects.exclude(status=CaseStatus.CLOSED).filter(burst_flag=False):
        last = c.reports.order_by("-created_at").first()
        if last and _burst(c, last, now):
            Case.objects.filter(pk=c.pk).update(burst_flag=True)
            n += 1
    return n
