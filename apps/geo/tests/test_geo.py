"""Spec 16 §8 and §15: loading INEC's list and matching existing free text to it."""
import io
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.test import TestCase

from apps.accounts.models import User
from apps.geo import loading, matching
from apps.geo.models import Dataset, Lga, PollingUnit, State, Ward
from apps.membership.models import Membership

SAMPLE = (Path(settings.BASE_DIR) / "data" / "geo" / "sample.csv").read_bytes()


class LoadTests(TestCase):
    def test_creates_hierarchy_then_changes_nothing(self):
        r = loading.load(SAMPLE, source="sample")
        self.assertEqual(r.created, {"states": 2, "lgas": 3, "wards": 4, "polling_units": 5})
        self.assertEqual((State.objects.count(), Lga.objects.count(), Ward.objects.count(), PollingUnit.objects.count()), (2, 3, 4, 5))
        self.assertEqual(set(State.objects.values_list("slug", flat=True)), {"lagos", "fct"})
        pu = PollingUnit.objects.get(delimitation="24-07-05-001")
        self.assertEqual((pu.ward.lga.name, pu.ward.lga.state.slug), ("Ikeja", "lagos"))
        ds = Dataset.objects.get()
        self.assertEqual((ds.states, ds.lgas, ds.wards, ds.polling_units), (2, 3, 4, 5))
        again = loading.load(SAMPLE, source="sample")
        self.assertEqual((sum(again.created.values()), again.updated, again.deactivated, again.dataset), (0, 0, 0, None))
        self.assertEqual(Dataset.objects.count(), 1)

    def test_missing_unit_becomes_inactive_and_links_keep_working(self):
        loading.load(SAMPLE, source="sample")
        pu = PollingUnit.objects.get(delimitation="24-07-05-002")
        m = Membership.objects.create(user=User.objects.create(clerk_user_id="u_g", email="g@example.com"), status="pending_approval",
                                      state="lagos", full_name="G", geo_polling_unit=pu)
        newer = b"\n".join(line for line in SAMPLE.splitlines() if b",002,Sample Primary School II" not in line) + b"\n"
        r = loading.load(newer, source="newer")
        self.assertEqual(r.deactivated, 1)
        pu.refresh_from_db()
        self.assertFalse(pu.active)
        m.refresh_from_db()
        self.assertEqual(m.geo_polling_unit_id, pu.pk)
        self.assertEqual(Dataset.objects.count(), 2)

    def test_dry_run_writes_nothing(self):
        r = loading.load(SAMPLE, source="sample", dry_run=True)
        self.assertEqual(r.created["polling_units"], 5)
        self.assertFalse(PollingUnit.objects.exists() or Dataset.objects.exists())

    def test_bad_file_refused(self):
        with self.assertRaises(loading.GeographyFileError):
            loading.load(b"state,lga\nlagos,ikeja\n", source="bad")
        self.assertFalse(State.objects.exists())

    def test_command(self):
        out = io.StringIO()
        path = str(Path(settings.BASE_DIR) / "data" / "geo" / "sample.csv")
        call_command("load_geography", path, "--source", "sample", stdout=out)  # spec 19: preview by default
        self.assertEqual(PollingUnit.objects.count(), 0)
        self.assertIn("Previewed", out.getvalue())
        call_command("load_geography", path, "--source", "sample", "--apply", stdout=out)
        self.assertEqual(PollingUnit.objects.count(), 5)


class MatchTests(TestCase):
    def setUp(self):
        loading.load(SAMPLE, source="sample")
        self.m = Membership.objects.create(user=User.objects.create(clerk_user_id="u_mt", email="mt@example.com"), status="pending_approval",
                                           state="lagos", full_name="M", lga="IKEJA", ward="anifowoshe / ikeja",
                                           polling_unit="Sample Primary School I")
        self.miss = Membership.objects.create(user=User.objects.create(clerk_user_id="u_mx", email="mx@example.com"),
                                              status="pending_approval", state="kano", full_name="X", lga="Ikeja")

    def test_dry_run_reports_and_writes_nothing(self):
        out = matching.match_all(dry_run=True)
        self.assertEqual(out["memberships"], {"checked": 2, "lga": 1, "ward": 1, "polling_unit": 1})
        self.m.refresh_from_db()
        self.assertIsNone(self.m.geo_lga_id)

    def test_links_exact_matches_only(self):
        matching.match_all()
        self.m.refresh_from_db()
        self.miss.refresh_from_db()
        self.assertEqual(self.m.geo_polling_unit.delimitation, "24-07-05-001")
        self.assertEqual(self.m.lga, "IKEJA")  # the member's own words are untouched
        self.assertIsNone(self.miss.geo_lga_id)  # Ikeja isn't in Kano

    def test_command_dry_run(self):
        out = io.StringIO()
        call_command("match_geography", "--dry-run", stdout=out)
        self.assertIn("memberships", out.getvalue())
        self.m.refresh_from_db()
        self.assertIsNone(self.m.geo_lga_id)
