"""Candidate import (data/candidates/ndc_2027.json + import_candidates) — counts, limits, idempotency, safety."""
import copy
import io
import json

from django.conf import settings
from django.core.management import CommandError, call_command
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APITestCase

from apps.audit.models import AuditLog
from apps.candidates import importing
from apps.candidates.models import Candidate
from apps.candidates.models import CandidateStatus as S
from apps.candidates.reference import Position, State
from apps.candidates.services import publish, verify

from .test_candidates import mk_admin

FILE = settings.BASE_DIR / "data" / "candidates" / "ndc_2027.json"
DATA = json.loads(FILE.read_text(encoding="utf-8"))


def mini(**over):
    rec = {"key": "governor-lagos-test-person", "full_name": "Test Person", "position": "governor", "state": "lagos",
           "area_name": "", "biography": "", "campaign_summary": "A summary.", "priorities": [], "links": [],
           "research": {"status": "inec_listed", "confidence": "high", "sources": ["https://example.com/a"]}}
    rec.update(over)
    return {"dataset": "test-2027", "party": "NDC", "election_year": 2027, "checked": "2026-11-10", "candidates": [rec]}


class DataFileTests(TestCase):
    """The shipped file itself."""

    def test_file_passes_checks(self):
        importing.check(DATA)  # raises on any problem

    def test_fifty_records_unique_keys_and_people(self):
        cands = DATA["candidates"]
        self.assertEqual(len(cands), 50)
        self.assertEqual(len({c["key"] for c in cands}), 50)
        self.assertEqual(len({(importing.fold(c["full_name"]), c["position"], c["state"]) for c in cands}), 50)

    def test_choices_years_and_limits(self):
        self.assertEqual(DATA["election_year"], 2027)
        for c in DATA["candidates"]:
            self.assertIn(c["position"], Position.values)
            if c["state"]:
                self.assertIn(c["state"], State.values)
            self.assertLessEqual(len(c["campaign_summary"]), 1000)
            self.assertLessEqual(len(c["biography"]), 5000)
            self.assertLessEqual(len(c["priorities"]), 8)
            self.assertTrue(all(0 < len(p) < 120 for p in c["priorities"]))
            self.assertLessEqual(len(c["links"]), 5)
            for link in c["links"]:
                self.assertTrue(link["url"].startswith("https://"), link)
            self.assertTrue(c["research"]["sources"])

    def test_summaries_are_distinct(self):
        summaries = [c["campaign_summary"] for c in DATA["candidates"] if c["campaign_summary"]]
        self.assertEqual(len(summaries), len(set(summaries)))


class ImportTests(TestCase):
    def test_imports_all_as_unverified_drafts(self):
        out = io.StringIO()
        call_command("import_candidates", stdout=out)
        self.assertIn("50 created", out.getvalue())
        self.assertEqual(Candidate.objects.count(), 50)
        self.assertEqual(Candidate.objects.filter(status=S.DRAFT, verified_at__isnull=True).count(), 50)
        self.assertFalse(Candidate.objects.filter(published_at__isnull=False).exists())
        self.assertEqual(set(Candidate.objects.values_list("election_year", flat=True)), {2027})
        self.assertEqual(set(Candidate.objects.values_list("party", flat=True)), {"NDC"})
        obi = Candidate.objects.get(full_name="Peter Obi")
        self.assertEqual((obi.position, obi.state), ("president", ""))
        self.assertIn("import-key: ndc-2027:president-peter-obi", obi.verification_notes)
        self.assertIn("NOT VERIFIED", obi.verification_notes)
        for c in Candidate.objects.all():
            c.full_clean()  # every row still satisfies the model
        self.assertEqual(AuditLog.objects.filter(action=AuditLog.Action.CANDIDATE_CREATED).count(), 50)

    def test_dry_run_saves_nothing(self):
        out = io.StringIO()
        call_command("import_candidates", "--dry-run", stdout=out)
        self.assertIn("Dry run", out.getvalue())
        self.assertEqual(Candidate.objects.count(), 0)

    def test_second_run_is_a_no_op(self):
        call_command("import_candidates", stdout=io.StringIO())
        ids = set(Candidate.objects.values_list("id", flat=True))
        out = io.StringIO()
        call_command("import_candidates", stdout=out)
        self.assertIn("0 created, 0 updated, 50 unchanged", out.getvalue())
        self.assertEqual(set(Candidate.objects.values_list("id", flat=True)), ids)

    def test_updates_own_unreviewed_draft(self):
        importing.run(mini())
        res = importing.run(mini(campaign_summary="A better summary."))
        self.assertEqual(len(res.updated), 1)
        self.assertEqual(Candidate.objects.get().campaign_summary, "A better summary.")

    def test_never_overwrites_a_verified_record(self):
        admin = mk_admin()
        importing.run(mini())
        c = Candidate.objects.get()
        verify(candidate=c, notes="Checked against INEC list.", actor=admin)
        res = importing.run(mini(campaign_summary="Changed by research."))
        c.refresh_from_db()
        self.assertEqual(c.campaign_summary, "A summary.")
        self.assertEqual(c.verification_notes, "Checked against INEC list.")
        self.assertEqual(len(res.kept_existing) + len(res.kept_reviewed), 1)
        self.assertEqual(Candidate.objects.count(), 1)

    def test_never_overwrites_a_published_record(self):
        admin = mk_admin()
        importing.run(mini())
        c = Candidate.objects.get()
        verify(candidate=c, notes="Checked.", actor=admin)
        publish(candidate=Candidate.objects.get(), actor=admin)
        importing.run(mini(biography="New text"))
        c.refresh_from_db()
        self.assertEqual((c.status, c.biography), (S.PUBLISHED, ""))

    def test_leaves_hand_made_duplicate_alone(self):
        admin = mk_admin()
        from apps.candidates.services import create_candidate
        create_candidate(data={"full_name": "TEST  person", "position": "governor", "election_year": 2027,
                               "state": "lagos", "biography": "Typed by an admin."}, actor=admin)
        res = importing.run(mini())
        self.assertEqual(res.kept_existing, ["Test Person (governor, lagos)"])
        self.assertEqual(Candidate.objects.count(), 1)
        self.assertEqual(Candidate.objects.get().biography, "Typed by an admin.")

    def test_unrelated_records_untouched(self):
        admin = mk_admin()
        from apps.candidates.services import create_candidate
        other = create_candidate(data={"full_name": "Someone Else", "position": "senator", "election_year": 2027,
                                       "state": "kano", "area_name": "Kano South"}, actor=admin)
        before = other.updated_at
        call_command("import_candidates", stdout=io.StringIO())
        other.refresh_from_db()
        self.assertEqual(other.updated_at, before)
        self.assertEqual(Candidate.objects.count(), 51)


class FileCheckTests(TestCase):
    def bad(self, data, needle):
        with self.assertRaises(importing.ImportFileError) as cm:
            importing.run(data)
        self.assertTrue(any(needle in e for e in cm.exception.errors), cm.exception.errors)
        self.assertEqual(Candidate.objects.count(), 0)

    def test_rejects_http_link(self):
        self.bad(mini(links=[{"label": "Site", "url": "http://example.com"}]), "https")

    def test_rejects_too_many_priorities(self):
        self.bad(mini(priorities=[f"Priority {i}" for i in range(9)]), "priorities")

    def test_rejects_long_priority(self):
        self.bad(mini(priorities=["x" * 120]), "priorities")

    def test_rejects_long_summary_and_bio(self):
        self.bad(mini(campaign_summary="x" * 1001), "campaign summary")
        self.bad(mini(biography="x" * 5001), "biography")

    def test_rejects_unknown_position_state_and_year(self):
        self.bad(mini(position="vice_president"), "position")
        self.bad(mini(state="lagoss"), "state")
        d = mini()
        d["election_year"] = 2020
        self.bad(d, "election_year")

    def test_rejects_duplicates(self):
        d = mini()
        d["candidates"].append(copy.deepcopy(d["candidates"][0]))
        self.bad(d, "duplicate key")
        d["candidates"][1]["key"] = "governor-lagos-test-person-2"
        self.bad(d, "same person")

    def test_needs_sources(self):
        self.bad(mini(research={"status": "inec_listed", "confidence": "high", "sources": []}), "source")

    def test_model_rule_failure_rolls_back_whole_run(self):
        d = mini()
        good = copy.deepcopy(d["candidates"][0])
        good.update(key="governor-oyo-ok-person", full_name="Ok Person", state="oyo")
        bad = copy.deepcopy(d["candidates"][0])
        bad.update(key="senator-oyo-no-area", full_name="No Area", position="senator", state="oyo", area_name="")
        d["candidates"] = [good, bad]
        with self.assertRaises(importing.ImportFileError):
            importing.run(d)
        self.assertEqual(Candidate.objects.count(), 0)

    def test_command_reports_errors(self):
        with self.assertRaises(CommandError):
            call_command("import_candidates", "--file", "/nonexistent.json", stdout=io.StringIO())


class ImportedApiTests(APITestCase):
    def setUp(self):
        call_command("import_candidates", stdout=io.StringIO())

    def test_admin_list_shows_drafts(self):
        self.client.force_authenticate(mk_admin())
        res = self.client.get(reverse("admin-candidates"), {"page_size": 100})
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body.get("count", len(body.get("results", body))), 50)

    def test_admin_detail_shows_research_notes(self):
        self.client.force_authenticate(mk_admin())
        c = Candidate.objects.get(full_name="Peter Obi")
        res = self.client.get(reverse("admin-candidate", args=[c.pk]))
        self.assertEqual(res.status_code, 200)
        self.assertIn("Sources:", res.json()["verification_notes"])

    def test_public_list_hides_unverified_drafts(self):
        res = self.client.get(reverse("candidates"))
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body.get("count", len(body.get("results", body))), 0)
        self.assertNotIn("import-key", json.dumps(body))

    def test_published_import_never_leaks_notes(self):
        admin = mk_admin()
        c = Candidate.objects.get(full_name="Peter Obi")
        verify(candidate=c, notes="Checked.", actor=admin)
        publish(candidate=Candidate.objects.get(pk=c.pk), actor=admin)
        res = self.client.get(reverse("candidate-detail", args=[c.slug]))
        self.assertEqual(res.status_code, 200)
        self.assertNotIn("verification_notes", res.json())
