"""
Append-only history (spec 16 §6.3). PostgreSQL triggers refuse UPDATE on every event table and the audit log, and
refuse DELETE unless the transaction has opted in:

    with allow_history_delete():
        old.delete()   # retention jobs only (prune_news, prune_posts, prune_moderation, prune_audit)

The opt-in is `SET LOCAL ndc.allow_prune = 'on'`, switched off again when the block ends, so it can't leak past it.
The audit log's chain step (seq, prev_hash, hash written once, right after the insert) is the only update allowed.
"""
from contextlib import contextmanager

from django.db import connection, transaction

HISTORY_TABLES = [
    "donations_donationevent", "membership_membershipevent", "membership_membershippaymentevent", "pvc_pvcevent",
    "news_articleevent", "posts_postevent", "moderation_caseevent", "privacy_datarequestevent", "privacy_incidentevent",
    "audit_auditlog",
]

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


def trigger_sql(table):
    return (f'CREATE TRIGGER ndc_append_only BEFORE UPDATE OR DELETE ON "{table}" '
            f"FOR EACH ROW EXECUTE FUNCTION ndc_append_only();")


def drop_trigger_sql(table):
    return f'DROP TRIGGER IF EXISTS ndc_append_only ON "{table}";'


@contextmanager
def allow_history_delete():
    """Inside a transaction: lets this transaction delete history rows (retention jobs)."""
    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute("SET LOCAL ndc.allow_prune = 'on'")
        yield
        # SET LOCAL lasts to the end of the outermost transaction, not this savepoint: switch it off again here. (On an
        # error the savepoint rollback undoes it.)
        with connection.cursor() as cur:
            cur.execute("SET LOCAL ndc.allow_prune = 'off'")


@contextmanager
def tamper(*tables):
    """Tests only: switch the guard off on these tables so a test can plant a broken row (the audit tamper tests). Never used by
    app code; a test fails if it is (apps/core/tests/test_schema.py)."""
    with connection.cursor() as cur:
        cur.execute("SET CONSTRAINTS ALL IMMEDIATE")  # ALTER TABLE refuses while deferred FK checks are queued
        for t in tables:
            cur.execute(f'ALTER TABLE "{t}" DISABLE TRIGGER ndc_append_only')
    try:
        yield
    finally:
        with connection.cursor() as cur:
            cur.execute("SET CONSTRAINTS ALL IMMEDIATE")
            for t in tables:
                cur.execute(f'ALTER TABLE "{t}" ENABLE TRIGGER ndc_append_only')
            cur.execute("SET CONSTRAINTS ALL DEFERRED")
