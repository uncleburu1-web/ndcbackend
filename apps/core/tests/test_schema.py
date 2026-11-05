"""Spec 16 §15: constraints, append-only history, singletons, StateField, IP hashes, the data dictionary and check_integrity."""
import io
import re
import uuid
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.apps import apps
from django.conf import settings
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.db.migrations.state import ProjectState
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.models import AuditLog
from apps.audit.services import record
from apps.candidates.models import Candidate
from apps.core import integrity
from apps.core.fields import StateField
from apps.core.hashing import ip_hash
from apps.core.history import HISTORY_TABLES, LATER_TABLES, allow_history_delete
from apps.core.migration_ops import AddConstraintChecked, ConstraintViolated
from apps.core.models import OutboxJob
from apps.core.schema_policy import EXCEPTIONS, JSON_REASONS, check_model
from apps.donations.models import Donation, DonationEvent
from apps.membership.models import Membership, MembershipPayment, MembershipSettings
from apps.moderation.models import Case
from apps.news.models import Article
from apps.posts.models import CandidatePost
from apps.pvc.models import PvcSettings, PvcVerification

NOW = timezone.now()


def mk_user(cid):
    return User.objects.create(clerk_user_id=cid, email=f"{cid}@example.com")


def mk_candidate(slug="c-one"):
    return Candidate.objects.create(slug=slug, full_name="Amina Bello", position="governor", election_year=2027, state="kano",
                                    status="published", verified_at=NOW, published_at=NOW)


def mk_donation(**kw):
    base = dict(reference=f"DON-{uuid.uuid4().hex[:8].upper()}", amount_kobo=500_000, designation="party", donor_name="Ada",
                donor_email="ada@example.com", donor_state="lagos", status="successful", public_label="Ada", paid_at=NOW)
    return Donation.objects.create(**{**base, **kw})


class ConstraintTests(TestCase):
    """Each new rule accepts a good row and refuses a bad one (§6.3)."""

    def refused(self, qs, **bad):
        with self.assertRaises(IntegrityError), transaction.atomic():
            qs.update(**bad)

    def test_money(self):
        d = mk_donation()
        q = Donation.objects.filter(pk=d.pk)
        self.refused(q, paid_at=None)
        self.refused(q, status="refunded", refunded_at=NOW, refunded_kobo=100)  # partial refunds aren't allowed
        q.update(status="refunded", refunded_at=NOW, refunded_kobo=d.amount_kobo)
        self.refused(q, status="nonsense")
        self.refused(q, donor_state="atlantis")
        self.refused(q, donor_state="")
        self.refused(q, election_year=1990)
        m = Membership.objects.create(user=mk_user("u_m"), status="pending_approval", state="kano", full_name="M")
        p = MembershipPayment.objects.create(membership=m, provider="fake", amount_kobo=2_000_000, status="successful",
                                             payer_email="m@example.com", paid_at=NOW)
        self.refused(MembershipPayment.objects.filter(pk=p.pk), paid_at=None)
        self.refused(MembershipPayment.objects.filter(pk=p.pk), status="refunded", refunded_at=None)
        self.refused(Membership.objects.filter(pk=m.pk), status="cancelled")
        Membership.objects.filter(pk=m.pk).update(status="cancelled", cancelled_at=NOW)
        self.refused(Membership.objects.filter(pk=m.pk), state="atlantis")

    def test_content_and_cases(self):
        a = Article.objects.create(source="official", status="successful", category="party", title="T", body="B", published_at=NOW)
        q = Article.objects.filter(pk=a.pk)
        self.refused(q, published_at=None)
        self.refused(q, status="removed")
        q.update(status="removed", removed_at=NOW)
        self.refused(q, state="nowhere")
        post = CandidatePost.objects.create(candidate=mk_candidate(), kind="news", title="T", body="B", published_at=NOW)
        pq = CandidatePost.objects.filter(pk=post.pk)
        self.refused(pq, status="removed")
        self.refused(pq, status="deleted")
        pq.update(status="deleted", deleted_at=NOW)
        case = Case.objects.create(target_kind="article", target_id=a.pk, due_at=NOW)
        self.refused(Case.objects.filter(pk=case.pk), status="closed")
        self.refused(Case.objects.filter(pk=case.pk), state="xx")

    def test_pvc_candidates_outbox(self):
        v = PvcVerification.objects.create(user=mk_user("u_p"), vin_last4="0123", surname="Obi", state="lagos", lga="ikeja",
                                           status="pending", consent_at=NOW)
        q = PvcVerification.objects.filter(pk=v.pk)
        self.refused(q, status="successful")
        q.update(status="successful", reviewed_at=NOW)
        self.refused(q, state="")
        c = mk_candidate()
        self.refused(Candidate.objects.filter(pk=c.pk), election_year=2019)
        self.refused(Candidate.objects.filter(pk=c.pk), status="gone")
        self.refused(Candidate.objects.filter(pk=c.pk), state="mars")
        job = OutboxJob.objects.create(kind="x", next_attempt_at=NOW)
        self.refused(OutboxJob.objects.filter(pk=job.pk), status="done")
        OutboxJob.objects.filter(pk=job.pk).update(status="done", finished_at=NOW)
        self.refused(User.objects.filter(pk=mk_user("u_s").pk), home_state="wakanda")

    def test_singletons(self):
        MembershipSettings.objects.get_or_create(pk=1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            MembershipSettings.objects.create(pk=2)
        PvcSettings.objects.get_or_create(pk=1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            PvcSettings.objects.create(pk=2)


class CountCheckTests(TestCase):
    """§10.4: the migration counts bad rows first and stops naming the rule; otherwise it adds NOT VALID then validates."""

    def test_stops_then_adds(self):
        c = next(x for x in PvcVerification._meta.constraints if x.name == "pvc_pvcverification_review_shape")
        with connection.cursor() as cur:
            cur.execute("SET CONSTRAINTS ALL IMMEDIATE")
        with connection.schema_editor() as ed:
            ed.remove_constraint(PvcVerification, c)
        bad = PvcVerification.objects.create(user=mk_user("u_c"), vin_last4="1", surname="S", state="lagos", lga="x",
                                             status="successful", consent_at=NOW)
        with connection.cursor() as cur:
            cur.execute("SET CONSTRAINTS ALL IMMEDIATE")
        state = ProjectState.from_apps(apps)
        op = AddConstraintChecked("pvcverification", c)
        with self.assertRaises(ConstraintViolated) as e, connection.schema_editor() as ed:
            op.database_forwards("pvc", ed, state, state)
        self.assertIn("pvc_pvcverification_review_shape: 1 existing row", str(e.exception))
        self.assertIn("check_integrity --constraint", str(e.exception))
        self.assertNotIn(str(bad.pk), str(e.exception))  # counts only
        PvcVerification.objects.filter(pk=bad.pk).update(reviewed_at=NOW)
        with connection.schema_editor() as ed:
            op.database_forwards("pvc", ed, state, state)
        with connection.cursor() as cur:
            cur.execute("SELECT convalidated FROM pg_constraint WHERE conname = %s", [c.name])
            self.assertEqual(cur.fetchone(), (True,))


class HistoryTests(TestCase):
    """§7 F3: event and audit rows can't be changed; they are deleted only by retention jobs."""

    def test_every_history_table_has_the_guard(self):
        with connection.cursor() as cur:
            cur.execute("SELECT c.relname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid WHERE t.tgname = 'ndc_append_only'")
            self.assertEqual({r[0] for r in cur.fetchall()}, set(HISTORY_TABLES) | set(LATER_TABLES))

    def test_update_refused_delete_only_in_prune(self):
        d = mk_donation()
        ev = DonationEvent.objects.create(donation=d, source="system", event="paid")
        row = record(AuditLog.Action.USER_VIEWED, actor_label="system:test")
        for qs, change in ((AuditLog.objects.filter(pk=row.pk), {"action": "user.updated"}),
                           (DonationEvent.objects.filter(pk=ev.pk), {"event": "refunded"})):
            with self.assertRaisesRegex(IntegrityError, "append-only"), transaction.atomic():
                qs.update(**change)
            with self.assertRaisesRegex(IntegrityError, "append-only"), transaction.atomic():
                qs.delete()
        with allow_history_delete():
            AuditLog.objects.filter(pk=row.pk).delete()
        self.assertFalse(AuditLog.objects.filter(pk=row.pk).exists())
        with connection.cursor() as cur:  # the switch doesn't outlive the block
            cur.execute("SELECT coalesce(current_setting('ndc.allow_prune', true), '')")
            self.assertNotEqual(cur.fetchone()[0], "on")

    def test_chain_step_still_allowed_and_verifies(self):
        from apps.audit.services import verify_chain

        for i in range(3):
            record(AuditLog.Action.USER_VIEWED, actor_label="system:test", n=i)
        self.assertFalse(AuditLog.objects.filter(seq__isnull=True).exists())
        self.assertTrue(verify_chain()["ok"])

    def test_tamper_helper_is_tests_only(self):
        root = Path(settings.BASE_DIR) / "apps"
        users = [p for p in root.rglob("*.py") if "tests" not in p.parts and p.name != "history.py" and "tamper(" in p.read_text(encoding="utf-8")]
        self.assertEqual(users, [])


class StateFieldTests(TestCase):
    def test_every_state_column_is_the_same(self):
        from apps.candidates.reference import State

        found = []
        for m in apps.get_models():
            for f in m._meta.concrete_fields:
                if (f.name in {"state", "home_state", "donor_state"} and not f.is_relation) or isinstance(f, StateField):
                    found.append(f"{m._meta.label}.{f.name}")
                    self.assertIsInstance(f, StateField, f)
                    self.assertEqual((f.max_length, list(f.choices)), (20, list(State.choices)), f)
        self.assertGreaterEqual(len(found), 9)


class IpHashTests(TestCase):
    def test_one_format_and_key(self):
        from apps.donations import services as donations
        from apps.moderation import services as moderation

        h = ip_hash("10.0.0.1")
        self.assertRegex(h, r"^[0-9a-f]{64}$")
        self.assertEqual(donations.ip_hash("10.0.0.1"), h)
        self.assertEqual(moderation._ip_hash("10.0.0.1"), h)
        with override_settings(IP_HASH_KEY="k1", SECRET_KEY="a" * 50):
            one = ip_hash("10.0.0.1")
        with override_settings(IP_HASH_KEY="k1", SECRET_KEY="b" * 50):
            self.assertEqual(ip_hash("10.0.0.1"), one)  # rotating SECRET_KEY doesn't break abuse limits
        with override_settings(IP_HASH_KEY="k2"):
            self.assertNotEqual(ip_hash("10.0.0.1"), one)


class DictionaryTests(TestCase):
    def test_committed_files_are_current(self):
        call_command("schema_doc", "--check", stdout=io.StringIO())

    def test_policy_failures(self):
        self.assertEqual(check_model(OutboxJob), [])
        with mock.patch.dict(JSON_REASONS, clear=False):
            del JSON_REASONS["core.OutboxJob.payload"]
            self.assertIn("core.OutboxJob.payload: JSON field without a reason in JSON_REASONS", check_model(OutboxJob))
        from apps.moderation.models import Appeal

        with mock.patch.dict(EXCEPTIONS, clear=False):
            del EXCEPTIONS["moderation.Appeal.case"]
            self.assertTrue(any(p.startswith("moderation.Appeal.case: on_delete=CASCADE") for p in check_model(Appeal)))
        with mock.patch.object(Candidate, "__doc__", ""):
            self.assertIn("candidates.Candidate: no docstring", check_model(Candidate))

    def test_every_model_passes(self):
        problems = [p for m in apps.get_models() if m.__module__.startswith("apps.") for p in check_model(m)]
        self.assertEqual(problems, [])


class IntegrityTests(TestCase):
    def test_counts_planted_problems_only(self):
        from apps.media.models import MediaAsset

        before = integrity.run()["counts"]
        Case.objects.create(target_kind="article", target_id=uuid.uuid4(), due_at=NOW)
        Case.objects.create(target_kind="article", target_id=uuid.uuid4(), due_at=NOW, status="closed", closed_at=NOW)  # item pruned: fine
        a = MediaAsset.objects.create(provider="local", public_id="x", purpose="news_image")
        MediaAsset.objects.filter(pk=a.pk).update(created_at=NOW - timedelta(days=2))
        u = mk_user("u_pw")
        u.set_password("not-used-here")
        u.save()
        result = integrity.run()
        after = result["counts"]
        for key in ("orphan_case_targets", "assets_unlinked", "usable_passwords"):
            self.assertEqual(after[key], before[key] + 1, key)
        self.assertTrue(all(isinstance(v, int) for v in after.values()))
        self.assertEqual(integrity.last()["counts"], after)
        out = io.StringIO()
        call_command("check_integrity", stdout=out)
        self.assertNotIn(str(a.pk), out.getvalue())
        self.assertNotIn("u_pw", out.getvalue())

    def test_constraint_option_lists_ids(self):
        out = io.StringIO()
        call_command("check_integrity", "--constraint", "pvc_pvcverification_review_shape", stdout=out)
        self.assertRegex(out.getvalue(), re.compile(r"0 row", re.I))


class FrozenMigrationTests(TestCase):
    def test_trigger_migration_matches_history_module(self):
        import importlib

        from apps.core import history

        mig = importlib.import_module("apps.core.migrations.0005_append_only_triggers")
        self.assertEqual((mig.TABLES, mig.FUNCTION_SQL), (history.HISTORY_TABLES, history.FUNCTION_SQL))
