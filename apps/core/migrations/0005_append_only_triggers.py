"""Spec 16 §6.3: event tables and the audit log refuse UPDATE, and DELETE outside retention jobs
(`apps/core/history.py`). PL/pgSQL is part of PostgreSQL: no new technology."""
from django.db import migrations

# Frozen copies (a migration must not change when app code does). apps/core/tests/test_schema.py checks they still match.
TABLES = ['donations_donationevent', 'membership_membershipevent', 'membership_membershippaymentevent', 'pvc_pvcevent', 'news_articleevent', 'posts_postevent', 'moderation_caseevent', 'privacy_datarequestevent', 'privacy_incidentevent', 'audit_auditlog']

FUNCTION_SQL = r"""
CREATE OR REPLACE FUNCTION ndc_append_only() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    IF current_setting('ndc.allow_prune', true) = 'on' THEN
      RETURN OLD;
    END IF;
    RAISE EXCEPTION 'ndc_append_only: % is append-only; rows are deleted only by retention jobs', TG_TABLE_NAME
      USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  -- to_jsonb so the other tables (no seq column) never touch OLD.seq
  IF TG_TABLE_NAME = 'audit_auditlog' AND to_jsonb(OLD)->>'seq' IS NULL AND to_jsonb(NEW)->>'seq' IS NOT NULL
     AND (to_jsonb(NEW) - 'seq' - 'prev_hash' - 'hash') = (to_jsonb(OLD) - 'seq' - 'prev_hash' - 'hash') THEN
    RETURN NEW;  -- the one-time chain step (audit.services.chain)
  END IF;
  RAISE EXCEPTION 'ndc_append_only: % is append-only; write a new event instead of changing one', TG_TABLE_NAME
    USING ERRCODE = 'integrity_constraint_violation';
END;
$$ LANGUAGE plpgsql;
"""

FORWARD = FUNCTION_SQL + "\n".join(
    f'CREATE TRIGGER ndc_append_only BEFORE UPDATE OR DELETE ON "{t}" FOR EACH ROW EXECUTE FUNCTION ndc_append_only();' for t in TABLES)
BACKWARD = "\n".join(f'DROP TRIGGER IF EXISTS ndc_append_only ON "{t}";' for t in TABLES) + "\nDROP FUNCTION IF EXISTS ndc_append_only();"


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0004_outbox_constraints_prune_index"),
        ("audit", "0012_alter_auditlog_action"),
        ("donations", "0002_state_field_constraints"),
        ("membership", "0002_state_field_constraints_geo_links"),
        ("pvc", "0003_state_field_constraints_geo_links"),
        ("news", "0005_state_field_constraints"),
        ("posts", "0005_constraints"),
        ("moderation", "0003_state_field_constraints"),
        ("privacy", "0002_legacy_consents"),
    ]
    operations = [migrations.RunSQL(FORWARD, BACKWARD)]
