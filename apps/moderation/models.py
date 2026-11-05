"""
Reports, cases, appeals, restrictions and legal notices (spec 13 §5.1). One process for every
public item: news articles (official and community), candidate posts and candidate profiles.
"""
import secrets
import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.core.fields import StateField, choice_check, state_check

ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"  # no 0/O, 1/I/L


def new_reference(prefix: str) -> str:
    return f"{prefix}-" + "".join(secrets.choice(ALPHABET) for _ in range(6))


class TargetKind(models.TextChoices):
    ARTICLE = "article", "News"
    POST = "post", "Candidate post"
    CANDIDATE = "candidate", "Candidate profile"
    COMMUNITY_POST = "community_post", "Community post"  # spec 17 §8.1
    COMMUNITY_COMMENT = "community_comment", "Community comment"


class ReportStatus(models.TextChoices):
    OPEN = "open", "Open"
    ACTIONED = "actioned", "Action taken"
    DISMISSED = "dismissed", "No action"


class CaseSource(models.TextChoices):
    USER_REPORT = "user_report", "User report"
    AGENCY = "agency_notice", "Government agency notice"
    COURT = "court_order", "Court order"
    COPYRIGHT = "copyright_notice", "Copyright notice"
    PROACTIVE = "proactive", "Found by NDCConnect"


class CaseStatus(models.TextChoices):
    OPEN = "open", "Open"
    ESCALATED = "escalated", "Escalated"
    CLOSED = "closed", "Closed"


class Priority(models.TextChoices):
    LEGAL = "legal", "Legal"
    URGENT = "urgent", "Urgent"
    HIGH = "high", "High"
    NORMAL = "normal", "Normal"
    LOW = "low", "Low"


class Decision(models.TextChoices):
    KEPT = "kept", "No action"
    REMOVED = "removed", "Removed"
    RESTORED = "restored", "Restored"
    AUTHOR_REMOVED = "author_removed", "Removed by author"


class Case(models.Model):
    """One moderation case per item at a time, grouping its reports and decisions (spec 13)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    target_kind = models.CharField(max_length=20, choices=TargetKind.choices)
    target_id = models.UUIDField()
    source = models.CharField(max_length=20, choices=CaseSource.choices, default=CaseSource.USER_REPORT)
    status = models.CharField(max_length=10, choices=CaseStatus.choices, default=CaseStatus.OPEN)
    priority = models.CharField(max_length=8, choices=Priority.choices, default=Priority.NORMAL)
    priority_rank = models.PositiveSmallIntegerField(default=3)  # legal 0 … low 4, for ordering
    due_at = models.DateTimeField()
    legal_due_at = models.DateTimeField(null=True, blank=True)
    assigned_to = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    assigned_at = models.DateTimeField(null=True, blank=True)
    decision = models.CharField(max_length=16, choices=Decision.choices, blank=True)
    rule = models.CharField(max_length=8, blank=True)
    note_to_author = models.CharField(max_length=500, blank=True)
    internal_note = models.CharField(max_length=1000, blank=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)
    escalated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    burst_flag = models.BooleanField(default=False)
    legal_hold = models.BooleanField(default=False)
    restorable = models.BooleanField(default=False)  # copyright: counter-notice window passed unanswered
    title = models.CharField(max_length=200, blank=True)  # snapshot for the queue (spec 13 §4.3)
    state = StateField()
    opened_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["priority_rank", "due_at"]
        constraints = [
            models.UniqueConstraint(fields=["target_kind", "target_id"], condition=~Q(status="closed"), name="moderation_one_live_case"),
            # spec 16 §6.3
            models.CheckConstraint(condition=~Q(status="closed") | Q(closed_at__isnull=False), name="moderation_case_closed_shape"),
            choice_check("status", CaseStatus.values, "moderation_case_status_valid"),
            state_check("state", "moderation_case_state_valid"),
        ]
        indexes = [
            models.Index(fields=["status", "priority_rank", "due_at"], name="moderation_queue_idx"),
            models.Index(fields=["assigned_to", "status"], name="moderation_assignee_idx"),
            models.Index(fields=["target_kind", "target_id"], name="moderation_target_idx"),
        ]

    def __str__(self):
        return f"{self.target_kind}:{self.target_id} ({self.status})"


class Report(models.Model):
    """One report about one item, by a signed-in member or a guest (spec 13). Reporters are masked from moderators."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reference = models.CharField(max_length=12, unique=True)
    target_kind = models.CharField(max_length=20, choices=TargetKind.choices)
    target_id = models.UUIDField()
    case = models.ForeignKey(Case, on_delete=models.CASCADE, related_name="reports")
    reason = models.CharField(max_length=16)
    details = models.CharField(max_length=1000, blank=True)
    reporter = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    contact_email = models.EmailField(blank=True)
    ip_hash = models.CharField(max_length=64, blank=True)
    status = models.CharField(max_length=10, choices=ReportStatus.choices, default=ReportStatus.OPEN)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at"]
        constraints = [
            models.UniqueConstraint(fields=["target_kind", "target_id", "reporter"], condition=Q(status="open", reporter__isnull=False),
                                    name="moderation_one_open_report_per_user"),
        ]
        indexes = [models.Index(fields=["status", "created_at"], name="moderation_report_open_idx"),
                   models.Index(fields=["ip_hash", "created_at"], name="moderation_report_ip_idx")]


class CaseEvent(models.Model):
    """Append-only history (spec 13 §5.1)."""

    class Kind(models.TextChoices):
        OPENED = "opened"
        REPORT_ADDED = "report_added"
        CLAIMED = "claimed"
        RELEASED = "released"
        ESCALATED = "escalated"
        KEPT = "kept"
        REMOVED = "removed"
        RESTORED = "restored"
        WARNED = "warned"
        PAUSED = "paused"
        PAUSE_LIFTED = "pause_lifted"
        APPEAL_OPENED = "appeal_opened"
        APPEAL_UPHELD = "appeal_upheld"
        APPEAL_DENIED = "appeal_denied"
        NOTICE_LINKED = "notice_linked"
        HOLD_ON = "hold_on"
        HOLD_OFF = "hold_off"
        REPORTER_VIEWED = "reporter_viewed"
        COUNTER_NOTICE = "counter_notice"
        AUTHOR_REMOVED = "author_removed"
        HIDDEN = "hidden"  # spec 17 §8.3
        UNHIDDEN = "unhidden"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    case = models.ForeignKey(Case, on_delete=models.CASCADE, related_name="events")
    event = models.CharField(max_length=20, choices=Kind.choices)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    note = models.CharField(max_length=1000, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise RuntimeError("CaseEvent is append-only.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise RuntimeError("CaseEvent is append-only.")


class Restriction(models.Model):
    """A warning, a community-news pause, or a community posting pause on a person's account (spec 13, 17 §8.4)."""

    class Kind(models.TextChoices):
        WARNING = "warning", "Warning"
        NEWS_PAUSE = "news_pause", "Community news paused"
        COMMUNITY_PAUSE = "community_pause", "Community posting paused"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="restrictions")
    kind = models.CharField(max_length=16, choices=Kind.choices)
    rule = models.CharField(max_length=8, blank=True)
    reason = models.CharField(max_length=500, blank=True)
    case = models.ForeignKey(Case, null=True, blank=True, on_delete=models.SET_NULL, related_name="restrictions")
    # community_pause only: the community (and the levels below it) it covers; null = every community (spec 17 §8.4)
    community = models.ForeignKey("communities.Community", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    lifted_at = models.DateTimeField(null=True, blank=True)
    lifted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "kind", "ends_at"], name="moderation_restriction_idx")]


class Appeal(models.Model):
    """An author's one appeal against a removal or a pause, decided by someone else (spec 13)."""

    class Kind(models.TextChoices):
        REMOVAL = "removal", "Removal"
        PAUSE = "pause", "Pause"

    class Status(models.TextChoices):
        PENDING = "pending", "Waiting for a decision"
        UPHELD = "upheld", "Upheld"
        DENIED = "denied", "Not upheld"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    case = models.ForeignKey(Case, null=True, blank=True, on_delete=models.CASCADE, related_name="appeals")
    restriction = models.ForeignKey(Restriction, null=True, blank=True, on_delete=models.CASCADE, related_name="appeals")
    kind = models.CharField(max_length=8, choices=Kind.choices)
    appellant = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    statement = models.CharField(max_length=2000)
    evidence_url = models.URLField(max_length=500, blank=True)
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.PENDING)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_reason = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        constraints = [
            models.UniqueConstraint(fields=["case"], condition=Q(kind="removal"), name="moderation_one_appeal_per_case"),
            models.UniqueConstraint(fields=["restriction"], condition=Q(kind="pause"), name="moderation_one_appeal_per_pause"),
        ]


class LegalNotice(models.Model):
    """An agency notice, court order or copyright notice received by NDCConnect (spec 13)."""

    class Kind(models.TextChoices):
        AGENCY = "agency", "Government agency notice"
        COURT = "court_order", "Court order"
        COPYRIGHT = "copyright", "Copyright notice"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reference = models.CharField(max_length=12, unique=True)
    kind = models.CharField(max_length=12, choices=Kind.choices)
    sender_name = models.CharField(max_length=150)
    sender_org = models.CharField(max_length=150, blank=True)
    sender_contact = models.CharField(max_length=254)
    sender_reference = models.CharField(max_length=100, blank=True)
    legal_basis = models.CharField(max_length=300, blank=True)
    received_at = models.DateTimeField()
    due_at = models.DateTimeField(null=True, blank=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    acknowledged_how = models.CharField(max_length=200, blank=True)
    attachment = models.CharField(max_length=255, blank=True)  # private storage path
    work_description = models.CharField(max_length=1000, blank=True)
    declaration_accepted = models.BooleanField(default=False)
    signature = models.CharField(max_length=150, blank=True)
    counter_notice_at = models.DateTimeField(null=True, blank=True)
    counter_notice_text = models.CharField(max_length=2000, blank=True)
    counter_notice_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    owner_notified_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=8, choices=[("open", "Open"), ("closed", "Closed")], default="open")
    ip_hash = models.CharField(max_length=64, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-received_at"]


class LegalNoticeItem(models.Model):
    """One item named in a legal notice, with what was done about it."""

    class Outcome(models.TextChoices):
        PENDING = "pending", "Waiting"
        REMOVED = "removed", "Removed with notice"
        NOT_REMOVED = "not_removed", "Not removed"
        RESTORED = "restored", "Restored"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    notice = models.ForeignKey(LegalNotice, on_delete=models.CASCADE, related_name="items")
    case = models.ForeignKey(Case, on_delete=models.CASCADE, related_name="notice_items")
    outcome = models.CharField(max_length=12, choices=Outcome.choices, default=Outcome.PENDING)
    outcome_reason = models.CharField(max_length=500, blank=True)


class ComplianceYear(models.Model):
    """Frozen yearly figures, kept when old case records are pruned (spec 13 §10)."""

    year = models.PositiveSmallIntegerField(primary_key=True)
    figures = models.JSONField()
    frozen_at = models.DateTimeField(auto_now=True)
