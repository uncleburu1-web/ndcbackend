"""
Spec 13 §5.3: copy every news.ArticleReport and posts.PostReport into moderation.Report, group open
ones into one open Case per item, and create closed cases for resolved ones. Idempotent; the old
tables are dropped by later migrations in news and posts, which depend on this one.
"""
import secrets
from datetime import timedelta

from django.db import migrations

ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
RANK = {"legal": 0, "urgent": 1, "high": 2, "normal": 3, "low": 4}
REASON_PRIORITY = {"abuse": "urgent", "privacy": "urgent", "false": "high", "spam": "low", "copyright": "normal", "other": "normal"}
HOURS = {"urgent": 24, "high": 48, "normal": 72, "low": 168}


def _ref(Report):
    while True:
        ref = "RPT-" + "".join(secrets.choice(ALPHABET) for _ in range(6))
        if not Report.objects.filter(reference=ref).exists():
            return ref


def forwards(apps, schema_editor):
    Report = apps.get_model("moderation", "Report")
    Case = apps.get_model("moderation", "Case")
    CaseEvent = apps.get_model("moderation", "CaseEvent")
    sources = [("article", apps.get_model("news", "ArticleReport"), "article", "title"),
               ("post", apps.get_model("posts", "PostReport"), "post", "title")]
    for kind, Model, fk, title_field in sources:
        for old in Model.objects.select_related(fk).order_by("created_at"):
            target = getattr(old, fk)
            if Report.objects.filter(target_kind=kind, target_id=target.pk, created_at=old.created_at, details=old.details,
                                     reporter_id=old.reporter_id).exists():
                continue  # already moved (idempotent)
            pr = REASON_PRIORITY.get(old.reason, "normal")
            if old.status == "open":
                case = Case.objects.filter(target_kind=kind, target_id=target.pk).exclude(status="closed").first()
                if case is None:
                    case = Case.objects.create(target_kind=kind, target_id=target.pk, source="user_report", status="open", priority=pr,
                                               priority_rank=RANK[pr], due_at=old.created_at + timedelta(hours=HOURS[pr]),
                                               title=getattr(target, title_field, "")[:200])
                    CaseEvent.objects.create(case=case, event="opened", note="Moved from the earlier report queue")
                elif RANK[pr] < case.priority_rank:
                    case.priority, case.priority_rank = pr, RANK[pr]
                    case.save(update_fields=["priority", "priority_rank"])
            else:
                decision = "removed" if old.status == "actioned" else "kept"
                case = Case.objects.create(target_kind=kind, target_id=target.pk, source="user_report", status="closed", priority=pr,
                                           priority_rank=RANK[pr], due_at=old.created_at + timedelta(hours=HOURS[pr]), decision=decision,
                                           decided_by_id=old.resolved_by_id, decided_at=old.resolved_at, closed_at=old.resolved_at,
                                           internal_note=(old.resolution_note or "")[:1000], title=getattr(target, title_field, "")[:200])
            r = Report.objects.create(reference=_ref(Report), target_kind=kind, target_id=target.pk, case=case, reason=old.reason,
                                      details=old.details, reporter_id=old.reporter_id, contact_email=old.contact_email, ip_hash=old.ip_hash,
                                      status=old.status, resolved_at=old.resolved_at)
            Report.objects.filter(pk=r.pk).update(created_at=old.created_at)
            Case.objects.filter(pk=case.pk).update(opened_at=min(case.opened_at, old.created_at))
    # Count check before the old tables can be dropped (spec 13 §11)
    moved = Report.objects.count()
    expected = sum(M.objects.count() for _, M, _, _ in sources)
    if moved < expected:
        raise RuntimeError(f"Report move incomplete: {moved} of {expected}. The old tables were not dropped.")


class Migration(migrations.Migration):
    dependencies = [
        ("moderation", "0001_initial"),
        ("news", "0001_initial"),
        ("posts", "0001_initial"),
    ]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
