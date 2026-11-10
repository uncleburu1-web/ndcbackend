"""
In-app notifications — spec 07 §6.4; one service since spec 15 §9.1. The only place notifications are written.

    notify(user, "membership_decision", target=membership, decision="approved")

`notify` writes one row in the caller's transaction (a decision and its notice commit together). Fan-out kinds
(announcements, candidate posts) are outbox jobs (`notify_fanout`) running one idempotent INSERT … SELECT.
"""
import logging
from datetime import timedelta

from django.db import connection
from django.db.models import Q
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.audit.services import record

from .models import Kind, Notification, NotificationPreference

logger = logging.getLogger(__name__)
UNREAD_CAP = 100

# spec 17 §9: an announcement aimed at a community goes to its members and the members of every community below it
AUDIENCE_SQL = """
INSERT INTO notifications_notification (id, recipient_id, kind, article_id, decision, count, created_at)
SELECT gen_random_uuid(), m.user_id, 'announcement', %(article)s, '', 1, now()
FROM (SELECT DISTINCT cm.user_id FROM communities_communitymembership cm JOIN communities_community c ON c.id = cm.community_id
      WHERE cm.status = 'active' AND (c.path = %(path)s OR c.path LIKE %(prefix)s)) m
JOIN accounts_user u ON u.id = m.user_id LEFT JOIN notifications_notificationpreference p ON p.user_id = u.id
WHERE u.is_active AND u.clerk_deleted_at IS NULL AND COALESCE(p.announcements, TRUE)
ON CONFLICT DO NOTHING
"""

COMMUNITY_POST_SQL = """
INSERT INTO notifications_notification (id, recipient_id, kind, community_post_id, community_id, decision, count, created_at)
SELECT gen_random_uuid(), cm.user_id, 'community_post', %(post)s, %(community)s, '', 1, now()
FROM communities_communitymembership cm JOIN accounts_user u ON u.id = cm.user_id
WHERE cm.community_id = %(community)s AND cm.status = 'active' AND cm.notify_new_posts AND cm.user_id <> %(author)s
  AND u.is_active AND u.clerk_deleted_at IS NULL
ON CONFLICT DO NOTHING
"""

ANNOUNCEMENT_SQL = """
INSERT INTO notifications_notification (id, recipient_id, kind, article_id, decision, count, created_at)
SELECT gen_random_uuid(), u.id, 'announcement', %(article)s, '', 1, now()
FROM accounts_user u LEFT JOIN notifications_notificationpreference p ON p.user_id = u.id
WHERE u.is_active AND u.clerk_deleted_at IS NULL AND COALESCE(p.announcements, TRUE)
ON CONFLICT DO NOTHING
"""


def eligible_for_announcements():
    from apps.accounts.models import User

    return User.objects.filter(is_active=True, clerk_deleted_at__isnull=True).exclude(notification_preference__announcements=False)


def deliver_announcement(article) -> int:
    """One idempotent statement for any number of users (no job queue needed)."""
    aud = getattr(article, "audience_community", None)
    with connection.cursor() as cur:
        if aud is not None and aud.level != "national":
            cur.execute(AUDIENCE_SQL, {"article": article.pk, "path": aud.path, "prefix": aud.path.replace("%", r"\%") + "/%"})
        else:
            cur.execute(ANNOUNCEMENT_SQL, {"article": article.pk})
        n = cur.rowcount
    record(AuditLog.Action.ANNOUNCEMENT_DELIVERED, target=article, actor_label="system:notifications", delivered=n)
    return n


def deliver_announcement_on_commit(article):
    """Spec 15 §9.3: an outbox job in the publishing transaction, tried right after commit, retried if it fails."""
    from apps.core import outbox

    outbox.enqueue("notify_fanout", {"kind": "announcement", "article": str(article.pk)}, dedupe_key=f"fanout:announcement:{article.pk}")


POST_SQL = """
INSERT INTO notifications_notification (id, recipient_id, kind, candidate_post_id, article_id, decision, created_at)
SELECT gen_random_uuid(), f.user_id, 'candidate_post', %(post)s, NULL, '', now()
FROM follows_candidatefollow f JOIN accounts_user u ON u.id = f.user_id
WHERE f.candidate_id = %(candidate)s AND f.notify AND f.notify_changed_at <= %(published_at)s
  AND u.is_active AND u.clerk_deleted_at IS NULL
ON CONFLICT DO NOTHING
"""


def deliver_post(post) -> int:
    """Spec 09 §6.3 (follows 04 §6.3): bell-on, active, non-deleted followers who opted in before publishing. Idempotent."""
    from apps.posts.services import candidate_published

    if post.status != "published" or not candidate_published(post.candidate):
        return 0
    with connection.cursor() as cur:
        cur.execute(POST_SQL, {"post": post.pk, "candidate": post.candidate_id, "published_at": post.published_at})
        n = cur.rowcount
    record(AuditLog.Action.POST_DELIVERED, target=post, actor_label="system:notifications", delivered=n)  # a count only
    return n


def deliver_community_post(post) -> int:
    """Spec 17 §10: members of an LGA, ward or polling-unit community who turned its bell on. IDs only."""
    with connection.cursor() as cur:
        cur.execute(COMMUNITY_POST_SQL, {"post": post.pk, "community": post.community_id, "author": post.author_id})
        return cur.rowcount


# spec 18 §7, §10: registered and waitlisted people (reminders: registered only, not within 2 h of registering). Folded: one
# row per person per event per kind per hour.
EVENT_SQL = """
INSERT INTO notifications_notification (id, recipient_id, kind, event_id, decision, count, created_at)
SELECT gen_random_uuid(), r.user_id, %(kind)s, %(event)s, %(decision)s, 1, now()
FROM events_eventregistration r JOIN accounts_user u ON u.id = r.user_id
LEFT JOIN notifications_notificationpreference p ON p.user_id = u.id
WHERE r.event_id = %(event)s AND r.status = ANY(%(statuses)s) AND u.is_active AND u.clerk_deleted_at IS NULL
  AND (%(kind)s <> 'event_reminder' OR (COALESCE(p.event_reminders, TRUE) AND r.updated_at <= now() - interval '2 hours'))
  AND NOT EXISTS (SELECT 1 FROM notifications_notification n WHERE n.recipient_id = r.user_id AND n.event_id = %(event)s
                  AND n.kind = %(kind)s AND n.decision = %(decision)s AND n.created_at > now() - interval '1 hour')
"""

CANDIDATE_EVENT_SQL = """
INSERT INTO notifications_notification (id, recipient_id, kind, event_id, decision, count, created_at)
SELECT gen_random_uuid(), f.user_id, 'candidate_event', %(event)s, '', 1, now()
FROM follows_candidatefollow f JOIN accounts_user u ON u.id = f.user_id
LEFT JOIN notifications_notificationpreference p ON p.user_id = u.id
WHERE f.candidate_id = %(candidate)s AND f.notify AND f.notify_changed_at <= %(published_at)s AND COALESCE(p.candidate_events, TRUE)
  AND u.is_active AND u.clerk_deleted_at IS NULL
ON CONFLICT DO NOTHING
"""

VOLUNTEER_UPDATE_SQL = """
INSERT INTO notifications_notification (id, recipient_id, kind, volunteer_update_id, candidate_id, decision, count, created_at)
SELECT gen_random_uuid(), a.user_id, 'volunteer_update', %(update)s, %(candidate)s, '', 1, now()
FROM volunteers_volunteerapplication a JOIN accounts_user u ON u.id = a.user_id
WHERE a.candidate_id = %(candidate)s AND a.status = 'accepted' AND (%(role)s = '' OR a.roles @> to_jsonb(ARRAY[%(role)s]::text[]))
  AND u.is_active AND u.clerk_deleted_at IS NULL
ON CONFLICT DO NOTHING
"""


def deliver_event(event, kind, decision="") -> int:
    """event_update to registered and waitlisted people; event_reminder to registered people. IDs and a word only."""
    statuses = ["registered"] if kind == Kind.EVENT_REMINDER else ["registered", "waitlisted"]
    with connection.cursor() as cur:
        cur.execute(EVENT_SQL, {"kind": kind, "event": event.pk, "decision": (decision or "")[:10], "statuses": statuses})
        return cur.rowcount


def deliver_candidate_event(event) -> int:
    """Spec 18 §7: bell-on followers of a published candidate, like 09's posts; never inside a blackout window. Not audited."""
    from apps.core import blackouts
    from apps.events.services import candidate_published

    if event.status != "published" or not candidate_published(event.candidate):
        return 0
    if blackouts.active_window(positions=[event.candidate.position]):
        return 0
    with connection.cursor() as cur:
        cur.execute(CANDIDATE_EVENT_SQL, {"event": event.pk, "candidate": event.candidate_id, "published_at": event.published_at})
        return cur.rowcount


def deliver_volunteer_update(update) -> int:
    with connection.cursor() as cur:
        cur.execute(VOLUNTEER_UPDATE_SQL, {"update": update.pk, "candidate": update.candidate_id, "role": update.role or ""})
        return cur.rowcount


def deliver_post_on_commit(post):
    from apps.core import outbox

    outbox.enqueue("notify_fanout", {"kind": "candidate_post", "post": str(post.pk)}, dedupe_key=f"fanout:candidate_post:{post.pk}")


def remove_for_unfollow(user, candidate) -> int:
    """Spec 09 §3.7 — unfollowing deletes that candidate's post notifications for the user."""
    n, _ = Notification.objects.filter(recipient=user, kind=Kind.CANDIDATE_POST, candidate_post__candidate=candidate).delete()
    return n


# Which target field each kind links to (None = no link; the row is about the person's own record).
TARGET_FIELD = {
    Kind.ANNOUNCEMENT: "article", Kind.NEWS_DECISION: "article", Kind.PVC_DECISION: None, Kind.CANDIDATE_POST: "candidate_post",
    Kind.REPORT_OUTCOME: "case", Kind.CONTENT_REMOVED: "case", Kind.ACCOUNT_WARNING: "case", Kind.SUBMISSIONS_PAUSED: "case",
    Kind.APPEAL_DECISION: "appeal", Kind.SECURITY_NOTICE: "incident", Kind.PRIVACY_REQUEST_UPDATE: "data_request",
    Kind.MEMBERSHIP_DECISION: "membership", Kind.DONATION_RECEIPT: "donation",
    Kind.COMMUNITY_REPLY: "community_post", Kind.COMMUNITY_POST: "community_post", Kind.COMMUNITY_PLACED: None,  # spec 17 §10
    Kind.EVENT_UPDATE: "event", Kind.EVENT_REMINDER: "event", Kind.EVENT_WAITLIST: "event", Kind.CANDIDATE_EVENT: "event",  # spec 18
    Kind.VOLUNTEER_DECISION: "candidate", Kind.VOLUNTEER_UPDATE: "volunteer_update",
    Kind.GEO_CONFIRM: None, Kind.GEO_UNIT_CHANGED: None,  # spec 19 §10: the record kind in `decision`, never a place
}

GEO_RECORDS = {"membership": "mem", "pvc": "pvc"}


def notify_geo(user, kind: str, record: str, change: str = ""):
    """Spec 19 §10. `decision` holds "mem" or "pvc", plus ".ret" or ".mov" for a changed unit. Never the unit's name."""
    word = GEO_RECORDS[record] + ({"retired": ".ret", "moved": ".mov"}[change] if change else "")
    return notify(user, kind, decision=word)


def can_receive(user) -> bool:
    return user is not None and user.is_active and not user.clerk_deleted_at


def notify(recipient, kind: str, *, target=None, decision: str = "", **links):
    """Spec 15 §9.1. One row for one person, in the caller's transaction. Returns the row, or None when the
    recipient can't receive notices (inactive, deleted) or already has this one (unique kinds).

    Rows carry a link and a short decision word only: never VINs, NINs, amounts, reporters or moderators."""
    if kind not in Kind.values:
        raise ValueError(f"Unknown notification kind {kind!r}")
    if not can_receive(recipient):
        return None
    field = TARGET_FIELD[kind]
    fields = dict(links)
    if target is not None and field:
        fields[field] = target
    unknown = set(fields) - {"article", "candidate_post", "case", "appeal", "data_request", "incident", "membership", "donation",
                             "community_post", "community", "event", "candidate", "volunteer_update"}
    if unknown:
        raise ValueError(f"Unknown notification target {unknown}")
    if kind == Kind.DONATION_RECEIPT and Notification.objects.filter(recipient=recipient, kind=kind, donation=fields.get("donation")).exists():
        return None
    return Notification.objects.create(recipient=recipient, kind=kind, decision=(decision or "")[:10], **fields)


def notify_many(recipients, kind: str, *, target=None) -> int:
    """The same rules as `notify` for a chosen list (14's security notices): one bulk insert, inactive and deleted skipped."""
    field = TARGET_FIELD[kind]
    rows = [Notification(recipient=u, kind=kind, **({field: target} if field and target is not None else {}))
            for u in recipients if can_receive(u)]
    Notification.objects.bulk_create(rows, batch_size=1000)
    return len(rows)


def notify_decision(article, decision: str):
    """Written inside the decision's transaction (spec 07 §6.4)."""
    return notify(article.author, Kind.NEWS_DECISION, target=article, decision=decision)


def notify_pvc_decision(user, decision: str):
    """Spec 08 §4.3 — always sent (the user's own request); no PVC data in the row. Written in the decision's transaction."""
    return notify(user, Kind.PVC_DECISION, decision=decision)


def notify_moderation(user, kind: str, *, case=None, appeal=None, decision=""):
    """Spec 13 §4.6. Written inside the decision's transaction, so a decision and its notices commit together.
    Never names the moderator or the reporter."""
    return notify(user, kind, decision=decision, case=case, appeal=appeal)


def notify_membership(membership, decision: str):
    """Spec 15 §9.2: approved, activated, rejected or cancelled. Status word only."""
    return notify(membership.user, Kind.MEMBERSHIP_DECISION, target=membership, decision=decision)


def notify_donation_receipt(donation):
    """Spec 15 §9.2: signed-in donors only (guests have the receipt page). Once per donation."""
    if not donation.donor_user_id:
        return None
    return notify(donation.donor_user, Kind.DONATION_RECEIPT, target=donation, decision="received")


def visible(user):
    """Announcements only while their article is public; decisions about your own requests always."""
    from .models import GEO_KINDS, MODERATION_KINDS, OWN_RECORD_KINDS, PRIVACY_KINDS, VOLUNTEER_KINDS

    return (Notification.objects.filter(recipient=user)
            .filter(Q(kind__in=[Kind.NEWS_DECISION, Kind.PVC_DECISION, Kind.COMMUNITY_PLACED, *MODERATION_KINDS, *PRIVACY_KINDS,
                                *OWN_RECORD_KINDS, *GEO_KINDS])
                    | Q(kind__in=[Kind.COMMUNITY_REPLY, Kind.COMMUNITY_POST], community_post__status="published",
                        community_post__community__active=True)
                    | Q(kind__in=VOLUNTEER_KINDS)
                    | Q(kind__in=[Kind.EVENT_UPDATE, Kind.EVENT_REMINDER, Kind.EVENT_WAITLIST, Kind.CANDIDATE_EVENT],
                        event__status__in=["published", "cancelled", "removed"])
                    | Q(kind=Kind.ANNOUNCEMENT, article__status="successful")
                    | Q(kind=Kind.CANDIDATE_POST, candidate_post__status="published", candidate_post__candidate__status="published",
                        candidate_post__candidate__verified_at__isnull=False))
            .select_related("article", "candidate_post__candidate", "case", "appeal__case", "data_request", "incident", "membership",
                            "donation", "community_post__community", "community", "event__candidate", "candidate",
                            "volunteer_update"))


def unread_count(user) -> int:
    return visible(user).filter(read_at__isnull=True)[:UNREAD_CAP].count()


def mark_read(user, notification_id) -> bool:
    return bool(Notification.objects.filter(recipient=user, pk=notification_id, read_at__isnull=True).update(read_at=timezone.now()))


def mark_all_read(user) -> int:
    return Notification.objects.filter(recipient=user, read_at__isnull=True).update(read_at=timezone.now())


PREF_FIELDS = ("announcements", "community_replies", "event_reminders", "candidate_events")


def preferences(user) -> dict:
    p = NotificationPreference.objects.filter(user=user).first()
    return {k: getattr(p, k) if p else True for k in PREF_FIELDS}


def set_preferences(user, announcements=None, community_replies=None, event_reminders=None, candidate_events=None) -> dict:
    given = {"announcements": announcements, "community_replies": community_replies, "event_reminders": event_reminders,
             "candidate_events": candidate_events}
    values = {k: bool(v) for k, v in given.items() if v is not None}
    NotificationPreference.objects.update_or_create(user=user, defaults=values)
    return preferences(user)


def erase_for_deleted_user(user) -> int:
    n, _ = Notification.objects.filter(recipient=user).delete()
    NotificationPreference.objects.filter(user=user).delete()
    return n


def prune(now=None) -> int:
    """Read > 180 days, unread > 365 days (spec 07 §10)."""
    now = now or timezone.now()
    n, _ = Notification.objects.filter(Q(read_at__isnull=False, created_at__lt=now - timedelta(days=180))
                                       | Q(read_at__isnull=True, created_at__lt=now - timedelta(days=365))).delete()
    return n
