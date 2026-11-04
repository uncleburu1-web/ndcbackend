"""Spec 15 §12.4: every Clerk Svix ID in accounts.ProcessedWebhook becomes a WebhookReceipt, so replays stay no-ops."""
from django.db import migrations


def forwards(apps, schema_editor):
    Old = apps.get_model("accounts", "ProcessedWebhook")
    New = apps.get_model("integrations", "WebhookReceipt")
    batch = []
    for o in Old.objects.all().iterator():
        batch.append(New(provider="clerk", event_id=o.svix_id, event_type=o.event_type, status="processed", signature_valid=True,
                         processed_at=o.received_at))
        if len(batch) >= 1000:
            New.objects.bulk_create(batch, ignore_conflicts=True)
            batch = []
    New.objects.bulk_create(batch, ignore_conflicts=True)
    # keep the original receipt times (auto_now_add set "now" on insert)
    for o in Old.objects.all().only("svix_id", "received_at").iterator():
        New.objects.filter(provider="clerk", event_id=o.svix_id).update(received_at=o.received_at)


def backwards(apps, schema_editor):
    Old = apps.get_model("accounts", "ProcessedWebhook")
    New = apps.get_model("integrations", "WebhookReceipt")
    Old.objects.bulk_create([Old(svix_id=r.event_id, event_type=r.event_type or "") for r in New.objects.filter(provider="clerk")],
                            ignore_conflicts=True)


class Migration(migrations.Migration):
    dependencies = [("integrations", "0001_initial"), ("accounts", "0003_adult_last_seen_mfa")]
    operations = [migrations.RunPython(forwards, backwards)]
