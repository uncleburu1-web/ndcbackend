"""
In-app notifications — spec 07 §6.4. The only place notifications are written (code standards).
Candidate posts (spec 09) are delivered by deliver_post.
"""
import logging
from datetime import timedelta

from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.audit.services import record

from .models import Kind, Notification, NotificationPreference

logger = logging.getLogger(__name__)
UNREAD_CAP = 100

ANNOUNCEMENT_SQL = """
INSERT INTO notifications_notification (id, recipient_id, kind, article_id, decision, created_at)
SELECT gen_random_uuid(), u.id, 'announcement', %(article)s, '', now()
FROM accounts_user u LEFT JOIN notifications_notificationpreference p ON p.user_id = u.id
WHERE u.is_active AND u.clerk_deleted_at IS NULL AND COALESCE(p.announcements, TRUE)
ON CONFLICT DO NOTHING
"""


def eligible_for_announcements():
    from apps.accounts.models import User

    return User.objects.filter(is_active=True, clerk_deleted_at__isnull=True).exclude(notification_preference__announcements=False)


def deliver_announcement(article) -> int:
    """One idempotent statement for any number of users (no job queue needed)."""
    with connection.cursor() as cur:
        cur.execute(ANNOUNCEMENT_SQL, {"article": article.pk})
        n = cur.rowcount
    record(AuditLog.Action.ANNOUNCEMENT_DELIVERED, target=article, actor_label="system:notifications", delivered=n)
    return n


def deliver_announcement_on_commit(article):
    def run():
        try:
            deliver_announcement(article)
        except Exception:  # the article stays published; re-run with `deliver_announcement <id>` (§11)
            logger.exception("Announcement delivery failed for article %s", article.pk)

    transaction.on_commit(run)


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


def deliver_post_on_commit(post):
    def run():
        try:
            deliver_post(post)
        except Exception:  # the post stays published; re-run with `deliver_post <id>` (spec 09 §11)
            logger.exception("Candidate post delivery failed for post %s", post.pk)

    transaction.on_commit(run)


def remove_for_unfollow(user, candidate) -> int:
    """Spec 09 §3.7 — unfollowing deletes that candidate's post notifications for the user."""
    n, _ = Notification.objects.filter(recipient=user, kind=Kind.CANDIDATE_POST, candidate_post__candidate=candidate).delete()
    return n


def notify_decision(article, decision: str):
    """Written inside the decision's transaction (spec 07 §6.4)."""
    author = article.author
    if author is None or not author.is_active or author.clerk_deleted_at:
        return None
    return Notification.objects.create(recipient=author, kind=Kind.NEWS_DECISION, article=article, decision=decision)


def notify_pvc_decision(user, decision: str):
    """Spec 08 §4.3 — always sent (the user's own request); no PVC data in the row. Written in the decision's transaction."""
    if user is None or not user.is_active or user.clerk_deleted_at:
        return None
    return Notification.objects.create(recipient=user, kind=Kind.PVC_DECISION, article=None, decision=decision)


def notify_moderation(user, kind: str, *, case=None, appeal=None, decision=""):
    """Spec 13 §4.6. Written inside the decision's transaction, so a decision and its notices commit together.
    Never names the moderator or the reporter."""
    if user is None or not user.is_active or user.clerk_deleted_at:
        return None
    return Notification.objects.create(recipient=user, kind=kind, case=case, appeal=appeal, decision=(decision or "")[:10])


def visible(user):
    """Announcements only while their article is public; decisions about your own requests always."""
    from .models import MODERATION_KINDS, PRIVACY_KINDS

    return (Notification.objects.filter(recipient=user)
            .filter(Q(kind__in=[Kind.NEWS_DECISION, Kind.PVC_DECISION, *MODERATION_KINDS, *PRIVACY_KINDS])
                    | Q(kind=Kind.ANNOUNCEMENT, article__status="successful")
                    | Q(kind=Kind.CANDIDATE_POST, candidate_post__status="published", candidate_post__candidate__status="published",
                        candidate_post__candidate__verified_at__isnull=False))
            .select_related("article", "candidate_post__candidate", "case", "appeal__case", "data_request", "incident"))


def unread_count(user) -> int:
    return visible(user).filter(read_at__isnull=True)[:UNREAD_CAP].count()


def mark_read(user, notification_id) -> bool:
    return bool(Notification.objects.filter(recipient=user, pk=notification_id, read_at__isnull=True).update(read_at=timezone.now()))


def mark_all_read(user) -> int:
    return Notification.objects.filter(recipient=user, read_at__isnull=True).update(read_at=timezone.now())


def preferences(user) -> dict:
    p = NotificationPreference.objects.filter(user=user).first()
    return {"announcements": p.announcements if p else True}


def set_preferences(user, announcements: bool) -> dict:
    NotificationPreference.objects.update_or_create(user=user, defaults={"announcements": bool(announcements)})
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
