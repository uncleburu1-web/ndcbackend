"""Account business logic. Views stay thin."""
from typing import Optional

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.audit.services import record

from . import clerk
from .models import User


def _primary_email(data: dict) -> str:
    primary = data.get("primary_email_address_id")
    emails = data.get("email_addresses") or []
    for e in emails:
        if e.get("id") == primary:
            return (e.get("email_address") or "").lower()
    return (emails[0].get("email_address") or "").lower() if emails else ""


def _prefill_name(data: dict) -> str:
    return " ".join(p for p in [(data.get("first_name") or "").strip(), (data.get("last_name") or "").strip()] if p)


def upsert_from_clerk(data: dict, *, source: str) -> Optional[User]:
    """
    Create or update the local user from a Clerk user payload (webhook or API fetch).
    Never resurrects a user Clerk has deleted. Returns None in that case.
    """
    clerk_id = data["id"]
    email = _primary_email(data)
    name = _prefill_name(data)
    now = timezone.now()

    with transaction.atomic():
        user = User.objects.select_for_update().filter(clerk_user_id=clerk_id).first()
        if user is None:
            try:
                with transaction.atomic():
                    user = User.objects.create_user(clerk_id, email=email, full_name=name, clerk_synced_at=now)
            except IntegrityError:  # created concurrently (JIT vs webhook)
                user = User.objects.select_for_update().get(clerk_user_id=clerk_id)
            else:
                record(AuditLog.Action.USER_CREATED, target=user, actor_label=source)
                return user

        if user.clerk_deleted_at:
            return None

        changed = []
        if email and user.email != email:
            changed.append("email")
            user.email = email
        if name and not user.full_name:  # prefill only; never overwrite what the user set in NDCConnect
            changed.append("full_name")
            user.full_name = name
        user.clerk_synced_at = now
        user.save(update_fields=[*changed, "clerk_synced_at", "updated_at"])
        if "email" in changed:
            record(AuditLog.Action.USER_UPDATED, target=user, actor_label=source, fields=["email"])
        return user


def get_or_create_for_session(clerk_user_id: str) -> User:
    """
    Load the local user for a verified session; create it just-in-time if the
    user.created webhook hasn't arrived yet (spec §4 step 4, §11).
    """
    user = User.objects.filter(clerk_user_id=clerk_user_id).first()
    if user is not None:
        return user

    data = clerk.fetch_user(clerk_user_id) or {"id": clerk_user_id}
    data["id"] = clerk_user_id  # trust only the verified token's subject
    user = upsert_from_clerk(data, source="system:jit")
    if user is None:  # deleted in Clerk — load the anonymised record (inactive)
        user = User.objects.get(clerk_user_id=clerk_user_id)
    return user


def mark_deleted(clerk_user_id: str) -> Optional[User]:
    """Clerk deleted the user: deactivate and anonymise, keep the row + audit history (spec §6.2)."""
    now = timezone.now()
    with transaction.atomic():
        user = User.objects.select_for_update().filter(clerk_user_id=clerk_user_id).first()
        if user is None:
            # Never seen: keep a tombstone so a late user.created can't create it.
            user = User.objects.create_user(clerk_user_id, is_active=False, deactivated_at=now, clerk_deleted_at=now, clerk_synced_at=now)
            record(AuditLog.Action.USER_DELETED, target=user, actor_label="clerk:webhook", tombstone=True)
            return user
        if user.clerk_deleted_at:
            return user
        user.email = ""
        user.full_name = ""
        user.phone_number = ""
        user.is_active = False
        user.deactivated_at = user.deactivated_at or now
        user.clerk_deleted_at = now
        user.clerk_synced_at = now
        user.save()
        roles_revoked = user.role_assignments.filter(is_active=True).update(is_active=False, revoked_at=now)
        # Who they followed is political-opinion data; it must not outlive the account (spec 04 §6.2).
        from apps.follows.services import remove_all_for_user

        follows_removed = remove_all_for_user(user)
        # Public donor labels become "Anonymous Donor"; the finance record is kept for party returns (spec 05 §6.7).
        from apps.donations.services import anonymise_for_deleted_user

        donations_anonymised = anonymise_for_deleted_user(user)
    record(AuditLog.Action.USER_DELETED, target=user, actor_label="clerk:webhook", roles_revoked=roles_revoked,
           follows_removed=follows_removed, donations_anonymised=donations_anonymised)
    return user


def update_profile(user: User, *, full_name=None, phone_number=None, request=None) -> bool:
    """
    Apply validated profile fields. Returns True exactly once: the first time the
    profile becomes complete (spec §6.3 `just_completed`).
    """
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=user.pk)
        changed = []
        if full_name is not None and full_name != user.full_name:
            user.full_name = full_name
            changed.append("full_name")
        if phone_number is not None and phone_number != user.phone_number:
            user.phone_number = phone_number
            changed.append("phone_number")

        just_completed = False
        if user.profile_completed_at is None and user.full_name and user.phone_number:
            user.profile_completed_at = timezone.now()
            just_completed = True

        if changed or just_completed:
            user.save()

    if just_completed:
        record(AuditLog.Action.PROFILE_COMPLETED, target=user, actor=user, request=request)
    elif changed:
        record(AuditLog.Action.PROFILE_UPDATED, target=user, actor=user, request=request, fields=changed)
    return just_completed
