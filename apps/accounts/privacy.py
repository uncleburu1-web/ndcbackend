from apps.privacy.exporting import pick

DESCRIPTION = "Your account: the details NDCConnect keeps about you."


def export_for(user) -> dict:
    from .models import UserPreferences

    out = {"account": pick(user, ["id", "email", "full_name", "phone_number", "home_state", "image_url", "profile_completed_at",
                                  "adult_confirmed_at", "created_at", "deactivation_kind"])}
    p = UserPreferences.objects.filter(user=user).select_related("profile_lga").first()
    if p is not None:  # spec 20: your settings and public profile
        out["settings"] = pick(p, ["language", "theme", "text_size", "contrast", "motion", "name_style", "donation_name_default",
                                   "public_profile", "profile_handle", "bio", "profile_location",
                                   ("profile_lga", lambda p: p.profile_lga.name if p.profile_lga_id else None), "profile_photo",
                                   "saved_at", "updated_at"])
    return out
