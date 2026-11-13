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


def _image(data: dict):
    """The uploaded Clerk photo's URL, "" when they have none, or None when the payload doesn't say (leave as is)."""
    if "has_image" not in data and "image_url" not in data:
        return None
    url = (data.get("image_url") or "").strip()
    if not data.get("has_image") or not url.startswith("https://img.clerk.com/") or len(url) > 500:
        return ""
    return url


def upsert_from_clerk(data: dict, *, source: str) -> Optional[User]:
    """
    Create or update the local user from a Clerk user payload (webhook or API fetch).
    Never resurrects a user Clerk has deleted. Returns None in that case.
    """
    clerk_id = data["id"]
    email = _primary_email(data)
    name = _prefill_name(data)
    image = _image(data)
    now = timezone.now()

    with transaction.atomic():
        user = User.objects.select_for_update().filter(clerk_user_id=clerk_id).first()
        if user is None:
            try:
                with transaction.atomic():
                    user = User.objects.create_user(clerk_id, email=email, full_name=name, image_url=image or "", clerk_synced_at=now)
            except IntegrityError:  # created concurrently (JIT vs webhook)
                user = User.objects.select_for_update().get(clerk_user_id=clerk_id)
            else:
                record(AuditLog.Action.USER_CREATED, target=user, actor_label=source)
                from .settings_services import apply_defaults_for_new_user

                apply_defaults_for_new_user(user)  # spec 20 §14
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
        if image is not None and image != user.image_url:  # spec 20 §4.1: the photo is changed in Clerk
            changed.append("image_url")
            user.image_url = image
        user.clerk_synced_at = now
        user.save(update_fields=[*changed, "clerk_synced_at", "updated_at"])
        if "image_url" in changed:  # a new photo is new content: a moderator's earlier removal no longer applies (spec 20 §5.1)
            from .models import UserPreferences

            UserPreferences.objects.filter(user=user, photo_removed_at__isnull=False).update(photo_removed_at=None)
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
        user.image_url = ""
        user.phone_number = ""
        user.home_state = ""
        user.last_seen_at, user.mfa_enabled = None, None  # spec 14 §10
        user.is_active = False
        user.deactivation_kind = "deleted"
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
        # NIN, VIN, date of birth and photo are erased; membership cancelled; payments kept (spec 06 §6.7).
        from apps.membership.services import erase_for_deleted_user

        membership_erased = erase_for_deleted_user(user)
        # PVC is its own unit: its record goes here, not through membership (spec 08 §6.2).
        from apps.pvc.models import PvcVerification
        from apps.pvc.services import erase_for_deleted_user as erase_pvc

        pvc_erased = PvcVerification.objects.filter(user=user).exists()
        erase_pvc(user)
        # Unpublished news cleared, bylines hidden, notifications deleted (spec 07 §6.5).
        from apps.news.services import erase_for_deleted_user as erase_news

        news_erased = erase_news(user)
        # Memberships and likes deleted; published community posts stay as "Former member" (spec 17 §17.4).
        from apps.communities.services import (
            erase_for_deleted_user as erase_communities,
        )

        erase_communities(user)
        # Event registrations and volunteer applications deleted; events they created stay with no creator (spec 18 §17.4).
        from apps.events.services import erase_for_deleted_user as erase_events
        from apps.volunteers.services import (
            erase_for_deleted_user as erase_volunteering,
        )

        erase_events(user)
        erase_volunteering(user)
        # Privacy requests keep what was asked, detached from the account (spec 14 §10). Consents stay on the anonymised row.
        from apps.privacy.models import DataRequest

        DataRequest.objects.filter(user=user).update(user=None)
        # Spec 20: settings go; the public-profile handle is retired so it's never handed to someone else.
        from .settings_services import erase_for_deleted_user as erase_settings

        erase_settings(user)
    record(AuditLog.Action.USER_DELETED, target=user, actor_label="clerk:webhook", roles_revoked=roles_revoked,
           follows_removed=follows_removed, donations_anonymised=donations_anonymised, membership_erased=membership_erased,
           pvc_erased=pvc_erased, news_erased=news_erased)
    return user


def update_profile(user: User, *, full_name=None, phone_number=None, home_state=None, adult_confirmed=None, request=None) -> bool:
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
        pref_changed = home_state is not None and home_state != user.home_state
        if pref_changed:
            user.home_state = home_state  # a private preference (spec 12 §4.5): saved, not audited

        was_complete = user.profile_complete
        adult_changed = bool(adult_confirmed) and user.adult_confirmed_at is None
        if adult_changed:
            user.adult_confirmed_at = timezone.now()  # spec 14 §4.3: "I'm 18 or older"
        details_now = user.profile_completed_at is None and bool(user.full_name and user.phone_number)
        if details_now:
            user.profile_completed_at = timezone.now()
        just_completed = not was_complete and user.profile_complete

        if changed or details_now or pref_changed or adult_changed:
            user.save()

    if just_completed:
        record(AuditLog.Action.PROFILE_COMPLETED, target=user, actor=user, request=request)
    elif changed:
        record(AuditLog.Action.PROFILE_UPDATED, target=user, actor=user, request=request, fields=changed)
    return just_completed


# --------------------------------------------------------------------------
# Admin users page (spec 11 §4.4, §6.5). Account facts only — never follows, donations,
# membership or PVC (spec 11 §3.2, §10).
# --------------------------------------------------------------------------
VIEW_DEDUPE_MINUTES = 10


def admin_search(q: str = "", status: str = "", flt: str = ""):
    from django.db.models import Q

    qs = User.objects.all()
    if flt == "ban_failed":
        qs = qs.filter(pk__in=ban_failed_ids())
    if q:
        qs = qs.filter(Q(email__icontains=q) | Q(full_name__icontains=q))
    if status == "active":
        qs = qs.filter(is_active=True)
    elif status == "deactivated":
        qs = qs.filter(is_active=False)
    return qs.order_by("-created_at")


def ban_failed_ids() -> list:
    """Inactive users whose latest deactivation audit entry says the Clerk ban failed."""
    from apps.audit.models import AuditLog

    latest = {}
    for target_id, ban, at in (AuditLog.objects.filter(action=AuditLog.Action.USER_DEACTIVATED)
                               .order_by("target_id", "-created_at").values_list("target_id", "metadata__clerk_ban", "created_at")):
        latest.setdefault(target_id, ban)
    failed = [t for t, ban in latest.items() if ban == "failed"]
    return list(User.objects.filter(pk__in=failed, is_active=False, clerk_deleted_at__isnull=True).values_list("pk", flat=True))


def record_view_once(action, *, target, viewer, request, **meta):
    """Audit a view, but at most once per viewer, target and metadata per 10 minutes (spec 11 §6.5–6.6)."""
    from datetime import timedelta

    from apps.audit.models import AuditLog
    from apps.audit.services import record

    recent = AuditLog.objects.filter(action=action, actor=viewer, target_id=str(target.pk) if target is not None else "",
                                     created_at__gte=timezone.now() - timedelta(minutes=VIEW_DEDUPE_MINUTES))
    if meta:
        recent = recent.filter(metadata=meta)
    if not recent.exists():
        record(action, target=target, actor=viewer, request=request, **meta)


def masked_phone(phone: str) -> str:
    return f"•••• {phone[-4:]}" if phone else ""
