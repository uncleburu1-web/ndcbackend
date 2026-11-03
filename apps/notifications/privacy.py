from apps.privacy.exporting import pick

DESCRIPTION = "Your in-app notifications and notification settings."


def export_for(user) -> dict:
    from .models import Notification, NotificationPreference

    pref = NotificationPreference.objects.filter(user=user).first()
    return {"notifications": [pick(n, ["kind", "decision", "created_at", "read_at"]) for n in Notification.objects.filter(recipient=user)
                              .order_by("created_at")],
            "notification_settings": {"announcements": pref.announcements if pref else True}}
