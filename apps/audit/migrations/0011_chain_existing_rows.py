"""Spec 14 §4.8: chain the existing audit rows in creation order. The hash recipe is frozen here on purpose
(it must match apps.audit.services.canonical/row_hash at the time of this migration)."""
import hashlib
import json

from django.db import migrations


def canonical(row):
    return json.dumps({
        "id": str(row.id), "seq": row.seq, "actor": str(row.actor_id or ""), "actor_label": row.actor_label, "action": row.action,
        "target_type": row.target_type, "target_id": row.target_id, "metadata": row.metadata, "ip": row.ip_address or "",
        "ua": row.user_agent, "created_at": row.created_at.isoformat(),
    }, sort_keys=True, separators=(",", ":"), default=str)


def forwards(apps, schema_editor):
    AuditLog = apps.get_model("audit", "AuditLog")
    prev, seq = "", 0
    batch = []
    for row in AuditLog.objects.filter(seq__isnull=True).order_by("created_at", "id").iterator(chunk_size=2000):
        seq += 1
        row.seq, row.prev_hash = seq, prev
        row.hash = hashlib.sha256((prev + "|" + canonical(row)).encode()).hexdigest()
        prev = row.hash
        batch.append(row)
        if len(batch) >= 2000:
            AuditLog.objects.bulk_update(batch, ["seq", "prev_hash", "hash"])
            batch = []
    if batch:
        AuditLog.objects.bulk_update(batch, ["seq", "prev_hash", "hash"])


def backwards(apps, schema_editor):
    apps.get_model("audit", "AuditLog").objects.update(seq=None, prev_hash="", hash="")


class Migration(migrations.Migration):
    dependencies = [("audit", "0010_audit_chain_fields")]
    operations = [migrations.RunPython(forwards, backwards)]
