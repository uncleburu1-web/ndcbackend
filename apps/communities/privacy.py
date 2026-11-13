"""Spec 17 §17.4: memberships, your own posts and comments, and the posts you liked (IDs and dates)."""
from apps.privacy.exporting import pick

DESCRIPTION = "Your communities (how you were placed), your community posts and comments, and the posts you liked."


def export_for(user) -> dict:
    from .models import (
        CommunityComment,
        CommunityMembership,
        CommunityPost,
        CommunityReaction,
    )

    ms = CommunityMembership.objects.filter(user=user).select_related("community").order_by("joined_at")
    posts = CommunityPost.objects.filter(author=user).select_related("community", "campaign_candidate").order_by("created_at")
    comments = CommunityComment.objects.filter(author=user).select_related("post__community").order_by("created_at")
    likes = CommunityReaction.objects.filter(user=user).order_by("created_at")
    return {
        "memberships": [pick(m, [("community", lambda m: m.community.name), "level", ("how", lambda m: m.get_how_display()), "status",
                                 "end_reason", "notify_new_posts", "joined_at", "left_at"]) for m in ms],
        "posts": [pick(p, ["id", ("community", lambda p: p.community.name), "body", "status", "removed_rule", "created_at", "edited_at",
                           "deleted_at", ("campaign", lambda p: p.campaign_candidate.full_name if p.campaign_candidate_id else "")])
                  for p in posts],
        "comments": [pick(c, ["id", ("post", lambda c: str(c.post_id)), ("community", lambda c: c.post.community.name), "body", "status",
                              "removed_rule", "created_at", "edited_at", "deleted_at"]) for c in comments],
        "likes": [pick(r, [("post", lambda r: str(r.post_id)), "kind", "created_at"]) for r in likes],
    }
