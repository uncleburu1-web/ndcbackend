"""Spec 05 §12 — backend tests for donations and public finance."""
import hashlib
import hmac
import json
from datetime import timedelta
from unittest import mock

from django.core.cache import caches
from django.core.exceptions import ImproperlyConfigured
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from apps.access.models import Role
from apps.access.services import grant_role
from apps.accounts.models import User
from apps.accounts.tests.helpers import signed_webhook
from apps.audit.models import AuditLog
from apps.candidates import services as cand
from apps.candidates.models import Candidate
from apps.donations import services
from apps.donations.models import Donation, DonationEvent, DonationStatus as S
from apps.donations.providers import ProviderError
from apps.donations.providers.fake import SIGNATURE_HEADER, FakeProvider, bank_get, sign
from apps.donations.providers.paystack import PaystackProvider
from apps.donations.views import csv_safe

OPEN = dict(DONATIONS_ENABLED=True, DONATIONS_COMPLIANCE_APPROVED=True, PAYMENT_PROVIDER="fake")
PRIVATE = {"donor_email", "donor_phone", "donor_address", "donor_state", "declarations", "provider_reference", "review_reason",
           "initiated_ip_hash", "id", "status", "channel"}


def mk_admin(clerk_id="user_admin"):
    u = User.objects.create_user(clerk_id, email=f"{clerk_id}@example.com", full_name="Admin Person")
    grant_role(user=u, role=Role.SYSTEM_ADMIN)
    return u


def mk_candidate(admin, name="Amina Bello", publish=True):
    c = Candidate(full_name=name, position="senator", election_year=2027, state="kano", area_name="Kano Central")
    c.full_clean(exclude=["slug"])
    c.assign_slug()
    c.save()
    if publish:
        cand.verify(candidate=c, notes="ok", actor=admin)
        c = cand.publish(candidate=c, actor=admin)
    return c


def payload(**kw):
    base = {
        "amount_kobo": 5_000_00, "designation": "party", "donor_name": "Ada Obi", "donor_email": "ada@example.com",
        "donor_phone": "0803 123 4567", "donor_state": "lagos", "donor_address": "12 Allen Avenue, Ikeja",
        "declarations": {"own_funds": True, "nigerian_source": True, "voluntary": True},
    }
    base.update(kw)
    return base


def webhook(client, event, reference, *, signature=None, provider="fake"):
    body = FakeProvider.webhook_body(event, reference)
    return client.post(reverse("payment-webhook", kwargs={"provider": provider}), data=body, content_type="application/json",
                       **{"HTTP_X_FAKE_SIGNATURE": signature if signature is not None else sign(body)})


class Base(APITestCase):
    def setUp(self):
        caches["fake_payments"].clear()
        self.admin = mk_admin()
        self.c = mk_candidate(self.admin)

    def start(self, client=None, **kw):
        return (client or self.client).post(reverse("donations"), payload(**kw), format="json")

    def pay(self, reference, outcome="success", **settle):
        FakeProvider.settle(reference, outcome, **settle)
        return webhook(self.client, "charge.success" if outcome == "success" else "charge.failed", reference)


# ======================================================================= gate
class GateTests(Base):
    def test_closed_by_default(self):
        self.assertFalse(services.donations_open())
        r = self.client.get(reverse("donation-config"))
        self.assertEqual(r.data["open"], False)
        self.assertEqual(r.data["presets_kobo"], [])
        self.assertIn("public", r["Cache-Control"])
        r = self.start()
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.data["error"]["code"], "donations_unavailable")
        self.assertEqual(Donation.objects.count(), 0)

    def test_every_condition_required(self):
        for missing in ("DONATIONS_ENABLED", "DONATIONS_COMPLIANCE_APPROVED"):
            with override_settings(**{**OPEN, missing: False}):
                self.assertFalse(services.donations_open(), missing)
        with override_settings(**{**OPEN, "PAYMENT_PROVIDER": "paystack", "PAYSTACK_SECRET_KEY": ""}):
            self.assertFalse(services.donations_open(), "paystack without a key")
        with override_settings(**{**OPEN, "PAYMENT_PROVIDER": "nonsense"}):
            self.assertFalse(services.donations_open())
        with override_settings(**{**OPEN, "DONATION_MIN_KOBO": 10_000_00, "DONATION_MAX_PER_PAYMENT_KOBO": 5_000_00}):
            self.assertFalse(services.donations_open(), "min above max")
        with override_settings(**OPEN):
            self.assertTrue(services.donations_open())
            self.assertEqual(self.client.get(reverse("donation-config")).data["presets_kobo"], [1_000_00, 5_000_00, 10_000_00, 50_000_00])

    def test_webhooks_still_settle_while_closed(self):
        with override_settings(**OPEN):
            ref = self.start().data["reference"]
        self.assertFalse(services.donations_open())
        self.pay(ref)
        self.assertEqual(Donation.objects.get(reference=ref).status, S.SUCCESSFUL)


# ================================================================= validation
@override_settings(**OPEN)
class ValidationTests(Base):
    def test_amount_bounds(self):
        for amount in (100, 499_99, 5_000_000_01):
            r = self.start(amount_kobo=amount)
            self.assertEqual(r.status_code, 400, amount)
            self.assertEqual(r.data["error"]["code"], "amount_out_of_range")
        self.assertEqual(self.start(amount_kobo=500_00).status_code, 201)

    def test_candidate_must_be_published(self):
        draft = mk_candidate(self.admin, "Draft Person", publish=False)
        r = self.start(designation="candidate", candidate_slug=draft.slug)
        self.assertEqual(r.data["error"]["code"], "candidate_unavailable")
        r = self.start(designation="candidate")
        self.assertIn("candidate_slug", r.data["error"]["fields"])
        r = self.start(designation="candidate", candidate_slug=self.c.slug)
        self.assertEqual(r.status_code, 201)
        d = Donation.objects.get(reference=r.data["reference"])
        self.assertEqual((d.candidate, d.election_year), (self.c, 2027))

    def test_donor_fields_and_declarations_required(self):
        r = self.client.post(reverse("donations"), {"amount_kobo": 5_000_00, "designation": "party", "declarations": {}}, format="json")
        self.assertEqual(r.status_code, 400)
        for f in ("donor_name", "donor_email", "donor_phone", "donor_state", "donor_address", "declarations"):
            self.assertIn(f, r.data["error"]["fields"], f)
        r = self.start(declarations={"own_funds": True, "nigerian_source": False, "voluntary": True})
        self.assertEqual(r.status_code, 400)
        self.assertIn("nigerian_source", str(r.data["error"]["fields"]))
        self.assertEqual(self.start(donor_phone="12345").status_code, 400)

    def test_guest_vs_signed_in_display_choice(self):
        r = self.start(display_choice="name")  # a guest can't be named
        self.assertEqual(Donation.objects.get(reference=r.data["reference"]).display_choice, "guest")
        u = User.objects.create_user("user_ada", email="ada@example.com", full_name="Ada Obi")
        self.client.force_authenticate(u)
        r = self.start(donor_email="ada2@example.com")
        d = Donation.objects.get(reference=r.data["reference"])
        self.assertEqual((d.display_choice, d.donor_user), ("name", u))
        r = self.start(donor_email="ada3@example.com", display_choice="anonymous")
        self.assertEqual(Donation.objects.get(reference=r.data["reference"]).display_choice, "anonymous")

    def test_stored_cleanly(self):
        r = self.start(donor_email="  Ada@Example.COM ")
        self.assertEqual(r.status_code, 201)
        self.assertTrue(r.data["checkout_url"].endswith(f"/api/v1/dev/fake-checkout/{r.data['reference']}/"))
        self.assertIn("no-store", r["Cache-Control"])
        d = Donation.objects.get(reference=r.data["reference"])
        self.assertEqual((d.donor_email, d.donor_phone, d.status, d.currency), ("ada@example.com", "+2348031234567", S.PENDING, "NGN"))
        self.assertTrue(d.reference.startswith("NDC-") and len(d.reference) == 24)
        self.assertTrue(d.initiated_ip_hash and "127.0.0.1" not in d.initiated_ip_hash)
        self.assertTrue(all(d.declarations[k] for k in ("own_funds", "nigerian_source", "voluntary")))

    def test_provider_down_fails_cleanly(self):
        with mock.patch.object(FakeProvider, "initialize", side_effect=ProviderError("down")):
            r = self.start()
        self.assertEqual((r.status_code, r.data["error"]["code"]), (502, "payment_provider_unavailable"))
        self.assertIn("No money was taken", r.data["error"]["message"])
        self.assertEqual(Donation.objects.get().status, S.FAILED)


# ===================================================================== limits
@override_settings(**OPEN, DONATION_CANDIDATE_DONOR_CAP_KOBO=10_000_00, DONATION_PARTY_DONOR_CAP_KOBO=7_000_00)
class LimitTests(Base):
    def test_candidate_cap_counts_pending_and_matches_email_phone_user(self):
        cand_kw = {"designation": "candidate", "candidate_slug": self.c.slug}
        self.assertEqual(self.start(amount_kobo=6_000_00, **cand_kw).status_code, 201)  # pending counts
        r = self.start(amount_kobo=6_000_00, **cand_kw)
        self.assertEqual((r.status_code, r.data["error"]["code"]), (400, "over_limit"))
        self.assertIn("₦4,000", r.data["error"]["message"])
        # same phone, different email → same donor
        r = self.start(amount_kobo=6_000_00, donor_email="other@example.com", **cand_kw)
        self.assertEqual(r.data["error"]["code"], "over_limit")
        # same account, different email and phone → same donor
        u = User.objects.create_user("user_x", email="x@example.com")
        self.client.force_authenticate(u)
        self.start(amount_kobo=6_000_00, donor_email="x@example.com", donor_phone="08099999999", **cand_kw)
        r = self.start(amount_kobo=6_000_00, donor_email="y@example.com", donor_phone="08088888888", **cand_kw)
        self.assertEqual(r.data["error"]["code"], "over_limit")

    def test_party_cap_is_separate(self):
        self.start(amount_kobo=6_000_00, designation="candidate", candidate_slug=self.c.slug)
        self.assertEqual(self.start(amount_kobo=6_000_00).status_code, 201)
        self.assertEqual(self.start(amount_kobo=2_000_00).data["error"]["code"], "over_limit")

    def test_failed_and_abandoned_dont_count(self):
        ref = self.start(amount_kobo=6_000_00, designation="candidate", candidate_slug=self.c.slug).data["reference"]
        self.pay(ref, "failed")
        self.assertEqual(self.start(amount_kobo=9_000_00, designation="candidate", candidate_slug=self.c.slug).status_code, 201)

    def test_race_over_the_cap_goes_to_review(self):
        # Two payments that each passed the start check (simulated) both succeed at the provider.
        a = Donation.objects.create(provider="fake", amount_kobo=6_000_00, designation="candidate", candidate=self.c, election_year=2027,
                                    donor_name="A", donor_email="r@example.com", donor_phone="+2348000000001", donor_state="lagos",
                                    donor_address="x", display_choice="guest", status=S.PENDING)
        b = Donation.objects.create(provider="fake", amount_kobo=6_000_00, designation="candidate", candidate=self.c, election_year=2027,
                                    donor_name="A", donor_email="r@example.com", donor_phone="+2348000000001", donor_state="lagos",
                                    donor_address="x", display_choice="guest", status=S.PENDING)
        for d in (a, b):
            FakeProvider.settle(d.reference, "success", amount_kobo=d.amount_kobo)
            webhook(self.client, "charge.success", d.reference)
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual(a.status, S.SUCCESSFUL)
        self.assertEqual((b.status, b.review_reason), (S.REVIEW, "over_limit"))


# ============================================================== state machine
@override_settings(**OPEN)
class StateMachineTests(Base):
    def test_success_needs_verified_matching_amount(self):
        u = User.objects.create_user("user_ada", email="ada@example.com", full_name="Ada Obi")
        self.client.force_authenticate(u)
        ref = self.start().data["reference"]
        r = self.pay(ref)
        self.assertEqual(r.status_code, 200)
        d = Donation.objects.get(reference=ref)
        self.assertEqual((d.status, d.public_label, d.channel), (S.SUCCESSFUL, "Ada Obi", "card"))
        self.assertIsNotNone(d.paid_at)
        events = list(d.events.values_list("status_to", flat=True))
        self.pay(ref)  # replay
        self.assertEqual(list(Donation.objects.get(reference=ref).events.values_list("status_to", flat=True)), events, "replay is a no-op")

    def test_webhook_payload_alone_never_succeeds(self):
        ref = self.start().data["reference"]
        webhook(self.client, "charge.success", ref)  # signed, but the bank still says pending
        self.assertEqual(Donation.objects.get(reference=ref).status, S.PENDING)

    def test_amount_or_currency_mismatch_goes_to_review(self):
        ref = self.start().data["reference"]
        self.pay(ref, amount_kobo=100_00)
        d = Donation.objects.get(reference=ref)
        self.assertEqual((d.status, d.review_reason), (S.REVIEW, "amount_or_currency_mismatch"))
        ref2 = self.start(donor_email="b@example.com", donor_phone="08011111111").data["reference"]
        self.pay(ref2, currency="USD")
        self.assertEqual(Donation.objects.get(reference=ref2).status, S.REVIEW)

    def test_failure(self):
        ref = self.start().data["reference"]
        self.pay(ref, "failed")
        self.assertEqual(Donation.objects.get(reference=ref).status, S.FAILED)

    def test_partial_then_full_refund(self):
        ref = self.start().data["reference"]
        self.pay(ref)
        from apps.donations.providers.fake import bank_set

        bank_set(ref, refunded_kobo=2_000_00)
        webhook(self.client, "refund.processed", ref)
        d = Donation.objects.get(reference=ref)
        self.assertEqual((d.status, d.refunded_kobo, d.net_kobo), (S.SUCCESSFUL, 2_000_00, 3_000_00))
        bank_set(ref, status="reversed", refunded_kobo=5_000_00)
        webhook(self.client, "refund.processed", ref)
        self.assertEqual(Donation.objects.get(reference=ref).status, S.REFUNDED)

    def test_sweep_settles_pending_and_abandons_old(self):
        ok_ref = self.start().data["reference"]
        stuck_ref = self.start(donor_email="s@example.com", donor_phone="08022222222").data["reference"]
        Donation.objects.filter(reference=ok_ref).update(created_at=timezone.now() - timedelta(minutes=30))
        # 91 days old: past the 24 h abandon point and the 90-day IP-hash retention.
        Donation.objects.filter(reference=stuck_ref).update(created_at=timezone.now() - timedelta(days=91), initiated_ip_hash="abc")
        FakeProvider.settle(ok_ref, "success")  # webhook "lost"
        counts = services.sweep()
        self.assertEqual(Donation.objects.get(reference=ok_ref).status, S.SUCCESSFUL)
        stuck = Donation.objects.get(reference=stuck_ref)
        self.assertEqual(stuck.status, S.ABANDONED)
        self.assertEqual(stuck.initiated_ip_hash, "")
        self.assertEqual(counts["abandoned"], 1)

    def test_events_are_append_only(self):
        ref = self.start().data["reference"]
        ev = Donation.objects.get(reference=ref).events.first()
        with self.assertRaises(RuntimeError):
            ev.save()
        with self.assertRaises(RuntimeError):
            ev.delete()


# =================================================================== webhooks
@override_settings(**OPEN)
class WebhookTests(Base):
    def test_bad_or_missing_signature_changes_nothing(self):
        ref = self.start().data["reference"]
        FakeProvider.settle(ref, "success")
        for sig in ("", "deadbeef"):
            r = webhook(self.client, "charge.success", ref, signature=sig)
            self.assertEqual((r.status_code, json.loads(r.content)["error"]["code"]), (400, "invalid_signature"))
        self.assertEqual(Donation.objects.get(reference=ref).status, S.PENDING)

    def test_unknown_reference_acknowledged(self):
        self.assertEqual(webhook(self.client, "charge.success", "NDC-NOPE").status_code, 200)

    def test_unknown_provider_404(self):
        self.assertEqual(self.client.post(reverse("payment-webhook", kwargs={"provider": "stripe"}), data=b"{}",
                                          content_type="application/json").status_code, 404)

    @override_settings(DEBUG=False, TESTING=False)
    def test_fake_provider_refuses_outside_debug_and_tests(self):
        with self.assertRaises(ImproperlyConfigured):
            FakeProvider()
        self.assertEqual(webhook(self.client, "charge.success", "NDC-X").status_code, 404)
        self.assertEqual(self.client.get(reverse("fake-checkout", kwargs={"reference": "NDC-X"})).status_code, 404)

    def test_fake_checkout_page_runs_the_real_path(self):
        ref = self.start().data["reference"]
        page = self.client.get(reverse("fake-checkout", kwargs={"reference": ref}))
        self.assertContains(page, "Development only")
        r = self.client.post(reverse("fake-checkout", kwargs={"reference": ref}), {"outcome": "success"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r["Location"], f"http://localhost:5173/donate/complete?reference={ref}")
        self.assertEqual(Donation.objects.get(reference=ref).status, S.SUCCESSFUL)


class PaystackAdapterTests(TestCase):
    @override_settings(PAYSTACK_SECRET_KEY="sk_test_unit")
    def test_signature_is_hmac_sha512_of_raw_body(self):
        p = PaystackProvider()
        body = json.dumps({"event": "charge.success", "data": {"reference": "NDC-ABC"}}).encode()
        good = hmac.new(b"sk_test_unit", body, hashlib.sha512).hexdigest()
        ev = p.parse_webhook(body, {"x-paystack-signature": good})
        self.assertEqual((ev.name, ev.reference), ("charge.success", "NDC-ABC"))
        refund = json.dumps({"event": "refund.processed", "data": {"transaction_reference": "NDC-ABC"}}).encode()
        sig = hmac.new(b"sk_test_unit", refund, hashlib.sha512).hexdigest()
        self.assertEqual(p.parse_webhook(refund, {"x-paystack-signature": sig}).reference, "NDC-ABC")
        from apps.donations.providers import InvalidSignature

        for bad in ("", "00", hmac.new(b"other", body, hashlib.sha512).hexdigest()):
            with self.assertRaises(InvalidSignature):
                p.parse_webhook(body, {"x-paystack-signature": bad})

    @override_settings(PAYSTACK_SECRET_KEY="sk_test_unit")
    def test_verify_maps_statuses_and_refunds(self):
        p = PaystackProvider()
        d = Donation(reference="NDC-ABC", amount_kobo=5_000_00)
        calls = []

        def fake_call(method, path, body=None):
            calls.append(path)
            if path.startswith("/transaction/verify/"):
                return {"status": "reversed", "amount": 500000, "currency": "NGN", "channel": "card", "id": 99,
                        "paid_at": "2026-11-01T10:00:00.000Z"}
            return [{"status": "processed", "amount": 500000}, {"status": "failed", "amount": 100}]

        with mock.patch.object(p, "_call", side_effect=fake_call):
            r = p.verify(d)
        self.assertEqual((r.status, r.amount_kobo, r.refunded_kobo, r.provider_reference), ("reversed", 500000, 500000, "99"))
        self.assertIn("/refund?transaction=99", calls)
        with mock.patch.object(p, "_call", return_value={"status": "ongoing", "amount": 500000, "currency": "NGN"}):
            self.assertEqual(p.verify(d).status, "pending")

    @override_settings(PAYSTACK_SECRET_KEY="")
    def test_unconfigured_rejects_every_webhook(self):
        from apps.donations.providers import InvalidSignature

        body = b"{}"
        with self.assertRaises(InvalidSignature):
            PaystackProvider().parse_webhook(body, {"x-paystack-signature": hmac.new(b"", body, hashlib.sha512).hexdigest()})


# ============================================================ public + receipt
@override_settings(**OPEN)
class PublicTests(Base):
    def make_paid(self, *, user=None, display=None, designation="party", amount=5_000_00, email="p@example.com", phone="08033333333"):
        client = APIClient()
        if user:
            client.force_authenticate(user)
        kw = {"amount_kobo": amount, "designation": designation, "donor_email": email, "donor_phone": phone}
        if designation == "candidate":
            kw["candidate_slug"] = self.c.slug
        if display:
            kw["display_choice"] = display
        ref = self.start(client=client, **kw).data["reference"]
        self.pay(ref)
        return Donation.objects.get(reference=ref)

    def test_finance_lists_successful_only_with_labels(self):
        named = User.objects.create_user("user_n", email="n@example.com", full_name="Ngozi Eze")
        anon = User.objects.create_user("user_a", email="a@example.com", full_name="Secret Person")
        self.make_paid(user=named, email="n@example.com", phone="08044444444", designation="candidate")
        self.make_paid(user=anon, display="anonymous", email="a@example.com", phone="08055555555")
        self.make_paid(email="g@example.com", phone="08066666666")
        failed_ref = self.start(donor_email="f@example.com", donor_phone="08077777777").data["reference"]
        self.pay(failed_ref, "failed")
        self.start(donor_email="pending@example.com", donor_phone="08088888888")  # pending
        r = self.client.get(reverse("finance-donations"))
        self.assertIn("public", r["Cache-Control"])
        labels = sorted(x["public_label"] for x in r.data["results"])
        self.assertEqual(labels, ["Anonymous Donor", "Guest", "Ngozi Eze"])
        text = json.dumps(r.data)
        for leak in ("Secret Person", "@example.com", "+234", "Allen Avenue", "fake_", "NDC-"):
            self.assertNotIn(leak, text, leak)
        for row in r.data["results"]:
            self.assertFalse(PRIVATE & set(row), set(row))
            self.assertRegex(row["paid_on"], r"^\d{4}-\d{2}-\d{2}$")
        cand_rows = self.client.get(reverse("finance-donations"), {"candidate": self.c.slug}).data["results"]
        self.assertEqual([x["candidate"]["full_name"] for x in cand_rows], ["Amina Bello"])
        summary = self.client.get(reverse("finance-summary")).data
        self.assertEqual(summary["overall"], {"total_kobo": 15_000_00, "count": 3})
        self.assertEqual(summary["party"]["count"], 2)
        self.assertEqual(summary["candidates"][0]["slug"], self.c.slug)

    def test_net_amount_after_partial_refund_and_refunded_hidden(self):
        d = self.make_paid()
        from apps.donations.providers.fake import bank_set

        bank_set(d.reference, refunded_kobo=1_000_00)
        webhook(self.client, "refund.processed", d.reference)
        self.assertEqual(self.client.get(reverse("finance-donations")).data["results"][0]["amount_kobo"], 4_000_00)
        bank_set(d.reference, status="reversed", refunded_kobo=5_000_00)
        webhook(self.client, "refund.processed", d.reference)
        self.assertEqual(self.client.get(reverse("finance-donations")).data["count"], 0)

    def test_former_candidate_flag(self):
        self.make_paid(designation="candidate")
        cand.unpublish(candidate=self.c, reason="x", actor=self.admin)
        row = self.client.get(reverse("finance-donations")).data["results"][0]
        self.assertEqual(row["candidate"], {"slug": self.c.slug, "full_name": "Amina Bello", "is_public": False})

    def test_receipt_fields_and_reverify(self):
        ref = self.start().data["reference"]
        r = self.client.get(reverse("donation-status", kwargs={"reference": ref}))
        self.assertEqual(r.data["status"], "pending")
        self.assertIn("no-store", r["Cache-Control"])
        self.assertFalse({"donor_email", "donor_phone", "donor_address", "declarations", "donor_state"} & set(r.data))
        FakeProvider.settle(ref, "success")  # webhook lost; reading the receipt re-checks with the provider
        Donation.objects.filter(reference=ref).update(last_verified_at=None)
        self.assertEqual(self.client.get(reverse("donation-status", kwargs={"reference": ref})).data["status"], "successful")
        self.assertEqual(self.client.get(reverse("donation-status", kwargs={"reference": "NDC-NOPE"})).status_code, 404)

    def test_my_donations(self):
        u = User.objects.create_user("user_m", email="m@example.com", full_name="Mo")
        self.make_paid(user=u, email="m@example.com", phone="08012121212")
        self.make_paid(email="other@example.com", phone="08013131313")
        self.assertEqual(self.client.get(reverse("my-donations")).status_code, 401)
        self.client.force_authenticate(u)
        r = self.client.get(reverse("my-donations"))
        self.assertEqual(r.data["count"], 1)
        self.assertIn("private", r["Cache-Control"])


# ========================================================== account deletion
@override_settings(**OPEN)
class AccountDeletionTests(Base):
    def test_labels_anonymised_finance_kept(self):
        u = User.objects.create_user("user_test123", email="del@example.com", full_name="Delete Me")
        client = APIClient()
        client.force_authenticate(u)
        ref = self.start(client=client, donor_email="del@example.com", donor_phone="08014141414").data["reference"]
        self.pay(ref)
        self.assertEqual(Donation.objects.get(reference=ref).public_label, "Delete Me")
        body, headers = signed_webhook("user.deleted", {"id": "user_test123", "deleted": True, "object": "user"})
        self.client.post(reverse("clerk-webhook"), data=body, content_type="application/json", **headers)
        d = Donation.objects.get(reference=ref)
        self.assertEqual((d.public_label, d.donor_user, d.donor_name, d.donor_email), ("Anonymous Donor", None, "Ada Obi", "del@example.com"))
        self.assertTrue(AuditLog.objects.filter(action="donation.label_anonymised").exists())
        self.assertEqual(AuditLog.objects.get(action="user.deleted", target_id=str(u.id)).metadata["donations_anonymised"], 1)
        self.assertNotIn("Delete Me", json.dumps(self.client.get(reverse("finance-donations")).data))


# ================================================================ admin/finance
@override_settings(**OPEN)
class AdminTests(Base):
    def setUp(self):
        super().setUp()
        self.ref = self.start(donor_name="=HYPERLINK(\"x\")").data["reference"]
        self.pay(self.ref)
        self.d = Donation.objects.get(reference=self.ref)
        self.finance = User.objects.create_user("user_fin", email="fin@example.com")
        grant_role(user=self.finance, role=Role.FINANCE_OFFICER)

    def test_access(self):
        urls = [("get", reverse("admin-donations")), ("get", reverse("admin-donation", kwargs={"pk": self.d.pk})),
                ("post", reverse("admin-donation-recheck", kwargs={"pk": self.d.pk})), ("get", reverse("admin-donations-export"))]
        for m, u in urls:
            self.assertEqual(getattr(self.client, m)(u).status_code, 401, u)
        regular = User.objects.create_user("user_r", email="r@example.com")
        linked = User.objects.create_user("user_l", email="l@example.com")
        grant_role(user=linked, role=Role.CANDIDATE, scope_type="candidate", scope_id=self.c.pk)
        mod = User.objects.create_user("user_mod", email="mod@example.com")
        grant_role(user=mod, role=Role.MODERATOR)
        for user in (regular, linked, mod):
            self.client.force_authenticate(user)
            for m, u in urls:
                self.assertEqual(getattr(self.client, m)(u).status_code, 403, f"{user} {u}")
        for user in (self.admin, self.finance):
            self.client.force_authenticate(user)
            r = self.client.get(reverse("admin-donations"))
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.data["totals"]["successful"]["count"], 1)
            self.assertEqual(r.data["results"][0]["donor_email"], "ada@example.com")

    def test_detail_audited_and_has_events(self):
        self.client.force_authenticate(self.finance)
        r = self.client.get(reverse("admin-donation", kwargs={"pk": self.d.pk}))
        self.assertEqual(r.data["donor_address"], "12 Allen Avenue, Ikeja")
        self.assertTrue(any(e["status_to"] == "successful" for e in r.data["events"]))
        self.assertTrue(AuditLog.objects.filter(action="donation.viewed", target_id=str(self.d.pk), actor=self.finance).exists())

    def test_export_audited_and_formula_safe(self):
        self.client.force_authenticate(self.finance)
        r = self.client.get(reverse("admin-donations-export"), {"status": "successful"})
        body = b"".join(r.streaming_content).decode("utf-8")
        self.assertTrue(body.startswith("﻿reference,paid_at,status,amount_naira"))
        self.assertIn("'=HYPERLINK", body)
        self.assertIn("5000.00", body)
        self.assertIn("no-store", r["Cache-Control"])
        entry = AuditLog.objects.get(action="donation.exported")
        self.assertEqual((entry.metadata["rows"], entry.metadata["filters"]), (1, {"status": "successful"}))
        self.assertEqual(csv_safe("+1"), "'+1")
        self.assertEqual(csv_safe("Ada"), "Ada")

    def test_recheck_clears_mismatch_only_via_provider(self):
        ref = self.start(donor_email="m@example.com", donor_phone="08015151515").data["reference"]
        self.pay(ref, amount_kobo=1_00)
        d = Donation.objects.get(reference=ref)
        self.assertEqual(d.status, S.REVIEW)
        self.client.force_authenticate(self.finance)
        self.client.post(reverse("admin-donation-recheck", kwargs={"pk": d.pk}))
        self.assertEqual(Donation.objects.get(pk=d.pk).status, S.REVIEW, "provider still disagrees")
        FakeProvider.settle(ref, "success", amount_kobo=d.amount_kobo)
        r = self.client.post(reverse("admin-donation-recheck", kwargs={"pk": d.pk}))
        self.assertEqual(r.data["status"], "successful")
        self.assertTrue(AuditLog.objects.filter(action="donation.rechecked").exists())

    def test_no_way_to_mark_successful_by_hand(self):
        self.client.force_authenticate(self.admin)
        r = self.client.patch(reverse("admin-donation", kwargs={"pk": self.d.pk}), {"status": "successful"}, format="json")
        self.assertEqual(r.status_code, 405)


class IntegerMoneyTests(TestCase):
    def test_amount_field_is_integer_kobo(self):
        f = Donation._meta.get_field("amount_kobo")
        self.assertEqual(f.get_internal_type(), "BigIntegerField")
        self.assertEqual(bank_get("NDC-NONE"), None)
