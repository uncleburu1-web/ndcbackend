import logging

from django.conf import settings

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
        return AuditLog.objects.create(
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
