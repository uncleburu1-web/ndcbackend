"""Spec 13 §5.3: the old report table goes once moderation has copied every row (its migration refuses to
finish if the counts differ)."""
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("posts", "0002_candidatepost_removed_rule"), ("moderation", "0002_move_existing_reports")]

    operations = [migrations.DeleteModel(name="PostReport")]
