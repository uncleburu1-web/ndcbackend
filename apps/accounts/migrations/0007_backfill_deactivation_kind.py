from django.db import migrations


def backfill(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    User.objects.filter(is_active=False, clerk_deleted_at__isnull=False).update(deactivation_kind="deleted")
    User.objects.filter(is_active=False, clerk_deleted_at__isnull=True).update(deactivation_kind="admin")


class Migration(migrations.Migration):
    dependencies = [("accounts", "0006_settings_and_preferences")]
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
