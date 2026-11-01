"""Spec 06 §12 — PVC verification."""
import logging
from datetime import timedelta

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from apps.access.models import Role
from apps.access.services import grant_role
from apps.accounts.models import User
from apps.audit.models import AuditLog
from apps.core.logging import RedactPII
from apps.pvc import services
from apps.pvc.models import PvcEvent, PvcVerification

VIN = "90F5B0F8E0295612345"
BODY = {"vin": "90f5 b0f8 e029 5612 345", "surname": "Obi", "state": "kano", "lga": "Nassarawa", "polling_unit": "PU 012",
        "consent": True}


def mk_user(cid="user_v", **kw):
    return User.objects.create_user(cid, email=f"{cid}@example.com", full_name="Ada Obi", phone_number="+2348031234567", **kw)


def mk_officer(cid="user_off"):
    u = mk_user(cid)
    grant_role(user=u, role=Role.MEMBERSHIP_OFFICER)
    return u


class SubmitTests(APITestCase):
    def setUp(self):
        self.user = mk_user()
        self.client.force_authenticate(self.user)

    def test_vin_formats(self):
        for good in (VIN, VIN.lower(), VIN[-10:], "90F5-B0F8 E0295612345"):
            self.assertEqual(self.client.put(reverse("my-pvc"), {**BODY, "vin": good}, format="json").status_code, 200, good)
        for bad in ("123", VIN + "X", "90F5B0F8E02956123!5"):
            r = self.client.put(reverse("my-pvc"), {**BODY, "vin": bad}, format="json")
            self.assertEqual(r.status_code, 400, bad)

    def test_encrypted_at_rest_and_masked(self):
        r = self.client.put(reverse("my-pvc"), BODY, format="json")
        self.assertEqual((r.data["status"], r.data["vin_masked"]), ("pending", "•••• 2345"))
        self.assertNotIn(VIN, str(r.data))
        rec = PvcVerification.objects.get()
        self.assertNotIn(VIN.encode(), bytes(rec.vin_encrypted))
        self.assertEqual(rec.vin_last4, "2345")
        g = self.client.get(reverse("my-pvc"))
        self.assertNotIn(VIN, str(g.data))
        self.assertIn("no-store", g["Cache-Control"])

    def test_consent_required(self):
        r = self.client.put(reverse("my-pvc"), {**BODY, "consent": False}, format="json")
        self.assertIn("consent", r.data["error"]["fields"])

    def test_unverified_and_inec_link(self):
        r = self.client.get(reverse("my-pvc"))
        self.assertEqual((r.data["status"], r.data["inec_pvc_url"]), ("unverified", None))
        with override_settings(INEC_PVC_URL="https://cvr.inecnigeria.org"):
            self.assertEqual(self.client.get(reverse("my-pvc")).data["inec_pvc_url"], "https://cvr.inecnigeria.org")

    def test_withdraw_deletes_record_keeps_event(self):
        self.client.put(reverse("my-pvc"), BODY, format="json")
        self.assertEqual(self.client.delete(reverse("my-pvc")).status_code, 204)
        self.assertFalse(PvcVerification.objects.exists())
        self.assertTrue(PvcEvent.objects.filter(kind="withdrawn").exists())

    def test_changing_details_clears_verification(self):
        self.client.put(reverse("my-pvc"), BODY, format="json")
        services.verify(PvcVerification.objects.get(), mk_officer())
        self.assertTrue(services.is_verified(self.user))
        self.assertEqual(self.client.delete(reverse("my-pvc")).status_code, 409, "only pending can be withdrawn")
        self.client.put(reverse("my-pvc"), {**BODY, "lga": "Fagge"}, format="json")
        self.assertFalse(services.is_verified(self.user))
        self.assertEqual(PvcVerification.objects.get().status, "pending")

    def test_guest_401(self):
        self.assertEqual(APIClient().get(reverse("my-pvc")).status_code, 401)


class OfficerTests(APITestCase):
    def setUp(self):
        self.user = mk_user()
        self.rec = services.submit(self.user, vin=VIN, surname="Obi", state="kano", lga="Nassarawa")
        self.officer = mk_officer()

    def test_only_officers_decide(self):
        urls = [reverse("admin-pvc"), reverse("admin-pvc-verify", kwargs={"pk": self.rec.pk}),
                reverse("admin-pvc-reveal", kwargs={"pk": self.rec.pk})]
        self.assertEqual(self.client.get(urls[0]).status_code, 401)
        for u in urls[1:]:
            self.assertEqual(self.client.post(u).status_code, 401)
        cand = mk_user("user_c")
        grant_role(user=cand, role=Role.CANDIDATE, scope_type="candidate", scope_id="11111111-1111-1111-1111-111111111111")
        fin = mk_user("user_f")
        grant_role(user=fin, role=Role.FINANCE_OFFICER)
        for who in (self.user, cand, fin):
            self.client.force_authenticate(who)
            self.assertEqual(self.client.get(urls[0]).status_code, 403)
            self.assertEqual(self.client.post(urls[1]).status_code, 403)

    def test_queue_verify_reject_reveal(self):
        self.client.force_authenticate(self.officer)
        q = self.client.get(reverse("admin-pvc"))
        self.assertEqual(q.data["count"], 1)
        self.assertEqual(q.data["lookup_url"], "https://cvr.inecnigeria.org/vvs")
        self.assertNotIn(VIN, str(q.data))
        r = self.client.post(reverse("admin-pvc-reveal", kwargs={"pk": self.rec.pk}))
        self.assertEqual(r.data["vin"], VIN)
        self.assertTrue(AuditLog.objects.filter(action="pvc.vin_revealed", actor=self.officer).exists())
        r = self.client.post(reverse("admin-pvc-reject", kwargs={"pk": self.rec.pk}), {"reason": ""}, format="json")
        self.assertEqual(r.status_code, 400)
        r = self.client.post(reverse("admin-pvc-verify", kwargs={"pk": self.rec.pk}), {}, format="json")
        self.assertEqual(r.data["status"], "successful")
        self.assertEqual(self.client.post(reverse("admin-pvc-verify", kwargs={"pk": self.rec.pk})).status_code, 409)
        self.assertTrue(AuditLog.objects.filter(action="pvc.verified").exists())

    def test_system_admin_counts_as_officer(self):
        admin = mk_user("user_adm")
        grant_role(user=admin, role=Role.SYSTEM_ADMIN)
        self.client.force_authenticate(admin)
        self.assertEqual(self.client.get(reverse("admin-pvc")).status_code, 200)

    def test_reject_then_prune_vin_after_90_days(self):
        services.reject(self.rec, self.officer, "We couldn't find that VIN in Kano.")
        PvcVerification.objects.filter(pk=self.rec.pk).update(reviewed_at=timezone.now() - timedelta(days=91))
        self.assertEqual(services.prune(), 1)
        rec = PvcVerification.objects.get(pk=self.rec.pk)
        self.assertEqual((bytes(rec.vin_encrypted), rec.vin_last4), (b"", ""))

    def test_events_append_only(self):
        ev = PvcEvent.objects.first()
        with self.assertRaises(RuntimeError):
            ev.save()


class RedactionTests(TestCase):
    def test_vin_and_nin_never_reach_logs(self):
        rec = logging.LogRecord("x", logging.INFO, __file__, 1, "payload %s nin=12345678901", ({"vin": VIN, "ok": 1},), None)
        RedactPII().filter(rec)
        text = rec.getMessage()
        self.assertNotIn(VIN, text)
        self.assertNotIn("12345678901", text)
        self.assertIn("'ok': 1", text)
