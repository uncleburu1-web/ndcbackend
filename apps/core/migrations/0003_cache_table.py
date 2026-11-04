"""Spec 15 §11.4: the shared database cache table (`createcachetable`), created whatever CACHE_BACKEND says so that
switching to `database` needs no extra step. Idempotent."""
from django.db import migrations

TABLE = "ndc_cache"


def create(apps, schema_editor):
    with schema_editor.connection.cursor() as cur:
        cur.execute(f"""CREATE TABLE IF NOT EXISTS "{TABLE}" (
            "cache_key" varchar(255) NOT NULL PRIMARY KEY,
            "value" text NOT NULL,
            "expires" timestamp with time zone NOT NULL)""")
        cur.execute(f'CREATE INDEX IF NOT EXISTS "{TABLE}_expires" ON "{TABLE}" ("expires")')


def drop(apps, schema_editor):
    with schema_editor.connection.cursor() as cur:
        cur.execute(f'DROP TABLE IF EXISTS "{TABLE}"')


class Migration(migrations.Migration):
    dependencies = [("core", "0002_outboxjob")]
    operations = [migrations.RunPython(create, drop)]
