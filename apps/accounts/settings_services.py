"""
Settings and user preferences (spec 20). The only place a person's settings are read or written.

- Preferences live in `UserPreferences` (created on first save; defaults when absent).
- Notification switches live in `notifications.NotificationPreference` (one row per person, as before).
- Nothing here lets anyone but the person read or change their settings (rule 6); admins only set defaults for new
  accounts (`SettingsDefaults`) and see counts.
"""
import secrets

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.audit.services import record
from apps.core.exceptions import ServiceError

from .models import (
    NATIVE_NAMES,
    Contrast,
    DonationName,
    Language,
    Motion,
    NameStyle,
    ProfileLocation,
    RetiredHandle,
    SettingsDefaults,
    TextSize,
    Theme,
    User,
    UserPreferences,
)
from .naming import format_name

# Languages whose translations are complete (spec 21 adds to this). Only these can be enabled by an admin.
TRANSLATIONS_COMPLETE = {"en"}

DISPLAY_FIELDS = {"theme": Theme, "text_size": TextSize, "contrast": Contrast, "motion": Motion}
BIO_MAX = 300
HANDLE_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no 0/o, 1/l/i: easy to read aloud

# §7: switchable categories → NotificationPreference field. Order is the page's order.
SWITCHABLE = [
    ("candidate_events", "Candidate events", ["candidate_event"], "New events from candidates whose bell is on."),
    ("announcements", "Platform announcements", ["announcement"], "Official NDC announcements."),
    ("community_replies", "Community replies", ["community_reply"], "Replies to your community posts."),
    ("community_posts", "New community posts", ["community_post"], "New posts in communities where you turned the bell on."),
    ("event_reminders", "Event reminders", ["event_reminder"], "Reminders before events you registered for."),
    ("volunteer_updates", "Volunteer updates from campaigns", ["volunteer_update"], "Updates from campaigns that accepted you."),
]
LOCKED = [
    ("your_events", "Your events", ["event_update", "event_waitlist"],
     "Changes to an event you registered for, and waitlist places, so you never turn up to a cancelled or moved event."),
    ("your_applications", "Your applications", ["membership_decision", "pvc_decision", "volunteer_decision", "news_decision",
                                                "community_placed", "geo_confirm", "geo_unit_changed"],
     "Decisions and changes about your own membership, PVC check, volunteering, news and communities."),
    ("payments", "Payments", ["donation_receipt"], "Your receipts."),
    ("moderation_privacy", "Moderation and privacy", ["report_outcome", "content_removed", "account_warning", "submissions_paused",
                                                      "appeal_decision", "privacy_request_update"],
     "Decisions about your content and answers to your privacy requests."),
    ("security", "Security", ["security_notice"], "Security notices about your account."),
]
SWITCH_KEYS = [k for k, *_ in SWITCHABLE]


def _bad(message, code="validation_error", status=400, field=None):
    e = ServiceError(message, code=code, status_code=status)
    if field:
        e.extra = {"fields": {field: [message]}}
    return e


# ------------------------------------------------------------------------------------------------------- defaults
def defaults() -> SettingsDefaults:
    return SettingsDefaults.objects.filter(pk=1).first() or SettingsDefaults(pk=1)


def enabled_languages() -> list[str]:
    stored = [c for c in (defaults().enabled_languages or []) if c in Language.values]
    return ["en", *[c for c in Language.values if c != "en" and c in stored]]


def language_options() -> list[dict]:
    on = set(enabled_languages())
    return [{"code": c, "label": label, "native": NATIVE_NAMES[c], "enabled": c in on} for c, label in Language.choices]


def prefs_for(user) -> UserPreferences:
    """The person's row, or an unsaved one with defaults (never written by a read)."""
    p = UserPreferences.objects.filter(user=user).select_related("profile_lga").first()
    if p is None:
        d = defaults()
        p = UserPreferences(user=user, theme=d.default_theme, text_size=d.default_text_size)
    return p


def _save(p: UserPreferences, fields: list[str]):
    if p.saved_at is None:
        p.saved_at = timezone.now()
        fields = [*fields, "saved_at"]
    if p._state.adding:
        p.save()
    else:
        p.save(update_fields=[*fields, "updated_at"])


def apply_defaults_for_new_user(user):
    """New accounts start with the admin's defaults (§14). Existing people are never changed."""
    from apps.notifications.models import NotificationPreference

    d = SettingsDefaults.objects.filter(pk=1).first()
    if d is None:
        return
    notif = {k: bool(v) for k, v in (d.notifications or {}).items() if k in SWITCH_KEYS}
    if any(v is False for v in notif.values()):
        NotificationPreference.objects.get_or_create(user=user, defaults=notif)
    if d.default_theme != Theme.SYSTEM or d.default_text_size != TextSize.STANDARD:
        UserPreferences.objects.get_or_create(user=user, defaults={"theme": d.default_theme, "text_size": d.default_text_size})


# --------------------------------------------------------------------------------------------------- read (§17.1)
def notifications_payload(user) -> dict:
    from apps.notifications.models import NotificationPreference

    np = NotificationPreference.objects.filter(user=user).first()
    cats = [{"key": k, "label": label, "kinds": kinds, "help": help_, "on": getattr(np, k) if np else True, "locked": False, "reason": ""}
            for k, label, kinds, help_ in SWITCHABLE]
    cats += [{"key": k, "label": label, "kinds": kinds, "help": "", "on": True, "locked": True, "reason": reason}
             for k, label, kinds, reason in LOCKED]
    return {"categories": cats, "candidate_bells_paused": bool(np and np.candidate_bells_paused)}


def _account(user, request) -> dict:
    from apps.access.security import is_admin
    from apps.pvc.services import status_for

    m = getattr(user, "membership", None) if hasattr(user, "membership") else None
    claims = getattr(getattr(request, "auth", None), "claims", None) if request is not None else None
    mfa = user.mfa_enabled
    if claims is not None and "mfa" in claims:
        from apps.access.security import mfa_enabled

        mfa = mfa_enabled(claims)
    staff = is_admin(user)
    return {"full_name": user.full_name, "phone_number": user.phone_number, "email": user.email, "created_at": user.created_at,
            "status": "active" if user.is_active else user.deactivation_kind, "home_state": user.home_state,
            "membership_status": m.status if m else None, "pvc_status": status_for(user),
            "staff": staff, "mfa_enabled": mfa if staff else None}


def _profile(user, p: UserPreferences) -> dict:
    from apps.privacy.register import CONSENT_PURPOSES

    purpose = CONSENT_PURPOSES["public_profile"]
    lga = p.profile_lga
    return {"public_profile": p.public_profile, "handle": p.profile_handle if p.public_profile else None,
            "name_style": p.name_style, "name_examples": {s: format_name(user.full_name, s) for s in NameStyle.values},
            "bio": p.bio, "profile_location": p.profile_location,
            "profile_lga": {"id": lga.pk, "name": lga.name} if lga else None, "profile_photo": p.profile_photo,
            "bio_removed": p.bio_removed_at is not None, "photo_removed": p.photo_removed_at is not None,
            "consent": {"version": purpose.version, "text": purpose.text, "label": purpose.label}}


def _privacy(user, p: UserPreferences) -> dict:
    from apps.news.models import Article

    pvc = getattr(user, "pvc", None) if hasattr(user, "pvc") else None
    return {"donation_name_default": p.donation_name_default, "verified_badge": bool(pvc and pvc.show_badge),
            "named_news": Article.objects.filter(author=user, show_author_name=True).count(),
            "public_profile": p.public_profile, "name_style": p.name_style}


def _connected(user) -> dict:
    from apps.posts.services import linked_candidates

    return {"campaigns": [{"name": c.full_name, "slug": c.slug} for c in linked_candidates(user)]}


def pause_preview(user) -> dict:
    from apps.events.models import EventRegistration
    from apps.volunteers.models import AppStatus, VolunteerApplication

    return {"upcoming_registrations": EventRegistration.objects.filter(user=user, event__ends_at__gt=timezone.now()).count(),
            "pending_volunteer_applications": VolunteerApplication.objects.filter(user=user, status=AppStatus.PENDING).count()}


def display_payload(p: UserPreferences) -> dict:
    return {"theme": p.theme, "text_size": p.text_size, "contrast": p.contrast, "motion": p.motion}


def settings_payload(user, request=None) -> dict:
    p = prefs_for(user)
    return {
        "account": _account(user, request),
        "profile": _profile(user, p),
        "language": {"language": p.language, "options": language_options()},
        "accessibility": display_payload(p),
        "notifications": notifications_payload(user),
        "privacy": _privacy(user, p),
        "connected": _connected(user),
        "manage": pause_preview(user),
        "saved_at": p.saved_at,
        "updated_at": p.updated_at,
    }


# -------------------------------------------------------------------------------------------------- write (§17.1)
def _choice(data, field, enum):
    v = data[field]
    if v not in enum.values:
        raise _bad(f"Choose one of: {', '.join(enum.values)}.", field=field)
    return v


def set_language(user, language) -> dict:
    if language not in Language.values:
        raise _bad("Choose a language from the list.", field="language")
    if language not in enabled_languages():
        raise _bad(f"{Language(language).label} isn't available yet.", code="language_unavailable", field="language")
    p = prefs_for(user)
    p.language = language
    _save(p, ["language"])
    return {"language": p.language, "options": language_options()}


def set_display(user, data: dict) -> dict:
    fields = [f for f in DISPLAY_FIELDS if f in data]
    if not fields:
        raise _bad("Send theme, text_size, contrast or motion.")
    p = prefs_for(user)
    for f in fields:
        setattr(p, f, _choice(data, f, DISPLAY_FIELDS[f]))
    _save(p, fields)
    return display_payload(p)


def set_privacy(user, data: dict) -> dict:
    if "donation_name_default" not in data:
        raise _bad("Send donation_name_default.")
    p = prefs_for(user)
    p.donation_name_default = _choice(data, "donation_name_default", DonationName)
    _save(p, ["donation_name_default"])
    return _privacy(user, p)


def _clean_bio(text) -> str:
    if not isinstance(text, str):
        raise _bad("The bio must be text.", field="bio")
    text = "".join(ch for ch in text if ch == "\n" or ch >= " ").strip()  # plain text: control characters removed
    if len(text) > BIO_MAX:
        raise _bad(f"Keep your bio under {BIO_MAX} characters.", field="bio")
    return text


def set_profile(user, data: dict) -> dict:
    from apps.geo.models import Lga

    p = prefs_for(user)
    fields = []
    if "name_style" in data:
        p.name_style = _choice(data, "name_style", NameStyle)
        fields.append("name_style")
    if "bio" in data:
        bio = _clean_bio(data["bio"])
        if bio != p.bio and p.bio_removed_at:  # a new bio is new content (§5.1); a moderator can remove it again
            p.bio_removed_at = None
            fields.append("bio_removed_at")
        p.bio = bio
        fields.append("bio")
    if "profile_photo" in data:
        if not isinstance(data["profile_photo"], bool):
            raise _bad("Send true or false.", field="profile_photo")
        p.profile_photo = data["profile_photo"]
        fields.append("profile_photo")
    if "profile_location" in data or "profile_lga" in data:
        loc = _choice(data, "profile_location", ProfileLocation) if "profile_location" in data else p.profile_location
        lga = p.profile_lga
        if "profile_lga" in data:
            lga = None
            if data["profile_lga"] not in (None, ""):
                lga = Lga.objects.select_related("state").filter(pk=data["profile_lga"]).first()
                if lga is None:
                    raise _bad("Choose your LGA from the list.", code="profile_location_invalid", field="profile_lga")
        if loc != ProfileLocation.NONE and not user.home_state:
            raise _bad("Set your home state in My account first.", code="profile_location_invalid", field="profile_location")
        if loc == ProfileLocation.LGA:
            if lga is None:
                raise _bad("Choose your LGA.", code="profile_location_invalid", field="profile_lga")
            if lga.state.slug != user.home_state:
                raise _bad("That LGA isn't in your home state.", code="profile_location_invalid", field="profile_lga")
        else:
            lga = None
        p.profile_location, p.profile_lga = loc, lga
        fields += ["profile_location", "profile_lga"]
    if not fields:
        raise _bad("Nothing to change.")
    _save(p, fields)
    return _profile(user, p)


def _new_handle() -> str:
    for _ in range(20):
        h = "".join(secrets.choice(HANDLE_ALPHABET) for _ in range(6))
        if not UserPreferences.objects.filter(profile_handle=h).exists() and not RetiredHandle.objects.filter(pk=h).exists():
            return h
    raise ServiceError("Couldn't create a profile address. Try again.", code="invalid_transition", status_code=409)


def public_profile_on(user, *, consent, version, request=None) -> dict:
    from apps.privacy import consent as C
    from apps.privacy.register import CONSENT_PURPOSES

    purpose = CONSENT_PURPOSES["public_profile"]
    if consent is not True or version != purpose.version:
        e = _bad("Agree to show a public profile first.", code="profile_consent_required")
        e.extra = {"purpose": purpose.key, "version": purpose.version, "text": purpose.text, "label": purpose.label}
        raise e
    with transaction.atomic():
        C.grant(user, "public_profile")
        p = prefs_for(user)
        if not p.profile_handle:
            p.profile_handle = _new_handle()
        p.public_profile = True
        try:
            with transaction.atomic():
                _save(p, ["public_profile", "profile_handle"])
        except IntegrityError:  # a handle collision in a race: try once more
            p.profile_handle = _new_handle()
            _save(p, ["public_profile", "profile_handle"])
    record(AuditLog.Action.PUBLIC_PROFILE_ON, target=user, actor=user, request=request)
    return _profile(user, p)


def profile_off(user, *, request=None, audit=True) -> bool:
    """Called by consent withdrawal (privacy._undo) and by the switch. Keeps the handle for next time (§5.1)."""
    n = UserPreferences.objects.filter(user=user, public_profile=True).update(public_profile=False, updated_at=timezone.now())
    if n and audit:
        record(AuditLog.Action.PUBLIC_PROFILE_OFF, target=user, actor=user, request=request)
    return bool(n)


def public_profile_off(user, *, request=None) -> dict:
    from apps.privacy import consent as C

    if C.active(user, "public_profile"):
        C.withdraw(user, "public_profile", request=request)  # its undo turns the profile off and audits it
    else:
        profile_off(user, request=request)
    return _profile(user, prefs_for(user))


def set_notifications(user, data: dict) -> dict:
    from apps.notifications.models import NotificationPreference

    keys = [k for k in [*SWITCH_KEYS, "candidate_bells_paused"] if k in data]
    locked = [k for k, *_ in LOCKED if k in data]
    if locked:
        raise _bad("These notices can't be turned off: " + ", ".join(locked) + ".", code="validation_error")
    if not keys or any(not isinstance(data[k], bool) for k in keys):
        raise _bad("Send one or more categories as true or false.")
    NotificationPreference.objects.update_or_create(user=user, defaults={k: data[k] for k in keys})
    return notifications_payload(user)


def import_device(user, data: dict) -> dict:
    """§13: the device's language and display settings, copied once to an account that never saved its own."""
    p = prefs_for(user)
    if p.saved_at is not None:
        raise ServiceError("Your account already has its own settings.", code="invalid_transition", status_code=409)
    fields = []
    for f, enum in DISPLAY_FIELDS.items():
        if f in data:
            setattr(p, f, _choice(data, f, enum))
            fields.append(f)
    if "language" in data and data["language"] in enabled_languages():
        p.language = data["language"]
        fields.append("language")
    if not fields:
        raise _bad("Nothing to copy.")
    _save(p, fields)
    return {"accessibility": display_payload(p), "language": {"language": p.language, "options": language_options()},
            "saved_at": p.saved_at}


# ---------------------------------------------------------------------------------------------- security (§9)
def sign_out_others(user, request) -> dict:
    from . import clerk

    current = (getattr(getattr(request, "auth", None), "claims", None) or {}).get("sid", "")
    try:
        n = clerk.revoke_other_sessions(user.clerk_user_id, keep=current)
    except clerk.ClerkUnavailable as exc:
        raise ServiceError("Clerk is unavailable, so no devices were signed out. Try again in a few minutes.", code="auth_unavailable",
                           status_code=503) from exc
    record(AuditLog.Action.SETTINGS_SIGNED_OUT_OTHERS, target=user, actor=user, request=request, sessions=n)
    return {"signed_out": n}


# ------------------------------------------------------------------------------------------- pause (§12)
def pause(user, *, confirm="", request=None) -> dict:
    from apps.access.services import RoleError, _guard_last_admin
    from apps.events.services import cancel_upcoming_for
    from apps.volunteers.services import withdraw_pending

    if (confirm or "").strip().lower() != "pause":
        raise _bad('Type "pause" to confirm.')
    with transaction.atomic():
        u = User.objects.select_for_update().get(pk=user.pk)
        if not u.is_active:
            raise ServiceError("This account is already inactive.", code="invalid_transition", status_code=409)
        try:
            _guard_last_admin(u)
        except RoleError as exc:
            raise ServiceError(str(exc), code=exc.code, status_code=exc.status) from exc
        cancelled = cancel_upcoming_for(u)
        withdrawn = withdraw_pending(u)
        u.is_active, u.deactivation_kind, u.deactivated_at = False, "self", timezone.now()
        u.save(update_fields=["is_active", "deactivation_kind", "deactivated_at", "updated_at"])
    record(AuditLog.Action.ACCOUNT_PAUSED_SELF, target=u, actor=u, request=request, registrations_cancelled=cancelled,
           applications_withdrawn=withdrawn)
    return {"paused": True, "registrations_cancelled": cancelled, "applications_withdrawn": withdrawn}


def reactivate(user, *, request=None) -> dict:
    with transaction.atomic():
        u = User.objects.select_for_update().get(pk=user.pk)
        if u.is_active:
            return {"active": True}
        if u.deactivation_kind != "self" or u.clerk_deleted_at:
            raise ServiceError("Only an administrator can reactivate this account.", code="account_deactivated", status_code=403)
        u.is_active, u.deactivation_kind, u.deactivated_at = True, "", None
        u.save(update_fields=["is_active", "deactivation_kind", "deactivated_at", "updated_at"])
    record(AuditLog.Action.ACCOUNT_REACTIVATED_SELF, target=u, actor=u, request=request)
    return {"active": True}


# ------------------------------------------------------------------------------------------- admin defaults (§14)
def defaults_payload() -> dict:
    d = defaults()
    return {"enabled_languages": enabled_languages(), "languages": language_options(),
            "translations_complete": sorted(TRANSLATIONS_COMPLETE),
            "notifications": {k: bool((d.notifications or {}).get(k, True)) for k in SWITCH_KEYS},
            "notification_labels": {k: label for k, label, *_ in SWITCHABLE},
            "default_theme": d.default_theme, "default_text_size": d.default_text_size,
            "updated_at": d.updated_at if d.pk and not d._state.adding else None}


def set_defaults(*, data: dict, actor, request=None) -> dict:
    with transaction.atomic():
        d, _ = SettingsDefaults.objects.select_for_update().get_or_create(pk=1)
        before = defaults_payload()
        if "enabled_languages" in data:
            langs = data["enabled_languages"]
            if not isinstance(langs, list) or any(c not in Language.values for c in langs):
                raise _bad("Choose languages from the list.", field="enabled_languages")
            not_ready = [c for c in langs if c not in TRANSLATIONS_COMPLETE]
            if not_ready:
                raise _bad(", ".join(Language(c).label for c in not_ready) + " can't be turned on until the translations are complete.",
                           code="language_unavailable", field="enabled_languages")
            d.enabled_languages = sorted(set(langs) - {"en"})
        if "notifications" in data:
            n = data["notifications"]
            if not isinstance(n, dict) or any(k not in SWITCH_KEYS or not isinstance(v, bool) for k, v in n.items()):
                raise _bad("Send notification categories as true or false.", field="notifications")
            d.notifications = {**(d.notifications or {}), **n}
        if "default_theme" in data:
            d.default_theme = _choice(data, "default_theme", Theme)
        if "default_text_size" in data:
            d.default_text_size = _choice(data, "default_text_size", TextSize)
        d.updated_by = actor
        d.save()
        after = defaults_payload()
    changed = {k: {"from": before[k], "to": after[k]} for k in ("enabled_languages", "notifications", "default_theme", "default_text_size")
               if before[k] != after[k]}
    record(AuditLog.Action.SETTINGS_DEFAULTS_CHANGED, target=d, actor=actor, request=request, changes=changed)
    return after


# ------------------------------------------------------------------------------------------- deletion (14)
def erase_for_deleted_user(user):
    """Inside accounts.mark_deleted: settings go; the handle is retired for good."""
    p = UserPreferences.objects.filter(user=user).first()
    if p is None:
        return
    if p.profile_handle:
        RetiredHandle.objects.get_or_create(handle=p.profile_handle)
    p.delete()


# ------------------------------------------------------------------------------------------- public profile (§5.1)
def public_profile(handle: str):
    """The public page's data, or None (404) when off, paused, deleted, deactivated or unknown."""
    p = (UserPreferences.objects.select_related("user", "profile_lga__state").filter(profile_handle=handle, public_profile=True).first())
    if p is None:
        return None
    u = p.user
    if not u.is_active or u.clerk_deleted_at:
        return None
    from apps.candidates.reference import State
    from apps.news.models import Article
    from apps.pvc.services import public_badge

    location = None
    if p.profile_location == ProfileLocation.STATE and u.home_state:
        location = {"state": u.home_state, "state_label": State(u.home_state).label, "lga": None}
    elif p.profile_location == ProfileLocation.LGA and p.profile_lga:
        location = {"state": p.profile_lga.state.slug, "state_label": p.profile_lga.state.name, "lga": p.profile_lga.name}
    news = (Article.objects.filter(author=u, source="community", show_author_name=True, status="successful")
            .order_by("-published_at").values("id", "title", "published_at")[:20])
    return {"handle": p.profile_handle, "name": format_name(u.full_name, p.name_style) or "A member",
            "bio": "" if p.bio_removed_at else p.bio, "location": location, "verified_voter": public_badge(u),
            "photo_url": (u.image_url or None) if p.profile_photo and not p.photo_removed_at else None, "member_since": u.created_at.strftime("%Y-%m"),
            "news": list(news)}


# ------------------------------------------------------------------------------------------- moderation (§5.1, 13)
PROFILE_PARTS = ("bio", "photo")


def moderator_remove(p: UserPreferences, moderator, *, rule, parts=None, note_to_author="", internal_note="", source=None):
    """13's Remove for a public profile: hides the bio, the photo or both. Nothing is edited or deleted, so Restore puts it back."""
    from apps.moderation.services import content_removed

    parts = list(parts) if parts else [x for x in PROFILE_PARTS if (x == "bio" and p.bio) or (x == "photo" and p.profile_photo)]
    if not parts or any(x not in PROFILE_PARTS for x in parts):
        raise _bad("Choose what to remove: the bio, the photo or both.", field="parts")
    now = timezone.now()
    fields = {f"{x}_removed_at": now for x in parts}
    UserPreferences.objects.filter(pk=p.pk).update(**fields, updated_at=now)
    p = UserPreferences.objects.select_related("user").get(pk=p.pk)
    words = " and ".join("bio" if x == "bio" else "photo" for x in parts)
    return content_removed("profile", p, moderator, rule_id=rule, internal_note=internal_note, source=source,
                           note_to_author=(f"Removed: your {words}." + (f" {note_to_author}" if note_to_author else "")))


def moderator_restore(p: UserPreferences, moderator, note=""):
    from apps.moderation.services import content_restored

    UserPreferences.objects.filter(pk=p.pk).update(bio_removed_at=None, photo_removed_at=None, updated_at=timezone.now())
    return content_restored("profile", UserPreferences.objects.select_related("user").get(pk=p.pk), moderator, note)
