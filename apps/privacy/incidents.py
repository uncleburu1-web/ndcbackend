"""Incidents and breaches (spec 14 §4.7, §6.5). Clocks are shown, never acted on automatically: people record what they sent."""
import csv
import io
import re
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.audit.models import AuditLog
from apps.audit.services import record
from apps.core.exceptions import ServiceError

from .models import Incident, IncidentEvent, new_reference
from .register import category_keys

CLOCK_HOURS = 72
PROCESSORS = ("", "clerk", "paystack", "hosting")
NOTIFY_KINDS = ("ndpc", "ngcert", "people")
EDITABLE = ("title", "description", "detected_at", "aware_at", "systems", "data_categories", "people_affected_estimate", "processor", "risk",
            "is_personal_data_breach", "is_cyber_incident", "status")
VERSIONED = ("risk", "data_categories", "is_personal_data_breach")
LINK_RE = re.compile(r"(https?://|www\.|\b[a-z0-9-]+\.(com|ng|org|net|io|xyz|info|co)\b)", re.I)
MAX_AUDIENCE_LINES = 5000


def _err(msg, code="validation_error", status=400):
    return ServiceError(msg, code, status)


def _when(v, field):
    if v in (None, ""):
        return None
    if hasattr(v, "tzinfo"):
        return v
    d = parse_datetime(str(v))
    if d is None:
        raise _err(f"{field}: use a date and time.")
    if timezone.is_naive(d):
        d = timezone.make_aware(d)
    if d > timezone.now() + timedelta(minutes=5):
        raise _err(f"{field} can't be in the future.")
    return d


def _clean(data, partial=False):
    out = {}
    for k in EDITABLE:
        if k not in data:
            continue
        v = data[k]
        if k in ("detected_at", "aware_at"):
            v = _when(v, k.replace("_", " "))
            if v is None:
                continue
        elif k == "title":
            v = (v or "").strip()
            if len(v) < 5:
                raise _err("Give the incident a title (at least 5 characters).")
            v = v[:200]
        elif k in ("description", "systems"):
            v = (v or "").strip()[:4000 if k == "description" else 300]
        elif k == "data_categories":
            v = list(dict.fromkeys(v or []))
            bad = [c for c in v if c not in category_keys()]
            if bad:
                raise _err("Unknown data category: " + ", ".join(bad))
        elif k == "people_affected_estimate":
            v = None if v in (None, "") else int(v)
            if v is not None and v < 0:
                raise _err("People affected can't be negative.")
        elif k == "processor":
            v = v or ""
            if v not in PROCESSORS:
                raise _err("Choose Clerk, Paystack, hosting or none.")
        elif k == "risk":
            if v not in Incident.Risk.values:
                raise _err("Choose a risk level.")
        elif k in ("is_personal_data_breach", "is_cyber_incident"):
            v = bool(v)
        elif k == "status":
            if v not in (Incident.Status.OPEN, Incident.Status.CONTAINED):
                raise _err("Use Close to close an incident.", "invalid_transition", 409)
        out[k] = v
    return out


def _event(inc, event, actor, note="", at=None):
    return IncidentEvent.objects.create(incident=inc, event=event, actor=actor, note=(note or "")[:2000], at=at or timezone.now())


@transaction.atomic
def log(by, data):
    if "title" not in data:
        raise _err("Give the incident a title (at least 5 characters).")
    fields = _clean(data)
    now = timezone.now()
    fields.setdefault("detected_at", now)
    fields.setdefault("aware_at", now)
    if fields["aware_at"] < fields["detected_at"] - timedelta(days=3650):
        raise _err("Check the dates.")
    for _ in range(5):
        try:
            with transaction.atomic():
                inc = Incident.objects.create(reference=new_reference("INC"), created_by=by, notice_version=1, **fields)
                break
        except IntegrityError:
            continue
    else:
        raise _err("Couldn't create a reference. Try again.", "reference_unavailable", 503)
    _event(inc, "logged", by)
    record(AuditLog.Action.SECURITY_INCIDENT_LOGGED, target=inc, actor=by, reference=inc.reference, risk=inc.risk)
    return inc


def _locked(pk):
    try:
        return Incident.objects.select_for_update().get(pk=pk)
    except (Incident.DoesNotExist, ValueError):
        raise _err("Incident not found.", "not_found", 404)


def _open(inc):
    if inc.status == Incident.Status.CLOSED:
        raise _err("This incident is closed.", "invalid_transition", 409)


@transaction.atomic
def update(pk, by, data):
    inc = _locked(pk)
    _open(inc)
    fields = _clean(data, partial=True)
    changed = [k for k, v in fields.items() if getattr(inc, k) != v]
    if not changed:
        return inc
    for k in changed:
        setattr(inc, k, fields[k])
    if any(k in VERSIONED for k in changed):
        inc.notice_version += 1
        changed.append("notice_version")
    inc.save(update_fields=changed)
    _event(inc, "updated", by, "Changed: " + ", ".join(c for c in changed if c not in ("description", "notice_version")))
    record(AuditLog.Action.SECURITY_INCIDENT_UPDATED, target=inc, actor=by, reference=inc.reference,
           fields=[c for c in changed if c != "notice_version"])
    return inc


@transaction.atomic
def add_step(pk, by, text, at=None):
    inc = _locked(pk)
    _open(inc)
    t = (text or "").strip()
    if len(t) < 3:
        raise _err("Describe the step.")
    return _event(inc, "step", by, t, _when(at, "time") or timezone.now())


@transaction.atomic
def record_notification(pk, by, kind, *, at=None, how="", reference=""):
    if kind not in NOTIFY_KINDS:
        raise _err("Choose NDPC, ngCERT or people affected.")
    inc = _locked(pk)
    at = _when(at, "sent at") or timezone.now()
    how = (how or "").strip()
    if len(how) < 3:
        raise _err("Say how it was sent (e.g. NDPC portal, email to ngCERT).")
    reference = (reference or "").strip()[:100]
    setattr(inc, f"{kind}_notified_at", at)
    fields = [f"{kind}_notified_at"]
    if kind in ("ndpc", "ngcert"):
        setattr(inc, f"{kind}_reference", reference)
        fields.append(f"{kind}_reference")
    inc.save(update_fields=fields)
    label = {"ndpc": "NDPC", "ngcert": "ngCERT", "people": "People affected"}[kind]
    _event(inc, f"notified_{kind}", by, f"{label} notified by {how}" + (f" (ref {reference})" if reference else ""), at)
    record(AuditLog.Action.SECURITY_INCIDENT_UPDATED, target=inc, actor=by, reference=inc.reference, notified=kind)
    return inc


def _check_text(text):
    t = (text or "").strip()
    if len(t) < 20:
        raise _err("Write the notice (at least 20 characters).")
    if len(t) > 1000:
        raise _err("Keep the notice under 1,000 characters.")
    if "<" in t or ">" in t:
        raise _err("Plain text only.")
    if LINK_RE.search(t):
        raise _err("Links to other sites aren't allowed. Use NDCConnect paths like /account/privacy.")
    return t


def match_audience(lines_text):
    """Pasted account IDs or emails, one per line (matched server-side). Returns (users, unmatched lines)."""
    from apps.accounts.models import User

    lines = [l.strip() for l in (lines_text or "").splitlines() if l.strip()]
    lines = list(dict.fromkeys(lines))[:MAX_AUDIENCE_LINES]
    ids, emails = set(), set()
    for l in lines:
        if "@" in l:
            emails.add(l.lower())
        else:
            ids.add(l)
    q = Q(email__in=emails)
    valid_ids = []
    for i in ids:
        try:
            import uuid

            valid_ids.append(uuid.UUID(i))
        except ValueError:
            continue
    if valid_ids:
        q |= Q(pk__in=valid_ids)
    users = list(User.objects.filter(q, is_active=True, clerk_deleted_at__isnull=True)) if (emails or valid_ids) else []
    found = {u.email.lower() for u in users} | {str(u.pk) for u in users}
    unmatched = [l for l in lines if (l.lower() if "@" in l else l) not in found]
    return users, unmatched


def audience_users(audience, lines_text=""):
    from apps.accounts.models import User

    if audience == "everyone":
        return list(User.objects.filter(is_active=True, clerk_deleted_at__isnull=True)), []
    if audience == "list":
        users, unmatched = match_audience(lines_text)
        return users, unmatched
    raise _err("Choose everyone or a list of accounts.")


def preview_notice(audience, lines_text=""):
    users, unmatched = audience_users(audience, lines_text)
    return {"count": len(users), "unmatched": unmatched[:200], "unmatched_count": len(unmatched)}


@transaction.atomic
def send_notice(pk, by, text, audience, lines_text=""):
    from apps.notifications.models import Kind, Notification

    inc = _locked(pk)
    if inc.notice_sent_version >= inc.notice_version:
        raise _err("A notice was already sent for this version of the incident. Update the incident to send another.", "notice_already_sent", 409)
    t = _check_text(text)
    users, unmatched = audience_users(audience, lines_text)
    if not users:
        raise _err("No accounts matched.", "no_recipients")
    Notification.objects.bulk_create([Notification(recipient=u, kind=Kind.SECURITY_NOTICE, incident=inc) for u in users], batch_size=1000)
    now = timezone.now()
    inc.notice_text, inc.notice_sent_at, inc.notice_sent_version = t, now, inc.notice_version
    fields = ["notice_text", "notice_sent_at", "notice_sent_version"]
    if inc.people_notified_at is None:
        inc.people_notified_at = now
        fields.append("people_notified_at")
    inc.save(update_fields=fields)
    _event(inc, "notice_sent", by, f"Security notice sent in-app to {len(users)} account(s)" + (" (everyone)" if audience == "everyone" else ""))
    record(AuditLog.Action.SECURITY_NOTICE_SENT, target=inc, actor=by, reference=inc.reference, recipients=len(users), audience=audience,
           version=inc.notice_version)
    return {"sent": len(users), "unmatched": unmatched[:200], "unmatched_count": len(unmatched)}


@transaction.atomic
def close(pk, by, root_cause, lessons=""):
    inc = _locked(pk)
    _open(inc)
    rc = (root_cause or "").strip()
    if len(rc) < 10:
        raise _err("Write the root cause (at least 10 characters).")
    inc.status, inc.root_cause, inc.lessons, inc.closed_at = Incident.Status.CLOSED, rc[:2000], (lessons or "").strip()[:2000], timezone.now()
    inc.save(update_fields=["status", "root_cause", "lessons", "closed_at"])
    _event(inc, "closed", by)
    record(AuditLog.Action.SECURITY_INCIDENT_CLOSED, target=inc, actor=by, reference=inc.reference)
    return inc


# --------------------------------------------------------------------------- clocks
def _hours_words(delta_s):
    h = abs(delta_s) / 3600
    if h >= 48:
        n = int(h // 24)
        return f"{n} days"
    if h >= 1:
        n = int(h)
        return f"{n} hour{'s' if n != 1 else ''}"
    n = max(int(abs(delta_s) // 60), 1)
    return f"{n} minute{'s' if n != 1 else ''}"


def clocks(inc, now=None):
    """[{key, label, due_at, done_at, state: due|overdue|done, words}]"""
    now = now or timezone.now()
    out = []

    def clock(key, label, due, done):
        if done:
            out.append({"key": key, "label": label, "due_at": due, "done_at": done, "state": "done",
                        "words": f"{label}: notified {timezone.localtime(done):%-d %b %H:%M}"})
            return
        if key == "people":
            out.append({"key": key, "label": label, "due_at": due, "done_at": None, "state": "overdue" if now > due + timedelta(hours=1) else "due",
                        "words": f"{label}: tell them now"})
            return
        left = (due - now).total_seconds()
        state = "due" if left >= 0 else "overdue"
        words = f"{label}: due in {_hours_words(left)}" if left >= 0 else f"{label}: overdue by {_hours_words(left)}"
        out.append({"key": key, "label": label, "due_at": due, "done_at": None, "state": state, "words": words})

    if inc.is_personal_data_breach and inc.risk != Incident.Risk.NONE:
        clock("ndpc", "NDPC", inc.aware_at + timedelta(hours=CLOCK_HOURS), inc.ndpc_notified_at)
    if inc.is_cyber_incident:
        clock("ngcert", "ngCERT", inc.detected_at + timedelta(hours=CLOCK_HOURS), inc.ngcert_notified_at)
    if inc.risk == Incident.Risk.HIGH:
        clock("people", "People affected", inc.aware_at, inc.people_notified_at)
    return out


def open_clocks(now=None):
    now = now or timezone.now()
    rows = []
    for inc in Incident.objects.exclude(status=Incident.Status.CLOSED):
        for c in clocks(inc, now):
            if c["state"] != "done":
                rows.append((inc, c))
    return rows


def payload(inc, *, detail=False, now=None):
    out = {"id": str(inc.pk), "reference": inc.reference, "title": inc.title, "status": inc.status, "status_label": inc.get_status_display(),
           "risk": inc.risk, "risk_label": inc.get_risk_display(), "detected_at": inc.detected_at, "aware_at": inc.aware_at,
           "is_personal_data_breach": inc.is_personal_data_breach, "is_cyber_incident": inc.is_cyber_incident,
           "clocks": clocks(inc, now), "closed_at": inc.closed_at}
    if detail:
        out.update({
            "description": inc.description, "systems": inc.systems, "data_categories": inc.data_categories,
            "people_affected_estimate": inc.people_affected_estimate, "processor": inc.processor,
            "ndpc_notified_at": inc.ndpc_notified_at, "ndpc_reference": inc.ndpc_reference, "ngcert_notified_at": inc.ngcert_notified_at,
            "ngcert_reference": inc.ngcert_reference, "people_notified_at": inc.people_notified_at,
            "notice_version": inc.notice_version, "notice_sent_version": inc.notice_sent_version, "notice_text": inc.notice_text,
            "notice_sent_at": inc.notice_sent_at, "can_send_notice": inc.notice_sent_version < inc.notice_version,
            "root_cause": inc.root_cause, "lessons": inc.lessons,
            "events": [{"event": e.event, "note": e.note, "at": e.at, "by": (e.actor.full_name or e.actor.email) if e.actor_id else ""}
                       for e in inc.events.select_related("actor")],
        })
    return out


CSV_COLUMNS = ["reference", "title", "status", "risk", "personal_data_breach", "cyber_incident", "detected_at", "aware_at", "systems",
               "data_categories", "people_affected_estimate", "processor", "ndpc_notified_at", "ndpc_reference", "ngcert_notified_at",
               "ngcert_reference", "people_notified_at", "closed_at"]


def _cell(v):
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s  # spreadsheet formula injection


def export_csv(by, request=None) -> str:
    """The NDPA s. 40 breach register. Free text (description, steps, notice, root cause) stays out: it may hold personal data."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_COLUMNS)
    n = 0
    for inc in Incident.objects.order_by("aware_at"):
        w.writerow([_cell(x) for x in [inc.reference, inc.title, inc.status, inc.risk, inc.is_personal_data_breach, inc.is_cyber_incident,
                                       inc.detected_at.isoformat(), inc.aware_at.isoformat(), inc.systems, ";".join(inc.data_categories),
                                       inc.people_affected_estimate, inc.processor,
                                       inc.ndpc_notified_at.isoformat() if inc.ndpc_notified_at else "", inc.ndpc_reference,
                                       inc.ngcert_notified_at.isoformat() if inc.ngcert_notified_at else "", inc.ngcert_reference,
                                       inc.people_notified_at.isoformat() if inc.people_notified_at else "",
                                       inc.closed_at.isoformat() if inc.closed_at else ""]])
        n += 1
    record(AuditLog.Action.SECURITY_INCIDENTS_EXPORTED, actor=by, request=request, rows=n)
    return buf.getvalue()
