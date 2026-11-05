"""Background jobs (spec 17 §12.3). Payloads hold IDs only."""
from apps.core.outbox import handler


@handler("communities_place")
def communities_place(payload):
    from apps.accounts.models import User

    from . import services

    u = User.objects.filter(pk=payload["user"]).first()
    if u is not None:
        services.place(u)


@handler("community_post_fanout")
def community_post_fanout(payload):
    from apps.notifications.services import deliver_community_post

    from .models import CommunityPost

    p = CommunityPost.objects.filter(pk=payload["post"], status="published").select_related("community").first()
    if p is not None:
        deliver_community_post(p)
