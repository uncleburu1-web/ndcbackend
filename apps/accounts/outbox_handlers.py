"""Clerk ban/unban retries (spec 15 §9.3). The local account state is the truth; these only bring Clerk in line."""
from apps.core.outbox import handler


def _still_wanted(clerk_user_id, banned):
    from .models import User

    u = User.objects.filter(clerk_user_id=clerk_user_id).first()
    return u is not None and (not u.is_active) == banned


@handler("clerk_ban")
def clerk_ban(payload):
    from . import clerk

    if _still_wanted(payload["clerk_user_id"], True):
        clerk.set_banned(payload["clerk_user_id"], True)


@handler("clerk_unban")
def clerk_unban(payload):
    from . import clerk

    if _still_wanted(payload["clerk_user_id"], False):
        clerk.set_banned(payload["clerk_user_id"], False)
