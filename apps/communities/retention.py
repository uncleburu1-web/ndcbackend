"""Spec 17 §17.3: deleted text erased after 30 days, rows after a year; removed items after a year; ended memberships after 30 days;
old edit records (previous text) after a year. Legal holds are respected."""
from datetime import timedelta

from django.db import transaction
from django.db.models import Q

from apps.core.history import allow_history_delete

from .models import CommunityComment, CommunityEvent, CommunityMembership, CommunityPost, ItemStatus, MembershipStatus

E = CommunityEvent.Kind


def prune(now) -> dict:
    from apps.moderation.services import held_ids

    month, year = now - timedelta(days=30), now - timedelta(days=365)
    held = {CommunityPost: held_ids("community_post"), CommunityComment: held_ids("community_comment")}
    out = {"erased": 0, "deleted_items": 0, "memberships": 0, "edits": 0}
    with transaction.atomic():
        for model in (CommunityPost, CommunityComment):
            due = model.objects.filter(status=ItemStatus.DELETED, deleted_at__lt=month, body_erased_at__isnull=True).exclude(pk__in=held[model])
            for obj in due.select_for_update():
                obj.body, obj.body_erased_at = "", now
                obj.save(update_fields=["body", "body_erased_at", "updated_at"])
                post = obj if model is CommunityPost else obj.post
                CommunityEvent.objects.create(post=post, comment=None if model is CommunityPost else obj, kind=E.BODY_ERASED)
                out["erased"] += 1
        with allow_history_delete():
            # previous text of erased items, and edit records older than a year
            erased_posts = CommunityPost.objects.filter(body_erased_at__isnull=False).values("pk")
            erased_comments = CommunityComment.objects.filter(body_erased_at__isnull=False).values("pk")
            n, _ = CommunityEvent.objects.filter(kind=E.EDITED).filter(
                Q(created_at__lt=year) | Q(comment__isnull=True, post__in=erased_posts) | Q(comment__in=erased_comments)).delete()
            out["edits"] = n
            for model in (CommunityComment, CommunityPost):
                old = (model.objects.filter(Q(status=ItemStatus.DELETED, deleted_at__lt=year) | Q(status=ItemStatus.REMOVED, removed_at__lt=year))
                       .exclude(pk__in=held[model]))
                n, _ = old.delete()
                out["deleted_items"] += n
        n, _ = CommunityMembership.objects.filter(status=MembershipStatus.ENDED, left_at__lt=month).delete()
        out["memberships"] = n
    return out
