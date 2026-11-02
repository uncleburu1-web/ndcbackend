"""Spec 13 §5.4: `removed_rule` next to `removed_reason`. Existing removals get "R4" when the reason mentions
false information or results; otherwise blank (shown as "our community rules")."""
from django.db import migrations, models


def backfill(apps, schema_editor):
    M = apps.get_model("posts", "CandidatePost")
    for obj in M.objects.filter(status="removed").only("pk", "removed_reason"):
        text = (obj.removed_reason or "").lower()
        if "false" in text or "result" in text or "mislead" in text:
            M.objects.filter(pk=obj.pk).update(removed_rule="R4")


class Migration(migrations.Migration):
    dependencies = [("posts", "0001_initial")]

    operations = [
        migrations.AddField(model_name="candidatepost", name="removed_rule", field=models.CharField(blank=True, max_length=8)),
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
