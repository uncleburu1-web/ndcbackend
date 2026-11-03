"""
Follow rules — spec 04 §6. Views stay thin; everything that decides lives here.

Nothing in this module writes to the audit log: follows are a user's own,
non-privileged action, and logging them would build a record of political
opinions for no purpose (§5.2, §10).
"""
import re

from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone
from rest_framework.exceptions import NotFound

from apps.candidates.models import Candidate, CandidateStatus

from .models import CandidateFollow

SLUG_RE = re.compile(r"^[a-z0-9-]{1,80}$")
NOT_FOUND = "We couldn't find that candidate."


def valid_slug(slug: str) -> bool:
    return bool(slug) and bool(SLUG_RE.fullmatch(slug))


def published_q(prefix: str = "") -> Q:
    return Q(**{f"{prefix}status": CandidateStatus.PUBLISHED, f"{prefix}verified_at__isnull": False})


def published_candidate(slug: str) -> Candidate:
    """The same 404 for unknown, malformed, draft, unpublished and archived (§3 rule 4)."""
    if not valid_slug(slug):
        raise NotFound(NOT_FOUND)
    c = Candidate.objects.filter(published_q(), slug=slug).first()
    if c is None:
        raise NotFound(NOT_FOUND)
    return c


def follow(user, candidate: Candidate) -> tuple[CandidateFollow, bool]:
    """Idempotent. The unique constraint + get_or_create keep concurrent taps to one row."""
    from apps.privacy.consent import require_consent

    require_consent(user, "political_following")  # spec 14 §4.2: explicit consent (NDPA s. 30)
    with transaction.atomic():
        return CandidateFollow.objects.get_or_create(user=user, candidate=candidate)


def unfollow(user, slug: str) -> None:
    """Always succeeds, whatever the candidate's status, so a stale tab can unfollow (§6.1)."""
    if not valid_slug(slug):
        return
    follow = CandidateFollow.objects.filter(user=user, candidate__slug=slug).select_related("candidate").first()
    if follow is None:
        return
    candidate = follow.candidate
    follow.delete()
    # Notifications reveal the follow, so they go with it (spec 09 §3.7).
    from apps.notifications.services import remove_for_unfollow

    remove_for_unfollow(user, candidate)


def set_notify(user, candidate: Candidate, on: bool) -> CandidateFollow:
    if on:
        from apps.privacy.consent import require_consent

        require_consent(user, "political_following")
    with transaction.atomic():
        f = CandidateFollow.objects.select_for_update().filter(user=user, candidate=candidate).first()
        if f is None:
            raise NotFound("You're not following this candidate.")
        if f.notify != on:
            f.notify = on
            f.notify_changed_at = timezone.now()
            f.save(update_fields=["notify", "notify_changed_at"])
        return f


def visible_follows(user):
    """The Following page: published candidates only, newest first (§4.4)."""
    return (CandidateFollow.objects.filter(user=user).filter(published_q("candidate__"))
            .select_related("candidate").order_by("-created_at", "-id"))


def hidden_count(user) -> int:
    return CandidateFollow.objects.filter(user=user).exclude(published_q("candidate__")).count()


def states_for(user, slugs: list[str]) -> list[dict]:
    """Follow state for the slugs on screen; only those the user follows come back (§7.1)."""
    clean = [s for s in slugs if valid_slug(s)]
    if not clean:
        return []
    rows = (CandidateFollow.objects.filter(user=user, candidate__slug__in=clean).filter(published_q("candidate__"))
            .values_list("candidate__slug", "notify"))
    return [{"slug": slug, "notify": notify} for slug, notify in rows]


def follower_counts(candidate: Candidate) -> dict:
    """Counts only — there is deliberately no function that lists followers (§7.4, §10)."""
    agg = CandidateFollow.objects.filter(candidate=candidate).aggregate(
        total=Count("id"), notify_on=Count("id", filter=Q(notify=True)))
    return {"total": agg["total"], "notify_on": agg["notify_on"]}


def remove_all_for_user(user) -> int:
    """Account deleted in Clerk: political-opinion data must not outlive the account (§6.2)."""
    deleted, _ = CandidateFollow.objects.filter(user=user).delete()
    return deleted
