"""Demo donations (seed_demo_donations): exact reconciliation, idempotency, never counted as real, never in production."""
import io
import random
from contextlib import contextmanager

from django.core.management import CommandError, call_command
from django.db import IntegrityError, connection, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from apps.donations.management.commands.seed_demo_donations import (
    EMAIL_DOMAINS,
    MAX_NAIRA,
    MIN_NAIRA,
    amounts,
)
from apps.donations.models import Designation, DisplayChoice, Donation, DonationStatus
from apps.donations.services import committed_kobo


@contextmanager
def local_db(host="localhost"):
    old = connection.settings_dict["HOST"]
    connection.settings_dict["HOST"] = host
    try:
        yield
    finally:
        connection.settings_dict["HOST"] = old


def seed(*args):
    out = io.StringIO()
    call_command("seed_demo_donations", *args, stdout=out)
    return out.getvalue()


def real_donation(**kw):
    from django.utils import timezone

    data = {"provider": "paystack", "amount_kobo": 1_000_000, "designation": Designation.PARTY, "donor_name": "Real Person",
                "donor_email": "real@example.com", "donor_phone": "+2348011111111", "donor_state": "lagos", "donor_address": "1 Road",
                "display_choice": DisplayChoice.NAME, "public_label": "Real P.", "status": DonationStatus.SUCCESSFUL, "paid_at": timezone.now()}
    data.update(kw)
    return Donation.objects.create(**data)


class AmountTests(TestCase):
    def test_exact_total_within_limits(self):
        out = amounts(1500, 557_000_000, random.Random(557))
        self.assertEqual((len(out), sum(out)), (1500, 557_000_000))
        self.assertTrue(all(MIN_NAIRA <= a <= MAX_NAIRA for a in out))
        self.assertGreater(len(set(out)), 500)  # varied

    def test_impossible_totals_refused(self):
        with self.assertRaises(CommandError):
            amounts(10, 100_000_000, random.Random(1))  # would need ₦10m each
        with self.assertRaises(CommandError):
            amounts(10, 4_000, random.Random(1))  # under ₦500 each


class GuardTests(TestCase):
    def test_refuses_without_debug(self):  # production runs with DEBUG off
        with self.assertRaises(CommandError):
            seed("--count", "5", "--total", "50000")
        self.assertFalse(Donation.objects.exists())

    @override_settings(DEBUG=True)
    def test_refuses_a_remote_database(self):
        with local_db("hayabusa.proxy.rlwy.net"), self.assertRaises(CommandError):
            seed("--count", "5", "--total", "50000")
        self.assertFalse(Donation.objects.exists())


@override_settings(DEBUG=True)
class SeedTests(TestCase):
    def test_full_spec_run_reconciles_exactly(self):
        real = real_donation()
        with local_db():
            out = seed("--count", "1500", "--total", "557000000")
        demo = Donation.objects.filter(is_demo=True)
        self.assertEqual(demo.count(), 1500)
        self.assertEqual(sum(demo.values_list("amount_kobo", flat=True)), 55_700_000_000)
        self.assertIn("55,700,000,000 kobo", out)
        self.assertEqual(len(set(demo.values_list("reference", flat=True))), 1500)
        self.assertTrue(all(r.startswith("NDC-DEMO-") for r in demo.values_list("reference", flat=True)))
        self.assertEqual(set(demo.values_list("provider", flat=True)), {"demo"})
        self.assertEqual(set(demo.values_list("status", flat=True)), {"successful"})
        self.assertTrue(all(e.split("@")[1] in EMAIL_DOMAINS for e in demo.values_list("donor_email", flat=True)))
        self.assertTrue(all(p.startswith("+23480000") and len(p) == 14 for p in demo.values_list("donor_phone", flat=True)))
        self.assertTrue(all("(demo" in a for a in demo.values_list("donor_address", flat=True)))
        self.assertGreater(len(set(demo.values_list("donor_state", flat=True))), 20)
        self.assertEqual(set(demo.values_list("display_choice", flat=True)), {"anonymous", "name"})
        self.assertFalse(demo.filter(events__isnull=False).exists())  # no provider events, webhooks or receipts
        real.refresh_from_db()
        self.assertFalse(real.is_demo)

    def test_idempotent_and_replace_and_remove(self):
        with local_db():
            seed("--count", "40", "--total", "400000")
            self.assertIn("Already seeded", seed("--count", "40", "--total", "400000"))
            self.assertEqual(Donation.objects.count(), 40)
            with self.assertRaises(CommandError):
                seed("--count", "30", "--total", "300000")
            seed("--count", "30", "--total", "300000", "--replace")
            self.assertEqual(Donation.objects.filter(is_demo=True).count(), 30)
            real = real_donation()
            seed("--remove")
        self.assertEqual(list(Donation.objects.all()), [real])

    def test_period_is_respected(self):
        with local_db():
            seed("--count", "50", "--total", "500000", "--start", "2026-07-01", "--end", "2026-07-31")
        days = {d.date() for d in Donation.objects.values_list("paid_at", flat=True)}
        self.assertTrue(all("2026-06-30" <= str(d) <= "2026-07-31" for d in days))

    def test_uses_published_candidates(self):
        from apps.candidates.models import Candidate, CandidateStatus
        from apps.candidates.services import create_candidate, publish, verify
        from apps.candidates.tests.test_candidates import mk_admin

        admin = mk_admin()
        c = create_candidate(data={"full_name": "Pub Lic", "position": "governor", "election_year": 2027, "state": "oyo"}, actor=admin)
        verify(candidate=c, notes="ok", actor=admin)
        publish(candidate=Candidate.objects.get(pk=c.pk), actor=admin)
        create_candidate(data={"full_name": "Dra Ft", "position": "governor", "election_year": 2027, "state": "ogun"}, actor=admin)
        with local_db():
            seed("--count", "100", "--total", "1000000", "--candidate-share", "0.5")
        given = Donation.objects.filter(designation="candidate")
        self.assertTrue(given.exists())
        self.assertEqual(set(given.values_list("candidate__status", flat=True)), {CandidateStatus.PUBLISHED})


class DemoMarkingTests(TestCase):
    def test_database_requires_demo_reference_and_provider(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            real_donation(is_demo=True)  # an ordinary reference

    def test_demo_never_counts_toward_donor_limits(self):
        real_donation(is_demo=True, reference="NDC-DEMO-00001", provider="demo", donor_email="x@example.com")
        self.assertEqual(committed_kobo(user=None, email="x@example.com", phone="+2348000000001", designation="party",
                                        candidate=None, election_year=None, year=2026), 0)


@override_settings(DEBUG=True)
class FinanceApiTests(TestCase):
    def setUp(self):
        self.real = real_donation(amount_kobo=2_500_000)
        with local_db():
            seed("--count", "60", "--total", "600000")

    def test_public_summary_keeps_demo_separate(self):
        body = APIClient().get(reverse("finance-summary")).json()
        self.assertEqual(body["overall"], {"total_kobo": 2_500_000, "count": 1})
        self.assertEqual(body["demo"]["overall"], {"total_kobo": 60_000_000, "count": 60})

    def test_public_summary_has_no_demo_block_without_demo_rows(self):
        Donation.objects.filter(is_demo=True).delete()
        self.assertIsNone(APIClient().get(reverse("finance-summary")).json()["demo"])

    def test_public_list_marks_each_row(self):
        rows = APIClient().get(reverse("finance-donations"), {"page_size": 100}).json()["results"]
        self.assertTrue(any(r["is_demo"] for r in rows))
        self.assertTrue(all(set(r) == {"public_label", "amount_kobo", "designation", "candidate", "paid_on", "is_demo"} for r in rows))

    def test_admin_totals_and_export_exclude_demo(self):
        from apps.access.models import Role
        from apps.access.services import grant_role
        from apps.accounts.models import User

        u = User.objects.create_user("fin", email="fin@example.com", full_name="Fin")
        grant_role(user=u, role=Role.FINANCE_OFFICER)
        c = APIClient()
        c.force_authenticate(u)
        body = c.get(reverse("admin-donations")).json()
        self.assertEqual(body["totals"]["successful"], {"total_kobo": 2_500_000, "count": 1})
        self.assertEqual(body["totals"]["demo"]["count"], 60)
        csv = b"".join(c.get(reverse("admin-donations-export")).streaming_content).decode()
        self.assertNotIn("NDC-DEMO-", csv)
        self.assertIn(self.real.reference, csv)

    def test_admin_dashboard_excludes_demo(self):
        from django.utils import timezone

        from apps.access.models import Role
        from apps.access.services import grant_role
        from apps.accounts.models import User
        from apps.donations.dashboard import admin_summary

        u = User.objects.create_user("fin2", email="fin2@example.com", full_name="Fin")
        grant_role(user=u, role=Role.FINANCE_OFFICER)
        amount = next(x for x in admin_summary(u, now=timezone.now()) if getattr(x, "label", "") == "Given (30 days)")
        self.assertEqual(amount.total, 2_500_000)  # only the real donation

    def test_recheck_refuses_demo(self):
        from apps.core.exceptions import ServiceError
        from apps.donations.services import recheck

        with self.assertRaises(ServiceError):
            recheck(Donation.objects.filter(is_demo=True).first(), actor=None)
