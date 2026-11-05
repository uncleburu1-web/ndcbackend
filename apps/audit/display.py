"""
How audit entries are shown in the admin (spec 11 §6.6).

Only metadata keys listed in ALLOWED leave the server. A new key added to some `record()` call
stays hidden until it is listed here on purpose. Counts that reveal one person's political
choices (follows removed, membership erased, donations anonymised at account deletion) are
never listed (NDPA; spec 11 §10).
"""
from .models import AuditLog

A = AuditLog.Action

# action → (words, allowed metadata keys)
ACTIONS: dict[str, tuple[str, tuple[str, ...]]] = {
    A.USER_CREATED: ("account created", ()),
    A.USER_UPDATED: ("account updated from Clerk", ("fields",)),
    A.USER_DELETED: ("account deleted in Clerk", ("roles_revoked",)),
    A.PROFILE_COMPLETED: ("completed their profile", ()),
    A.PROFILE_UPDATED: ("updated their profile", ("fields",)),
    A.ROLE_GRANTED: ("granted a role", ("role", "scope_type")),
    A.ROLE_REVOKED: ("revoked a role", ("role", "reason")),
    A.USER_DEACTIVATED: ("deactivated an account", ("reason", "clerk_ban", "retry")),
    A.USER_REACTIVATED: ("reactivated an account", ("reason", "clerk_unban", "retry")),
    A.USER_VIEWED: ("opened an account page", ()),
    A.USER_PHONE_REVEALED: ("revealed a phone number", ()),
    A.CANDIDATE_CREATED: ("added a candidate", ("slug",)),
    A.CANDIDATE_UPDATED: ("edited a candidate", ("fields",)),
    A.CANDIDATE_VERIFIED: ("verified a candidate", ("notes",)),
    A.CANDIDATE_UNVERIFIED: ("removed a candidate's verification", ("fields", "reason")),
    A.CANDIDATE_PUBLISHED: ("published a candidate", ()),
    A.CANDIDATE_UNPUBLISHED: ("unpublished a candidate", ("fields", "reason")),
    A.CANDIDATE_ARCHIVED: ("archived a candidate", ("reason", "links_revoked")),
    A.CANDIDATE_RESTORED: ("restored a candidate", ()),
    A.CANDIDATE_PHOTO_UPDATED: ("changed a candidate photo", ("change",)),
    A.DONATION_VIEWED: ("opened a donation", ()),
    A.DONATION_EXPORTED: ("exported donations", ("rows", "filters")),
    A.DONATION_RECHECKED: ("rechecked a donation", ("status",)),
    A.DONATION_LABEL_ANONYMISED: ("anonymised donation names", ()),
    A.PVC_SUBMITTED: ("submitted PVC details", ()),
    A.PVC_VERIFIED: ("verified a PVC", ()),
    A.PVC_REJECTED: ("rejected a PVC check", ()),
    A.PVC_VIN_REVEALED: ("revealed a VIN", ()),
    A.PVC_SETTINGS_CHANGED: ("changed PVC guidance", ("fields",)),
    A.MEMBERSHIP_SUBMITTED: ("submitted a membership application", ()),
    A.MEMBERSHIP_PAYMENT_STARTED: ("started a membership payment", ()),
    A.MEMBERSHIP_ACTIVATED: ("activated a membership", ()),
    A.MEMBERSHIP_APPROVED: ("approved a membership", ()),
    A.MEMBERSHIP_REJECTED: ("rejected a membership", ("reason", "refund_due")),
    A.MEMBERSHIP_CANCELLED: ("cancelled a membership", ("reason",)),
    A.MEMBERSHIP_NIN_REVEALED: ("revealed a NIN", ()),
    A.MEMBERSHIP_PHOTO_VIEWED: ("viewed a membership photo", ()),
    A.MEMBERSHIP_REGISTER_EXPORTED: ("exported the membership register", ("kind", "rows", "state")),
    A.MEMBERSHIP_SETTINGS_CHANGED: ("changed membership settings", ("requires_approval",)),
    A.MEMBERSHIP_ERASED: ("erased a membership on account deletion", ()),
    A.MEMBERSHIP_PAYMENT_RECHECKED: ("rechecked a membership payment", ()),
    A.NEWS_SUBMITTED: ("submitted community news", ()),
    A.NEWS_EDITED: ("edited community news", ("fields",)),
    A.NEWS_WITHDRAWN: ("withdrew community news", ()),
    A.NEWS_APPROVED: ("approved community news", ()),
    A.NEWS_REJECTED: ("rejected community news", ()),
    A.NEWS_REMOVED: ("removed news", ("rule",)),
    A.NEWS_RESTORED: ("restored news", ()),
    A.NEWS_REPORT_RESOLVED: ("resolved a news report", ("outcome",)),
    A.NEWS_ERASED: ("erased news on account deletion", ()),
    A.ANNOUNCEMENT_PUBLISHED: ("published an announcement", ("category", "notify")),
    A.ANNOUNCEMENT_UPDATED: ("edited an announcement", ("fields",)),
    A.ANNOUNCEMENT_UNPUBLISHED: ("unpublished an announcement", ("reports_resolved", "rule")),
    A.ANNOUNCEMENT_RESTORED: ("restored an announcement", ()),
    A.ANNOUNCEMENT_PINNED: ("pinned or unpinned an announcement", ("pinned",)),
    A.ANNOUNCEMENT_DELIVERED: ("delivered announcement notifications", ("delivered",)),
    A.POST_CREATED: ("published a candidate post", ("kind",)),
    A.POST_UPDATED: ("edited a candidate post", ("fields",)),
    A.POST_IMAGE_UPDATED: ("changed a candidate post image", ("change",)),
    A.POST_DELETED: ("deleted a candidate post", ()),
    A.POST_REMOVED: ("removed a candidate post", ("reports_resolved", "rule")),
    A.POST_RESTORED: ("restored a candidate post", ()),
    A.POST_REPORT_RESOLVED: ("resolved a candidate post report", ("outcome",)),
    A.POST_DELIVERED: ("delivered candidate post notifications", ("delivered",)),
    A.AUDIT_VIEWED: ("searched the audit log", ("filters",)),
    A.MODERATION_CASE_CLAIMED: ("took a moderation case", ()),
    A.MODERATION_KEPT: ("closed a case with no action", ()),
    A.MODERATION_REMOVED: ("removed content under a rule", ("rule", "kind")),
    A.MODERATION_RESTORED: ("restored removed content", ("kind",)),
    A.MODERATION_ESCALATED: ("escalated a case to system administrators", ()),
    A.MODERATION_WARNED: ("warned an author", ("rule",)),
    A.MODERATION_PAUSED: ("paused an author's community news or community posting", ("days", "rule", "kind")),
    A.MODERATION_PAUSE_LIFTED: ("lifted a pause", ()),
    A.MODERATION_APPEAL_DECIDED: ("decided an appeal", ("upheld",)),
    A.MODERATION_REPORTER_VIEWED: ("revealed who filed a report", ()),
    A.MODERATION_LEGAL_NOTICE_LOGGED: ("logged a legal notice", ("kind", "items")),
    A.MODERATION_LEGAL_NOTICE_UPDATED: ("updated a legal notice", ("change",)),
    A.MODERATION_LEGAL_HOLD_CHANGED: ("changed a legal hold", ("on",)),
    A.MODERATION_ATTACHMENT_VIEWED: ("opened a legal notice attachment", ()),
    A.MODERATION_COMPLIANCE_EXPORTED: ("downloaded the compliance report", ("year",)),
    A.PRIVACY_CONSENT_WITHDRAWN: ("withdrew a consent", ("purpose",)),
    A.PRIVACY_REQUEST_VIEWED: ("opened a privacy request", ("reference",)),
    A.PRIVACY_REQUEST_CLOSED: ("closed a privacy request", ("reference", "outcome")),
    A.PRIVACY_DATA_EXPORTED_BY_OFFICER: ("exported a person's data for a privacy request", ("reference",)),
    A.PRIVACY_ACCOUNT_DELETED_SELF: ("deleted their own account", ()),
    A.PRIVACY_LINKED_ACTION: ("acted on a privacy request", ("reference", "linked_action")),
    A.SECURITY_INCIDENT_LOGGED: ("logged an incident", ("reference", "risk")),
    A.SECURITY_INCIDENT_UPDATED: ("updated an incident", ("reference", "fields", "notified")),
    A.SECURITY_INCIDENT_CLOSED: ("closed an incident", ("reference",)),
    A.SECURITY_NOTICE_SENT: ("sent a security notice", ("reference", "recipients", "audience", "version")),
    A.SECURITY_INCIDENTS_EXPORTED: ("exported the breach register", ("rows",)),
    A.SECURITY_ACCESS_REVIEW_COMPLETED: ("finished an access review", ("kept", "revoked")),
    A.SECURITY_ADMIN_ACCESS_DENIED: ("was refused admin access repeatedly", ("count", "window")),
    A.SECURITY_RESTORE_DRILL: ("recorded a restore test", ("notes",)),
    A.SECURITY_WEBHOOK_SIGNATURE_FAILURES: ("saw repeated webhook signature failures", ("provider", "count", "window")),
    A.INTEGRATION_JOB_RETRIED: ("retried a background task", ("kind", "attempts")),
    A.MODERATION_HIDDEN: ("hid a community item while deciding", ("kind",)),
    A.MODERATION_UNHIDDEN: ("unhid a community item", ("kind",)),
    A.COMMUNITY_SETTINGS_CHANGED: ("changed a community's settings", ("fields",)),
    A.COMMUNITY_COUNTS_VIEWED: ("viewed community member counts", ()),
}

# area → (words, action prefixes)
AREAS: dict[str, tuple[str, tuple[str, ...]]] = {
    "accounts": ("Accounts and roles", ("user.", "profile.", "role.")),
    "candidates": ("Candidates", ("candidate.",)),
    "donations": ("Donations", ("donation.",)),
    "pvc": ("PVC", ("pvc.",)),
    "membership": ("Membership", ("membership.",)),
    "news": ("News", ("news.",)),
    "announcements": ("Announcements", ("announcement.",)),
    "posts": ("Candidate posts", ("post.",)),
    "moderation": ("Moderation", ("moderation.",)),
    "communities": ("Communities", ("community.",)),
    "privacy": ("Privacy", ("privacy.",)),
    "security": ("Security", ("security.",)),
    "audit": ("Audit views", ("audit.",)),
}

TARGET_WORDS = {
    "accounts.user": "Account",
    "access.roleassignment": "Role",
    "candidates.candidate": "Candidate",
    "donations.donation": "Donation",
    "pvc.pvcverification": "PVC check",
    "pvc.pvcsettings": "PVC guidance",
    "membership.membership": "Membership",
    "membership.membershipsettings": "Membership settings",
    "news.article": "Article",
    "posts.candidatepost": "Candidate post",
    "moderation.case": "Moderation case",
    "moderation.restriction": "Author restriction",
    "moderation.appeal": "Appeal",
    "moderation.legalnotice": "Legal notice",
    "communities.community": "Community",
    "moderation.report": "Report",
    "privacy.datarequest": "Privacy request",
    "privacy.incident": "Incident",
    "privacy.accessreview": "Access review",
}

TARGET_LINKS = {
    "candidates.candidate": "/admin/candidates/{id}",
    "news.article": "/admin/news/{id}",
    "posts.candidatepost": "/admin/posts/{id}",
    "membership.membership": "/admin/membership/{id}",
    "donations.donation": "/admin/donations/{id}",
    "accounts.user": "/admin/users/{id}",
    "access.roleassignment": "/admin/team",
    "moderation.case": "/admin/moderation/{id}",
    "moderation.legalnotice": "/admin/moderation/legal/{id}",
    "privacy.datarequest": "/admin/privacy/requests/{id}",
    "privacy.incident": "/admin/privacy/incidents/{id}",
}

ACTOR_LABELS = {"system:cli": "System (CLI)", "clerk:webhook": "Clerk", "system:jit": "System", "system:notifications": "System",
                "public:copyright_form": "Copyright form"}


def action_label(action: str) -> str:
    return ACTIONS.get(action, (action.replace(".", " ").replace("_", " "), ()))[0]


def allowed_metadata(action: str, metadata: dict) -> dict:
    keys = ACTIONS.get(action, ("", ()))[1]
    return {k: metadata[k] for k in keys if k in (metadata or {})}


def actor_display(entry) -> str:
    if entry.actor_id:
        return entry.actor.email if entry.actor and entry.actor.email else "Deleted account"
    return ACTOR_LABELS.get(entry.actor_label, "System")


def target_link(entry) -> str | None:
    tpl = TARGET_LINKS.get(entry.target_type)
    return tpl.format(id=entry.target_id) if tpl and entry.target_id else None


def summary(action: str, meta: dict) -> str:
    """One short line from allowed metadata, for the list and the dashboard activity feed."""
    from apps.access.models import Role

    if "role" in meta:
        try:
            return Role(meta["role"]).label
        except ValueError:
            return str(meta["role"])
    if meta.get("reason"):
        return str(meta["reason"])[:120]
    if meta.get("fields"):
        f = meta["fields"]
        return "Changed: " + (", ".join(map(str, f)) if isinstance(f, (list, tuple)) else str(f))[:120]
    for k in ("delivered", "rows", "reports_resolved"):
        if k in meta:
            return f"{k.replace('_', ' ').capitalize()}: {meta[k]}"
    return ""


def entry_payload(entry, *, full=False) -> dict:
    meta = allowed_metadata(entry.action, entry.metadata)
    data = {
        "id": str(entry.id), "created_at": entry.created_at, "action": entry.action, "action_label": action_label(entry.action),
        "actor": actor_display(entry), "actor_id": str(entry.actor_id) if entry.actor_id else None,
        "target_type": entry.target_type, "target_label": TARGET_WORDS.get(entry.target_type, ""), "target_id": entry.target_id,
        "link": target_link(entry), "summary": summary(entry.action, meta),
    }
    if full:
        data.update(metadata=meta, ip_address=entry.ip_address, user_agent=entry.user_agent)
    return data
