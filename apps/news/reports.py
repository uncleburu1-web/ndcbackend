"""Reports on articles: a thin wrapper over moderation (spec 13 §5.3), kept so 07's paths and callers still work."""
from apps.moderation import services as moderation
from apps.moderation.services import ip_hash  # noqa: F401  (re-exported for older callers)


def report(article, reason: str, details: str = "", *, user=None, contact_email="", ip="") -> dict:
    return moderation.report("article", article, reason, details, user=user, contact_email=contact_email, ip=ip)


def dismiss(report_obj, reviewer, note: str = "", *, request=None):
    return moderation.dismiss_report(report_obj, reviewer, note, request=request)
