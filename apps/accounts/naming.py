"""
Public names (spec 20 §5.2): one style per person, used wherever they're named publicly by their own choice: news
bylines (07), community bylines (17) and the public profile. Built from the account name, never typed separately.

Organiser and campaign views (events, volunteering, moderation) keep `communities.text.short_name` on purpose: those
are not the person's public byline and always use first name and last initial.
"""
FORMER = "Former member"


def is_gone(user) -> bool:
    """Deleted or deactivated by an admin. A person who paused their own account keeps their bylines (§12)."""
    if user is None or getattr(user, "clerk_deleted_at", None):
        return True
    return not user.is_active and getattr(user, "deactivation_kind", "") != "self"


def _prefs(user) -> tuple[str, str | None]:
    """(name style, public handle if the profile is on), read once per user object."""
    cached = getattr(user, "_prefs_cache", None)
    if cached is None:
        from .models import UserPreferences

        row = UserPreferences.objects.filter(user_id=user.pk).values_list("name_style", "public_profile", "profile_handle").first()
        cached = (row[0], row[2] if row[1] else None) if row else ("first_initial", None)
        user._prefs_cache = cached
    return cached


def name_style(user) -> str:
    return _prefs(user)[0]


def public_handle(user) -> str | None:
    """The person's public profile handle, only while it's live (§5.1: linked only from their named news bylines)."""
    if user is None or not user.is_active or getattr(user, "clerk_deleted_at", None):
        return None
    return _prefs(user)[1]


def format_name(full_name: str, style: str) -> str:
    parts = (full_name or "").split()
    if not parts:
        return ""
    if style == "full" or len(parts) == 1:
        return " ".join(parts)
    return f"{parts[0]} {parts[-1][0].upper()}."


def public_name(user, *, missing: str = "A member") -> str:
    """The person's public name in their chosen style; "Former member" for deleted or admin-deactivated accounts."""
    if is_gone(user):
        return FORMER
    return format_name(user.full_name, name_style(user)) or missing
