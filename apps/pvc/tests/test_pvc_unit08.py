"""Spec 08 §12 — PVC as its own unit: roles, notifications, badge, config, separation from membership."""
from datetime import timedelta

from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from apps.access.models import Role
from apps.access.services import grant_role
from apps.accounts.models import User
from apps.accounts.tests.helpers import signed_webhook
from apps.audit.models import AuditLog
from apps.news.models import Article
from apps.notifications.models import Notification, NotificationPreference
from apps.pvc import services
from apps.pvc.models import PvcVerification

VIN = "90F5B0F8E0295612345"
BODY = {"vin": VIN, "surname": "Obi", "state": "kano", "lga": "Nassarawa", "consent": True}


def mk_user(cid, role=None, **kw):
    u = User.objects.create_user(cid, email=f"{cid}@example.com", full_name=kw.pop("full_name", "Ada Obi"),
                                 phone_number="+2348031234567", profile_completed_at=timezone.now(), **kw)
    if role:
        grant_role(user=u, role=role)
    return u


def client(user):
    c = APIClient()
    c.force_authenticate(user)
    return c


class Base(APITestCase):
    def setUp(self):
        self.voter = mk_user("user_voter")
        self.c = client(self.voter)
        self.pvc_officer = mk_user("user_pvcoff", Role.PVC_OFFICER, full_name="Pat Officer")

    def submit(self, c=None, **over):
        r = (c or self.c).put(reverse("my-pvc"), {**BODY, **over}, format="json")
        assert r.status_code == 200, r.data
        return PvcVerification.objects.get(user=r.wsgi_request.user)


class RoleTests(Base):
    def test_queue_access(self):
        rec = self.submit()
        url = reverse("admin-pvc")
        allowed = [self.pvc_officer, mk_user("user_mem", Role.MEMBERSHIP_OFFICER), mk_user("user_sa", Role.SYSTEM_ADMIN)]
        denied = [mk_user("user_fin", Role.FINANCE_OFFICER), mk_user("user_mod", Role.MODERATOR), mk_user("user_news", Role.NEWS_REVIEWER),
                  self.voter]
        for u in allowed:
            self.assertEqual(client(u).get(url).status_code, 200, u)
        for u in denied:
            self.assertEqual(client(u).get(url).status_code, 403, u)
            self.assertEqual(client(u).post(reverse("admin-pvc-verify", kwargs={"pk": rec.pk})).status_code, 403, u)
        self.assertEqual(APIClient().get(url).status_code, 401)

    def test_no_self_review(self):
        own = self.submit(c=client(self.pvc_officer))
        r = client(self.pvc_officer).post(reverse("admin-pvc-verify", kwargs={"pk": own.pk}))
        self.assertEqual((r.status_code, r.data["error"]["code"]), (403, "self_review"))
        listed = client(self.pvc_officer).get(reverse("admin-pvc")).data["results"]
        self.assertTrue([x for x in listed if x["id"] == str(own.pk)][0]["is_own"])

    def test_queue_state_filter_oldest_and_history(self):
        a = self.submit()
        PvcVerification.objects.filter(pk=a.pk).update(created_at=timezone.now() - timedelta(days=4))
        self.submit(c=client(mk_user("user_lagos")), state="lagos")
        oc = client(self.pvc_officer)
        data = oc.get(reverse("admin-pvc"), {"state": "lagos"}).data
        self.assertEqual([x["state"] for x in data["results"]], ["lagos"])
        self.assertEqual(data["pending_count"], 2)
        self.assertLess(timezone.now() - data["oldest_pending_at"], timedelta(days=4, minutes=1))
        oc.post(reverse("admin-pvc-reveal", kwargs={"pk": a.pk}))
        item = [x for x in oc.get(reverse("admin-pvc")).data["results"] if x["id"] == str(a.pk)][0]
        self.assertEqual([e["kind"] for e in item["events"]], ["submitted", "vin_revealed"])
        self.assertNotIn(VIN, str(item))


class NotificationTests(Base):
    def test_verify_and_reject_notify_without_pvc_data(self):
        NotificationPreference.objects.create(user=self.voter, announcements=False)  # decisions still arrive
        rec = self.submit()
        oc = client(self.pvc_officer)
        oc.post(reverse("admin-pvc-reject", kwargs={"pk": rec.pk}), {"reason": "No voter with that VIN in Kano"}, format="json")
        n = Notification.objects.get(recipient=self.voter)
        self.assertEqual((n.kind, n.decision, n.article_id), ("pvc_decision", "rejected", None))
        self.submit()
        oc.post(reverse("admin-pvc-verify", kwargs={"pk": rec.pk}))
        items = self.c.get(reverse("my-notifications")).data["results"]
        self.assertEqual([(i["kind"], i["decision"], i["article"]) for i in items],
                         [("pvc_decision", "verified", None), ("pvc_decision", "rejected", None)])
        self.assertNotIn("Kano", str(items))
        self.assertNotIn("2345", str(items))
        self.assertEqual(self.c.get(reverse("my-notifications-unread")).data["unread"], 2)

    def test_no_notification_for_withdraw_change_or_reveal(self):
        rec = self.submit()
        client(self.pvc_officer).post(reverse("admin-pvc-reveal", kwargs={"pk": rec.pk}))
        self.submit(surname="Obi-Okafor")
        self.c.delete(reverse("my-pvc"))
        self.assertFalse(Notification.objects.exists())

    def test_check_constraint(self):
        from django.db import IntegrityError, transaction

        a = Article.objects.create(source="official", status="successful", category="party", title="T", body="B")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Notification.objects.create(recipient=self.voter, kind="pvc_decision", article=a, decision="verified")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Notification.objects.create(recipient=self.voter, kind="announcement", article=None)


class BadgeTests(Base):
    def verify(self):
        rec = self.submit()
        services.verify(rec, self.pvc_officer)
        return rec

    def test_badge_only_when_verified_and_reset_on_change(self):
        self.submit()
        self.assertEqual(self.c.patch(reverse("my-pvc-badge"), {"show_badge": True}, format="json").status_code, 409)
        services.verify(PvcVerification.objects.get(user=self.voter), self.pvc_officer)
        r = self.c.patch(reverse("my-pvc-badge"), {"show_badge": True}, format="json")
        self.assertTrue(r.data["show_badge"])
        self.assertTrue(services.public_badge(self.voter))
        self.submit(surname="Changed")
        self.assertFalse(PvcVerification.objects.get(user=self.voter).show_badge)
        self.assertFalse(services.public_badge(self.voter))
        self.assertEqual(self.c.patch(reverse("my-pvc-badge"), {"show_badge": "yes"}, format="json").status_code, 400)

    def test_byline_verified(self):
        self.verify()
        reviewer = mk_user("user_rev", Role.NEWS_REVIEWER, full_name="Rita")
        body = "Polling units in Ikeja opened on time this morning according to INEC officers on ground. " * 2
        decl = {"accurate": True, "no_private_info": True, "rules": True}
        named = self.c.post(reverse("my-news"), {"title": "Named", "body": body, "show_author_name": True, "declarations": decl},
                            format="json").data["id"]
        anon = self.c.post(reverse("my-news"), {"title": "Anon", "body": body, "declarations": decl}, format="json").data["id"]
        for pk in (named, anon):
            client(reviewer).post(reverse("admin-news-approve", kwargs={"pk": pk}))
        get = lambda pk: APIClient().get(reverse("news-detail", kwargs={"pk": pk})).data["byline_verified"]  # noqa: E731
        self.assertFalse(get(named))  # not opted in yet
        self.c.patch(reverse("my-pvc-badge"), {"show_badge": True}, format="json")
        self.assertTrue(get(named))
        self.assertFalse(get(anon))  # never on "a member" items
        self.assertEqual([x["byline_verified"] for x in APIClient().get(reverse("news-list")).data["results"]].count(True), 1)

    def test_me_status(self):
        self.assertEqual(self.c.get(reverse("me")).data["pvc_status"], "unverified")
        self.submit()
        self.assertEqual(self.c.get(reverse("me")).data["pvc_status"], "pending")
        services.verify(PvcVerification.objects.get(user=self.voter), self.pvc_officer)
        self.assertEqual(self.c.get(reverse("me")).data["pvc_status"], "successful")


class ConfigTests(Base):
    def test_public_config_and_settings(self):
        r = APIClient().get(reverse("pvc-config"))
        self.assertIn("public", r["Cache-Control"])
        self.assertEqual(set(r.data), {"inec_pvc_url", "inec_voter_check_url", "guidance_note", "collection_note"})
        self.assertIn("9 October 2026", r.data["collection_note"])
        self.assertEqual(client(self.pvc_officer).get(reverse("admin-pvc-settings")).status_code, 200)
        self.assertEqual(client(self.pvc_officer).patch(reverse("admin-pvc-settings"), {"guidance_note": "x"}, format="json").status_code, 403)
        sa = mk_user("user_sa2", Role.SYSTEM_ADMIN)
        r = client(sa).patch(reverse("admin-pvc-settings"), {"guidance_note": "Registration is closed until INEC announces the next phase."},
                             format="json")
        self.assertEqual(r.data["guidance_note"], "Registration is closed until INEC announces the next phase.")
        self.assertTrue(AuditLog.objects.filter(action="pvc.settings_changed").exists())
        self.assertEqual(client(sa).patch(reverse("admin-pvc-settings"), {"guidance_note": "x" * 1001}, format="json").status_code, 400)

    def test_membership_config_no_longer_has_pvc_url(self):
        self.assertNotIn("inec_pvc_url", APIClient().get(reverse("membership-config")).data)


class SeparationTests(Base):
    def test_membership_reads_status_and_deletion_erases_pvc(self):
        from apps.membership import services as membership

        self.assertEqual(membership.display_status(self.voter), "not_joined")
        rec = self.submit()
        self.assertEqual(membership.display_status(self.voter), "pvc_pending")
        services.verify(rec, self.pvc_officer)
        self.assertEqual(membership.display_status(self.voter), "pvc_verified")
        self.assertFalse(hasattr(self.voter, "membership") and self.voter.membership)  # verifying never creates a membership
        uid = "user_test123"
        u = mk_user(uid)
        self.submit(c=client(u))
        body, headers = signed_webhook("user.deleted", {"id": uid, "deleted": True, "object": "user"})
        APIClient().post(reverse("clerk-webhook"), data=body, content_type="application/json", **headers)
        self.assertFalse(PvcVerification.objects.filter(user=u).exists())
        self.assertTrue(AuditLog.objects.filter(action="user.deleted").latest("created_at").metadata["pvc_erased"])

    def test_no_pvc_data_in_public_endpoints(self):
        self.submit()
        for name in ("news-list", "candidates", "pvc-config"):
            try:
                url = reverse(name)
            except Exception:
                continue
            self.assertNotIn("2345", str(APIClient().get(url).data))
