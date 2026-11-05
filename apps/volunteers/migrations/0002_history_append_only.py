"""Spec 18 §11 / spec 16 §6.3: volunteerchange is append-only. Uses `ndc_append_only()` from core 0005."""
from django.db import migrations

TABLE = "volunteers_volunteerchange"


class Migration(migrations.Migration):
    dependencies = [("volunteers", "0001_initial"), ("core", "0005_append_only_triggers")]
    operations = [migrations.RunSQL(
        f'CREATE TRIGGER ndc_append_only BEFORE UPDATE OR DELETE ON "{TABLE}" FOR EACH ROW EXECUTE FUNCTION ndc_append_only();',
        f'DROP TRIGGER IF EXISTS ndc_append_only ON "{TABLE}";')]
