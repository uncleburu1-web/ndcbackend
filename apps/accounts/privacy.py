from apps.privacy.exporting import pick

DESCRIPTION = "Your account: the details NDCConnect keeps about you."


def export_for(user) -> dict:
    return {"account": pick(user, ["id", "email", "full_name", "phone_number", "home_state", "profile_completed_at", "adult_confirmed_at",
                                   "created_at"])}
