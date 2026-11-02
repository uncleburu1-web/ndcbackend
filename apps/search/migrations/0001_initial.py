import uuid

import django.contrib.postgres.indexes
import django.contrib.postgres.search
from django.contrib.postgres.operations import TrigramExtension
from django.db import migrations, models
from django.db.utils import ProgrammingError


class EnableTrigram(TrigramExtension):
    """CREATE EXTENSION pg_trgm, with a clear message when the database user may not (spec 12 §5.3, §11)."""

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        try:
            super().database_forwards(app_label, schema_editor, from_state, to_state)
        except ProgrammingError as exc:
            raise ProgrammingError(
                "Enable the pg_trgm extension on this database (managed PostgreSQL: run CREATE EXTENSION pg_trgm "
                f"as the database owner), then run migrate again. Original error: {exc}") from exc


class Migration(migrations.Migration):
    initial = True
    dependencies = []

    operations = [
        EnableTrigram(),
        migrations.CreateModel(
            name="SearchEntry",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("kind", models.CharField(choices=[("candidate", "Candidate"), ("article", "News"), ("post", "Candidate post")], db_index=True,
                                          max_length=12)),
                ("object_id", models.UUIDField()),
                ("title", models.CharField(max_length=200)),
                ("folded_title", models.CharField(max_length=200)),
                ("title_key", models.CharField(blank=True, default="", max_length=200)),
                ("document", django.contrib.postgres.search.SearchVectorField(null=True)),
                ("body_text", models.TextField(blank=True, default="")),
                ("state", models.CharField(blank=True, db_index=True, default="", max_length=20)),
                ("position", models.CharField(blank=True, db_index=True, default="", max_length=20)),
                ("election_year", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("source", models.CharField(blank=True, default="", max_length=10)),
                ("published_at", models.DateTimeField(blank=True, null=True)),
                ("rank_order", models.PositiveSmallIntegerField(default=0)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "indexes": [
                    django.contrib.postgres.indexes.GinIndex(fields=["document"], name="search_document_gin"),
                    django.contrib.postgres.indexes.GinIndex(fields=["folded_title"], name="search_title_trgm", opclasses=["gin_trgm_ops"]),
                    models.Index(fields=["kind", "state", "published_at"], name="search_kind_state_date"),
                ],
                "constraints": [models.UniqueConstraint(fields=("kind", "object_id"), name="search_entry_unique_item")],
            },
        ),
    ]
