from django.db import migrations


def drop_unflagged_demo_rows(apps, schema_editor):
    # The first seed_demo_donations (10 Nov 2026, local only) wrote rows without a flag. They can't be genuine: the dev-only
    # fake provider, a "demo" channel, reserved .invalid emails and an NDC-DEMO reference. Remove them so they're never counted.
    Donation = apps.get_model("donations", "Donation")
    Donation.objects.filter(provider="fake", channel="demo", reference__startswith="NDC-DEMO",
                            donor_email__endswith="@example.invalid").delete()



class Migration(migrations.Migration):
    dependencies = [("donations", "0003_demo_flag")]
    operations = [migrations.RunPython(drop_unflagged_demo_rows, migrations.RunPython.noop)]
