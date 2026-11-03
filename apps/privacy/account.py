"""Self-service deletion and officer linked actions (spec 14 §4.4, §4.5, §6.3)."""
from django.db import transaction
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.audit.services import record
from apps.core.exceptions import ServiceError

WHAT_GOES = [
    "Your profile (name, email, phone, home state)",
    "Who you follow and your notification bells",
    "Your PVC check and verified-voter badge",
    "Your membership register details (NIN, date of birth, address, photo)",
    "Your drafts, notifications and your name on news you submitted",
]
WHAT_STAYS = [
    "Donations, anonymised, because the party must report them in its returns",
    "Membership fee payments, for the party's accounts",
    "Moderation records about content, for 1 year",
    "Audit and consent records, for 6 years, as proof of what was done",
]


def delete_own_account(user, request=None, confirm=""):
    """Reverification is checked at the authentication boundary (security.ANY_FACTOR['my-delete'])."""
    from apps.accounts.clerk import delete_user
    from apps.accounts.services import mark_deleted

    if (confirm or "").strip().lower() != "delete":
        raise ServiceError('Type "delete" to confirm.', "validation_error", 400)
    clerk_id = user.clerk_user_id
    user_pk = user.pk
    if not delete_user(clerk_id):
        raise ServiceError("We couldn't delete your account just now. Nothing was removed. Try again in a few minutes.", "auth_unavailable", 503)
    gone = mark_deleted(clerk_id)
    record(AuditLog.Action.PRIVACY_ACCOUNT_DELETED_SELF, target=gone, actor_label="self", request=request, user_id=str(user_pk))
    return {"deleted": True}


# ------------------------------------------------------------------ officer linked actions (§4.5)
LINKED = {
    "restrict_notifications": "Follow notifications turned off",
    "hide_bylines": "Name hidden on news they submitted",
    "erase_follows": "All follows removed",
    "erase_pvc": "PVC record erased",
    "erase_membership": "Membership register data erased",
}


def linked_action(req, by, action, request=None):
    """Reuses the owning app's service and rules. The result is an internal event on the request."""
    from .models import DataRequestEvent

    if action not in LINKED:
        raise ServiceError("Unknown action.", "validation_error", 400)
    user = req.user
    if user is None or user.clerk_deleted_at:
        raise ServiceError("This person's account has been deleted.", "invalid_transition", 409)
    if req.status in ("done", "refused"):
        raise ServiceError("This request is closed.", "invalid_transition", 409)
    with transaction.atomic():
        if action == "restrict_notifications":
            from apps.follows.models import CandidateFollow

            n = CandidateFollow.objects.filter(user=user, notify=True).update(notify=False)
        elif action == "hide_bylines":
            from apps.news.models import Article

            n = Article.objects.filter(author=user, show_author_name=True).update(show_author_name=False)
        elif action == "erase_follows":
            from apps.follows.services import remove_all_for_user

            n = remove_all_for_user(user)
        elif action == "erase_pvc":
            from apps.pvc.services import erase_for_withdrawn_consent

            n = erase_for_withdrawn_consent(user)
        else:
            from apps.membership.services import erase_for_withdrawn_consent as erase_m

            n = erase_m(user)
        DataRequestEvent.objects.create(request=req, event=action, actor=by, note=f"{LINKED[action]} ({int(n)})")
        record(AuditLog.Action.PRIVACY_LINKED_ACTION, target=req, actor=by,
               request=request, reference=req.reference, linked_action=action)
    return {"action": action, "label": LINKED[action], "count": int(n), "at": timezone.now()}
