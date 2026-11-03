"""Spec 14 §4.2: follows, applications, PVC records and badges from before this unit get a legacy consent record
(method `before_records`). People with follows are asked to confirm on their next sign-in (§15.3)."""
from django.db import migrations
from django.utils import timezone


def forwards(apps, schema_editor):
    Consent = apps.get_model("privacy", "ConsentRecord")
    Follow = apps.get_model("follows", "CandidateFollow")
    Membership = apps.get_model("membership", "Membership")
    Pvc = apps.get_model("pvc", "PvcVerification")
    now = timezone.now()
    sets = {
        "political_following": set(Follow.objects.values_list("user_id", flat=True).distinct()),
        "membership": set(Membership.objects.exclude(submitted_at__isnull=True).values_list("user_id", flat=True)),
        "pvc_check": set(Pvc.objects.values_list("user_id", flat=True)),
        "verified_badge": set(Pvc.objects.filter(show_badge=True).values_list("user_id", flat=True)),
    }
    rows = []
    for purpose, users in sets.items():
        have = set(Consent.objects.filter(purpose=purpose, withdrawn_at__isnull=True).values_list("user_id", flat=True))
        rows += [Consent(user_id=u, purpose=purpose, version=1, method="before_records") for u in users - have if u]
    Consent.objects.bulk_create(rows, batch_size=1000)
    Consent.objects.filter(method="before_records", granted_at__isnull=True).update(granted_at=now)


def backwards(apps, schema_editor):
    apps.get_model("privacy", "ConsentRecord").objects.filter(method="before_records").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("privacy", "0001_initial"),
        ("follows", "0001_initial"),
        ("membership", "0001_initial"),
        ("pvc", "0002_pvcsettings_pvcverification_show_badge_and_more"),
    ]
    operations = [migrations.RunPython(forwards, backwards)]
