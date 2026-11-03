import hashlib
import json
import logging

from django.conf import settings
from django.db import connection, transaction

from .models import AuditLog

logger = logging.getLogger(__name__)

# Never written to metadata, whatever a caller passes.
_REDACT = {"password", "token", "secret", "vin", "pvc", "authorization", "phone_number", "email"}


def client_ip(request):
    if request is None:
        return None
    if settings.REST_FRAMEWORK.get("NUM_PROXIES"):
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def record(action, *, target=None, actor=None, actor_label="", request=None, **metadata):
    """
    Write one audit entry. Never raises into the caller: a failed audit write is logged
    loudly but must not break the operation it describes.

        record(AuditLog.Action.ROLE_GRANTED, target=assignment, actor_label="system:cli", role="moderator")
    """
    if actor is not None and not getattr(actor, "is_authenticated", False):
        actor = None
    clean = {k: v for k, v in metadata.items() if k.lower() not in _REDACT}
    try:
        row = AuditLog.objects.create(
            actor=actor,
            actor_label=actor_label,
            action=action,
            target_type=target._meta.label_lower if target is not None else "",
            target_id=str(target.pk) if target is not None else "",
            metadata=clean,
            ip_address=client_ip(request),
            user_agent=(request.META.get("HTTP_USER_AGENT", "")[:255] if request is not None else ""),
        )
    except Exception:  # pragma: no cover
        logger.exception("AUDIT WRITE FAILED action=%s target=%s", action, target)
        return None
    try:
        chain(row)
    except Exception:  # pragma: no cover - verify_audit reports the unhashed row
        logger.exception("AUDIT CHAIN FAILED for %s", row.pk)
    return row


# --------------------------------------------------------------------------
# Hash chain (spec 14 §4.8)
# --------------------------------------------------------------------------
CHAIN_LOCK = 7_314_001  # pg advisory lock key for audit chain appends


def canonical(row) -> str:
    return json.dumps({
        "id": str(row.id), "seq": row.seq, "actor": str(row.actor_id or ""), "actor_label": row.actor_label, "action": row.action,
        "target_type": row.target_type, "target_id": row.target_id, "metadata": row.metadata, "ip": row.ip_address or "",
        "ua": row.user_agent, "created_at": row.created_at.isoformat(),
    }, sort_keys=True, separators=(",", ":"), default=str)


def row_hash(row, prev_hash: str) -> str:
    return hashlib.sha256((prev_hash + "|" + canonical(row)).encode()).hexdigest()


def chain_tip():
    """(seq, hash) of the newest chained row, or of the anchor left by pruning, or (0, "")."""
    last = AuditLog.objects.filter(seq__isnull=False).order_by("-seq").values_list("seq", "hash").first()
    if last:
        return last
    from apps.privacy.models import AuditAnchor

    a = AuditAnchor.objects.order_by("-seq").first()
    return (a.seq, a.hash) if a else (0, "")


def chain(row):
    with transaction.atomic():
        with connection.cursor() as c:
            c.execute("SELECT pg_advisory_xact_lock(%s)", [CHAIN_LOCK])
        seq, prev = chain_tip()
        row.refresh_from_db()
        row.seq, row.prev_hash = seq + 1, prev
        row.hash = row_hash(row, prev)
        AuditLog.objects.filter(pk=row.pk).update(seq=row.seq, prev_hash=row.prev_hash, hash=row.hash)


def verify_chain() -> dict:
    """Walk every chained row in order. Returns {"ok", "checked", "broken_at", "unhashed"}."""
    from apps.privacy.models import AuditAnchor

    anchor = AuditAnchor.objects.order_by("-seq").first()
    expected_seq, prev = (anchor.seq, anchor.hash) if anchor else (0, "")
    checked = 0
    for row in AuditLog.objects.filter(seq__isnull=False).order_by("seq").iterator(chunk_size=2000):
        if row.seq != expected_seq + 1 or row.prev_hash != prev or row.hash != row_hash(row, prev):
            return {"ok": False, "checked": checked, "broken_at": row.seq, "unhashed": AuditLog.objects.filter(seq__isnull=True).count()}
        expected_seq, prev = row.seq, row.hash
        checked += 1
    unhashed = AuditLog.objects.filter(seq__isnull=True).count()
    return {"ok": unhashed == 0, "checked": checked, "broken_at": None, "unhashed": unhashed}


# --------------------------------------------------------------------------
# Audit log viewer (spec 11 §4.5, §6.6)
# --------------------------------------------------------------------------
def search(params) -> tuple:
    """Filter the log from query params. Returns (queryset, filters-as-stored). Emails are stored as user IDs."""
    from datetime import datetime, time, timedelta

    from django.contrib.auth import get_user_model
    from django.db.models import Q
    from django.utils import timezone

    from .display import AREAS

    qs = AuditLog.objects.select_related("actor").order_by("-created_at")
    stored = {}
    area = params.get("area", "")
    if area in AREAS:
        q = Q()
        for prefix in AREAS[area][1]:
            q |= Q(action__startswith=prefix)
        qs = qs.filter(q)
        stored["area"] = area
    if area != "audit":
        qs = qs.exclude(action__startswith="audit.")  # audit views only when asked for (spec 11 §4.5)
    if params.get("action"):
        qs = qs.filter(action=params["action"])
        stored["action"] = params["action"]
    actor = params.get("actor", "").strip()
    if actor:
        if actor.lower() == "system":
            qs = qs.filter(actor__isnull=True)
            stored["actor"] = "system"
        else:
            u = get_user_model().objects.filter(email__iexact=actor).first()
            qs = qs.filter(actor=u) if u else qs.none()
            stored["actor"] = str(u.pk) if u else "unknown"
    if params.get("target"):
        qs = qs.filter(target_id=params["target"].strip())
        stored["target"] = params["target"].strip()[:64]
    tz = timezone.get_current_timezone()
    for key, op, end in (("from", "gte", False), ("to", "lt", True)):
        raw = params.get(key, "")
        if raw:
            try:
                d = datetime.strptime(raw, "%Y-%m-%d").date()
            except ValueError:
                continue
            at = timezone.make_aware(datetime.combine(d + timedelta(days=1) if end else d, time.min), tz)
            qs = qs.filter(**{f"created_at__{op}": at})
            stored[key] = raw
    return qs, stored
