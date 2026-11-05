"""Spec 06 §12 — NDC membership."""
import io
import json
import threading
import zipfile
from datetime import date, timedelta
from pathlib import Path

from django.conf import settings
from django.core.cache import caches
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image
from rest_framework.test import APIClient, APITestCase

from apps.access.models import Role
from apps.access.services import grant_role
from apps.accounts.models import User
from apps.accounts.tests.helpers import signed_webhook
from apps.audit.models import AuditLog
from apps.donations.models import Donation
from apps.follows.models import CandidateFollow
from apps.membership import services
from apps.membership.models import Membership, MembershipPayment, MembershipSettings, MembershipStatus as M
from apps.payments.providers.fake import FakeProvider, bank_set, sign
from apps.pvc import services as pvc
from apps.pvc.models import PvcVerification

OPEN = dict(MEMBERSHIP_ENABLED=True, MEMBERSHIP_DATA_APPROVED=True, PAYMENT_PROVIDER="fake")
NIN = "12345678901"
DETAILS = {"full_name": "Ada Nneka Obi", "sex": "female", "date_of_birth": "1990-05-04", "address": "12 Allen Avenue, Ikeja",
           "state": "lagos", "lga": "Ikeja", "ward": "Ward 03", "polling_unit": "PU 012, Allen Primary School", "nin": NIN,
           "declarations": {"no_other_party": True, "constitution": True, "data_processing": True}}


def mk_user(cid="user_m"):
    return User.objects.create_user(cid, email=f"{cid}@example.com", full_name="Ada Obi", phone_number="+2348031234567",
                                    profile_completed_at=timezone.now(), adult_confirmed_at=timezone.now())


def mk_role(cid, role):
    u = mk_user(cid)
    grant_role(user=u, role=role)
    return u


def photo_bytes(gps=True):
    img = Image.new("RGB", (800, 900), (46, 43, 125))
    out = io.BytesIO()
    exif = Image.Exif()
    if gps:
        exif[0x8825] = {1: "N", 2: (6.0, 27.0, 0.0)}
    img.save(out, format="JPEG", exif=exif)
    return out.getvalue()


def verify_pvc(user, officer):
    rec = pvc.submit(user, vin="90F5B0F8E0295612345", surname="Obi", state="lagos", lga="Ikeja")
    pvc.verify(rec, officer)


def webhook(client, event, reference, signature=None):
    body = FakeProvider.webhook_body(event, reference)
    return client.post(reverse("payment-webhook", kwargs={"provider": "fake"}), data=body, content_type="application/json",
                       HTTP_X_FAKE_SIGNATURE=signature if signature is not None else sign(body))


class Base(APITestCase):
    def setUp(self):
        caches["fake_payments"].clear()
        self.officer = mk_role("user_officer", Role.MEMBERSHIP_OFFICER)
        self.user = mk_user()
        self.client.force_authenticate(self.user)

    def fill(self, **over):
        r = self.client.put(reverse("my-membership"), {**DETAILS, **over}, format="json")
        assert r.status_code == 200, r.data
        up = SimpleUploadedFile("me.jpg", photo_bytes(), content_type="image/jpeg")
        r = self.client.put(reverse("my-membership-photo"), {"photo": up, "consent": "true"}, format="multipart")
        assert r.status_code == 200, r.data
        return r

    def ready(self):
        verify_pvc(self.user, self.officer)
        self.fill()

    def pay(self, outcome="success", **settle):
        r = self.client.post(reverse("my-membership-pay"), {"amount_kobo": 1}, format="json")
        assert r.status_code == 201, r.data
        ref = r.data["reference"]
        FakeProvider.settle(ref, outcome, **settle)
        webhook(self.client, "charge.success" if outcome == "success" else "charge.failed", ref)
        return ref


# ===================================================================== drafts
class DetailsTests(Base):
    def test_config(self):
        c = APIClient().get(reverse("membership-config"))
        self.assertEqual((c.data["open"], c.data["fee_kobo"], c.data["requires_approval"]), (False, 2_000_000, True))
        with override_settings(**OPEN):
            self.assertTrue(APIClient().get(reverse("membership-config")).data["open"])

    def test_profile_must_be_complete_to_apply(self):
        u = User.objects.create_user("user_np", email="np@example.com")  # no name/phone
        c = APIClient()
        c.force_authenticate(u)
        self.assertEqual(c.get(reverse("my-membership")).status_code, 200)
        self.assertEqual(c.put(reverse("my-membership"), DETAILS, format="json").status_code, 403)

    def test_partial_draft_nin_encrypted_and_masked(self):
        r = self.client.put(reverse("my-membership"), {"full_name": "Ada Obi"}, format="json")
        self.assertEqual((r.data["status"], r.data["display_status"]), ("draft", "pvc_required"))
        r = self.client.put(reverse("my-membership"), {"nin": "123 456 789 01"}, format="json")
        self.assertEqual(r.data["nin_masked"], "•••• 8901")
        self.assertNotIn(NIN, json.dumps(r.data, default=str))
        m = Membership.objects.get()
        self.assertNotIn(NIN.encode(), bytes(m.nin_encrypted))
        self.assertEqual(self.client.put(reverse("my-membership"), {"nin": "1234"}, format="json").status_code, 400)

    def test_under_18_refused(self):
        dob = (timezone.localdate() - timedelta(days=17 * 365)).isoformat()
        r = self.client.put(reverse("my-membership"), {"date_of_birth": dob}, format="json")
        self.assertEqual((r.status_code, r.data["error"]["code"]), (400, "under_18"))

    def test_photo_private_and_stripped(self):
        self.fill()
        m = Membership.objects.get()
        path = Path(settings.PRIVATE_MEDIA_ROOT) / m.photo
        self.assertTrue(path.exists())
        self.assertNotIn(str(settings.MEDIA_ROOT), str(path))
        with Image.open(path) as img:
            self.assertEqual((img.format, img.size), ("WEBP", (600, 600)))
            self.assertFalse(img.getexif())
        r = self.client.get(reverse("my-membership-photo"))
        self.assertEqual((r.status_code, r["Content-Type"]), (200, "image/webp"))
        self.assertEqual(APIClient().get(reverse("my-membership-photo")).status_code, 401)
        up = SimpleUploadedFile("me.jpg", photo_bytes(), content_type="image/jpeg")
        self.assertEqual(self.client.put(reverse("my-membership-photo"), {"photo": up}, format="multipart").status_code, 400,
                         "consent required")


# ====================================================================== pay
@override_settings(**OPEN)
class PayTests(Base):
    def test_blocked_until_ready(self):
        with override_settings(MEMBERSHIP_DATA_APPROVED=False):
            self.assertEqual(self.client.post(reverse("my-membership-pay")).data["error"]["code"], "membership_unavailable")
        self.fill()
        r = self.client.post(reverse("my-membership-pay"))
        self.assertEqual((r.status_code, r.data["error"]["code"]), (409, "pvc_not_verified"))
        verify_pvc(self.user, self.officer)
        self.client.put(reverse("my-membership"), {"ward": ""}, format="json")
        r = self.client.post(reverse("my-membership-pay"))
        self.assertEqual((r.status_code, r.data["error"]["code"]), (400, "application_incomplete"))
        self.assertIn("ward", r.data["error"]["fields"])

    def test_amount_from_settings_and_one_open_payment(self):
        self.ready()
        r = self.client.post(reverse("my-membership-pay"), {"amount_kobo": 1}, format="json")
        self.assertEqual(r.status_code, 201)
        self.assertTrue(r.data["reference"].startswith("MEM-"))
        p = MembershipPayment.objects.get()
        self.assertEqual((p.amount_kobo, p.status), (2_000_000, "pending"))
        self.assertEqual(Membership.objects.get().status, M.PAYMENT_PENDING)
        self.assertEqual(self.client.post(reverse("my-membership-pay")).data["error"]["code"], "payment_in_progress")
        self.assertEqual(Donation.objects.count(), 0)

    def test_success_with_approval_on_then_approve(self):
        self.ready()
        ref = self.pay()
        m = Membership.objects.get()
        self.assertEqual((m.status, m.membership_number), (M.PENDING_APPROVAL, None))
        status = APIClient().get(reverse("membership-payment-status", kwargs={"reference": ref})).data
        self.assertEqual((status["status"], status["membership_status"]), ("successful", "pending_approval"))
        self.assertNotIn("Ada", json.dumps(status, default=str))
        self.client.force_authenticate(self.officer)
        r = self.client.post(reverse("admin-membership-approve", kwargs={"pk": m.pk}))
        self.assertEqual(r.data["status"], "active")
        self.assertRegex(r.data["membership_number"], r"^NDC-LA-\d{4}-000001$")
        self.client.force_authenticate(self.user)
        card = self.client.get(reverse("my-membership-card")).data
        self.assertEqual((card["full_name"], card["state"]), ("Ada Nneka Obi", "Lagos"))
        self.assertEqual(self.client.get(reverse("my-membership")).data["display_status"], "active")

    def test_success_with_approval_off_activates(self):
        MembershipSettings.objects.update_or_create(pk=1, defaults={"requires_approval": False})
        self.ready()
        self.pay()
        m = Membership.objects.get()
        self.assertEqual(m.status, M.ACTIVE)
        self.assertTrue(m.membership_number.startswith("NDC-LA-"))

    def test_failure_back_to_draft_and_retry(self):
        self.ready()
        self.pay("failed")
        self.assertEqual(Membership.objects.get().status, M.DRAFT)
        self.assertEqual(self.client.post(reverse("my-membership-pay")).status_code, 201, "can pay again")

    def test_mismatch_goes_to_review_replay_noop(self):
        self.ready()
        ref = self.pay(amount_kobo=1_000_00)
        p = MembershipPayment.objects.get(reference=ref)
        self.assertEqual((p.status, Membership.objects.get().status), ("review", M.PAYMENT_PENDING))
        n = p.events.count()
        webhook(self.client, "charge.success", ref)
        self.assertEqual(MembershipPayment.objects.get(reference=ref).events.count(), n)
        self.assertEqual(webhook(self.client, "charge.success", ref, signature="forged").status_code, 400)

    def test_reject_paid_then_refund(self):
        self.ready()
        ref = self.pay()
        m = Membership.objects.get()
        self.client.force_authenticate(self.officer)
        self.assertEqual(self.client.post(reverse("admin-membership-reject", kwargs={"pk": m.pk}), {"reason": ""}, format="json").status_code, 400)
        r = self.client.post(reverse("admin-membership-reject", kwargs={"pk": m.pk}), {"reason": "Not eligible under party rules."}, format="json")
        self.assertEqual((r.data["status"], r.data["refund_due"]), ("rejected", True))
        bank_set(ref, status="reversed", refunded_kobo=2_000_000)
        webhook(self.client, "refund.processed", ref)
        self.assertEqual(Membership.objects.get().status, M.REFUNDED)
        self.client.force_authenticate(self.user)
        self.assertEqual(self.client.put(reverse("my-membership"), {"ward": "Ward 04"}, format="json").data["status"], "draft",
                         "may re-apply")

    def test_cancel(self):
        MembershipSettings.objects.update_or_create(pk=1, defaults={"requires_approval": False})
        self.ready()
        self.pay()
        self.assertEqual(self.client.post(reverse("my-membership-cancel"), {"reason": ""}, format="json").status_code, 400)
        r = self.client.post(reverse("my-membership-cancel"), {"reason": "Moving abroad"}, format="json")
        self.assertEqual(r.data["display_status"], "cancelled")
        self.assertEqual(self.client.get(reverse("my-membership-card")).status_code, 404)

    def test_sweep_settles_lost_webhook(self):
        self.ready()
        r = self.client.post(reverse("my-membership-pay"))
        MembershipPayment.objects.update(created_at=timezone.now() - timedelta(minutes=30))
        FakeProvider.settle(r.data["reference"], "success")
        services.sweep()
        self.assertEqual(Membership.objects.get().status, M.PENDING_APPROVAL)

    def test_joining_creates_no_follow_or_donation(self):
        MembershipSettings.objects.update_or_create(pk=1, defaults={"requires_approval": False})
        self.ready()
        self.pay()
        self.assertEqual((CandidateFollow.objects.count(), Donation.objects.count()), (0, 0))
        self.assertEqual(self.client.get(reverse("finance-donations")).data["count"], 0)


# ===================================================================== admin
@override_settings(**OPEN)
class AdminTests(Base):
    def setUp(self):
        super().setUp()
        MembershipSettings.objects.update_or_create(pk=1, defaults={"requires_approval": False})
        self.ready()
        self.pay()
        self.m = Membership.objects.get()

    def test_access(self):
        urls = [reverse("admin-membership"), reverse("admin-membership-detail", kwargs={"pk": self.m.pk}),
                reverse("admin-membership-register"), reverse("admin-membership-payments")]
        fin = mk_role("user_fin", Role.FINANCE_OFFICER)
        mod = mk_role("user_mod", Role.MODERATOR)
        for who in (self.user, fin, mod):
            self.client.force_authenticate(who)
            for u in urls:
                self.assertEqual(self.client.get(u).status_code, 403, f"{who} {u}")
        self.assertEqual(APIClient().get(urls[0]).status_code, 401)

    def test_detail_masks_nin_reveal_audited(self):
        self.client.force_authenticate(self.officer)
        r = self.client.get(reverse("admin-membership-detail", kwargs={"pk": self.m.pk}))
        self.assertEqual(r.data["nin_masked"], "•••• 8901")
        self.assertNotIn(NIN, json.dumps(r.data, default=str))
        self.assertEqual(r.data["pvc_status"], "successful")
        r = self.client.post(reverse("admin-membership-reveal-nin", kwargs={"pk": self.m.pk}))
        self.assertEqual(r.data["nin"], NIN)
        self.assertTrue(AuditLog.objects.filter(action="membership.nin_revealed", actor=self.officer).exists())
        ph = self.client.get(reverse("admin-membership-photo", kwargs={"pk": self.m.pk}))
        self.assertEqual(ph.status_code, 200)
        self.assertTrue(AuditLog.objects.filter(action="membership.photo_viewed").exists())

    def test_detail_history_and_list_filters(self):
        self.client.force_authenticate(self.officer)
        self.client.post(reverse("admin-membership-reveal-nin", kwargs={"pk": self.m.pk}))
        d = self.client.get(reverse("admin-membership-detail", kwargs={"pk": self.m.pk})).data
        self.assertEqual([e["status_to"] for e in d["events"]][-1], M.ACTIVE)
        self.assertIn("verified", [e["kind"] for e in d["pvc_history"]])
        self.assertIn("membership.nin_revealed", [a["action"] for a in d["audit"]])
        self.assertTrue(d["payments"][0]["events"])
        rows = self.client.get(reverse("admin-membership")).data["results"]
        self.assertNotIn("audit", rows[0])  # history only on the detail screen
        today = timezone.localdate().isoformat()
        self.assertEqual(self.client.get(reverse("admin-membership"), {"date_from": today}).data["count"], 1)
        self.assertEqual(self.client.get(reverse("admin-membership"), {"date_to": "2000-01-01"}).data["count"], 0)
        self.assertEqual(self.client.get(reverse("admin-membership"), {"date_from": "nope"}).status_code, 400)

    def test_settings_system_admin_only(self):
        self.client.force_authenticate(self.officer)
        self.assertEqual(self.client.patch(reverse("admin-membership-settings"), {"requires_approval": True}, format="json").status_code, 403)
        admin = mk_role("user_sa", Role.SYSTEM_ADMIN)
        self.client.force_authenticate(admin)
        r = self.client.patch(reverse("admin-membership-settings"), {"requires_approval": True}, format="json")
        self.assertTrue(r.data["requires_approval"])
        self.assertTrue(AuditLog.objects.filter(action="membership.settings_changed").exists())

    def test_register_export(self):
        other = mk_user("user_draft")
        c = APIClient()
        c.force_authenticate(other)
        c.put(reverse("my-membership"), {**DETAILS, "full_name": "=HYPERLINK(1)"}, format="json")  # draft: excluded
        self.client.force_authenticate(self.officer)
        r = self.client.get(reverse("admin-membership-register"))
        body = b"".join(r.streaming_content).decode()
        lines = body.strip().splitlines()
        self.assertTrue(lines[0].startswith("﻿full_name,sex,date_of_birth,address,state,lga,ward,polling_unit,nin,photograph"))
        self.assertEqual(len(lines), 2, "active members only")
        self.assertIn(NIN, body)
        self.assertIn(self.m.membership_number, body)
        z = self.client.get(reverse("admin-membership-register-photos"))
        names = zipfile.ZipFile(io.BytesIO(z.content)).namelist()
        self.assertEqual(names, [f"{self.m.membership_number}.webp"])
        self.assertEqual(AuditLog.objects.filter(action="membership.register_exported").count(), 2)
        self.assertEqual(AuditLog.objects.filter(action="membership.register_exported").first().metadata["rows"], 1)

    def test_formula_safe_export(self):
        Membership.objects.filter(pk=self.m.pk).update(full_name="=cmd")
        self.client.force_authenticate(self.officer)
        body = b"".join(self.client.get(reverse("admin-membership-register")).streaming_content).decode()
        self.assertIn("'=cmd", body)

    def test_privacy_on_public_surfaces(self):
        anon = APIClient()
        for url in (reverse("candidates"), reverse("finance-donations"), reverse("membership-config")):
            self.assertNotIn("Ada Nneka", json.dumps(anon.get(url).data, default=str))
        other = mk_user("user_o")
        c = APIClient()
        c.force_authenticate(other)
        self.assertEqual(c.get(reverse("my-membership")).data["display_status"], "not_joined")

    def test_account_deletion_erases_identifiers(self):
        uid = "user_test123"
        u = mk_user(uid)
        c = APIClient()
        c.force_authenticate(u)
        verify_pvc(u, self.officer)
        c.put(reverse("my-membership"), DETAILS, format="json")
        c.put(reverse("my-membership-photo"), {"photo": SimpleUploadedFile("p.jpg", photo_bytes(), "image/jpeg"), "consent": "true"},
              format="multipart")
        photo_path = Path(settings.PRIVATE_MEDIA_ROOT) / Membership.objects.get(user=u).photo
        body, headers = signed_webhook("user.deleted", {"id": uid, "deleted": True, "object": "user"})
        APIClient().post(reverse("clerk-webhook"), data=body, content_type="application/json", **headers)
        m = Membership.objects.get(user=u)
        self.assertEqual((m.status, bytes(m.nin_encrypted), m.nin_last4, m.date_of_birth, m.photo), (M.CANCELLED, b"", "", None, ""))
        self.assertFalse(photo_path.exists())
        self.assertFalse(PvcVerification.objects.filter(user=u).exists())
        self.assertTrue(AuditLog.objects.filter(action="membership.erased_on_account_deletion").exists())


class DisplayStatusTests(Base):
    def test_mapping(self):
        g = lambda: self.client.get(reverse("my-membership")).data["display_status"]  # noqa: E731
        self.assertEqual(g(), "not_joined")
        rec = pvc.submit(self.user, vin="90F5B0F8E0295612345", surname="Obi", state="lagos", lga="Ikeja")
        self.assertEqual(g(), "pvc_pending")
        pvc.reject(rec, self.officer, "Not found")
        self.assertEqual(g(), "pvc_rejected")
        rec = pvc.submit(self.user, vin="90F5B0F8E0295612345", surname="Obi", state="lagos", lga="Ikeja")
        pvc.verify(rec, self.officer)
        self.assertEqual(g(), "pvc_verified")


@override_settings(**OPEN)
class NumberConcurrencyTests(TransactionTestCase):
    def test_numbers_unique_under_concurrency(self):
        officer = mk_role("user_off2", Role.MEMBERSHIP_OFFICER)
        members = []
        for i in range(4):
            u = mk_user(f"user_c{i}")
            m = Membership.objects.create(user=u, status=M.PENDING_APPROVAL, state="kano", full_name=f"M {i}")
            MembershipPayment.objects.create(membership=m, provider="fake", amount_kobo=2_000_000, status="successful",
                                             payer_email="x@example.com", paid_at=timezone.now())
            pvc.verify(pvc.submit(u, vin="90F5B0F8E0295612345", surname="X", state="kano", lga="Fagge"), officer)
            members.append(m)
        barrier = threading.Barrier(4)
        errors = []

        def work(m):
            try:
                barrier.wait()
                services.approve(m, officer=officer)
            except Exception as exc:  # pragma: no cover
                errors.append(exc)
            finally:
                connection.close()

        threads = [threading.Thread(target=work, args=(m,)) for m in members]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(errors, [])
        numbers = sorted(Membership.objects.values_list("membership_number", flat=True))
        year = timezone.localdate().year
        self.assertEqual(numbers, [f"NDC-KN-{year}-{i:06d}" for i in range(1, 5)])
