"""Reports on candidate posts: a thin wrapper over moderation (spec 13 §5.3), kept so 09's paths still work."""
from apps.moderation import services as moderation


def report(post, reason: str, details: str = "", *, user=None, contact_email="", ip="") -> dict:
    return moderation.report("post", post, reason, details, user=user, contact_email=contact_email, ip=ip)


def dismiss(report_obj, admin, note: str = "", *, request=None):
    return moderation.dismiss_report(report_obj, admin, note, request=request)
