"""Spec 19 §4.4 / spec 16 §6.3: geo_change is append-only. Uses `ndc_append_only()` from core 0005."""
from django.db import migrations

TABLE = "geo_change"


class Migration(migrations.Migration):
    dependencies = [("geo", "0002_spec19_geography"), ("core", "0005_append_only_triggers")]
    operations = [migrations.RunSQL(
        f'CREATE TRIGGER ndc_append_only BEFORE UPDATE OR DELETE ON "{TABLE}" FOR EACH ROW EXECUTE FUNCTION ndc_append_only();',
        f'DROP TRIGGER IF EXISTS ndc_append_only ON "{TABLE}";')]
